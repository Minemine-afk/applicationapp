"""Multiple-choice and free-text screening questions, end to end through Telegram."""

import json

import pytest
import respx

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.models import Job
from mcf_autoapply.notify import Notifier
from mcf_autoapply.overrides import apply_overrides
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import apply_filters
from mcf_autoapply.telegram import TelegramAPI
from test_notify import CHAT, TOKEN, FakeTelegram

CHOICE = "Which of the following best describes your experience with Python?"
OPTIONS = ["None", "Less than 1 year", "1-2 years", "3+ years"]
TEXT = "What is your expected monthly salary?"


def test_answer_bank_keeps_any_text(tmp_path):
    bank = AnswerBank(tmp_path / "a.yml")
    bank.add("Are you a citizen?", "YES")
    bank.add(CHOICE, "1-2 years")
    bank.add(TEXT, "4500")
    again = AnswerBank(tmp_path / "a.yml")
    assert again.lookup("Are you a citizen?") == "yes"       # yes/no normalised
    assert again.lookup(CHOICE) == "1-2 years"
    assert again.lookup(TEXT) == "4500"
    with pytest.raises(ValueError, match="empty"):
        bank.add("Blank?", "   ")


@pytest.fixture
def tg():
    fake = FakeTelegram()
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=f"https://api.telegram.org/bot{TOKEN}/").mock(side_effect=fake)
        yield fake


@pytest.fixture
def setup(tmp_path, tg):
    cfg = Config.model_validate({
        "db_path": ":memory:", "searches": [{"query": "x"}],
        "apply": {"mode": "manual", "answers_path": str(tmp_path / "answers.yml")},
        "jobstreet": {"enabled": True},
    })
    cfg.filters.title_include_keywords = []
    db = Database(":memory:")
    db.upsert_job(Job.from_jobstreet({"id": "7", "title": "Junior Dev", "companyName": "Acme",
                                      "listingDate": "2026-10-10T00:00:00Z"}), "x")
    apply_filters(db, cfg)
    db.create_application("js:7", Q.NEEDS_INPUT, "user", [CHOICE, TEXT], [None, None])
    Q.remember_question_options(db, [{"question": CHOICE, "options": OPTIONS},
                                     {"question": TEXT, "options": []}])
    db.commit()
    notifier = Notifier(cfg, db, TelegramAPI(TOKEN), CHAT)
    yield cfg, db, notifier
    db.close()


def card_for(tg, question):
    return next(c for c in tg.sent() if question in c["text"])


def test_choice_question_gets_option_buttons_and_a_tap_answers_it(setup, tg):
    cfg, db, notifier = setup
    assert notifier.send_question_cards() == 2
    card = card_for(tg, CHOICE)
    buttons = [b for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons] == OPTIONS
    key = buttons[2]["callback_data"]
    tg.tap(1, key, message_id=card_msg_id(db, CHOICE), text=card["text"])
    assert notifier.handle_updates() == [f"answered 1-2 years: {CHOICE}"]
    assert AnswerBank(cfg.apply.answers_path).lookup(CHOICE) == "1-2 years"
    assert json.loads(db.application("js:7")["answers"]) == ["1-2 years", None]


def card_msg_id(db, question):
    from mcf_autoapply.pipeline.answers import question_key
    return json.loads(db.get_state(f"tg:q:{question_key(question)}"))["message_id"]


def test_free_text_question_is_answered_by_replying(setup, tg):
    cfg, db, notifier = setup
    notifier.send_question_cards()
    card = card_for(tg, TEXT)
    assert "reply to this message" in card["text"]
    msg_id = card_msg_id(db, TEXT)
    tg.pending_updates.append({"update_id": 5, "message": {
        "chat": {"id": CHAT}, "text": "4500",
        "reply_to_message": {"message_id": msg_id, "text": card["text"]}}})
    assert notifier.handle_updates() == [f"answered 4500: {TEXT}"]
    assert AnswerBank(cfg.apply.answers_path).lookup(TEXT) == "4500"
    assert "Saved your answer" in tg.sent()[-1]["text"]


def test_typed_answer_to_a_choice_question_is_refused(setup, tg):
    cfg, db, notifier = setup
    notifier.send_question_cards()
    tg.pending_updates.append({"update_id": 5, "message": {
        "chat": {"id": CHAT}, "text": "a few years",
        "reply_to_message": {"message_id": card_msg_id(db, CHOICE), "text": "card"}}})
    assert notifier.handle_updates() == ["rejected a typed answer to a choice question"]
    assert AnswerBank(cfg.apply.answers_path).lookup(CHOICE) is None


def test_both_answered_releases_the_job(setup, tg):
    cfg, db, notifier = setup
    bank = AnswerBank(cfg.apply.answers_path)
    bank.add(CHOICE, "3+ years")
    bank.add(TEXT, "4500")
    [d] = Q.refresh_answers(db, bank)
    assert d.state == Q.APPROVED
    assert json.loads(db.application("js:7")["answers"]) == ["3+ years", "4500"]


def test_limits_jobstreet_command(setup, tg):
    cfg, db, notifier = setup
    tg.pending_updates += [{"update_id": i, "message": {"chat": {"id": CHAT}, "text": t}}
                           for i, t in enumerate(["/limits", "/limits jobstreet 3 10",
                                                  "/limits jobstreet 9 99"], 1)]
    notifier.handle_updates()
    texts = [b["text"] for b in tg.sent()]
    assert "JobStreet:" in texts[0] and "/limits jobstreet" in texts[0]
    assert "JobStreet limits set: <b>3/hour, 10/day</b>" in texts[1]
    assert texts[2].startswith("Can't set that")
    cfg.jobstreet.hourly_cap = cfg.jobstreet.daily_cap = 1
    apply_overrides(cfg, db)
    assert (cfg.jobstreet.hourly_cap, cfg.jobstreet.daily_cap) == (3, 10)
