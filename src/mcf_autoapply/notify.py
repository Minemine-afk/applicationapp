"""Telegram notifications and one-tap decisions. Does nothing when Telegram is off."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from mcf_autoapply.config import Config, get_env
from mcf_autoapply.db import STATUS_NEW, Database, applied_key
from mcf_autoapply.overrides import (MAX_DAILY, MAX_HOURLY, MAX_JS_DAILY, MAX_JS_HOURLY, MODES,
                                     set_jobstreet_enabled, set_limits, set_mode,
                                     set_site_limits)
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank, question_key
from mcf_autoapply.pipeline.pacer import Pacer
from mcf_autoapply.sources import LABELS, SITE_NAMES
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
    "queue": "What's still to be submitted, and what was applied to today",
    "mode": "Switch dry run / manual / auto",
    "limits": "Show or set how many to apply per hour and per day",
    "jobstreet": "Turn JobStreet on or off (/jobstreet on)",
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
        # Set by serve: returns a live one-line summary of the running task, or None.
        self.progress: Callable[[], str | None] | None = None

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
        applied_uuids, applied_keys = self.db.applied_on_site_uuids(), self.db.applied_on_site_keys()
        for job in self.db.jobs([STATUS_NEW], sort="score"):
            if job["score"] is None or job["score"] < t.quick_review_min_score:
                break
            if job["score"] >= upper or self.db.application(job["uuid"]):
                continue
            if (job["uuid"] in applied_uuids
                    or applied_key(job["company_name"], job["title"]) in applied_keys):
                continue   # MCF says you've applied (maybe by hand, or to a repost of it)
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
            options = Q.question_options(self.db, question)
            text = (f"❓ <b>Screening question</b>\n{escape(question)}\n\n"
                    f"<i>Waiting: {waiting}</i>\n"
                    "Your answer is sent to employers and reused for every job that asks it.")
            if options:
                # One button per option, two per row; the index travels in the callback.
                buttons = [[(opt[:40], f"q:{key}:{i}") for i, opt in row]
                           for row in _rows(list(enumerate(options)), 2)]
            else:
                buttons = [[("Yes", f"q:{key}:yes"), ("No", f"q:{key}:no")]]
                text += "\nIf it needs words or a number instead, <b>reply to this message</b> with your answer."
            msg = self._send(text, buttons)
            if msg is None:
                break
            self.db.set_state(f"tg:q:{key}", json.dumps({"question": question,
                                                         "message_id": msg["message_id"]}))
            self.db.set_state(f"tg:qmsg:{msg['message_id']}", key)
            sent += 1
        self.db.commit()
        return sent

    QR_MSG_KEY = "tg:login_qr_msg"
    QR_LIFETIME_S = 60

    def send_login_qr(self, qr, n: int = 0) -> bool:
        """Send a Singpass login QR, with a button that opens the Singpass app on this phone.

        Only the newest QR works (each lasts about a minute), so the previous QR message is
        deleted first: a stale link just opens the Singpass app with nothing to approve.
        """
        if not self.enabled:
            return False
        self.clear_login_qr()
        tz = ZoneInfo(self.cfg.apply.timezone)
        until = (datetime.now(tz) + timedelta(seconds=self.QR_LIFETIME_S)).strftime("%H:%M:%S")
        head = ("🔐 <b>Singpass login for MyCareersFuture</b>" if n == 0
                else "🔄 <b>New Singpass QR</b> (the previous one expired)")
        caption = (f"{head}\n"
                   f"⏱ Valid until about <b>{until}</b>. Tap <b>Open in Singpass app</b> now and "
                   "approve. If the app opens with nothing to approve, this code has expired: "
                   "wait for the next one, which arrives by itself.\n"
                   "Or scan the QR with the Singpass app from another screen.\n"
                   "Only approve a login you started. Don't forward this.")
        links = [("Open in Singpass app", qr.link)] if qr.link and qr.link.startswith("https://") else None
        try:
            msg = self.api.send_photo(self.chat_id, qr.png, caption, links)
        except TelegramError as e:
            self.errors.append(str(e))
            return False
        self.db.set_state(self.QR_MSG_KEY, str(msg.get("message_id", "")))
        self.db.commit()
        return True

    def clear_login_qr(self) -> None:
        """Delete the last QR message, if any: it's expired or the login is over."""
        msg_id = self.db.get_state(self.QR_MSG_KEY)
        if not msg_id or not self.enabled:
            return
        try:
            self.api.call("deleteMessage", chat_id=self.chat_id, message_id=int(msg_id))
        except (TelegramError, ValueError):
            pass   # already gone, or too old to delete
        self.db.set_state(self.QR_MSG_KEY, None)
        self.db.commit()

    def run_summary(self, new_passed: int = 0, auto_approved: int = 0, result=None) -> bool:
        """One message per run, only when something happened."""
        outcomes = result.outcomes if result else []
        skipped = getattr(result, "skipped_applied", []) if result else []
        counts = Counter(o.state for _, o in outcomes)
        if not (outcomes or auto_approved or skipped):
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
        if skipped:
            lines.append(f"⏭ Skipped {len(skipped)} you'd already applied to on MCF: "
                         + "; ".join(escape(t) for t in skipped[:5])
                         + (" …" if len(skipped) > 5 else ""))
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
        if msg.get("reply_to_message") and not command.startswith("/"):
            return self._on_reply(msg)
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

    def _save_answer(self, question: str, answer: str, message_id: int, original: str,
                     ack: Callable[[str], None] | None = None) -> str:
        bank = AnswerBank(self.cfg.apply.answers_path)
        bank.add(question, answer)
        released = Q.refresh_answers(self.db, bank)
        note = f" · {len(released)} job(s) queued" if released else ""
        try:
            self.api.edit(self.chat_id, message_id,
                          f"{original}\n\n<b>→ answered {escape(answer)}{note}</b>")
        except TelegramError:
            pass
        if ack:
            ack(f"Saved: {answer}")
        return f"answered {answer}: {question}"

    def _on_reply(self, msg: dict) -> str | None:
        """A reply to a question card = a typed answer (free-text questions)."""
        parent = msg.get("reply_to_message") or {}
        key = self.db.get_state(f"tg:qmsg:{parent.get('message_id')}") if parent else None
        text = (msg.get("text") or "").strip()
        if not key or not text:
            return None
        stored = json.loads(self.db.get_state(f"tg:q:{key}") or "{}")
        question = stored.get("question")
        if not question:
            return None
        options = Q.question_options(self.db, question)
        if options and not any(text.lower() == o.lower() for o in options):
            self._send("That question has fixed options; tap one of the buttons on the card.")
            return "rejected a typed answer to a choice question"
        self._save_answer(question, text, parent["message_id"], escape(parent.get("text") or ""))
        self._send(f"Saved your answer: <b>{escape(text)}</b>")
        return f"answered {text}: {question}"

    def builtin_commands(self) -> dict[str, Handler]:
        return {
            "/start": lambda a: self.help_text(),
            "/help": lambda a: self.help_text(),
            "/status": lambda a: self.status_text(),
            "/queue": lambda a: self.queue_text(),
            "/jobs": self._cmd_jobs,
            "/questions": self._cmd_questions,
            "/mode": self._cmd_mode,
            "/limits": self._cmd_limits,
            "/jobstreet": self._cmd_jobstreet,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
        }

    def help_text(self, handlers: dict | None = None) -> str:
        handlers = handlers or self._handlers
        extra = {"login": "Log in to MCF with Singpass (/login jobstreet for JobStreet)",
                 "run": "Search and apply now",
                 "apply": "Apply to the queue now, without searching",
                 "update": "Install the latest version and restart",
                 "restart": "Restart mcf-apply"}
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
        items = ({"login": "Log in to MCF with Singpass", "run": "Search and apply now",
                  "apply": "Apply to the queue now, without searching",
                  "update": "Install the latest version and restart",
                  "restart": "Restart mcf-apply"}
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

    def _cmd_limits(self, args: str) -> str:
        a = self.cfg.apply
        parts = args.split()
        if len(parts) == 3 and parts[0].lower() in ("jobstreet", "js") and all(p.isdigit() for p in parts[1:]):
            try:
                set_site_limits(self.cfg, self.db, "jobstreet", int(parts[1]), int(parts[2]))
            except ValueError as e:
                return f"Can't set that: {e}."
            return (f"JobStreet limits set: <b>{self.cfg.jobstreet.hourly_cap}/hour, "
                    f"{self.cfg.jobstreet.daily_cap}/day</b> (within the overall limits).")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            try:
                set_limits(self.cfg, self.db, int(parts[0]), int(parts[1]))
            except ValueError as e:
                return f"Can't set that: {e}."
            return (f"Limits set: <b>{a.hourly_cap}/hour, {a.daily_cap}/day</b>, with a "
                    f"{int(a.delay_seconds[0])}–{int(a.delay_seconds[1])} s gap between submissions.\n"
                    "MCF asks job seekers to avoid mass applications, so keep this sensible.")
        ramp = f" (rising weekly to {max(a.ramp)})" if a.ramp else ""
        js = self.cfg.jobstreet
        js_line = (f"\nJobStreet: <b>{js.hourly_cap}/hour, {js.daily_cap}/day</b>; change with "
                   f"<code>/limits jobstreet &lt;h&gt; &lt;d&gt;</code> (max {MAX_JS_HOURLY}/{MAX_JS_DAILY})."
                   if js.enabled else "")
        return (f"Current limits: <b>{a.hourly_cap}/hour, {Pacer(a, self.db).daily_cap()}/day</b>"
                f"{ramp}, active {a.active_hours[0]:%H:%M}–{a.active_hours[1]:%H:%M}.\n"
                f"Change with <code>/limits &lt;per hour&gt; &lt;per day&gt;</code>, e.g. "
                f"<code>/limits 6 30</code> (max {MAX_HOURLY}/hour, {MAX_DAILY}/day).{js_line}")

    JOBSTREET_WARNING = (
        "⚠️ JobStreet's robots.txt disallows the search this uses and its terms forbid automated "
        "applications, so an account doing this can be suspended. Limits are kept low "
        "({h}/hour, {d}/day) and JobStreet jobs are never auto-approved unless you allow it.")

    def _cmd_jobstreet(self, args: str) -> str:
        js = self.cfg.jobstreet
        want = args.strip().lower()
        if want in ("on", "enable", "yes"):
            set_jobstreet_enabled(self.cfg, self.db, True)
            return ("🟠 <b>JobStreet is on.</b> Searches now include it from the next run.\n"
                    + self.JOBSTREET_WARNING.format(h=js.hourly_cap, d=js.daily_cap)
                    + "\n\nNext: send /login jobstreet and sign in in the window that opens on the PC. "
                      "Stay in dry run for the first JobStreet jobs and check the screenshots.")
        if want in ("off", "disable", "no"):
            set_jobstreet_enabled(self.cfg, self.db, False)
            return "⚪ JobStreet is off. Queued JobStreet jobs stay queued but won't be applied to."
        state = "on" if js.enabled else "off"
        return (f"JobStreet is <b>{state}</b>. /jobstreet on or /jobstreet off.\n"
                + self.JOBSTREET_WARNING.format(h=js.hourly_cap, d=js.daily_cap))

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

    def queue_text(self, max_lines: int = 15) -> str:
        """What's still to be submitted, and what was submitted today."""
        tz = ZoneInfo(self.cfg.apply.timezone)
        midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        today = [r for r in self.db.applications([Q.APPLIED])
                 if r["submitted_at"] and datetime.fromisoformat(r["submitted_at"]) >= midnight]
        queued = self.db.applications([Q.APPROVED])
        lines = []

        def listing(label: str, rows: list) -> None:
            lines.append(f"<b>{label} ({len(rows)})</b>")
            for r in rows[:max_lines]:
                lines.append(f"• {escape(r['title'])} ({escape(r['company_name'])}) · "
                             f"{LABELS.get(r['source'] if 'source' in r.keys() else 'mcf', '')}")
            if len(rows) > max_lines:
                lines.append(f"… and {len(rows) - max_lines} more")

        if queued:
            listing("📥 To be submitted", queued)
        if n := len(self.db.applications([Q.NEEDS_INPUT])):
            lines.append(f"❓ {n} waiting on your answers (/questions)")
        if today:
            if lines:
                lines.append("")
            listing("✅ Applied today", today)
        return "\n".join(lines) or "Nothing queued and nothing applied today. Tap Apply on a /jobs card to queue one."

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
            if not stored:
                self.api.answer_callback(cb["id"], "Unknown question")
                return "ignored an unknown question"
            question = json.loads(stored)["question"]
            if answer.isdigit():
                options = Q.question_options(self.db, question)
                if int(answer) >= len(options):
                    self.api.answer_callback(cb["id"], "Unknown option")
                    return "ignored an unknown option"
                answer = options[int(answer)]
            elif answer not in ("yes", "no"):
                self.api.answer_callback(cb["id"], "Unknown answer")
                return "ignored an unknown answer"
            return self._save_answer(question, answer, message["message_id"], original,
                                     lambda text: self.api.answer_callback(cb["id"], text))

        self.api.answer_callback(cb["id"])
        return "ignored an unknown button"

    def status_text(self) -> str:
        pacer = Pacer(self.cfg.apply, self.db)
        counts = self.db.application_counts()
        lines = [f"<b>Mode</b> {MODE_LABELS[self.cfg.apply.mode]}"
                 + (" · JobStreet on" if self.cfg.jobstreet.enabled else ""),
                 f"Today {pacer.applied_today()}/{pacer.daily_cap()} · "
                 f"this hour {pacer.applied_this_hour()}/{self.cfg.apply.hourly_cap}"]
        lines += [f"{state}: {n}" for state, n in sorted(counts.items())] or ["Queue is empty"]
        if reason := pacer.blocked_reason():
            lines.append(f"⏸ {escape(reason)}")
        lines += self._last_run_lines()
        busy = (self.progress() if self.progress else None) or self.activity
        if busy:
            lines.append(f"⏳ Busy: {escape(busy)}")
        elif self.progress:
            lines.append("💤 Idle: waiting for the next run")
        if self.cfg.apply.mode == "dry_run" and counts.get(Q.APPROVED, 0) + counts.get(Q.DRY_RUN_OK, 0):
            lines.append("\nℹ️ Dry run mode never submits. When the screenshots look right, "
                         "send /mode and pick ✋ Manual to start applying.")
        return "\n".join(lines)

    def _last_run_lines(self) -> list[str]:
        """What the last run did and, if it applied nothing, why (recorded by the runner)."""
        raw = self.db.get_state("last_run")
        if not raw:
            return ["No run yet."]
        try:
            last = json.loads(raw)
            at = datetime.fromisoformat(last["at"]).astimezone(ZoneInfo(self.cfg.apply.timezone))
        except Exception:
            return []
        ago = int((datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])).total_seconds() // 60)
        did = ", ".join(f"{last.get(k, 0)} {k.replace('_', ' ')}"
                        for k in ("applied", "dry_run", "failed") if last.get(k))
        lines = [f"Last {last.get('kind', 'run')} {at:%H:%M} ({ago} min ago): {did or 'nothing applied'}"]
        if last.get("idle_reason"):
            lines.append(f"↳ {escape(last['idle_reason'])}")
        return lines


def job_card(job) -> str:
    details = json.loads(job["rank_details"] or "{}")
    salary = ""
    if job["salary_max"]:
        salary = f" · ${job['salary_min'] or 0:,}–{job['salary_max']:,}"
    exp = "" if job["min_years_exp"] is None else f" · {job['min_years_exp']}y exp"
    skills = ", ".join(details.get("matched_skills", [])[:6]) or "none matched"
    fresh = " · 🎓 fresh grad" if details.get("fresh_grad") else ""
    source = job["source"] if "source" in job.keys() else "mcf"
    return (f"<b>{escape(job['title'])}</b>\n{escape(job['company_name'])}{salary}{exp}\n"
            f"Score {job['score']}{fresh} · {LABELS.get(source, source)}\nSkills: {escape(skills)}\n"
            f'<a href="{escape(job["url"])}">View on {SITE_NAMES.get(source, source)}</a>')


def _rows(items: list, per_row: int) -> list[list]:
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]
