"""Telegram notifications and one-tap decisions. Does nothing when Telegram is off."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape

from mcf_autoapply.config import Config, get_env
from mcf_autoapply.db import STATUS_NEW, Database
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.pacer import Pacer
from mcf_autoapply.telegram import TelegramAPI, TelegramError

CHAT_ID_KEY = "tg:chat_id"
OFFSET_KEY = "tg:offset"


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

    def review_candidates(self) -> list:
        t, a = self.cfg.telegram, self.cfg.apply
        upper = a.auto_apply_min_score if a.mode == "auto" else 101
        out = []
        for job in self.db.jobs([STATUS_NEW], sort="score"):
            if job["score"] is None or job["score"] < t.quick_review_min_score:
                break
            if job["score"] >= upper or self.db.application(job["uuid"]):
                continue
            if self.db.get_state(f"tg:job:{job['uuid']}"):
                continue
            out.append(job)
        return out

    def send_review_cards(self) -> int:
        """Borderline jobs as cards with Apply / Skip buttons."""
        if not self.enabled:
            return 0
        sent = 0
        for job in self.review_candidates()[: self.cfg.telegram.max_cards_per_run]:
            msg = self._send(job_card(job), [[("✅ Apply", f"a:{job['uuid']}"),
                                               ("⏭ Skip", f"s:{job['uuid']}")]])
            if msg is None:
                break
            self.db.set_state(f"tg:job:{job['uuid']}", str(msg["message_id"]))
            sent += 1
        self.db.commit()
        return sent

    def send_question_cards(self) -> int:
        """Unanswered screening questions with Yes / No buttons."""
        if not self.enabled:
            return 0
        sent = 0
        for question, jobs in Q.unanswered_questions(self.db).items():
            key = question_key(question)
            if self.db.get_state(f"tg:q:{key}"):
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
        pacer = Pacer(self.cfg.apply, self.db)
        lines.append(f"Today {pacer.applied_today()}/{pacer.daily_cap()}")
        return self._send("\n".join(lines)) is not None

    def report_stop(self, reason: str | None) -> None:
        if not reason:
            return
        if "session expired" in reason:
            url = self.cfg.serve.login_view_url
            how = (f"Send /login here, then open {url} and scan the Singpass QR code." if url
                   else "Run `mcf-apply login` on the machine running mcf-apply.")
            self.alert("logged_out", f"MCF login expired. {how}")
        elif reason.startswith("paused"):
            self.alert("paused", f"Submissions {reason}")

    # --- incoming ------------------------------------------------------------------------

    def handle_updates(
        self, timeout: int = 0, commands: dict[str, Callable[[], str]] | None = None,
    ) -> list[str]:
        """Process button taps and /commands. Returns what was done, for logging.

        `commands` adds handlers (e.g. /login, /run in serve mode); each returns a reply.
        """
        if not self.enabled:
            return []
        offset = self.db.get_state(OFFSET_KEY)
        try:
            updates = self.api.updates(int(offset) if offset else None, timeout)
        except TelegramError as e:
            self.errors.append(str(e))
            return []
        done = []
        for update in updates:
            self.db.set_state(OFFSET_KEY, str(update["update_id"] + 1))
            try:
                if cb := update.get("callback_query"):
                    done.append(self._on_callback(cb))
                elif (msg := update.get("message")) and msg["chat"]["id"] == self.chat_id:
                    command = msg.get("text", "").split()[0].split("@")[0] if msg.get("text") else ""
                    handlers = {"/status": self.status_text, **(commands or {})}
                    if command in handlers:
                        # Save the offset first: a long command (/login) must not run twice.
                        self.db.commit()
                        self._send(handlers[command]())
                        done.append(f"handled {command}")
                    elif command.startswith("/"):
                        self._send("Commands: " + ", ".join(sorted(handlers)))
            except TelegramError as e:
                self.errors.append(str(e))
            self.db.commit()
        return done

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
        lines = [f"<b>Mode</b> {self.cfg.apply.mode}",
                 f"Today {pacer.applied_today()}/{pacer.daily_cap()} · "
                 f"last hour {pacer.applied_last_hour()}/{self.cfg.apply.hourly_cap}"]
        lines += [f"{state}: {n}" for state, n in sorted(counts.items())] or ["Queue is empty"]
        if reason := pacer.blocked_reason():
            lines.append(f"⏸ {escape(reason)}")
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
