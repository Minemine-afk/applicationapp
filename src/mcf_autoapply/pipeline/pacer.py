"""Spread submissions across the day: active hours, hourly/daily caps, ramp, auto-pause."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mcf_autoapply.config import Apply
from mcf_autoapply.db import Database

APPLIED_EVENT = "application_applied"
FAILED_EVENT = "application_failed"
PAUSE_KEY = "paused"
RAMP_START_KEY = "ramp_start"
UNPAUSED_KEY = "unpaused_at"


class Pacer:
    """Caps from `rules` for the main site(s), plus per-site caps ({site: (hourly, daily)}).

    A site with its own caps (JobStreet) runs independently: its submissions count only
    against its own caps, and the main caps count only the other sites' submissions.
    Pauses and active hours apply to every site.
    """

    def __init__(
        self, rules: Apply, db: Database, now: Callable[[], datetime] | None = None,
        site_caps: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.rules = rules
        self.db = db
        self.tz = ZoneInfo(rules.timezone)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.site_caps = site_caps or {}

    def now(self) -> datetime:
        return self._now().astimezone(self.tz)

    # --- limits ---------------------------------------------------------------------------

    def daily_cap(self) -> int:
        if not self.rules.ramp:
            return self.rules.daily_cap
        start = self._ramp_start()
        weeks = 0 if start is None else max(0, (self.now() - start).days // 7)
        return min(self.rules.ramp[min(weeks, len(self.rules.ramp) - 1)], self.rules.daily_cap)

    def applied_today(self, source: str | None = None) -> int:
        midnight = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.count_events(APPLIED_EVENT, midnight, source, self._excluded(source))

    def _excluded(self, source: str | None) -> tuple[str, ...]:
        """For the main caps (no source given), leave out sites that have their own caps."""
        return tuple(self.site_caps) if source is None else ()

    def applied_this_hour(self, source: str | None = None) -> int:
        """Submissions since the top of the current clock hour.

        Not a sliding 60-minute window: with a run every hour, the previous run's
        submissions (made a few minutes past the hour) would still be inside that window
        when the next run started, so every other run applied nothing.
        """
        top = self.now().replace(minute=0, second=0, microsecond=0)
        return self.db.count_events(APPLIED_EVENT, top, source, self._excluded(source))

    applied_last_hour = applied_this_hour   # older name

    def remaining(self, source: str | None = None) -> int:
        """How many more submissions are allowed right now (for one site, if given)."""
        if source in self.site_caps:
            hourly, daily = self.site_caps[source]
            left = min(daily - self.applied_today(source), hourly - self.applied_this_hour(source))
        else:
            left = min(self.daily_cap() - self.applied_today(),
                       self.rules.hourly_cap - self.applied_this_hour())
        return max(0, left)

    def blocked_reason(self, source: str | None = None) -> str | None:
        """Why no submission may happen right now, or None if one may."""
        if paused := self.paused(source):
            who = f"{paused['source']} " if paused.get("source") else ""
            return f"{who}paused: {paused['reason']} (send /resume once resolved)"
        if not self.within_active_hours():
            start, end = self.rules.active_hours
            return f"outside active hours ({start:%H:%M}–{end:%H:%M} {self.rules.timezone})"
        if source in self.site_caps:
            hourly, daily = self.site_caps[source]
            if self.applied_today(source) >= daily:
                return f"{source} daily cap reached ({daily})"
            if self.applied_this_hour(source) >= hourly:
                return f"{source} hourly cap reached ({hourly})"
            return None
        if self.applied_today() >= self.daily_cap():
            return f"daily cap reached ({self.daily_cap()})"
        if self.applied_this_hour() >= self.rules.hourly_cap:
            return f"hourly cap reached ({self.rules.hourly_cap})"
        return None

    def within_active_hours(self) -> bool:
        """Start == end means no limit: any time of day. A window may cross midnight."""
        start, end = self.rules.active_hours
        if start == end:
            return True
        now = self.now().time()
        return start <= now < end if start < end else (now >= start or now < end)

    def delay_seconds(self) -> float:
        return random.uniform(*self.rules.delay_seconds)

    # --- pausing --------------------------------------------------------------------------

    def paused(self, source: str | None = None) -> dict | None:
        """The pause in force: everything (/pause), or for one site (its failures, a CAPTCHA)."""
        for key in ([PAUSE_KEY] + ([f"{PAUSE_KEY}:{source}"] if source else [])):
            if value := self.db.get_state(key):
                return json.loads(value)
        return None

    def site_pauses(self) -> dict[str, dict]:
        rows = self.db.conn.execute("SELECT key, value FROM state WHERE key LIKE ?",
                                    (f"{PAUSE_KEY}:%",)).fetchall()
        return {r["key"].split(":", 1)[1]: json.loads(r["value"]) for r in rows if r["value"]}

    def pause(self, reason: str, source: str | None = None) -> None:
        key = f"{PAUSE_KEY}:{source}" if source else PAUSE_KEY
        self.db.set_state(key, json.dumps({"reason": reason, "at": self.now().isoformat(),
                                           "source": source}))
        self.db.log(None, "paused", {"reason": reason, "source": source})
        self.db.commit()

    def unpause(self) -> None:
        """Lift every pause. Failures before this moment no longer count towards a new one."""
        self.db.set_state(PAUSE_KEY, None)
        for site in self.site_pauses():
            self.db.set_state(f"{PAUSE_KEY}:{site}", None)
        self.db.set_state(UNPAUSED_KEY, self.now().isoformat())
        # The ramp restarts from its first step after a pause.
        self.db.set_state(RAMP_START_KEY, self.now().isoformat())
        self.db.log(None, "unpaused")
        self.db.commit()

    def record_failure(self, source: str | None = None) -> bool:
        """Pause that site if its failures this past hour (since the last resume) hit the
        limit; the other site carries on. Returns True if it paused."""
        since = self.now() - timedelta(hours=1)
        if value := self.db.get_state(UNPAUSED_KEY):
            # Events are stamped to the second: round up so ones just before don't count.
            resumed = datetime.fromisoformat(value)
            if resumed.microsecond:
                resumed = resumed.replace(microsecond=0) + timedelta(seconds=1)
            since = max(since, resumed)
        recent = self.db.count_events(FAILED_EVENT, since, source)
        if recent >= self.rules.max_failures_per_hour:
            self.pause(f"{recent} failed submissions in the last hour", source)
            return True
        return False

    def _ramp_start(self) -> datetime | None:
        if value := self.db.get_state(RAMP_START_KEY):
            return datetime.fromisoformat(value)
        first = self.db.first_event_ts(APPLIED_EVENT)
        return first.astimezone(self.tz) if first else None
