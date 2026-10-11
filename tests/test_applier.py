"""Drive the applier through a stand-in MCF site (tests/fixtures/fake_mcf.html)."""

import json
import os
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from mcf_autoapply.mcf.applier import CAPTCHA, LOGGED_OUT, Applier  # noqa: E402
from mcf_autoapply.pipeline import queue as Q  # noqa: E402

FAKE_SITE = (Path(__file__).parent / "fixtures" / "fake_mcf.html").read_text()
BASE = "https://fake-mcf.test/job"
QUESTIONS = [
    "Do you have a degree in Computer Science or a related field?",
    "Are you able to start work within one month?",
]
CHROMIUM = "/opt/pw-browsers/chromium"


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
    """A page whose requests to fake-mcf.test are served from the fixture."""
    page = browser.new_page()
    submitted = []

    def handle(route):
        req = route.request
        if req.method == "POST" and req.url.endswith("/graphql"):
            submitted.append(json.loads(req.post_data))
            return route.fulfill(json={"data": {"createApplication": {"id": "app-123"}}})
        return route.fulfill(body=FAKE_SITE, content_type="text/html")

    page.route("https://fake-mcf.test/**", handle)

    def applier(dry_run=False, resume_name=""):
        return Applier(page, tmp_path / "shots", resume_name, dry_run, step_timeout_ms=5_000)

    yield applier, submitted
    page.close()


def test_submits_with_saved_answers(site):
    make, submitted = site
    out = make().apply("u1", f"{BASE}/ok?q=2", QUESTIONS, ["yes", "no"])
    assert out.state == Q.APPLIED, out.detail
    assert out.application_id == "app-123"
    assert submitted[0]["variables"]["answers"] == {"0": "yes", "1": "no"}
    assert Path(out.screenshot).exists()


def test_dry_run_stops_at_review_without_submitting(site):
    make, submitted = site
    out = make(dry_run=True).apply("u2", f"{BASE}/ok?q=1", QUESTIONS[:1], ["yes"])
    assert out.state == Q.DRY_RUN_OK, out.detail
    assert submitted == []


def test_picks_named_resume(site):
    make, submitted = site
    out = make(resume_name="Old_CV_2024").apply("u3", f"{BASE}/ok", [], [])
    assert out.state == Q.APPLIED, out.detail
    assert submitted[0]["variables"]["resume"] == "Old_CV_2024.pdf"


def test_missing_resume_fails(site):
    make, _ = site
    out = make(resume_name="Nope.pdf").apply("u4", f"{BASE}/ok", [], [])
    assert out.state == Q.FAILED and "not found" in out.detail


@pytest.mark.parametrize("variant, state", [
    ("applied", Q.ALREADY_APPLIED),
    ("careers-gov", Q.EXTERNAL),
    ("closed", Q.CLOSED),
    ("not-permitted", Q.CLOSED),
    ("logged-out", LOGGED_OUT),
    ("captcha", CAPTCHA),
])
def test_job_page_states(site, variant, state):
    make, submitted = site
    out = make().apply("u5", f"{BASE}/{variant}", [], [])
    assert out.state == state, out.detail
    assert submitted == []


def test_step_needing_manual_details_fails_cleanly(site):
    make, submitted = site
    out = make().apply("u6", f"{BASE}/needs-details", [], [])
    assert out.state == Q.FAILED
    assert "Add experience" in out.detail and "by hand" in out.detail
    assert Path(out.screenshot).with_suffix(".html").exists()
    assert submitted == []


def test_question_mismatch_is_not_answered(site):
    make, submitted = site
    out = make().apply("u7", f"{BASE}/ok?q=2", ["Something else entirely?", QUESTIONS[1]],
                       ["yes", "yes"])
    assert out.state == Q.FAILED and "differs" in out.detail
    assert submitted == []


def test_error_banner_after_submit(site):
    make, _ = site
    out = make().apply("u8", f"{BASE}/submit-error", [], [])
    assert out.state == Q.FAILED and "error banner" in out.detail


def test_unanswered_question_never_starts(site):
    make, submitted = site
    out = make().apply("u9", f"{BASE}/ok?q=1", QUESTIONS[:1], [None])
    assert out.state == Q.FAILED and submitted == []
