"""Read your applications from MCF's Applied Jobs page, in a logged-in browser."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from playwright.sync_api import Page

from mcf_autoapply.db import Database, applied_key
from mcf_autoapply.mcf import selectors as S
from mcf_autoapply.pipeline import queue as Q

MAX_PAGES = 100
PAGE_TIMEOUT_MS = 30_000


class AppliedSyncError(RuntimeError):
    pass


@dataclass
class AppliedJob:
    uuid: str
    title: str = ""
    employer: str = ""
    status: str | None = None
    submitted_on: str | None = None
    application_id: str | None = None


@dataclass
class SyncResult:
    total: int                           # applications MCF listed
    new: int = 0                         # not known to us before this sync
    released: list[str] = field(default_factory=list)   # queued jobs now marked already applied


def _parse(data: dict) -> tuple[list[AppliedJob], int | None]:
    block = ((data or {}).get("data") or {}).get("applicationsForApplicant") or {}
    jobs = []
    for a in block.get("applications") or []:
        job = a.get("job") or {}
        if not job.get("uuid"):
            continue
        jobs.append(AppliedJob(
            uuid=job["uuid"], title=job.get("title") or "",
            employer=a.get("hiringEmployerName") or a.get("employerName") or "",
            status=a.get("statusDescription"), submitted_on=a.get("submittedOn") or a.get("createdOn"),
            application_id=str(a["id"]) if a.get("id") is not None else None))
    return jobs, block.get("total")


def fetch_applied_jobs(page: Page, timeout_ms: int = PAGE_TIMEOUT_MS) -> list[AppliedJob]:
    """Open the Applied Jobs page, capture its own list request, then page through the rest
    by re-sending that request with a higher `skip` from inside the page (same cookies)."""
    captured: dict = {}

    def on_response(resp):
        req = resp.request
        if (req.method == "POST" and req.post_data and S.APPLIED_JOBS_QUERY in req.post_data
                and "req" not in captured):
            captured["req"] = req
            try:
                captured["body"] = resp.json()
            except Exception:
                captured["body"] = None

    page.on("response", on_response)
    try:
        page.goto(S.APPLIED_JOBS_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout_ms / 1000
        while "req" not in captured and time.monotonic() < deadline:
            if page.locator(S.NAVBAR_LOGIN).count():
                raise AppliedSyncError("not logged in")
            page.wait_for_timeout(250)
    finally:
        page.remove_listener("response", on_response)
    if "req" not in captured:
        raise AppliedSyncError("the Applied Jobs page didn't load its list")

    req = captured["req"]
    jobs, total = _parse(captured["body"] or {})
    seen = {j.uuid for j in jobs}
    try:
        payload = json.loads(req.post_data)
    except (TypeError, ValueError):
        return jobs
    variables = payload.get("variables") or {}
    limit = int(variables.get("limit") or len(jobs) or 20)
    headers = {k: v for k, v in req.headers.items()
               if k.lower() in ("content-type", "authorization", "accept")}

    for _ in range(MAX_PAGES):
        skip = int(variables.get("skip") or 0) + limit
        if (total is not None and skip >= total) or not jobs:
            break
        variables = {**variables, "skip": skip}
        body = page.evaluate(
            """async ([url, headers, body]) => {
                 const r = await fetch(url, {method: "POST", headers, body, credentials: "include"});
                 return await r.json();
               }""",
            [req.url, headers, json.dumps({**payload, "variables": variables})])
        more, total = _parse(body)
        fresh = [j for j in more if j.uuid not in seen]
        if not fresh:
            break
        jobs += fresh
        seen |= {j.uuid for j in fresh}
    return jobs


def sync_applied(page: Page, db: Database) -> SyncResult:
    """Pull MCF's list and mark those jobs (and reposts of them) as already applied."""
    applied = fetch_applied_jobs(page)
    result = SyncResult(total=len(applied))
    for a in applied:
        if db.upsert_applied_on_site(a.uuid, a.title, a.employer, a.status, a.submitted_on,
                                     a.application_id, source="mcf"):
            result.new += 1
    result.released = mark_already_applied(db)
    db.log(None, "applied_sync", {"total": result.total, "new": result.new,
                                  "released": len(result.released)})
    db.commit()
    return result


def mark_already_applied(db: Database) -> list[str]:
    """Move queued/approved jobs that MCF lists as applied (by uuid or as a repost) to
    already_applied. Returns their titles."""
    uuids, keys = db.applied_on_site_uuids(), db.applied_on_site_keys()
    moved = []
    for app in db.applications([Q.APPROVED, Q.NEEDS_INPUT, Q.DRY_RUN_OK, Q.FAILED]):
        if app["job_uuid"] in uuids or applied_key(app["company_name"], app["title"]) in keys:
            db.update_application(app["job_uuid"], state=Q.ALREADY_APPLIED, error=None)
            moved.append(app["title"])
    db.commit()
    return moved


def is_already_applied(db: Database, job) -> bool:
    """True if MCF lists this job (or the same title at the same employer) as applied."""
    return (job["uuid"] in db.applied_on_site_uuids()
            or applied_key(job["company_name"], job["title"]) in db.applied_on_site_keys())
