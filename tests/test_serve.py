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
    cfg = Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        "apply": {"answers_path": str(tmp_path / "answers.yml")},
        "serve": {"interval_minutes": 60, "login_view_url": "http://pi.local:6080/vnc.html"},
    })
    cfg.project_dir = tmp_path   # no config.yml here: start-up patches stay out of the tests
    return cfg


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


def test_status_summarises_the_running_task(cfg, db, tg):
    """/status during a run says what the run is doing right now, not just "busy"."""
    from mcf_autoapply.runner import TaskRunner
    tasks = TaskRunner({"run": lambda: None}, lambda m: None, background=False)
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    notifier.progress = tasks.progress.text
    assert "💤 Idle" in notifier.status_text()
    tasks.progress.start("run")
    tasks.progress.set("applying to job 2/5: Graduate Engineer · so far ✅ 1 ❌ 0")
    status = notifier.status_text()
    assert "⏳ Busy: searching and applying (0 min in): applying to job 2/5" in status
    tasks.progress.clear()
    assert "Busy" not in notifier.status_text()


def test_run_reports_progress_per_job(tmp_path, job_factory):
    from datetime import time as dtime

    from test_apply_flow import add_jobs, approve_all, make_cfg
    from mcf_autoapply.mcf.applier import Outcome
    from mcf_autoapply.pipeline import apply as A
    from mcf_autoapply.pipeline import queue as Q

    cfg = make_cfg(tmp_path, mode="manual")
    cfg.apply.active_hours = (dtime(0, 0), dtime(23, 59, 59))
    db = Database(":memory:")
    approve_all(db, cfg, add_jobs(db, cfg, job_factory, 2))
    seen = []

    class FakeApplier:
        def apply(self, *a):
            return Outcome(Q.APPLIED)

    A.run(cfg, db, lambda s: FakeApplier(), dry_run=False, sleep=lambda s: None,
          on_progress=seen.append)
    assert seen[0].startswith("applying to job 1/2:") and "so far ✅ 0 ❌ 0" in seen[0]
    assert seen[1].startswith("waiting ") and "before job 2/2" in seen[1] and "✅ 1" in seen[1]


def test_hourly_cap_leftovers_are_applied_at_the_top_of_the_next_hour(cfg, db, tg):
    """A round capped at N/hour left the rest for the next scheduled run (up to an hour
    later); now serve applies them at the next clock hour."""
    from datetime import datetime, timedelta, timezone

    from mcf_autoapply.runner import RETRY_HOUR_KEY

    applied = []
    import mcf_autoapply.runner as R
    original = R.apply_only
    R.apply_only = lambda *a, **k: applied.append(1)
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    try:
        db.set_state(RETRY_HOUR_KEY, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        db.commit()
        clock = FakeClock()
        serve(cfg, db, notifier, lambda m: None, clock=clock, sleep=clock.sleep, run=lambda: None,
              max_loops=2, background=False)
        assert applied == []                                   # not yet that hour
        db.set_state(RETRY_HOUR_KEY, (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        db.commit()
        serve(cfg, db, notifier, lambda m: None, clock=clock, sleep=clock.sleep, run=lambda: None,
              max_loops=2, background=False)
        assert applied == [1] and db.get_state(RETRY_HOUR_KEY) is None
    finally:
        R.apply_only = original


def test_retry_time_is_set_only_when_the_hourly_cap_is_the_blocker(tmp_path, job_factory):
    from datetime import time as dtime

    from test_apply_flow import add_jobs, approve_all, make_cfg
    from mcf_autoapply.pipeline.pacer import APPLIED_EVENT
    from mcf_autoapply.runner import hour_cap_retry_at

    cfg = make_cfg(tmp_path, mode="manual", hourly_cap=1)
    cfg.apply.active_hours = (dtime(0, 0), dtime(23, 59, 59))
    db = Database(":memory:")
    assert hour_cap_retry_at(cfg, db) is None                 # nothing queued
    approve_all(db, cfg, add_jobs(db, cfg, job_factory, 2))
    assert hour_cap_retry_at(cfg, db) is None                 # cap not reached
    db.log(None, APPLIED_EVENT, {"source": "mcf"})
    db.commit()
    retry = hour_cap_retry_at(cfg, db)
    assert retry and retry.endswith(":01:00+00:00")           # a minute past the next hour


def test_jobstreet_runs_on_its_own_schedule_when_on(cfg, db):
    """MCF and JobStreet rounds are separate tasks on separate timers."""
    from mcf_autoapply.overrides import set_jobstreet_enabled
    from mcf_autoapply.runner import TaskRunner

    set_jobstreet_enabled(cfg, db, True)
    cfg.jobstreet.interval_minutes = 60
    clock, names = FakeClock(), []
    import mcf_autoapply.runner as R
    original = R.TaskRunner.submit

    def spy(self, name, *a, **k):
        if name.startswith("run"):
            names.append((name, clock()))
        return original(self, name, *a, **k)
    R.TaskRunner.submit = spy
    try:
        serve(cfg, db, Notifier(cfg, db, None, None), lambda m: None, clock=clock,
              sleep=clock.sleep, run=lambda: None, max_loops=150, background=False)
    finally:
        R.TaskRunner.submit = original
    mcf = [t for n, t in names if n == "run_mcf"]
    js = [t for n, t in names if n == "run_jobstreet"]
    assert mcf[:2] == [0, 3600] and js[0] == 1800 and js[1] - js[0] == 3600
    assert not any(n == "run" for n, _ in names)


def test_run_and_apply_take_a_site(cfg, db, tg):
    from mcf_autoapply.overrides import set_jobstreet_enabled
    set_jobstreet_enabled(cfg, db, True)
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    tg.pending_updates += [message(1, "/run jobstreet"), message(2, "/apply mcf"),
                           message(3, "/run mars")]
    serve(cfg, db, notifier, lambda m: None, run=lambda: None, max_loops=1, background=False)
    texts = [b["text"] for b in tg.sent()]
    assert any(t.startswith("Starting a run on JobStreet now") for t in texts)
    assert any(t.startswith("Applying to the queue on MCF now") for t in texts)
    assert "Use /run, /run mcf or /run jobstreet." in texts


def test_your_commands_queue_behind_each_other_instead_of_interrupting():
    """/run then /apply stopped the run you'd just started; now /apply waits for it."""
    from mcf_autoapply.runner import TaskRunner
    tasks = TaskRunner({n: (lambda: None) for n in ("run", "apply", "run_mcf")}, lambda m: None,
                       background=False)
    tasks.current = "run_mcf"                                   # the hourly round
    assert tasks.submit("run", preempt=True) == "preempting"    # your /run goes first
    tasks.interrupt.clear()
    tasks._queue.remove("run")
    tasks.current = "run"                                       # now your /run is running
    assert tasks.submit("apply", preempt=True) == "queued"      # /apply waits its turn
    assert not tasks.should_stop()
