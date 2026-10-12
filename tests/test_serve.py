import pytest
import respx

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.notify import Notifier
from mcf_autoapply.runner import serve
from mcf_autoapply.telegram import TelegramAPI
from test_notify import CHAT, TOKEN, FakeTelegram


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def cfg(tmp_path):
    return Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        "apply": {"answers_path": str(tmp_path / "answers.yml")},
        "serve": {"interval_minutes": 60, "login_view_url": "http://pi.local:6080/vnc.html"},
    })


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def test_runs_on_start_then_every_interval_and_survives_errors(cfg, db):
    clock, runs, logs = FakeClock(), [], []

    def run():
        runs.append(clock())
        if len(runs) == 2:
            raise RuntimeError("MCF API down")

    serve(cfg, db, Notifier(cfg, db, None, None), logs.append, clock=clock, sleep=clock.sleep,
          run=run, max_loops=400, background=False)
    assert runs[0] == 0
    assert [b - a for a, b in zip(runs, runs[1:])] == [3600] * (len(runs) - 1)
    assert len(runs) >= 4                          # kept going after the failed run
    assert any("MCF API down" in m for m in logs)


@pytest.fixture
def tg():
    fake = FakeTelegram()
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=f"https://api.telegram.org/bot{TOKEN}/").mock(side_effect=fake)
        yield fake


def message(update_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": CHAT}, "text": text}}


def test_login_and_run_commands(cfg, db, tg):
    clock, runs = FakeClock(), []
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    tg.pending_updates += [message(1, "/login"), message(2, "/run@my_bot"), message(3, "/nope")]
    logins = []

    def do_login():
        logins.append(clock())
        return True

    serve(cfg, db, notifier, lambda m: None, clock=clock, sleep=clock.sleep,
          run=lambda: runs.append(clock()), do_login=do_login, max_loops=3,
          background=False)
    texts = [b["text"] for b in tg.sent()]
    assert any("QR code will arrive here" in t for t in texts)
    assert "✅ Logged in. Starting a run." in texts
    assert any(t.startswith("Starting a run now") for t in texts)
    assert any(t.startswith("<b>Commands</b>") and "/login" in t for t in texts)   # /nope
    assert logins == [0] and len(runs) == 3       # start-up, after login, and /run


def test_login_failure_is_reported_not_raised(cfg, db, tg):
    clock = FakeClock()
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    tg.pending_updates.append(message(1, "/login"))

    def no_display():
        raise RuntimeError("Missing X server or $DISPLAY")

    serve(cfg, db, notifier, lambda m: None, clock=clock, sleep=clock.sleep,
          run=lambda: None, do_login=no_display, max_loops=1, background=False)
    assert tg.sent()[-1]["text"] == "Login failed: Missing X server or $DISPLAY"


def test_logged_out_alert_says_how_to_log_in(cfg, db, tg):
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    notifier.report_stop("MCF session expired; run `mcf-apply login`")
    text = tg.sent()[-1]["text"]
    assert "Send /login" in text and "Singpass app" in text


def test_only_one_serve_at_a_time():
    from mcf_autoapply.runner import AlreadyRunning, single_instance
    lock = single_instance(port=47392)
    try:
        with pytest.raises(AlreadyRunning):
            single_instance(port=47392)
    finally:
        lock.close()
    single_instance(port=47392).close()   # free again once the first one stops


def test_telegram_answers_while_a_long_run_is_in_progress(cfg, db, tg):
    """The bug this guards against: the bot went silent for minutes during a run."""
    import threading
    import time as real_time

    from mcf_autoapply.runner import TaskRunner

    release, started = threading.Event(), threading.Event()

    def slow_run():
        started.set()
        release.wait(10)

    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    tg.pending_updates += [message(1, "/status"), message(2, "/run"), message(3, "/login")]
    t0 = real_time.monotonic()
    serve(cfg, db, notifier, lambda m: None, run=slow_run, do_login=lambda: True,
          max_loops=1, background=True)   # the start-up run goes to the background thread
    texts = [b["text"] for b in tg.sent()]
    assert started.wait(5)                         # the run really ran, in the background
    assert real_time.monotonic() - t0 < 5          # answered without waiting for the run
    assert any("⏳ Busy: searching and applying" in t for t in texts)
    assert "A run is already in progress or queued." in texts
    # /login doesn't wait: the run is told to stop between jobs and resumes afterwards.
    assert any("Stopping searching and applying so you can log in now" in t for t in texts)
    release.set()

    # One task at a time, in order, each queued once.
    order = []
    tasks = TaskRunner({"a": lambda: order.append("a"), "b": lambda: order.append("b")},
                       lambda m: None, background=False)
    assert tasks.submit("a") == "started" and tasks.submit("b") == "started"
    assert order == ["a", "b"] and not tasks.busy


def test_apply_command_skips_search_and_login_jumps_the_queue(cfg, db, tg):
    from mcf_autoapply.runner import TaskRunner
    order = []
    tasks = TaskRunner({"run": lambda: order.append("run"), "apply": lambda: order.append("apply"),
                        "login": lambda: order.append("login")}, lambda m: None, background=False)
    # Simulate a running task holding the worker: queue behind it, login first.
    tasks.current = "run"
    assert tasks.submit("apply") == "queued"
    assert tasks.submit("login", first=True) == "queued"
    assert tasks._queue == ["login", "apply"]
    tasks.current = None
    tasks._drain()
    assert order == ["login", "apply"]

    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    tg.pending_updates += [message(1, "/apply")]
    applied = []
    import mcf_autoapply.runner as R
    original = R.apply_only
    R.apply_only = lambda *a, **k: applied.append(1)
    try:
        serve(cfg, db, notifier, lambda m: None, run=lambda: None, do_login=lambda: True,
              max_loops=1, background=False)
    finally:
        R.apply_only = original
    assert applied == [1]
    assert any(t.startswith("Applying to the queue now") for t in [b["text"] for b in tg.sent()])


def test_telegram_tasks_preempt_a_running_round_which_resumes_after():
    from mcf_autoapply.runner import TaskRunner
    order = []
    tasks = TaskRunner({"run": lambda: order.append("run"), "apply": lambda: order.append("apply"),
                        "login": lambda: order.append("login")}, lambda m: None, background=False)
    tasks.current = "run"                               # the hourly round is mid-way
    assert tasks.submit("login", preempt=True) == "preempting"
    assert tasks.should_stop()                          # the round stops between jobs
    assert tasks._queue == ["login", "run"]             # login first, the round resumes after
    assert tasks.submit("apply", preempt=True) == "preempting"
    assert tasks._queue == ["apply", "login", "run"]
    tasks.current = None
    tasks._drain()
    assert order == ["apply", "login", "run"] and not tasks.should_stop()

    # A login in progress is never cut short: it's waiting on the user.
    tasks.current = "login"
    assert tasks.submit("run", preempt=True) == "queued" and not tasks.should_stop()


def test_apply_round_stops_between_jobs_when_asked(tmp_path, job_factory):
    from test_apply_flow import add_jobs, approve_all, make_cfg
    from mcf_autoapply.pipeline import apply as A
    from mcf_autoapply.mcf.applier import Outcome
    from mcf_autoapply.pipeline import queue as Q

    from datetime import time as dtime

    cfg = make_cfg(tmp_path, mode="manual")
    cfg.apply.active_hours = (dtime(0, 0), dtime(23, 59, 59))   # the pacer uses the real clock
    db = Database(":memory:")
    approve_all(db, cfg, add_jobs(db, cfg, job_factory, 3))
    calls = []

    class FakeApplier:
        def apply(self, uuid, url, questions, answers):
            calls.append(uuid)
            return Outcome(Q.APPLIED)

    stop_after = 1
    result = A.run(cfg, db, lambda s: FakeApplier(), dry_run=False, sleep=lambda s: None,
                   should_stop=lambda: len(calls) >= stop_after)
    assert len(calls) == 1 and result.stopped == A.INTERRUPTED
    assert len(A.pending(db, dry_run=False)) == 2      # the rest waits for the next round
