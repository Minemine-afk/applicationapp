import json

import httpx
import pytest
import respx

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.mcf.applier import Outcome
from mcf_autoapply.models import Job
from mcf_autoapply.notify import Notifier, question_key
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.apply import RunResult
from mcf_autoapply.pipeline.discover import apply_filters
from mcf_autoapply.telegram import TelegramAPI

CHAT = 4242
TOKEN = "123:secret"


class FakeTelegram:
    """Records Bot API calls; serves queued updates."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.pending_updates: list[dict] = []
        self.next_id = 100
        self.fail: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("multipart/"):
            body = {"multipart": request.content.decode("utf-8", "replace")}
        else:
            body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if self.fail:
            return httpx.Response(403, json={"ok": False, "description": self.fail})
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getUpdates":
            offset = body.get("offset") or 0
            updates = [u for u in self.pending_updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method="sendMessage"):
        return [b for m, b in self.calls if m == method]

    def tap(self, update_id, data, chat=CHAT, message_id=101, text="card"):
        self.pending_updates.append({"update_id": update_id, "callback_query": {
            "id": f"cb{update_id}", "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}, "text": text}}})


class FakeClient:
    def __init__(self, questions=None):
        self.questions = questions or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def screening_questions(self, uuid):
        return self.questions.get(uuid, [])


@pytest.fixture
def tg():
    fake = FakeTelegram()
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=f"https://api.telegram.org/bot{TOKEN}/").mock(side_effect=fake)
        yield fake


@pytest.fixture
def setup(tmp_path, job_factory, tg):
    cfg = Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        # Entry-level fit only: top = 100, mid (1 year) = 30, low (2 years) = 10.
        "ranking": {"entry_level_keywords": ["graduate"], "entry_level_weight": 1.0},
        "apply": {"mode": "auto", "auto_apply_min_score": 80,
                  "answers_path": str(tmp_path / "answers.yml")},
        "telegram": {"enabled": True, "quick_review_min_score": 25, "max_cards_per_run": 5},
    })
    db = Database(":memory:")
    jobs = {}
    for name, overrides in {
        "top": dict(title="Graduate Software Engineer", minimumYearsExperience=0),  # >= 80: auto
        "mid": dict(title="Software Engineer", minimumYearsExperience=1),           # review band
        "low": dict(title="Backend Engineer", minimumYearsExperience=2),
    }.items():
        raw = job_factory(**overrides)
        db.upsert_job(Job.from_api(raw), "x")
        jobs[name] = raw["uuid"]
    apply_filters(db, cfg)
    client = FakeClient()
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT, client_factory=lambda: client)
    yield cfg, db, notifier, jobs, client
    db.close()


def scores(db):
    return {r["uuid"]: r["score"] for r in db.jobs(["new"])}


def test_review_cards_only_for_the_band_and_only_once(setup, tg):
    cfg, db, notifier, jobs, _ = setup
    s = scores(db)
    assert (s[jobs["top"]], s[jobs["mid"]], s[jobs["low"]]) == (100, 30, 10)
    band = {jobs["mid"]}

    assert notifier.send_review_cards() == len(band)
    cards = tg.sent()
    assert {c["reply_markup"]["inline_keyboard"][0][0]["callback_data"][2:] for c in cards} == band
    assert "Score" in cards[0]["text"] and cards[0]["parse_mode"] == "HTML"
    assert notifier.send_review_cards() == 0     # not sent twice


def test_manual_mode_sends_every_job_above_the_floor(setup, tg):
    cfg, db, notifier, jobs, _ = setup
    cfg.apply.mode = "manual"
    notifier.send_review_cards()
    assert jobs["top"] in {c["reply_markup"]["inline_keyboard"][0][0]["callback_data"][2:]
                           for c in tg.sent()}


def test_apply_tap_queues_and_skip_tap_skips(setup, tg):
    cfg, db, notifier, jobs, _ = setup
    tg.tap(1, f"a:{jobs['mid']}")
    tg.tap(2, f"s:{jobs['low']}")
    done = notifier.handle_updates()
    assert done == ["approved: Software Engineer", "skipped: Backend Engineer"]
    assert db.application(jobs["mid"])["state"] == Q.APPROVED
    assert db.application(jobs["low"])["state"] == Q.SKIPPED
    edits = tg.sent("editMessageText")
    assert "queued to apply" in edits[0]["text"] and "skipped" in edits[1]["text"]
    assert notifier.handle_updates() == []       # offset advanced: taps handled once


def test_taps_from_other_chats_are_ignored(setup, tg):
    _, db, notifier, jobs, _ = setup
    tg.tap(1, f"a:{jobs['mid']}", chat=999)
    assert notifier.handle_updates() == ["ignored a tap from another chat"]
    assert db.application(jobs["mid"]) is None


def test_question_cards_and_answer_tap_release_jobs(setup, tg):
    cfg, db, notifier, jobs, client = setup
    question = "Can you start within one month?"
    client.questions[jobs["mid"]] = [question]
    tg.tap(1, f"a:{jobs['mid']}")
    notifier.handle_updates()
    assert db.application(jobs["mid"])["state"] == Q.NEEDS_INPUT
    q_cards = [c for c in tg.sent() if c["text"].startswith("❓")]
    assert len(q_cards) == 1 and question in q_cards[0]["text"]   # sent right after the tap
    assert notifier.send_question_cards() == 0

    tg.tap(2, f"q:{question_key(question)}:yes")
    assert notifier.handle_updates() == [f"answered yes: {question}"]
    assert db.application(jobs["mid"])["state"] == Q.APPROVED
    assert AnswerBank(cfg.apply.answers_path).lookup(question) == "yes"
    assert "1 job(s) queued" in tg.sent("editMessageText")[-1]["text"]


def test_alerts_are_rate_limited(setup, tg):
    _, _, notifier, _, _ = setup
    notifier.report_stop("MCF session expired; run `mcf-apply login`")
    notifier.report_stop("MCF session expired; run `mcf-apply login`")
    notifier.report_stop("daily cap reached (15)")        # not alert-worthy
    texts = [c["text"] for c in tg.sent()]
    assert len(texts) == 1 and "login expired" in texts[0]


def test_run_summary_only_when_something_happened(setup, tg):
    _, _, notifier, _, _ = setup
    assert notifier.run_summary(3, 0, RunResult(dry_run=False)) is False
    result = RunResult(dry_run=False, outcomes=[
        ("Associate AI Engineer", Outcome(Q.APPLIED)),
        ("Data Analyst <Contract>", Outcome(Q.FAILED, "stuck")),
    ])
    assert notifier.run_summary(3, 1, result) is True
    text = tg.sent()[-1]["text"]
    assert "✅ Associate AI Engineer" in text and "Data Analyst &lt;Contract&gt; — stuck" in text


def test_status_command_and_disabled_notifier(setup, tg):
    cfg, db, notifier, _, _ = setup
    tg.pending_updates.append({"update_id": 7, "message": {"chat": {"id": CHAT}, "text": "/status"}})
    assert notifier.handle_updates() == ["handled /status"]
    assert "Mode" in tg.sent()[-1]["text"]

    off = Notifier(cfg, db, None, None)
    assert not off.enabled and off.send_review_cards() == 0 and off.handle_updates() == []


def test_telegram_errors_never_break_a_run(setup, tg):
    _, _, notifier, _, _ = setup
    tg.fail = "Forbidden: bot was blocked by the user"
    assert notifier.send_review_cards() == 0
    assert notifier.handle_updates() == []
    assert any("bot was blocked" in e for e in notifier.errors)
    assert all(TOKEN not in e for e in notifier.errors)


def test_login_qr_is_sent_as_photo_with_open_in_app_button(setup, tg):
    from mcf_autoapply.mcf.browser import LoginQR
    _, _, notifier, _, _ = setup
    link = "https://app.singpass.gov.sg/qrlogin?session=abc"
    assert notifier.send_login_qr(LoginQR(b"\x89PNG fake", link), 0)
    [photo] = tg.sent("sendPhoto")
    body = photo["multipart"]
    assert "Singpass login for MyCareersFuture" in body and "Open in Singpass app" in body
    assert link in body and "image/png" in body

    assert notifier.send_login_qr(LoginQR(b"png", None), 1)   # undecodable QR: photo only
    assert "Open in Singpass app" not in tg.sent("sendPhoto")[-1]["multipart"].split("caption")[0]
    assert "inline_keyboard" not in tg.sent("sendPhoto")[-1]["multipart"]


# --- control from Telegram ---------------------------------------------------------------

def msg(update_id, text, chat=CHAT):
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


def test_first_start_links_the_chat_and_other_chats_are_ignored(setup, tg):
    cfg, db, notifier, _, _ = setup
    notifier.chat_id = None                       # fresh install: nothing linked yet
    tg.pending_updates += [msg(1, "hello", chat=555), msg(2, "/start", chat=555),
                           msg(3, "/status", chat=999)]
    assert notifier.handle_updates() == ["linked chat 555"]
    assert notifier.chat_id == 555 and db.get_state("tg:chat_id") == "555"
    assert "Linked" in tg.sent()[0]["text"] and "/jobs" in tg.sent()[0]["text"]
    assert len(tg.sent()) == 1                    # chat 999 got nothing


def test_mode_command_and_buttons_persist(setup, tg):
    from mcf_autoapply.overrides import apply_overrides
    cfg, db, notifier, _, _ = setup
    tg.pending_updates += [msg(1, "/mode")]
    notifier.handle_updates()
    buttons = tg.sent()[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["m:dry_run", "m:manual", "m:auto"]

    tg.tap(2, "m:manual")
    assert notifier.handle_updates() == ["mode set to manual"]
    assert "Manual" in tg.sent("editMessageText")[-1]["text"]
    cfg.apply.mode = "auto"
    assert apply_overrides(cfg, db).apply.mode == "manual"   # survives restarts

    tg.pending_updates += [msg(3, "/mode dry run")]
    notifier.handle_updates()
    assert cfg.apply.mode == "dry_run" and "Mode set to" in tg.sent()[-1]["text"]


def test_pause_resume_queue_jobs_and_unknown_text(setup, tg):
    cfg, db, notifier, jobs, _ = setup
    tg.pending_updates += [msg(1, "/pause"), msg(2, "/jobs 2"), msg(3, "/queue"),
                           msg(4, "/resume"), msg(5, "what?")]
    notifier.handle_updates()
    texts = [b["text"] for b in tg.sent()]
    assert texts[0].startswith("⏸")
    cards = [t for t in texts if "Score" in t]
    assert len(cards) == 2                         # /jobs 2: the best two, any score band
    assert "Queue is empty" in texts[3]
    assert texts[4].startswith("▶️")
    assert texts[5].startswith("<b>Commands</b>")  # unknown text → help


def test_run_summary_sends_dry_run_and_failure_screenshots(setup, tg, tmp_path):
    from mcf_autoapply.mcf.applier import Outcome
    from mcf_autoapply.pipeline.apply import RunResult
    _, _, notifier, _, _ = setup
    shot = tmp_path / "review.png"
    shot.write_bytes(b"\x89PNG")
    result = RunResult(dry_run=True, outcomes=[
        ("Associate AI Engineer", Outcome(Q.DRY_RUN_OK, "stopped", str(shot))),
        ("Data Analyst", Outcome(Q.FAILED, "stuck on Add experience", str(shot))),
        ("Gone", Outcome(Q.FAILED, "x", str(tmp_path / "missing.png"))),
    ])
    notifier.run_summary(0, 0, result)
    photos = [p["multipart"] for p in tg.sent("sendPhoto")]
    assert len(photos) == 2
    assert "Review page (not submitted)" in photos[0] and "stuck on Add experience" in photos[1]


def test_dry_run_mode_explains_why_nothing_was_submitted(setup, tg, tmp_path):
    from mcf_autoapply.mcf.applier import Outcome
    from mcf_autoapply.pipeline.apply import RunResult
    cfg, db, notifier, jobs, _ = setup
    cfg.apply.mode = "dry_run"
    tg.tap(1, f"a:{jobs['mid']}")
    notifier.handle_updates()
    assert "Dry run mode never submits" in notifier.status_text()
    notifier.run_summary(0, 0, RunResult(dry_run=True, outcomes=[("Job", Outcome(Q.DRY_RUN_OK))]))
    assert "Nothing was submitted (dry run)" in tg.sent()[-1]["text"]
    cfg.apply.mode = "manual"
    assert "Dry run mode" not in notifier.status_text()


def test_login_qr_replaces_the_previous_one_and_is_cleared_at_the_end(setup, tg):
    from mcf_autoapply.mcf.browser import LoginQR
    _, db, notifier, _, _ = setup
    assert notifier.send_login_qr(LoginQR(b"png1", "https://app.singpass.gov.sg/qrlogin?s=1"), 0)
    first_id = int(db.get_state(notifier.QR_MSG_KEY))
    assert "Valid until about" in tg.sent("sendPhoto")[0]["multipart"]
    assert tg.sent("deleteMessage") == []

    assert notifier.send_login_qr(LoginQR(b"png2", "https://app.singpass.gov.sg/qrlogin?s=2"), 1)
    assert [d["message_id"] for d in tg.sent("deleteMessage")] == [first_id]   # stale QR gone
    second_id = int(db.get_state(notifier.QR_MSG_KEY))
    assert second_id != first_id

    notifier.clear_login_qr()
    assert [d["message_id"] for d in tg.sent("deleteMessage")] == [first_id, second_id]
    assert db.get_state(notifier.QR_MSG_KEY) is None
    notifier.clear_login_qr()                       # nothing left: no extra calls
    assert len(tg.sent("deleteMessage")) == 2


def test_limits_command_shows_and_sets_caps(setup, tg):
    from mcf_autoapply.overrides import apply_overrides
    cfg, db, notifier, _, _ = setup
    tg.pending_updates += [msg(1, "/limits"), msg(2, "/limits 6 30"), msg(3, "/limits 99 1")]
    notifier.handle_updates()
    texts = [b["text"] for b in tg.sent()]
    assert "Current limits" in texts[0] and "/limits 6 30" in texts[0]
    assert "6/hour, 30/day" in texts[1]
    assert texts[2].startswith("Can't set that")
    cfg.apply.hourly_cap, cfg.apply.daily_cap, cfg.apply.ramp = 4, 15, [15, 25, 40]
    apply_overrides(cfg, db)
    assert (cfg.apply.hourly_cap, cfg.apply.daily_cap, cfg.apply.ramp) == (6, 30, [])
