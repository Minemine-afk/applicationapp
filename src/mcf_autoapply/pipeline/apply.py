"""Work through the approved queue: pace, apply, record outcomes, pause on trouble."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
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


@dataclass
class RunResult:
    dry_run: bool
    outcomes: list[tuple[str, Outcome]] = field(default_factory=list)   # (title, outcome)
    stopped: str | None = None   # why the run ended early, if it did
    skipped_applied: list[str] = field(default_factory=list)   # titles MCF already lists


def pending(db: Database, dry_run: bool, limit: int | None = None) -> list:
    """Approved jobs to process, best score first. Live runs also take dry-run passes."""
    states = [Q.APPROVED, Q.FAILED] + ([] if dry_run else [Q.DRY_RUN_OK])
    applied = db.applied_on_site_uuids()
    rows = [r for r in db.applications(states)
            if (r["state"] != Q.FAILED or r["attempts"] < Q.MAX_ATTEMPTS)
            and r["job_uuid"] not in applied]
    return rows[:limit] if limit else rows


def site_caps(cfg: Config) -> dict[str, tuple[int, int]]:
    return {"jobstreet": (cfg.jobstreet.hourly_cap, cfg.jobstreet.daily_cap)}


def run(
    cfg: Config, db: Database, applier_factory: Callable[..., Applier], dry_run: bool,
    limit: int | None = None, sleep: Callable[[float], None] = time.sleep,
    pacer: Pacer | None = None, on_outcome: Callable[[str, Outcome], None] | None = None,
) -> RunResult:
    """Apply to the queue. `applier_factory(source)` gives the applier for a site (older
    factories take no argument). A site that logs us out or hits its cap is skipped for the
    rest of the run while the other continues."""
    if cfg.apply.mode == "dry_run":
        dry_run = True   # live submission needs apply.mode set to manual or auto
    pacer = pacer or Pacer(cfg.apply, db, site_caps=site_caps(cfg))
    result = RunResult(dry_run)
    Q.refresh_answers(db, AnswerBank(cfg.apply.answers_path))

    if not dry_run and (reason := pacer.blocked_reason()):
        result.stopped = reason
        return result
    budget = limit if dry_run else min(limit or 10**6, pacer.remaining())
    jobs = pending(db, dry_run, budget)
    if not jobs:
        return result

    appliers: dict[str, Applier] = {}
    stopped_sites: dict[str, str] = {}
    first = True
    for app in jobs:
        source = source_of(app["job_uuid"])
        if source in stopped_sites:
            continue
        if not dry_run and (reason := pacer.blocked_reason(source)):
            if reason.startswith(source):   # this site's own cap: skip just this site
                stopped_sites[source] = reason
                continue
            result.stopped = reason
            break
        if not first:
            sleep(random.uniform(*DRY_RUN_DELAY_S) if dry_run else pacer.delay_seconds())
        first = False
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
            continue
        if outcome.state == CAPTCHA:
            pacer.pause(f"CAPTCHA seen while applying to '{app['title']}'")
            result.stopped = "paused: CAPTCHA (log in and check the site, then /resume)"
            break
        if outcome.state == Q.NEEDS_INPUT:
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
