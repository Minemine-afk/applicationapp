"""JobStreet search (sg.jobstreet.com). No login needed, but see config: JobStreet's
robots.txt disallows this endpoint, so keep the rate low and back off when it says no."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from mcf_autoapply.config import JobStreet
from mcf_autoapply.db import Database

API_URL = "https://sg.jobstreet.com/api/jobsearch/v5/search"
BLOCKED_KEY = "jobstreet:blocked_until"
BLOCK_HOURS = 1
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/128.0.0.0 Safari/537.36")


class JobStreetBlocked(RuntimeError):
    """JobStreet answered 403 (bot protection). Searches pause for BLOCK_HOURS."""


class JobStreetClient:
    def __init__(self, rules: JobStreet, db: Database | None = None,
                 http: httpx.Client | None = None, max_retries: int = 3) -> None:
        self.rules = rules
        self.db = db
        self._min_interval = 1.0 / rules.requests_per_second
        self._max_retries = max_retries
        self._last_request = 0.0
        self._http = http or httpx.Client(timeout=30, headers={
            "User-Agent": _UA, "Accept": "application/json", "Accept-Language": "en-SG,en;q=0.9",
            "Referer": "https://sg.jobstreet.com/"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JobStreetClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- blocking -------------------------------------------------------------------------

    def blocked_until(self) -> datetime | None:
        if not self.db or not (value := self.db.get_state(BLOCKED_KEY)):
            return None
        until = datetime.fromisoformat(value)
        if until <= datetime.now(timezone.utc):
            self.db.set_state(BLOCKED_KEY, None)
            self.db.commit()
            return None
        return until

    def _mark_blocked(self) -> datetime:
        until = datetime.now(timezone.utc) + timedelta(hours=BLOCK_HOURS)
        if self.db:
            self.db.set_state(BLOCKED_KEY, until.isoformat())
            self.db.log(None, "jobstreet_blocked", {"until": until.isoformat()})
            self.db.commit()
        return until

    # --- search ---------------------------------------------------------------------------

    def search_page(self, query: str, page: int) -> dict[str, Any]:
        """One page of results, newest first. Pages are 1-based on JobStreet."""
        if until := self.blocked_until():
            raise JobStreetBlocked(f"JobStreet searches paused until {until:%H:%M} UTC")
        params = {"siteKey": self.rules.site_key, "sourcesystem": "houston", "keywords": query,
                  "where": self.rules.where, "page": page, "pageSize": self.rules.page_size,
                  "locale": "en-SG", "sortmode": "ListedDate"}
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._http.get(API_URL, params=params)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
            else:
                if resp.status_code == 403:
                    until = self._mark_blocked()
                    raise JobStreetBlocked(
                        f"JobStreet is blocking searches (403); pausing them until {until:%H:%M} UTC")
                if resp.status_code not in _RETRY_STATUSES or attempt == self._max_retries:
                    resp.raise_for_status()
                    return resp.json()
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def search(self, query: str, max_pages: int | None = None) -> Iterator[list[dict]]:
        for page in range(1, (max_pages or self.rules.max_pages) + 1):
            results = self.search_page(query, page).get("data") or []
            if not results:
                return
            yield results
            if len(results) < self.rules.page_size:
                return

    def _throttle(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
