"""Decide which jobs to apply to: approve, skip, and auto-approve by score."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from mcf_autoapply.config import Config
from mcf_autoapply.db import STATUS_NEW, Database
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.sources import source_of

# Application states (DESIGN.md §3.8).
NEEDS_INPUT = "needs_input"      # a screening question has no saved answer yet
APPROVED = "approved"            # waiting in the queue to be submitted
SKIPPED = "skipped"
APPLIED = "applied"
DRY_RUN_OK = "dry_run_ok"        # went through the whole form, stopped before Submit
FAILED = "failed"                # see `error`; retried on the next run up to MAX_ATTEMPTS
EXTERNAL = "external"            # apply on the employer's own site (e.g. Careers@Gov)
ALREADY_APPLIED = "already_applied"
CLOSED = "closed"

QUEUED = (APPROVED, DRY_RUN_OK, FAILED)   # states the applier picks up
MAX_ATTEMPTS = 3


@dataclass
class Decision:
    job_uuid: str
    title: str
    state: str
    note: str = ""


def approve(
    db: Database, client, bank: AnswerBank, uuids: Iterable[str], by: str = "user",
) -> list[Decision]:
    """Queue jobs. `client` fetches MCF screening questions; JobStreet's are only visible in
    its apply form, so those jobs queue with none and the applier asks if it meets any."""
    out = []
    for uuid in uuids:
        job = db.get_job(uuid)
        if job is None:
            out.append(Decision(uuid, "?", "error", "no unique job with this ID"))
            continue
        existing = db.application(job["uuid"])
        if existing and existing["state"] not in (SKIPPED,):
            out.append(Decision(job["uuid"], job["title"], existing["state"], "already queued"))
            continue

        if _mcf_says_applied(db, job):
            if existing:
                db.update_application(job["uuid"], state=ALREADY_APPLIED)
            else:
                db.create_application(job["uuid"], ALREADY_APPLIED, "mcf", [], [])
            out.append(Decision(job["uuid"], job["title"], ALREADY_APPLIED,
                                "MCF lists this (or the same job at this employer) as applied"))
            continue
        raw = json.loads(job["raw"])
        if source_of(job["uuid"]) != "mcf":
            state, questions, answers, note = APPROVED, [], [], ""
        elif raw.get("psdUrl"):
            state, questions, answers, note = EXTERNAL, [], [], "apply on Careers@Gov"
        else:
            questions = client.screening_questions(job["uuid"])
            answers = bank.answer_all(questions)
            missing = sum(a is None for a in answers)
            state = NEEDS_INPUT if missing else APPROVED
            note = f"{missing} question(s) need your answer" if missing else ""
        if existing:
            db.update_application(job["uuid"], state=state, approved_by=by,
                                  questions=questions, answers=answers)
        else:
            db.create_application(job["uuid"], state, by, questions, answers)
        out.append(Decision(job["uuid"], job["title"], state, note))
    db.commit()
    return out


def skip(db: Database, uuids: Iterable[str]) -> list[Decision]:
    out = []
    for uuid in uuids:
        job = db.get_job(uuid)
        if job is None:
            out.append(Decision(uuid, "?", "error", "no unique job with this ID"))
            continue
        app = db.application(job["uuid"])
        if app and app["state"] in (APPLIED, ALREADY_APPLIED):
            out.append(Decision(job["uuid"], job["title"], app["state"], "already applied"))
            continue
        if app:
            db.update_application(job["uuid"], state=SKIPPED)
        else:
            db.create_application(job["uuid"], SKIPPED, "user", [], [])
        out.append(Decision(job["uuid"], job["title"], SKIPPED))
    db.commit()
    return out


def auto_approve(
    cfg: Config, db: Database, client, bank: AnswerBank, limit: int | None = None,
) -> list[Decision]:
    """In auto mode, approve unseen jobs scoring at or above the threshold."""
    if cfg.apply.mode != "auto":
        return []
    picked = []
    for job in db.jobs([STATUS_NEW], sort="score"):
        if job["score"] is None or job["score"] < cfg.apply.auto_apply_min_score:
            break   # sorted by score, so nothing further qualifies
        if db.application(job["uuid"]) or _mcf_says_applied(db, job):
            continue
        if source_of(job["uuid"]) == "jobstreet" and not cfg.jobstreet.allow_auto:
            continue   # JobStreet jobs wait for a tap unless you opt in
        details = json.loads(job["rank_details"] or "{}")
        if cfg.apply.auto_apply_fresh_grad_only and not details.get("fresh_grad"):
            continue
        picked.append(job["uuid"])
        if limit and len(picked) >= limit:
            break
    return approve(db, client, bank, picked, by="auto")


def refresh_answers(db: Database, bank: AnswerBank) -> list[Decision]:
    """Re-check jobs waiting on answers; release the ones that are now fully answered."""
    out = []
    for app in db.applications([NEEDS_INPUT]):
        questions = json.loads(app["questions"])
        answers = bank.answer_all(questions)
        if None not in answers:
            db.update_application(app["job_uuid"], state=APPROVED, answers=answers)
            out.append(Decision(app["job_uuid"], app["title"], APPROVED))
        else:
            db.update_application(app["job_uuid"], answers=answers)
    db.commit()
    return out


def unanswered_questions(db: Database) -> dict[str, list[str]]:
    """Each unanswered question → the job titles waiting on it."""
    waiting: dict[str, list[str]] = {}
    for app in db.applications([NEEDS_INPUT]):
        for q, a in zip(json.loads(app["questions"]), json.loads(app["answers"])):
            if a is None:
                waiting.setdefault(q, []).append(f"{app['title']} ({app['company_name']})")
    return waiting


def _mcf_says_applied(db: Database, job) -> bool:
    from mcf_autoapply.mcf.applied import is_already_applied   # avoids an import cycle
    return is_already_applied(db, job)
