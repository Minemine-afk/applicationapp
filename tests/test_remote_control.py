"""/jobstreet on|off and /update from Telegram."""

from pathlib import Path

import pytest
import respx

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.models import Job
from mcf_autoapply.notify import Notifier
from mcf_autoapply.overrides import apply_overrides, set_jobstreet_enabled
from mcf_autoapply.pipeline import apply as A
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import apply_filters
from mcf_autoapply.runner import serve
from mcf_autoapply.telegram import TelegramAPI
from test_notify import CHAT, TOKEN, FakeTelegram


@pytest.fixture
def tg():
    fake = FakeTelegram()
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=f"https://api.telegram.org/bot{TOKEN}/").mock(side_effect=fake)
        yield fake


@pytest.fixture
def ctx(tmp_path, tg):
    cfg = Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        "apply": {"mode": "manual", "answers_path": str(tmp_path / "answers.yml")},
    })
    cfg.filters.title_include_keywords = []
    cfg.project_dir = tmp_path   # no config.yml here: start-up patches stay out of the tests
    db = Database(":memory:")
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    yield cfg, db, notifier
    db.close()


def msg(i, text):
    return {"update_id": i, "message": {"chat": {"id": CHAT}, "text": text}}


def test_jobstreet_on_off_from_telegram(ctx, tg):
    cfg, db, notifier = ctx
    assert cfg.enabled_sources() == ["mcf"]
    tg.pending_updates += [msg(1, "/jobstreet"), msg(2, "/jobstreet on"), msg(3, "/status")]
    notifier.handle_updates()
    texts = [b["text"] for b in tg.sent()]
    assert "JobStreet is <b>off</b>" in texts[0]
    assert texts[1].startswith("🟠 <b>JobStreet is on.</b>") and "suspended" in texts[1]
    assert "/login jobstreet" in texts[1]
    assert "JobStreet on" in texts[2]
    assert cfg.enabled_sources() == ["mcf", "jobstreet"]

    fresh = Config.model_validate({"db_path": ":memory:", "searches": [{"query": "x"}]})
    assert apply_overrides(fresh, db).jobstreet.enabled is True        # survives a restart
    assert fresh.searches[0].sources is None                            # = every enabled site

    tg.pending_updates.append(msg(4, "/jobstreet off"))
    notifier.handle_updates()
    assert cfg.jobstreet.enabled is False and tg.sent()[-1]["text"].startswith("⚪")


def test_queued_jobstreet_jobs_wait_while_jobstreet_is_off(ctx):
    cfg, db, _ = ctx
    db.upsert_job(Job.from_jobstreet({"id": "5", "title": "Dev", "companyName": "Acme",
                                      "listingDate": "2026-10-10T00:00:00Z"}), "x")
    apply_filters(db, cfg)
    Q.approve(db, None, AnswerBank(cfg.apply.answers_path), ["js:5"])
    assert A.pending(db, dry_run=False, jobstreet=False) == []
    assert [r["job_uuid"] for r in A.pending(db, dry_run=False, jobstreet=True)] == ["js:5"]


def test_queue_shows_only_whats_pending_and_todays_submissions(ctx, tg):
    from datetime import datetime, timedelta, timezone

    cfg, db, notifier = ctx
    for i, title in enumerate(["Queued Dev", "Done Today", "Done Last Week", "Failed One",
                               "Needs Answers"]):
        db.upsert_job(Job.from_jobstreet({"id": str(i), "title": title, "companyName": "Acme",
                                          "listingDate": "2026-10-10T00:00:00Z"}), "x")
    apply_filters(db, cfg)
    Q.approve(db, None, AnswerBank(cfg.apply.answers_path), [f"js:{i}" for i in range(5)])
    now = datetime.now(timezone.utc)
    db.update_application("js:1", state=Q.APPLIED, submitted_at=now.isoformat(timespec="seconds"))
    db.update_application("js:2", state=Q.APPLIED,
                          submitted_at=(now - timedelta(days=8)).isoformat(timespec="seconds"))
    db.update_application("js:3", state=Q.FAILED, error="boom")
    db.update_application("js:4", state=Q.NEEDS_INPUT)
    db.commit()
    text = notifier.queue_text()
    assert "📥 To be submitted (1)" in text and "Queued Dev" in text
    assert "✅ Applied today (1)" in text and "Done Today" in text
    assert "1 waiting on your answers" in text
    assert "Done Last Week" not in text and "Failed One" not in text and "Needs Answers" not in text


def test_update_command_pulls_then_restarts_unless_the_pull_failed(ctx, tg):
    cfg, db, notifier = ctx
    restarted = []
    tg.pending_updates += [msg(1, "/update")]
    serve(cfg, db, notifier, lambda m: None, run=lambda: None, max_loops=1, background=False,
          do_update=lambda: ("❌ git pull failed:\nno internet", False),
          do_restart=lambda: restarted.append(1))
    texts = [b["text"] for b in tg.sent()]
    assert any("Checking for an update" in t for t in texts)
    assert any(t.startswith("❌ git pull failed") for t in texts)
    assert any(t.startswith("Not restarting") for t in texts)
    assert restarted == []

    for i, reply in enumerate(("✅ Already on the latest version.", "⬇️ Updated:\n 3 files changed"), 2):
        tg.pending_updates += [msg(i, "/update")]
        serve(cfg, db, notifier, lambda m: None, run=lambda: None, max_loops=1, background=False,
              do_update=lambda: (reply, True), do_restart=lambda: restarted.append(1))
        texts = [b["text"] for b in tg.sent()]
        assert reply in texts
        assert any(t.startswith("♻️ Restarting") for t in texts)
    assert restarted == [1, 1]


def test_run_update_reports_a_real_git_checkout(tmp_path):
    import subprocess

    from mcf_autoapply.updater import run_update
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    summary, changed = run_update(tmp_path)        # no remote: pull fails cleanly
    assert summary.startswith("❌ git pull failed") and changed is False


def test_update_never_reinstalls_while_running():
    """pip install -e while mcf-apply runs removed the package under it (crash loop with
    "No module named 'mcf_autoapply'"). The start script reinstalls before each launch instead."""
    import inspect

    from mcf_autoapply import updater
    assert "pip" not in inspect.getsource(updater.run_update)
    script = (Path(__file__).parents[1] / "scripts" / "start-serve-windows.cmd").read_text()
    assert script.index("pip install -q -e .") < script.index("mcf-apply.exe\" serve")


def test_jobstreet_check_names_the_blockers(ctx, tg):
    cfg, db, notifier = ctx
    cfg.apply.mode = "auto"
    tg.pending_updates += [msg(1, "/jobstreet check")]
    notifier.handle_updates()
    text = tg.sent()[-1]["text"]
    assert text.startswith("<b>JobStreet check</b>\n🔴 Blocking: JobStreet is off; never logged in")
    assert "/jobstreet on" in text and "/login jobstreet" in text

    set_jobstreet_enabled(cfg, db, True)
    db.set_state("jobstreet:login", "in")
    db.upsert_job(Job.from_jobstreet({"id": "9", "title": "Dev", "companyName": "Acme",
                                      "listingDate": "2026-10-10T00:00:00Z"}), "x")
    apply_filters(db, cfg)
    text = notifier.jobstreet_check()
    assert "🔴 Blocking: auto mode not allowed for JobStreet" in text
    assert "/jobstreet auto on" in text and "1 JobStreet jobs found" in text

    tg.pending_updates += [msg(2, "/jobstreet auto on")]
    notifier.handle_updates()
    assert cfg.jobstreet.allow_auto is True
    fresh = Config.model_validate({"db_path": ":memory:", "searches": [{"query": "x"}]})
    assert apply_overrides(fresh, db).jobstreet.allow_auto is True
    assert notifier.jobstreet_check().splitlines()[1].startswith("🟢 Nothing is blocking")


def test_config_command_edits_config_yml(ctx, tg, tmp_path):
    import shutil

    cfg, db, notifier = ctx
    cfg.project_dir = tmp_path
    shutil.copy(Path(__file__).parents[1] / "config.example.yml", tmp_path / "config.yml")
    assert "already" in notifier._cmd_config("apply.auto_apply_min_score 35")
    assert notifier._cmd_config("apply.auto_apply_min_score 60").startswith("✏️")
    assert "auto_apply_min_score: 60\n" in (tmp_path / "config.yml").read_text()
    assert "isn't valid" in notifier._cmd_config("apply.auto_apply_min_score banana")
    assert "auto_apply_min_score: 60\n" in (tmp_path / "config.yml").read_text()   # rolled back
    assert "Couldn't find" in notifier._cmd_config("apply.nonsense 1")
    assert "Use /config" in notifier._cmd_config("nonsense")


def test_requested_settings_patch_runs_once(ctx, tmp_path):
    import shutil

    from mcf_autoapply.patches import apply_patches

    cfg, db, _ = ctx
    cfg.project_dir = tmp_path
    text = (Path(__file__).parents[1] / "config.example.yml").read_text()
    (tmp_path / "config.yml").write_text(text.replace("auto_apply_min_score: 55", "auto_apply_min_score: 70")
                                         .replace("delay_seconds: [45, 100]", "delay_seconds: [45, 120]"))
    report = apply_patches(cfg, db)
    assert len(report) == 5 and "apply.auto_apply_min_score: 55" in report[0]
    new = (tmp_path / "config.yml").read_text()
    assert "auto_apply_min_score: 35\n" in new and "delay_seconds: [45, 100] " in new   # 2nd patch: 35
    assert "  enabled: true\n" in new.split("jobstreet:")[1]
    assert cfg.apply.auto_apply_min_score == 35 and cfg.apply.delay_seconds == (45, 100)
    assert cfg.jobstreet.enabled and (cfg.apply.hourly_cap, cfg.apply.daily_cap) == (20, 170)   # 5th patch
    assert apply_patches(cfg, db) == []            # never twice


def test_broader_search_patch_rewrites_sections_and_refilters(ctx, tmp_path, job_factory):
    from mcf_autoapply.patches import apply_patches
    from mcf_autoapply.setup import section_block

    cfg, db, _ = ctx
    cfg.project_dir = tmp_path
    example = Path(__file__).parents[1] / "config.example.yml"
    old = example.read_text().replace("max_years_experience_required: 1", "max_years_experience_required: 0")
    old = old[:old.index("searches:")] + 'searches:\n  - query: "old"\n\n' + old[old.index("filters:"):]
    (tmp_path / "config.yml").write_text(old)
    (tmp_path / "config.example.yml").write_text(example.read_text())
    db.upsert_job(Job.from_api(job_factory(title="Cloud Engineer", minimumYearsExperience=1)), "x")

    report = apply_patches(cfg, db)
    assert any("broader-search" in r for r in report)
    new = (tmp_path / "config.yml").read_text()
    assert section_block(new, "searches") == section_block(example.read_text(), "searches")
    assert "max_years_experience_required: 1" in new and "auto_apply_min_score: 35" in new
    assert "- query: \"old\"" not in new
    assert len(cfg.searches) == 7 and cfg.filters.max_years_experience_required == 1
    assert db.jobs(["new"])[0]["title"] == "Cloud Engineer"   # 1 year passes the filter
