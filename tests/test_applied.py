"""Syncing MCF's Applied Jobs list, and skipping everything on it."""

import json
import os

import pytest

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.mcf.applied import AppliedSyncError, mark_already_applied
from mcf_autoapply.models import Job
from mcf_autoapply.notify import Notifier
from mcf_autoapply.pipeline import apply as A
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import apply_filters

playwright = pytest.importorskip("playwright.sync_api")

from mcf_autoapply.mcf.applied import fetch_applied_jobs, sync_applied  # noqa: E402

CHROMIUM = "/opt/pw-browsers/chromium"
HOME = "https://fake-mcf.test/"

APPLIED = [
    {"id": 101, "statusDescription": "Received", "submittedOn": "2026-10-10T09:00:00Z",
     "job": {"title": "Associate AI Engineer", "uuid": "a" * 32}, "employerName": "ZURICH INSURANCE"},
    {"id": 102, "statusDescription": "Unsuccessful", "submittedOn": "2026-10-08T09:00:00Z",
     "job": {"title": "Data Analyst", "uuid": "b" * 32}, "employerName": "AGENCY PTE LTD",
     "hiringEmployerName": "REAL EMPLOYER PTE LTD"},
    {"id": 103, "statusDescription": "Received", "submittedOn": "2026-10-07T09:00:00Z",
     "job": {"title": "Junior Developer", "uuid": "c" * 32}, "employerName": "ACME PTE. LTD."},
]
PAGE_SIZE = 2

# The Applied Jobs page: on load it POSTs MCF's own list query (page 1), like the real site.
APPLIED_PAGE = """<!doctype html><body><nav><a id="navbar-search-jobs">Search Jobs</a></nav>
<div id="list"></div><script>
fetch("/graphql", {method: "POST", headers: {"content-type": "application/json"},
  body: JSON.stringify({operationName: "getJobApplications",
    query: "query getJobApplications($applicantId: ID!, $skip: Int!, $limit: Int!) {...}",
    variables: {applicantId: "A1", skip: 0, limit: %d, sortBy: "createdOn"}})})
  .then(r => r.json()).then(d => document.getElementById("list").textContent = JSON.stringify(d));
</script></body>""" % PAGE_SIZE


@pytest.fixture
def page():
    with playwright.sync_playwright() as p:
        kwargs = {"executable_path": CHROMIUM} if os.path.exists(CHROMIUM) else {}
        try:
            browser = p.chromium.launch(**kwargs)
        except playwright.Error as e:
            pytest.skip(f"Chromium not available: {e}")
        pg = browser.new_page()
        calls = []

        def handle(route):
            req = route.request
            if req.url.endswith("/graphql"):
                body = json.loads(req.post_data)
                calls.append(body["variables"])
                skip, limit = body["variables"]["skip"], body["variables"]["limit"]
                return route.fulfill(json={"data": {"applicationsForApplicant": {
                    "total": len(APPLIED), "applications": APPLIED[skip:skip + limit]}}})
            if req.url.endswith("/applied-jobs"):
                return route.fulfill(body=APPLIED_PAGE, content_type="text/html")
            return route.fulfill(body="<html><body>home</body></html>", content_type="text/html")

        pg.route("https://fake-mcf.test/**", handle)
        pg.calls = calls
        yield pg
        browser.close()


def test_fetch_reads_every_page(page, monkeypatch):
    monkeypatch.setattr("mcf_autoapply.mcf.selectors.APPLIED_JOBS_URL", HOME + "applied-jobs")
    jobs = fetch_applied_jobs(page)
    assert [j.uuid for j in jobs] == ["a" * 32, "b" * 32, "c" * 32]
    assert jobs[1].employer == "REAL EMPLOYER PTE LTD"       # hiring employer beats the agency
    assert jobs[0].status == "Received" and jobs[0].application_id == "101"
    assert [c["skip"] for c in page.calls] == [0, 2]         # page 1 captured, page 2 re-sent


def test_fetch_fails_cleanly_when_logged_out(page, monkeypatch):
    monkeypatch.setattr("mcf_autoapply.mcf.selectors.APPLIED_JOBS_URL", HOME + "applied-jobs")
    page.route("https://fake-mcf.test/applied-jobs", lambda r: r.fulfill(
        body='<button id="navbar-login">Log In</button>', content_type="text/html"))
    with pytest.raises(AppliedSyncError, match="not logged in"):
        fetch_applied_jobs(page, timeout_ms=3000)


# --- database side -----------------------------------------------------------------------

def make_cfg(tmp_path):
    return Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        "ranking": {"entry_level_keywords": ["junior"], "entry_level_weight": 1.0},
        "apply": {"mode": "manual", "answers_path": str(tmp_path / "answers.yml")},
        "telegram": {"quick_review_min_score": 0},
    })


class FakeClient:
    def screening_questions(self, uuid):
        return []


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def add(db, cfg, job_factory, **overrides):
    raw = job_factory(**overrides)
    db.upsert_job(Job.from_api(raw), "x")
    apply_filters(db, cfg)
    return raw["uuid"]


def test_sync_marks_queued_jobs_and_reposts_as_already_applied(page, db, tmp_path, job_factory,
                                                               monkeypatch):
    monkeypatch.setattr("mcf_autoapply.mcf.selectors.APPLIED_JOBS_URL", HOME + "applied-jobs")
    cfg = make_cfg(tmp_path)
    bank = AnswerBank(cfg.apply.answers_path)
    by_uuid = add(db, cfg, job_factory, uuid="a" * 32, title="Associate AI Engineer")
    repost = add(db, cfg, job_factory, title="Junior Developer")      # ACME, new uuid
    other = add(db, cfg, job_factory, title="Junior Cloud Engineer")  # ACME, different title
    Q.approve(db, FakeClient(), bank, [by_uuid, repost, other])
    assert {a["state"] for a in db.applications()} == {Q.APPROVED}

    result = sync_applied(page, db)
    assert (result.total, result.new) == (3, 3)
    assert sorted(result.released) == ["Associate AI Engineer", "Junior Developer"]
    states = {a["job_uuid"]: a["state"] for a in db.applications()}
    assert states[by_uuid] == Q.ALREADY_APPLIED       # same uuid
    assert states[repost] == Q.ALREADY_APPLIED        # same employer + title, new uuid
    assert states[other] == Q.APPROVED                # still queued
    assert A.pending(db, dry_run=False) and all(
        r["job_uuid"] == other for r in A.pending(db, dry_run=False))

    # Syncing again changes nothing, and a later approve of a listed job is refused.
    assert sync_applied(page, db).new == 0
    again = add(db, cfg, job_factory, title="Data Analyst",
                postedCompany={"name": "REAL EMPLOYER PTE LTD", "uen": "R1", "ssicCode2020": "62011"})
    [d] = Q.approve(db, FakeClient(), bank, [again])
    assert d.state == Q.ALREADY_APPLIED and "MCF lists" in d.note


def test_cards_and_auto_approve_skip_applied_jobs(db, tmp_path, job_factory):
    cfg = make_cfg(tmp_path)
    db.upsert_applied_on_mcf("a" * 32, "Associate AI Engineer", "ZURICH INSURANCE")
    db.upsert_applied_on_mcf("z" * 32, "Junior Developer", "ACME PTE. LTD.")   # applied by hand
    listed = add(db, cfg, job_factory, uuid="a" * 32, title="Junior AI Engineer")
    repost = add(db, cfg, job_factory, title="Junior Developer")
    fresh = add(db, cfg, job_factory, title="Junior Data Engineer")

    notifier = Notifier(cfg, db, None, None)
    assert [j["uuid"] for j in notifier.review_candidates()] == [fresh]

    cfg.apply.mode, cfg.apply.auto_apply_min_score = "auto", 0
    picked = [d.job_uuid for d in Q.auto_approve(cfg, db, FakeClient(),
                                                   AnswerBank(cfg.apply.answers_path))]
    assert picked == [fresh]
    assert listed not in picked and repost not in picked


def test_applied_outcome_is_remembered_for_reposts(db, tmp_path, job_factory):
    from mcf_autoapply.mcf.applier import Outcome
    from mcf_autoapply.pipeline.pacer import Pacer
    from datetime import time
    cfg = make_cfg(tmp_path)
    cfg.apply.active_hours = (time(0, 0), time(23, 59, 59))
    uuid = add(db, cfg, job_factory, title="Junior Developer")
    Q.approve(db, FakeClient(), AnswerBank(cfg.apply.answers_path), [uuid])

    class Applier:
        def apply(self, *a):
            return Outcome(Q.ALREADY_APPLIED, "MCF shows this job as applied")

    A.run(cfg, db, lambda: Applier(), dry_run=False, sleep=lambda s: None, pacer=Pacer(cfg.apply, db))
    assert db.applied_on_mcf_uuids() == {uuid}
    repost = add(db, cfg, job_factory, title="Junior Developer")
    assert mark_already_applied(db) == []            # nothing queued for it
    [d] = Q.approve(db, FakeClient(), AnswerBank(cfg.apply.answers_path), [repost])
    assert d.state == Q.ALREADY_APPLIED
