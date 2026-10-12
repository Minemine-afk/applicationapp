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
        if paused := self.paused():
            return f"paused: {paused['reason']} (run `mcf-apply unpause` once resolved)"
        start, end = self.rules.active_hours
        if not start <= self.now().time() < end:
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

    def delay_seconds(self) -> float:
        return random.uniform(*self.rules.delay_seconds)

    # --- pausing --------------------------------------------------------------------------

    def paused(self) -> dict | None:
        value = self.db.get_state(PAUSE_KEY)
        return json.loads(value) if value else None

    def pause(self, reason: str) -> None:
        self.db.set_state(PAUSE_KEY, json.dumps({"reason": reason, "at": self.now().isoformat()}))
        self.db.log(None, "paused", {"reason": reason})
        self.db.commit()

    def unpause(self) -> None:
        # The ramp restarts from its first step after a pause.
        self.db.set_state(PAUSE_KEY, None)
        self.db.set_state(RAMP_START_KEY, self.now().isoformat())
        self.db.log(None, "unpaused")
        self.db.commit()

    def record_failure(self) -> bool:
        """Pause if failures in the last hour hit the limit. Returns True if it paused."""
        recent = self.db.count_events(FAILED_EVENT, self.now() - timedelta(hours=1))
        if recent >= self.rules.max_failures_per_hour:
            self.pause(f"{recent} failed submissions in the last hour")
            return True
        return False

    def _ramp_start(self) -> datetime | None:
        if value := self.db.get_state(RAMP_START_KEY):
            return datetime.fromisoformat(value)
        first = self.db.first_event_ts(APPLIED_EVENT)
        return first.astimezone(self.tz) if first else None
