"""/jobstreet on|off and /update from Telegram."""

from pathlib import Path

import pytest
import respx

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.models import Job
from mcf_autoapply.notify import Notifier
from mcf_autoapply.overrides import apply_overrides
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


def test_update_command_pulls_and_restarts_only_when_changed(ctx, tg):
    cfg, db, notifier = ctx
    restarted = []
    tg.pending_updates += [msg(1, "/update")]
    serve(cfg, db, notifier, lambda m: None, run=lambda: None, max_loops=1, background=False,
          do_update=lambda: ("✅ Already on the latest version.", False),
          do_restart=lambda: restarted.append(1))
    texts = [b["text"] for b in tg.sent()]
    assert any("Checking for an update" in t for t in texts)
    assert "✅ Already on the latest version." in texts
    assert restarted == []

    tg.pending_updates += [msg(2, "/update")]
    serve(cfg, db, notifier, lambda m: None, run=lambda: None, max_loops=1, background=False,
          do_update=lambda: ("⬇️ Updated:\n 3 files changed", True),
          do_restart=lambda: restarted.append(1))
    texts = [b["text"] for b in tg.sent()]
    assert any(t.startswith("⬇️ Updated") for t in texts)
    assert any(t.startswith("♻️ Restarting to load the update") for t in texts)
    assert restarted == [1]


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
