"""Work through the approved queue: pace, apply, record outcomes, pause on trouble."""

from __future__ import annotations

import json
import random
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.mcf.applier import CAPTCHA, LOGGED_OUT, Applier, Outcome
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.pacer import Pacer
from mcf_autoapply.sources import source_of

DRY_RUN_DELAY_S = (3, 8)
INTERRUPTED = "interrupted by your Telegram command; the rest of the queue waits for the next run"


@dataclass
class RunResult:
    dry_run: bool
    outcomes: list[tuple[str, Outcome]] = field(default_factory=list)   # (title, outcome)
    stopped: str | None = None   # why the run ended early, if it did
    skipped_applied: list[str] = field(default_factory=list)   # titles MCF already lists


def pending(db: Database, dry_run: bool, limit: int | None = None, jobstreet: bool = True,
            sites: Iterable[str] | None = None) -> list:
    """Approved jobs to process, best score first. Live runs also take dry-run passes.
    JobStreet jobs are held back while JobStreet is off; `sites` restricts to some sites."""
    states = [Q.APPROVED, Q.FAILED] + ([] if dry_run else [Q.DRY_RUN_OK])
    applied = db.applied_on_site_uuids()
    allowed = set(sites) if sites is not None else None
    rows = [r for r in db.applications(states)
            if (r["state"] != Q.FAILED or r["attempts"] < Q.MAX_ATTEMPTS)
            and r["job_uuid"] not in applied
            and (jobstreet or source_of(r["job_uuid"]) != "jobstreet")
            and (allowed is None or source_of(r["job_uuid"]) in allowed)]
    return rows[:limit] if limit else rows


def _is_cap(reason: str) -> bool:
    return "cap reached" in reason


def order_for_run(jobs: list, jobstreet_slots: int | None) -> list:
    """JobStreet's few allowed jobs go first, then everything else by score.

    JobStreet listings score lower (no skills field), so by score alone they never made it
    into an hour's budget while MCF jobs were queued, and JobStreet's own cap went unused.
    """
    js = [j for j in jobs if source_of(j["job_uuid"]) == "jobstreet"]
    rest = [j for j in jobs if source_of(j["job_uuid"]) != "jobstreet"]
    head = js if jobstreet_slots is None else js[:max(0, jobstreet_slots)]
    tail = [j for j in js if j not in head]
    return head + rest + tail


def site_caps(cfg: Config) -> dict[str, tuple[int, int]]:
    return {"jobstreet": (cfg.jobstreet.hourly_cap, cfg.jobstreet.daily_cap)}


def run(
    cfg: Config, db: Database, applier_factory: Callable[..., Applier], dry_run: bool,
    limit: int | None = None, sleep: Callable[[float], None] = time.sleep,
    pacer: Pacer | None = None, on_outcome: Callable[[str, Outcome], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None, sites: Iterable[str] | None = None,
) -> RunResult:
    """Apply to the queue. `applier_factory(source)` gives the applier for a site (older
    factories take no argument). A site that logs us out or hits its cap is skipped for the
    rest of the run while the other continues. `should_stop()` is checked between jobs so a
    Telegram command can take over; the rest of the queue waits for the next run."""
    if cfg.apply.mode == "dry_run":
        dry_run = True   # live submission needs apply.mode set to manual or auto
    pacer = pacer or Pacer(cfg.apply, db, site_caps=site_caps(cfg))
    result = RunResult(dry_run)
    Q.refresh_answers(db, AnswerBank(cfg.apply.answers_path))

    if not dry_run and (reason := pacer.blocked_reason()) and not _is_cap(reason):
        result.stopped = reason          # paused / outside active hours: no site may apply
        return result
    jobs = order_for_run(pending(db, dry_run, None, jobstreet=cfg.jobstreet.enabled, sites=sites),
                         None if dry_run else pacer.remaining("jobstreet"))
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        return result

    appliers: dict[str, Applier] = {}
    stopped_sites: dict[str, str] = {}
    first = True

    def progress(text: str) -> None:
        if on_progress:
            done = Counter(o.state for _, o in result.outcomes)
            on_progress(f"{text} · so far ✅ {done.get(Q.APPLIED, 0) + done.get(Q.DRY_RUN_OK, 0)}"
                        f" ❌ {done.get(Q.FAILED, 0)}")

    for i, app in enumerate(jobs, 1):
        if should_stop and should_stop():
            result.stopped = INTERRUPTED
            break
        source = source_of(app["job_uuid"])
        if source in stopped_sites:
            continue
        if not dry_run and (reason := pacer.blocked_reason(source)):
            if _is_cap(reason):   # each site has its own caps: skip just this site
                stopped_sites[source] = reason
                continue
            result.stopped = reason
            break
        if not first:
            wait = random.uniform(*DRY_RUN_DELAY_S) if dry_run else pacer.delay_seconds()
            progress(f"waiting {wait:.0f} s before job {i}/{len(jobs)}: {app['title']}")
            sleep(wait)
            if should_stop and should_stop():
                result.stopped = INTERRUPTED
                break
        first = False
        progress(f"{'dry-running' if dry_run else 'applying to'} job {i}/{len(jobs)}: {app['title']}")
        if source not in appliers:
            try:
                appliers[source] = applier_factory(source)
            except TypeError:
                appliers[source] = applier_factory()

        outcome = appliers[source].apply(app["job_uuid"], app["url"],
                                         json.loads(app["questions"]), json.loads(app["answers"]))
        result.outcomes.append((app["title"], outcome))
        if on_outcome:
            on_outcome(app["title"], outcome)

        if outcome.state == LOGGED_OUT:
            stopped_sites[source] = outcome.detail
            db.set_state(f"{source}:login", "out")
            db.log(None, "logged_out", {"source": source, "detail": outcome.detail})
            db.commit()
            continue
        if outcome.state == CAPTCHA:
            pacer.pause(f"CAPTCHA seen while applying to '{app['title']}'")
            result.stopped = "paused: CAPTCHA (log in and check the site, then /resume)"
            break
        if outcome.state == Q.NEEDS_INPUT:
            Q.remember_question_options(db, outcome.questions or [])
            questions = [q["question"] for q in outcome.questions or []]
            db.update_application(app["job_uuid"], state=Q.NEEDS_INPUT, questions=questions,
                                  answers=[None] * len(questions), screenshot=outcome.screenshot,
                                  error=None)
            db.commit()
            continue

        # Login and CAPTCHA stops above aren't the job's fault, so only these count as attempts.
        fields: dict = {"state": outcome.state, "error": None, "screenshot": outcome.screenshot,
                        "attempts": app["attempts"] + 1}
        if outcome.state == Q.FAILED:
            fields["error"] = outcome.detail
        if outcome.state == Q.APPLIED:
            fields["submitted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fields["mcf_application_id"] = outcome.application_id
        if outcome.state in (Q.APPLIED, Q.ALREADY_APPLIED):
            # Remember it the way the MCF sync does, so reposts are skipped too.
            db.upsert_applied_on_site(app["job_uuid"], app["title"], app["company_name"],
                                     mcf_application_id=outcome.application_id)
        db.update_application(app["job_uuid"], **fields)
        db.commit()

        if outcome.state == Q.FAILED and not dry_run and pacer.record_failure():
            result.stopped = "paused: too many failures in the last hour"
            break
    if stopped_sites and not result.stopped:
        result.stopped = "; ".join(stopped_sites.values())
    return result
