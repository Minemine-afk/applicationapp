from datetime import datetime, time, timedelta, timezone

import pytest

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.mcf.applier import CAPTCHA, LOGGED_OUT, Outcome
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline import apply as A
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import apply_filters
from mcf_autoapply.pipeline.pacer import APPLIED_EVENT, Pacer

SGT = timezone(timedelta(hours=8))
NOON = datetime(2026, 10, 12, 12, 0, tzinfo=SGT)


# --- answer bank ---------------------------------------------------------------------------

def test_answer_bank_exact_fuzzy_and_unknown(tmp_path):
    bank = AnswerBank(tmp_path / "answers.yml")
    bank.add("Are you a Singapore Citizen or PR?", "yes")
    bank.add("Do you have 5 years of Kubernetes experience?", "no")
    reloaded = AnswerBank(tmp_path / "answers.yml")   # survives a save/load round trip
    assert reloaded.lookup("are you a singapore citizen or pr") == "yes"
    assert reloaded.lookup("Are you a Singapore Citizen / PR?") == "yes"   # small wording change
    assert reloaded.lookup("Do you have 5 years of Kubernetes experience ?") == "no"
    assert reloaded.lookup("Do you have a driving licence?") is None       # never guessed


def test_answer_bank_reads_yaml_booleans_and_rejects_junk(tmp_path):
    path = tmp_path / "answers.yml"
    path.write_text("- question: Can you work weekends?\n  answer: yes\n")
    assert AnswerBank(path).lookup("Can you work weekends?") == "yes"
    path.write_text("- question: Can you work weekends?\n  answer: maybe\n")
    with pytest.raises(ValueError, match="entry 1"):
        AnswerBank(path)


# --- fixtures ------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, questions=None):
        self.questions = questions or {}

    def screening_questions(self, uuid):
        return self.questions.get(uuid, [])


class FakeApplier:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def apply(self, uuid, url, questions, answers):
        self.calls.append((uuid, answers))
        return self.outcomes.pop(0)


def make_cfg(tmp_path, **apply):
    return Config.model_validate({
        "db_path": ":memory:",
        "searches": [{"query": "x"}],
        "apply": {"mode": "manual", "answers_path": str(tmp_path / "answers.yml"),
                  "active_hours": ["08:00", "22:00"], "ramp": [], "daily_cap": 10,
                  "hourly_cap": 3, **apply},
    })


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def add_jobs(db, cfg, job_factory, n, **overrides):
    uuids = []
    for _ in range(n):
        raw = job_factory(**overrides)
        db.upsert_job(Job.from_api(raw), "x")
        uuids.append(raw["uuid"])
    apply_filters(db, cfg)
    return uuids


def fixed_pacer(cfg, db, now=NOON):
    return Pacer(cfg.apply, db, now=lambda: now)


def live_pacer(cfg, db):
    """Real clock, since the DB stamps events with it; active all day so tests never block."""
    cfg.apply.active_hours = (time(0, 0), time(23, 59, 59))
    return Pacer(cfg.apply, db)


# --- queue ---------------------------------------------------------------------------------

def test_approve_needs_input_then_answer_releases(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path)
    (u1,) = add_jobs(db, cfg, job_factory, 1)
    client = FakeClient({u1: ["Can you start within a month?"]})
    bank = AnswerBank(cfg.apply.answers_path)

    [d] = Q.approve(db, client, bank, [u1[:8]])
    assert d.state == Q.NEEDS_INPUT
    assert Q.unanswered_questions(db) == {"Can you start within a month?": [
        "Software Engineer (ACME PTE. LTD.)"]}

    bank.add("Can you start within a month?", "yes")
    [released] = Q.refresh_answers(db, bank)
    assert released.state == Q.APPROVED
    assert db.application(u1)["answers"] == '["yes"]'


def test_careers_gov_jobs_are_external_and_skips_stick(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path)
    ext, other = add_jobs(db, cfg, job_factory, 1, psdUrl="https://careers.gov.sg/x") + \
        add_jobs(db, cfg, job_factory, 1)
    bank = AnswerBank(cfg.apply.answers_path)
    assert Q.approve(db, FakeClient(), bank, [ext])[0].state == Q.EXTERNAL
    Q.skip(db, [other])
    cfg.apply.mode = "auto"
    cfg.apply.auto_apply_min_score = 0
    assert Q.auto_approve(cfg, db, FakeClient(), bank) == []   # skipped job is left alone


def test_auto_approve_respects_threshold_and_mode(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path, mode="auto", auto_apply_min_score=50)
    fresh = add_jobs(db, cfg, job_factory, 1, title="Graduate Software Engineer",
                     minimumYearsExperience=0)
    cfg.ranking.entry_level_keywords = ["graduate"]
    apply_filters(db, cfg)
    add_jobs(db, cfg, job_factory, 1, minimumYearsExperience=2)   # scores below 50
    bank = AnswerBank(cfg.apply.answers_path)
    decisions = Q.auto_approve(cfg, db, FakeClient(), bank)
    assert [d.job_uuid for d in decisions] == fresh
    cfg.apply.mode = "manual"
    assert Q.auto_approve(cfg, db, FakeClient(), bank) == []


# --- pacer ---------------------------------------------------------------------------------

def test_pacer_hours_and_caps(tmp_path, db):
    cfg = make_cfg(tmp_path, hourly_cap=2, daily_cap=3)
    assert fixed_pacer(cfg, db).blocked_reason() is None
    assert "active hours" in fixed_pacer(cfg, db, NOON.replace(hour=23)).blocked_reason()

    for minutes in (10, 20):
        db.conn.execute("INSERT INTO events (ts, type) VALUES (?, ?)",
                        ((NOON - timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat(),
                         APPLIED_EVENT))
    pacer = fixed_pacer(cfg, db)
    assert pacer.remaining() == 0 and "hourly cap" in pacer.blocked_reason()
    later = fixed_pacer(cfg, db, NOON + timedelta(hours=2))
    assert later.remaining() == 1          # daily cap 3, 2 used today


def test_ramp_steps_up_weekly_and_restarts_after_pause(tmp_path, db):
    cfg = make_cfg(tmp_path, ramp=[2, 4, 6], daily_cap=6)
    db.conn.execute("INSERT INTO events (ts, type) VALUES (?, ?)",
                    ((NOON - timedelta(days=8)).astimezone(timezone.utc).isoformat(), APPLIED_EVENT))
    assert fixed_pacer(cfg, db).daily_cap() == 4
    pacer = fixed_pacer(cfg, db)
    pacer.pause("test")
    assert "paused" in pacer.blocked_reason()
    pacer.unpause()
    assert pacer.blocked_reason() is None and pacer.daily_cap() == 2


# --- run -----------------------------------------------------------------------------------

def approve_all(db, cfg, uuids):
    Q.approve(db, FakeClient(), AnswerBank(cfg.apply.answers_path), uuids)


def test_run_applies_in_score_order_within_caps(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path, hourly_cap=2)
    uuids = add_jobs(db, cfg, job_factory, 3)
    approve_all(db, cfg, uuids)
    applier = FakeApplier([Outcome(Q.APPLIED, application_id="a1"), Outcome(Q.APPLIED)])
    pacer = live_pacer(cfg, db)
    result = A.run(cfg, db, lambda: applier, dry_run=False, sleep=lambda s: None, pacer=pacer)
    assert [o.state for _, o in result.outcomes] == [Q.APPLIED, Q.APPLIED]
    assert db.application_counts() == {Q.APPLIED: 2, Q.APPROVED: 1}
    assert "hourly cap" in pacer.blocked_reason()


def test_dry_run_mode_never_submits(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path, mode="dry_run")
    approve_all(db, cfg, add_jobs(db, cfg, job_factory, 1))
    applier = FakeApplier([Outcome(Q.DRY_RUN_OK)])
    result = A.run(cfg, db, lambda: applier, dry_run=False, sleep=lambda s: None,
                   pacer=fixed_pacer(cfg, db))
    assert result.dry_run is True
    assert db.application_counts() == {Q.DRY_RUN_OK: 1}


def test_captcha_pauses_and_logout_stops(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path)
    approve_all(db, cfg, add_jobs(db, cfg, job_factory, 2))
    pacer = fixed_pacer(cfg, db)
    result = A.run(cfg, db, lambda: FakeApplier([Outcome(LOGGED_OUT, "expired")]),
                   dry_run=False, sleep=lambda s: None, pacer=pacer)
    assert result.stopped == "expired" and db.application_counts() == {Q.APPROVED: 2}

    result = A.run(cfg, db, lambda: FakeApplier([Outcome(CAPTCHA)]),
                   dry_run=False, sleep=lambda s: None, pacer=pacer)
    assert "CAPTCHA" in result.stopped and pacer.paused()
    result = A.run(cfg, db, lambda: FakeApplier([]), dry_run=False, sleep=lambda s: None,
                   pacer=pacer)
    assert "paused" in result.stopped and result.outcomes == []


def test_repeated_failures_pause_and_retries_are_capped(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path, max_failures_per_hour=2)
    approve_all(db, cfg, add_jobs(db, cfg, job_factory, 3))
    pacer = live_pacer(cfg, db)
    applier = FakeApplier([Outcome(Q.FAILED, "x")] * 3)
    result = A.run(cfg, db, lambda: applier, dry_run=False, sleep=lambda s: None, pacer=pacer)
    assert len(result.outcomes) == 2 and "failures" in result.stopped

    db.conn.execute("UPDATE applications SET attempts = ?", (Q.MAX_ATTEMPTS,))
    assert A.pending(db, dry_run=False) == [
        r for r in db.applications([Q.APPROVED])]   # failed jobs at the cap drop out


def test_answer_bank_keeps_non_ascii_text(tmp_path):
    bank = AnswerBank(tmp_path / "answers.yml")
    bank.add("Are you comfortable with “on-call” rotations — including weekends?", "yes")
    raw = (tmp_path / "answers.yml").read_bytes().decode("utf-8")   # written as UTF-8
    assert "“on-call”" in raw
    assert AnswerBank(tmp_path / "answers.yml").lookup(
        "Are you comfortable with “on-call” rotations — including weekends?") == "yes"
