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

DRY_RUN_DELAY_S = (3, 8)


@dataclass
class RunResult:
    dry_run: bool
    outcomes: list[tuple[str, Outcome]] = field(default_factory=list)   # (title, outcome)
    stopped: str | None = None   # why the run ended early, if it did


def pending(db: Database, dry_run: bool, limit: int | None = None) -> list:
    """Approved jobs to process, best score first. Live runs also take dry-run passes."""
    states = [Q.APPROVED, Q.FAILED] + ([] if dry_run else [Q.DRY_RUN_OK])
    rows = [r for r in db.applications(states)
            if r["state"] != Q.FAILED or r["attempts"] < Q.MAX_ATTEMPTS]
    return rows[:limit] if limit else rows


def run(
    cfg: Config, db: Database, applier_factory: Callable[[], Applier], dry_run: bool,
    limit: int | None = None, sleep: Callable[[float], None] = time.sleep,
    pacer: Pacer | None = None, on_outcome: Callable[[str, Outcome], None] | None = None,
) -> RunResult:
    if cfg.apply.mode == "dry_run":
        dry_run = True   # live submission needs apply.mode set to manual or auto
    pacer = pacer or Pacer(cfg.apply, db)
    result = RunResult(dry_run)
    Q.refresh_answers(db, AnswerBank(cfg.apply.answers_path))

    if not dry_run and (reason := pacer.blocked_reason()):
        result.stopped = reason
        return result
    budget = limit if dry_run else min(limit or 10**6, pacer.remaining())
    jobs = pending(db, dry_run, budget)
    if not jobs:
        return result

    applier = applier_factory()
    for i, app in enumerate(jobs):
        if i:
            sleep(random.uniform(*DRY_RUN_DELAY_S) if dry_run else pacer.delay_seconds())
            if not dry_run and (reason := pacer.blocked_reason()):
                result.stopped = reason
                break

        outcome = applier.apply(app["job_uuid"], app["url"],
                                json.loads(app["questions"]), json.loads(app["answers"]))
        result.outcomes.append((app["title"], outcome))
        if on_outcome:
            on_outcome(app["title"], outcome)

        if outcome.state == LOGGED_OUT:
            result.stopped = outcome.detail
            break
        if outcome.state == CAPTCHA:
            pacer.pause(f"CAPTCHA seen while applying to '{app['title']}'")
            result.stopped = "paused: CAPTCHA (log in and check MCF, then `mcf-apply unpause`)"
            break

        # Login and CAPTCHA stops above aren't the job's fault, so only these count as attempts.
        fields: dict = {"state": outcome.state, "error": None, "screenshot": outcome.screenshot,
                        "attempts": app["attempts"] + 1}
        if outcome.state == Q.FAILED:
            fields["error"] = outcome.detail
        if outcome.state == Q.APPLIED:
            fields["submitted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fields["mcf_application_id"] = outcome.application_id
        db.update_application(app["job_uuid"], **fields)
        db.commit()

        if outcome.state == Q.FAILED and not dry_run and pacer.record_failure():
            result.stopped = "paused: too many failures in the last hour"
            break
    return result
