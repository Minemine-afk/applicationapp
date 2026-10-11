"""Telegram notifications and one-tap decisions. Does nothing when Telegram is off."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from mcf_autoapply.config import Config, get_env
from mcf_autoapply.db import STATUS_NEW, Database
from mcf_autoapply.overrides import MODES, set_mode
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.pacer import Pacer
from mcf_autoapply.telegram import TelegramAPI, TelegramError

CHAT_ID_KEY = "tg:chat_id"
OFFSET_KEY = "tg:offset"
MAX_SCREENSHOTS_PER_RUN = 5

Handler = Callable[[str], "str | None"]   # gets the text after the command; returns a reply

# Shown in Telegram's "/" menu. /login and /run are added by `mcf-apply serve`.
COMMAND_HELP = {
    "status": "Mode, today's count and the queue",
    "jobs": "Best jobs not applied to yet, with Apply / Skip",
    "questions": "Screening questions waiting for your answer",
    "queue": "What's queued, applied or failed",
    "mode": "Switch dry run / manual / auto",
    "pause": "Stop submitting until /resume",
    "resume": "Start submitting again",
    "help": "All commands",
}
MODE_LABELS = {"dry_run": "🧪 Dry run", "manual": "✋ Manual", "auto": "🤖 Auto"}
MODE_HELP = {
    "dry_run": "fills forms up to the review page and sends screenshots, never submits",
    "manual": "submits only jobs you tap Apply on",
    "auto": "also applies to jobs scoring {score}+ by itself",
}


def question_key(question: str) -> str:
    return hashlib.sha1(question.encode()).hexdigest()[:12]


class Notifier:
    def __init__(
        self, cfg: Config, db: Database, api: TelegramAPI | None, chat_id: int | None,
        client_factory: Callable[[], MCFClient] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.api = api
        self.chat_id = chat_id
        self._client_factory = client_factory or (
            lambda: MCFClient(cfg.discovery.requests_per_second))
        self.errors: list[str] = []
        self._handlers: dict = {}
        self.activity: str | None = None   # set by serve while a background task runs

    @classmethod
    def from_config(cls, cfg: Config, db: Database, **kwargs) -> Notifier:
        token = get_env(cfg.telegram.token_env)
        if not (cfg.telegram.enabled and token):
            return cls(cfg, db, None, None)
        stored = db.get_state(CHAT_ID_KEY)
        chat_id = cfg.telegram.chat_id or (int(stored) if stored else None)
        return cls(cfg, db, TelegramAPI(token), chat_id, **kwargs)

    @property
    def enabled(self) -> bool:
        return self.api is not None and self.chat_id is not None

    def close(self) -> None:
        if self.api:
            self.api.close()

    # --- outgoing ------------------------------------------------------------------------

    def _send(self, text: str, buttons=None) -> dict | None:
        if not self.enabled:
            return None
        try:
            return self.api.send(self.chat_id, text, buttons)
        except TelegramError as e:   # notifications must never break a run
            self.errors.append(str(e))
            return None

    def alert(self, key: str, text: str) -> bool:
        """Send an alert, at most once per alert_every_hours for the same key."""
        if not self.enabled:
            return False
        state_key = f"tg:alert:{key}"
        now = datetime.now(timezone.utc)
        if last := self.db.get_state(state_key):
            if now - datetime.fromisoformat(last) < timedelta(hours=self.cfg.telegram.alert_every_hours):
                return False
        if self._send(f"⚠️ {escape(text)}") is None:
            return False
        self.db.set_state(state_key, now.isoformat())
        self.db.commit()
        return True

    def review_candidates(self, include_sent: bool = False, any_score: bool = False) -> list:
        t, a = self.cfg.telegram, self.cfg.apply
        upper = a.auto_apply_min_score if a.mode == "auto" and not any_score else 101
        out = []
        for job in self.db.jobs([STATUS_NEW], sort="score"):
            if job["score"] is None or job["score"] < t.quick_review_min_score:
                break
            if job["score"] >= upper or self.db.application(job["uuid"]):
                continue
            if not include_sent and self.db.get_state(f"tg:job:{job['uuid']}"):
                continue
            out.append(job)
        return out

    def send_review_cards(self, limit: int | None = None, include_sent: bool = False) -> int:
        """Borderline jobs as cards with Apply / Skip buttons (/jobs: the best, sent or not)."""
        if not self.enabled:
            return 0
        sent = 0
        jobs = self.review_candidates(include_sent, any_score=include_sent)
        for job in jobs[: limit or self.cfg.telegram.max_cards_per_run]:
            msg = self._send(job_card(job), [[("✅ Apply", f"a:{job['uuid']}"),
                                               ("⏭ Skip", f"s:{job['uuid']}")]])
            if msg is None:
                break
            self.db.set_state(f"tg:job:{job['uuid']}", str(msg["message_id"]))
            sent += 1
        self.db.commit()
        return sent

    def send_question_cards(self, include_sent: bool = False) -> int:
        """Unanswered screening questions with Yes / No buttons."""
        if not self.enabled:
            return 0
        sent = 0
        for question, jobs in Q.unanswered_questions(self.db).items():
            key = question_key(question)
            if not include_sent and self.db.get_state(f"tg:q:{key}"):
                continue
            waiting = "; ".join(escape(j) for j in jobs[:3]) + (
                f" +{len(jobs) - 3} more" if len(jobs) > 3 else "")
            text = (f"❓ <b>Screening question</b>\n{escape(question)}\n\n"
                    f"<i>Waiting: {waiting}</i>\n"
                    "Your answer is sent to employers and reused for every job that asks it.")
            msg = self._send(text, [[("Yes", f"q:{key}:yes"), ("No", f"q:{key}:no")]])
            if msg is None:
                break
            self.db.set_state(f"tg:q:{key}", json.dumps({"question": question,
                                                         "message_id": msg["message_id"]}))
            sent += 1
        self.db.commit()
        return sent

    def send_login_qr(self, qr, n: int = 0) -> bool:
        """Send a Singpass login QR, with a button that opens the Singpass app on this phone."""
        if not self.enabled:
            return False
        head = ("🔐 <b>Singpass login for MyCareersFuture</b>" if n == 0
                else "🔄 <b>New Singpass QR</b> (the previous one expired)")
        caption = (f"{head}\nOn this phone: tap <b>Open in Singpass app</b> and approve.\n"
                   "Or scan the QR with the Singpass app from another screen.\n"
                   "Each code lasts about a minute; a fresh one follows until you're in.\n"
                   "Only approve a login you started. Don't forward this.")
        links = [("Open in Singpass app", qr.link)] if qr.link and qr.link.startswith("https://") else None
        try:
            self.api.send_photo(self.chat_id, qr.png, caption, links)
            return True
        except TelegramError as e:
            self.errors.append(str(e))
            return False

    def run_summary(self, new_passed: int = 0, auto_approved: int = 0, result=None) -> bool:
        """One message per run, only when something happened."""
        outcomes = result.outcomes if result else []
        counts = Counter(o.state for _, o in outcomes)
        if not (outcomes or auto_approved):
            return False
        lines = [f"📋 <b>{'Dry run' if result and result.dry_run else 'Run'} summary</b>"]
        if new_passed:
            lines.append(f"{new_passed} new jobs passed your filters")
        if auto_approved:
            lines.append(f"{auto_approved} auto-approved")
        for title, o in outcomes:
            icon = {Q.APPLIED: "✅", Q.DRY_RUN_OK: "🧪", Q.FAILED: "❌"}.get(o.state, "•")
            detail = f" — {escape(o.detail)}" if o.state == Q.FAILED else ""
            lines.append(f"{icon} {escape(title)}{detail}")
        if counts:
            lines.append(", ".join(f"{n} {s}" for s, n in counts.items()))
        if result and result.dry_run and counts.get(Q.DRY_RUN_OK):
            lines.append("ℹ️ Nothing was submitted (dry run). Check the screenshots below; "
                         "if they look right, /mode → ✋ Manual and /run.")
        pacer = Pacer(self.cfg.apply, self.db)
        lines.append(f"Today {pacer.applied_today()}/{pacer.daily_cap()}")
        if self._send("\n".join(lines)) is None:
            return False
        self._send_screenshots(outcomes)
        return True

    def _send_screenshots(self, outcomes) -> None:
        """Dry-run review pages and failures, so you can check them from your phone."""
        shots = [(t, o) for t, o in outcomes
                 if o.state in (Q.DRY_RUN_OK, Q.FAILED) and o.screenshot
                 and Path(o.screenshot).is_file()]
        for title, o in shots[:MAX_SCREENSHOTS_PER_RUN]:
            icon = "🧪 Review page (not submitted)" if o.state == Q.DRY_RUN_OK else "❌ Failed"
            caption = f"{icon}\n<b>{escape(title)}</b>"
            if o.state == Q.FAILED:
                caption += f"\n{escape(o.detail)}"
            try:
                self.api.send_photo(self.chat_id, Path(o.screenshot).read_bytes(), caption[:1000])
            except (TelegramError, OSError) as e:
                self.errors.append(str(e))
                break

    def report_stop(self, reason: str | None) -> None:
        if not reason:
            return
        if "session expired" in reason:
            self.alert("logged_out", "MCF login expired. Send /login and approve it in the "
                                     "Singpass app.")
        elif reason.startswith("paused"):
            self.alert("paused", f"Submissions {reason}")

    # --- incoming ------------------------------------------------------------------------

    def handle_updates(
        self, timeout: int = 0, commands: dict[str, Handler] | None = None,
    ) -> list[str]:
        """Process button taps and /commands. Returns what was done, for logging.

        `commands` adds handlers (/login, /run in serve mode). Before a chat is linked,
        the first /start links it; after that, other chats are ignored.
        """
        if self.api is None:
            return []
        offset = self.db.get_state(OFFSET_KEY)
        try:
            updates = self.api.updates(int(offset) if offset else None, timeout)
        except TelegramError as e:
            self.errors.append(str(e))
            return []
        handlers: dict[str, Handler] = {**self.builtin_commands(), **(commands or {})}
        self._handlers = handlers
        done = []
        for update in updates:
            self.db.set_state(OFFSET_KEY, str(update["update_id"] + 1))
            try:
                if cb := update.get("callback_query"):
                    if self.chat_id is not None:
                        done.append(self._on_callback(cb))
                elif msg := update.get("message"):
                    if result := self._on_message(msg, handlers):
                        done.append(result)
            except TelegramError as e:
                self.errors.append(str(e))
            self.db.commit()
        return done

    def _on_message(self, msg: dict, handlers: dict[str, Handler]) -> str | None:
        text = (msg.get("text") or "").strip()
        command, _, args = text.partition(" ")
        command = command.split("@")[0].lower()
        chat = msg.get("chat", {}).get("id")

        if self.chat_id is None:
            if command != "/start":
                return None
            self.chat_id = chat
            self.db.set_state(CHAT_ID_KEY, str(chat))
            self.db.commit()
            self._send("✅ Linked. This chat now controls mcf-apply.\n\n" + self.help_text(handlers))
            return f"linked chat {chat}"
        if chat != self.chat_id:
            return None
        if command in handlers:
            # Save the offset first: a long command (/login) must not run twice.
            self.db.commit()
            reply = handlers[command](args.strip())
            if reply:
                self._send(reply)
            return f"handled {command}"
        if text:
            self._send(self.help_text(handlers))
        return None

    def builtin_commands(self) -> dict[str, Handler]:
        return {
            "/start": lambda a: self.help_text(),
            "/help": lambda a: self.help_text(),
            "/status": lambda a: self.status_text(),
            "/queue": lambda a: self.queue_text(),
            "/jobs": self._cmd_jobs,
            "/questions": self._cmd_questions,
            "/mode": self._cmd_mode,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
        }

    def help_text(self, handlers: dict | None = None) -> str:
        handlers = handlers or self._handlers
        extra = {"login": "Log in to MCF with Singpass", "run": "Search and apply now"}
        lines = ["<b>Commands</b>"]
        lines += [f"/{n} — {d}" for n, d in extra.items() if f"/{n}" in handlers]
        lines += [f"/{n} — {d}" for n, d in COMMAND_HELP.items()]
        if "/login" not in handlers:
            lines.append("\n<i>/login and /run work while `mcf-apply serve` is running.</i>")
        return "\n".join(lines)

    def register_commands(self, serve: bool = True) -> None:
        """Fill Telegram's "/" menu with the commands."""
        if self.api is None:
            return
        items = ({"login": "Log in to MCF with Singpass", "run": "Search and apply now"}
                 if serve else {}) | COMMAND_HELP
        try:
            self.api.call("setMyCommands", commands=[
                {"command": c, "description": d} for c, d in items.items()])
        except TelegramError as e:
            self.errors.append(str(e))

    def _cmd_jobs(self, args: str) -> str | None:
        n = int(args) if args.isdigit() else 5
        if not self.send_review_cards(limit=min(n, 20), include_sent=True):
            return ("No unapplied jobs above your score floor "
                    f"({self.cfg.telegram.quick_review_min_score}). Try /run to search again.")
        return None

    def _cmd_questions(self, args: str) -> str | None:
        if not self.send_question_cards(include_sent=True):
            return "No screening questions waiting for you."
        return None

    def _cmd_mode(self, args: str) -> str | None:
        mode = args.lower().replace("-", "_").replace(" ", "_")
        if mode in ("dryrun", "dry"):
            mode = "dry_run"
        if mode in MODES:
            set_mode(self.cfg, self.db, mode)
            return self.mode_text(changed=True)
        self._send(self.mode_text(), [[(MODE_LABELS[m], f"m:{m}") for m in MODES]])
        return None

    def mode_text(self, changed: bool = False) -> str:
        mode = self.cfg.apply.mode
        desc = MODE_HELP[mode].format(score=self.cfg.apply.auto_apply_min_score)
        head = "Mode set to" if changed else "Current mode:"
        return f"{head} <b>{MODE_LABELS[mode]}</b>: {desc}."

    def _cmd_pause(self, args: str) -> str:
        Pacer(self.cfg.apply, self.db).pause("paused from Telegram")
        return "⏸ Submissions paused. Searching and job cards continue. /resume to restart."

    def _cmd_resume(self, args: str) -> str:
        pacer = Pacer(self.cfg.apply, self.db)
        if not pacer.paused():
            return "Not paused."
        pacer.unpause()
        return f"▶️ Resumed. Daily cap is {pacer.daily_cap()}."

    def queue_text(self) -> str:
        groups = [(Q.APPROVED, "📥 Queued"), (Q.NEEDS_INPUT, "❓ Waiting on your answers"),
                  (Q.DRY_RUN_OK, "🧪 Dry run passed"), (Q.FAILED, "❌ Failed"),
                  (Q.APPLIED, "✅ Applied (latest)")]
        lines = []
        for state, label in groups:
            rows = self.db.applications([state], limit=5)
            if not rows:
                continue
            lines.append(f"<b>{label}</b>")
            for r in rows:
                note = f" — {escape(r['error'])}" if state == Q.FAILED and r["error"] else ""
                lines.append(f"• {escape(r['title'])} ({escape(r['company_name'])}){note}")
        return "\n".join(lines) or "Queue is empty. Tap Apply on a /jobs card to queue one."

    def _on_callback(self, cb: dict) -> str:
        message = cb.get("message") or {}
        if message.get("chat", {}).get("id") != self.chat_id:
            self.api.answer_callback(cb["id"], "Not allowed")
            return "ignored a tap from another chat"
        kind, _, rest = cb.get("data", "").partition(":")
        original = escape(message.get("text", ""))

        if kind in ("a", "s"):
            if kind == "a":
                with self._client_factory() as client:
                    [d] = Q.approve(self.db, client, AnswerBank(self.cfg.apply.answers_path), [rest])
            else:
                [d] = Q.skip(self.db, [rest])
            label = {Q.APPROVED: "queued to apply", Q.NEEDS_INPUT: "queued, needs your answers",
                     Q.SKIPPED: "skipped", Q.EXTERNAL: "apply on Careers@Gov yourself"
                     }.get(d.state, d.state)
            self.api.edit(self.chat_id, message["message_id"], f"{original}\n\n<b>→ {label}</b>")
            self.api.answer_callback(cb["id"], label)
            if d.state == Q.NEEDS_INPUT:
                self.send_question_cards()
            return f"{d.state}: {d.title}"

        if kind == "m" and rest in MODES:
            set_mode(self.cfg, self.db, rest)
            self.api.edit(self.chat_id, message["message_id"], self.mode_text(changed=True))
            self.api.answer_callback(cb["id"], MODE_LABELS[rest])
            return f"mode set to {rest}"

        if kind == "q":
            key, _, answer = rest.partition(":")
            stored = self.db.get_state(f"tg:q:{key}")
            if not stored or answer not in ("yes", "no"):
                self.api.answer_callback(cb["id"], "Unknown question")
                return "ignored an unknown question"
            question = json.loads(stored)["question"]
            bank = AnswerBank(self.cfg.apply.answers_path)
            bank.add(question, answer)
            released = Q.refresh_answers(self.db, bank)
            note = f" · {len(released)} job(s) queued" if released else ""
            self.api.edit(self.chat_id, message["message_id"],
                          f"{original}\n\n<b>→ answered {answer}{note}</b>")
            self.api.answer_callback(cb["id"], f"Saved: {answer}")
            return f"answered {answer}: {question}"

        self.api.answer_callback(cb["id"])
        return "ignored an unknown button"

    def status_text(self) -> str:
        pacer = Pacer(self.cfg.apply, self.db)
        counts = self.db.application_counts()
        lines = [f"<b>Mode</b> {MODE_LABELS[self.cfg.apply.mode]}",
                 f"Today {pacer.applied_today()}/{pacer.daily_cap()} · "
                 f"last hour {pacer.applied_last_hour()}/{self.cfg.apply.hourly_cap}"]
        lines += [f"{state}: {n}" for state, n in sorted(counts.items())] or ["Queue is empty"]
        if reason := pacer.blocked_reason():
            lines.append(f"⏸ {escape(reason)}")
        if self.activity:
            lines.append(f"⏳ Busy: {self.activity}")
        if self.cfg.apply.mode == "dry_run" and counts.get(Q.APPROVED, 0) + counts.get(Q.DRY_RUN_OK, 0):
            lines.append("\nℹ️ Dry run mode never submits. When the screenshots look right, "
                         "send /mode and pick ✋ Manual to start applying.")
        return "\n".join(lines)


def job_card(job) -> str:
    details = json.loads(job["rank_details"] or "{}")
    salary = ""
    if job["salary_max"]:
        salary = f" · ${job['salary_min'] or 0:,}–{job['salary_max']:,}"
    exp = "" if job["min_years_exp"] is None else f" · {job['min_years_exp']}y exp"
    skills = ", ".join(details.get("matched_skills", [])[:6]) or "none matched"
    fresh = " · 🎓 fresh grad" if details.get("fresh_grad") else ""
    return (f"<b>{escape(job['title'])}</b>\n{escape(job['company_name'])}{salary}{exp}\n"
            f"Score {job['score']}{fresh}\nSkills: {escape(skills)}\n"
            f'<a href="{escape(job["url"])}">View on MyCareersFuture</a>')
