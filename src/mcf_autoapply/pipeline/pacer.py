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
    """Global caps from `rules`, plus optional per-site caps ({site: (hourly, daily)})."""

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
        return self.db.count_events(APPLIED_EVENT, midnight, source)

    def applied_last_hour(self, source: str | None = None) -> int:
        return self.db.count_events(APPLIED_EVENT, self.now() - timedelta(hours=1), source)

    def remaining(self, source: str | None = None) -> int:
        """How many more submissions are allowed right now (for one site, if given)."""
        left = min(self.daily_cap() - self.applied_today(),
                   self.rules.hourly_cap - self.applied_last_hour())
        if source in self.site_caps:
            hourly, daily = self.site_caps[source]
            left = min(left, daily - self.applied_today(source),
                       hourly - self.applied_last_hour(source))
        return max(0, left)

    def blocked_reason(self, source: str | None = None) -> str | None:
        """Why no submission may happen right now, or None if one may."""
        if paused := self.paused():
            return f"paused: {paused['reason']} (run `mcf-apply unpause` once resolved)"
        start, end = self.rules.active_hours
        if not start <= self.now().time() < end:
            return f"outside active hours ({start:%H:%M}–{end:%H:%M} {self.rules.timezone})"
        if self.applied_today() >= self.daily_cap():
            return f"daily cap reached ({self.daily_cap()})"
        if self.applied_last_hour() >= self.rules.hourly_cap:
            return f"hourly cap reached ({self.rules.hourly_cap})"
        if source in self.site_caps:
            hourly, daily = self.site_caps[source]
            if self.applied_today(source) >= daily:
                return f"{source} daily cap reached ({daily})"
            if self.applied_last_hour(source) >= hourly:
                return f"{source} hourly cap reached ({hourly})"
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
