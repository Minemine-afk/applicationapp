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
          run=run, max_loops=400)
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
          run=lambda: runs.append(clock()), do_login=do_login, max_loops=3)
    texts = [b["text"] for b in tg.sent()]
    assert any("QR code will arrive here" in t for t in texts)
    assert "✅ Logged in. Starting a run." in texts
    assert "Starting a run now." in texts
    assert any(t.startswith("Commands:") and "/login" in t for t in texts)
    assert logins == [0] and len(runs) == 2       # start-up run + run triggered by commands


def test_login_failure_is_reported_not_raised(cfg, db, tg):
    clock = FakeClock()
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    tg.pending_updates.append(message(1, "/login"))

    def no_display():
        raise RuntimeError("Missing X server or $DISPLAY")

    serve(cfg, db, notifier, lambda m: None, clock=clock, sleep=clock.sleep,
          run=lambda: None, do_login=no_display, max_loops=1)
    assert tg.sent()[-1]["text"] == "Login failed: Missing X server or $DISPLAY"


def test_logged_out_alert_points_to_login_view(cfg, db, tg):
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    notifier.report_stop("MCF session expired; run `mcf-apply login`")
    text = tg.sent()[-1]["text"]
    assert "Send /login" in text and "http://pi.local:6080/vnc.html" in text
