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
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if self.fail:
            return httpx.Response(403, json={"ok": False, "description": self.fail})
        if method == "sendMessage":
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
