"""JobStreet applier against a stand-in SEEK-style site, and the per-site apply loop."""

import json
import os
from datetime import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from mcf_autoapply.config import Config  # noqa: E402
from mcf_autoapply.db import Database  # noqa: E402
from mcf_autoapply.jobstreet.applier import JobStreetApplier  # noqa: E402
from mcf_autoapply.jobstreet.session import fetch_applied_jobs, is_logged_in  # noqa: E402
from mcf_autoapply.mcf.applier import CAPTCHA, LOGGED_OUT, Outcome  # noqa: E402
from mcf_autoapply.models import Job  # noqa: E402
from mcf_autoapply.pipeline import apply as A  # noqa: E402
from mcf_autoapply.pipeline import queue as Q  # noqa: E402
from mcf_autoapply.pipeline.answers import AnswerBank  # noqa: E402
from mcf_autoapply.pipeline.discover import apply_filters  # noqa: E402
from mcf_autoapply.pipeline.pacer import Pacer  # noqa: E402

FAKE = (Path(__file__).parent / "fixtures" / "fake_jobstreet.html").read_text()
BASE = "https://fake-jobstreet.test/job"
CHROMIUM = "/opt/pw-browsers/chromium"
Q1 = "Do you have a degree in Computer Science or a related field?"
Q2 = "Which of the following best describes your experience with Python?"
Q3 = "What is your expected monthly salary?"


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        kwargs = {"executable_path": CHROMIUM} if os.path.exists(CHROMIUM) else {}
        try:
            b = p.chromium.launch(**kwargs)
        except playwright.Error as e:
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


@pytest.fixture
def site(browser, tmp_path):
    page = browser.new_page()
    submitted = []

    def handle(route):
        req = route.request
        if req.method == "POST" and req.url.endswith("/submitted"):
            submitted.append(json.loads(req.post_data))
            return route.fulfill(json={"ok": True})
        if req.url.endswith("/my-activity/applied-jobs"):
            return route.fulfill(content_type="text/html", body="""
                <header><a data-automation="profile-menu" href="/profile">Me</a></header>
                <ul><li><a href="/job/111">Data Analyst</a><div>Acme Pte Ltd</div></li>
                    <li><a href="/job/222?type=standard">Junior Developer</a><div>Globex</div></li>
                    <li><a href="/job/111">Data Analyst</a></li></ul>""")
        return route.fulfill(body=FAKE, content_type="text/html")

    page.route("https://fake-jobstreet.test/**", handle)

    def applier(dry_run=False, resume_name="", cloudflare_wait_s=2):
        return JobStreetApplier(page, tmp_path / "shots", resume_name, dry_run,
                                step_timeout_ms=5_000, cloudflare_wait_s=cloudflare_wait_s)

    yield page, applier, submitted
    page.close()


def test_quick_apply_submits_with_chosen_resume_and_answers(site):
    page, make, submitted = site
    out = make(resume_name="Kumar").apply("js:1", f"{BASE}/ok?q=3",
                                           [Q1, Q2, Q3], ["yes", "1-2 years", "4500"])
    assert out.state == Q.APPLIED, out.detail
    assert submitted[0]["resume"] == "Kumar_CV_2026.pdf" and submitted[0]["cover"] == "none"
    assert submitted[0]["answers"] == {Q1: "Yes", Q2: "1-2 years", Q3: "4500"}
    assert Path(out.screenshot).exists()


def test_unanswered_employer_questions_come_back_as_needs_input(site):
    page, make, submitted = site
    out = make().apply("js:2", f"{BASE}/ok?q=2", [Q1], ["yes"])    # Q2 unknown
    assert out.state == Q.NEEDS_INPUT, out.detail
    assert [q["question"] for q in out.questions] == [Q1, Q2]
    assert out.questions[1]["options"] == ["None", "Less than 1 year", "1-2 years", "3+ years"]
    assert submitted == []


def test_dry_run_stops_at_review(site):
    page, make, submitted = site
    out = make(dry_run=True).apply("js:3", f"{BASE}/ok", [], [])
    assert out.state == Q.DRY_RUN_OK and submitted == []


@pytest.mark.parametrize("variant, state", [
    ("external", Q.EXTERNAL), ("applied", Q.ALREADY_APPLIED), ("closed", Q.CLOSED),
    ("logged-out", LOGGED_OUT), ("cloudflare", CAPTCHA),
])
def test_job_page_states(site, variant, state):
    page, make, submitted = site
    out = make().apply("js:4", f"{BASE}/{variant}", [], [])
    assert out.state == state, out.detail
    assert submitted == []


def test_plain_apply_to_seeks_own_form_is_applied_not_external(site):
    """Only where the link goes decides it: SEEK's form at /job/<id>/apply, whatever the label."""
    page, make, submitted = site
    out = make().apply("js:6", f"{BASE}/plain-apply", [], [])
    assert out.state == Q.APPLIED, out.detail
    assert len(submitted) == 1 and len(page.context.pages) == 1   # no stray tab left open


def test_documents_step_groups_are_never_asked_as_questions(site):
    page, make, submitted = site
    out = make().apply("js:7", f"{BASE}/ok?q=1", [], [])
    assert out.state == Q.NEEDS_INPUT
    assert [q["question"] for q in out.questions] == [Q1]          # not "Resumé"/"Cover letter"


def test_non_submissions_leave_a_screenshot_and_the_page(site):
    page, make, submitted = site
    out = make().apply("js:8", f"{BASE}/external", [], [])
    assert out.state == Q.EXTERNAL and out.source == "jobstreet"
    assert Path(out.screenshot).exists() and Path(out.screenshot).with_suffix(".html").exists()


def test_step_needing_manual_details_fails_cleanly(site):
    page, make, submitted = site
    out = make().apply("js:5", f"{BASE}/needs-profile", [], [])
    assert out.state == Q.FAILED and "by hand" in out.detail
    assert Path(out.screenshot).with_suffix(".html").exists() and submitted == []


def test_applied_jobs_page_is_read_and_login_detected(site):
    page, make, _ = site
    jobs = fetch_applied_jobs(page, "https://fake-jobstreet.test/my-activity/applied-jobs",
                              max_scrolls=2)
    assert [(j["id"], j["title"], j["employer"]) for j in jobs] == [
        ("111", "Data Analyst", "Acme Pte Ltd"), ("222", "Junior Developer", "Globex")]
    assert is_logged_in(page, "https://fake-jobstreet.test/") is True


# --- the per-site apply loop ----------------------------------------------------------------

def make_cfg(tmp_path):
    cfg = Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        "apply": {"mode": "manual", "answers_path": str(tmp_path / "answers.yml"),
                  "hourly_cap": 10, "daily_cap": 10, "ramp": []},
        "jobstreet": {"enabled": True, "hourly_cap": 1, "daily_cap": 1},
    })
    cfg.apply.active_hours = (time(0, 0), time(23, 59, 59))
    cfg.filters.title_include_keywords = []
    return cfg


class Fake:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def apply(self, uuid, url, questions, answers):
        self.calls.append(uuid)
        return self.outcomes.pop(0)


def seed(db, cfg, job_factory):
    mcf = [job_factory()["uuid"] for _ in range(2)]
    for u in mcf:
        db.upsert_job(Job.from_mcf(job_factory(uuid=u, title=f"MCF {u[:4]}")), "x")
    for i in (1, 2):
        db.upsert_job(Job.from_jobstreet({"id": str(i), "title": f"JS {i}", "companyName": f"C{i}",
                                          "listingDate": "2026-10-10T00:00:00Z"}), "x")
    apply_filters(db, cfg)
    Q.approve(db, type("C", (), {"screening_questions": lambda s, u: []})(),
              AnswerBank(cfg.apply.answers_path), mcf + ["js:1", "js:2"])
    return mcf


def test_each_site_gets_its_applier_and_its_own_cap(tmp_path, job_factory):
    cfg, db = make_cfg(tmp_path), Database(":memory:")
    mcf = seed(db, cfg, job_factory)
    appliers = {"mcf": Fake([Outcome(Q.APPLIED)] * 2), "jobstreet": Fake([Outcome(Q.APPLIED)] * 2)}
    result = A.run(cfg, db, lambda source: appliers[source], dry_run=False,
                   sleep=lambda s: None, pacer=Pacer(cfg.apply, db, site_caps=A.site_caps(cfg)))
    assert sorted(appliers["mcf"].calls) == sorted(mcf)
    assert appliers["jobstreet"].calls == ["js:1"]              # JobStreet cap of 1/day
    assert "jobstreet daily cap reached (1)" in result.stopped
    assert db.application("js:2")["state"] == Q.APPROVED        # still queued for tomorrow


def test_logout_on_one_site_does_not_stop_the_other(tmp_path, job_factory):
    cfg, db = make_cfg(tmp_path), Database(":memory:")
    seed(db, cfg, job_factory)
    appliers = {"mcf": Fake([Outcome(Q.APPLIED)] * 2),
                "jobstreet": Fake([Outcome(LOGGED_OUT, "JobStreet session expired")])}
    result = A.run(cfg, db, lambda source: appliers[source], dry_run=False,
                   sleep=lambda s: None, pacer=Pacer(cfg.apply, db, site_caps=A.site_caps(cfg)))
    assert len(appliers["mcf"].calls) == 2 and appliers["jobstreet"].calls == ["js:1"]
    assert result.stopped == "JobStreet session expired"


def test_needs_input_from_the_form_parks_the_job_with_its_questions(tmp_path, job_factory):
    cfg, db = make_cfg(tmp_path), Database(":memory:")
    seed(db, cfg, job_factory)
    outcome = Outcome(Q.NEEDS_INPUT, "1 question", questions=[{"question": Q2, "options": ["None", "3+ years"]}])
    appliers = {"mcf": Fake([Outcome(Q.APPLIED)] * 2), "jobstreet": Fake([outcome, Outcome(Q.APPLIED)])}
    A.run(cfg, db, lambda source: appliers[source], dry_run=False, sleep=lambda s: None,
          pacer=Pacer(cfg.apply, db, site_caps=A.site_caps(cfg)))
    app = db.application("js:1")
    assert app["state"] == Q.NEEDS_INPUT and json.loads(app["questions"]) == [Q2]
    assert Q.unanswered_questions(db) == {Q2: ["JS 1 (C1)"]}


def test_old_applied_on_mcf_table_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE applied_on_mcf (job_uuid TEXT PRIMARY KEY, title TEXT,
        employer TEXT, status TEXT, submitted_on TEXT, mcf_application_id TEXT, synced_at TEXT);
        INSERT INTO applied_on_mcf VALUES ('a', 'Data Analyst', 'Acme', 'Received', NULL, '1', 't');""")
    c.commit(); c.close()
    db = Database(path)
    assert db.applied_on_site_uuids() == {"a"} and db.applied_on_site_count("mcf") == 1
    assert "applied_on_mcf" not in {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_jobstreet_jobs_get_their_slots_before_higher_scoring_mcf_jobs():
    from mcf_autoapply.pipeline.apply import order_for_run
    jobs = [{"job_uuid": "m1"}, {"job_uuid": "m2"}, {"job_uuid": "js:1"}, {"job_uuid": "js:2"},
            {"job_uuid": "js:3"}]
    assert [j["job_uuid"] for j in order_for_run(jobs, 2)] == ["js:1", "js:2", "m1", "m2", "js:3"]
    assert [j["job_uuid"] for j in order_for_run(jobs, 0)] == ["m1", "m2", "js:1", "js:2", "js:3"]
    assert [j["job_uuid"] for j in order_for_run(jobs, None)] == ["js:1", "js:2", "js:3", "m1", "m2"]


def test_auto_mode_uses_the_lower_jobstreet_bar_only_when_allowed(tmp_path):
    from test_apply_flow import make_cfg
    from mcf_autoapply.db import Database
    from mcf_autoapply.models import Job
    from mcf_autoapply.pipeline import queue as Q
    from mcf_autoapply.pipeline.answers import AnswerBank
    from mcf_autoapply.pipeline.discover import apply_filters

    cfg = make_cfg(tmp_path, mode="auto", auto_apply_min_score=50)
    cfg.filters.title_include_keywords = []
    cfg.ranking.entry_level_keywords = ["junior"]
    cfg.jobstreet.enabled = True
    db = Database(":memory:")
    db.upsert_job(Job.from_jobstreet({"id": "1", "title": "Junior Developer", "companyName": "A",
                                      "listingDate": "2026-10-10T00:00:00Z"}), "x")
    apply_filters(db, cfg)
    score = db.get_job("js:1")["score"]
    assert 20 <= score < 50                       # entry-level only: below the MCF bar
    bank = AnswerBank(cfg.apply.answers_path)
    assert Q.auto_approve(cfg, db, None, bank) == []            # allow_auto off
    cfg.jobstreet.allow_auto = True
    assert [d.job_uuid for d in Q.auto_approve(cfg, db, None, bank)] == ["js:1"]


def test_jobstreet_caps_are_independent_of_the_main_caps(tmp_path, job_factory):
    """JobStreet submissions don't eat MCF's budget and MCF's cap doesn't stop JobStreet."""
    from datetime import time as dtime

    from test_apply_flow import make_cfg
    from mcf_autoapply.db import Database
    from mcf_autoapply.pipeline.apply import site_caps
    from mcf_autoapply.pipeline.pacer import APPLIED_EVENT, Pacer

    cfg = make_cfg(tmp_path, hourly_cap=2, daily_cap=10)
    cfg.apply.active_hours = (dtime(0, 0), dtime(23, 59, 59))
    cfg.jobstreet.enabled, cfg.jobstreet.hourly_cap = True, 1
    db = Database(":memory:")
    for src in ("mcf", "mcf", "jobstreet"):
        db.log(None, APPLIED_EVENT, {"source": src})
    db.commit()
    pacer = Pacer(cfg.apply, db, site_caps=site_caps(cfg))
    assert pacer.applied_this_hour() == 2 and pacer.applied_this_hour("jobstreet") == 1
    assert "hourly cap reached (2)" in pacer.blocked_reason()
    assert "jobstreet hourly cap reached (1)" in pacer.blocked_reason("jobstreet")
    db.log(None, APPLIED_EVENT, {"source": "jobstreet"})
    db.commit()
    assert pacer.applied_this_hour() == 2            # JobStreet's extra one isn't MCF's problem
