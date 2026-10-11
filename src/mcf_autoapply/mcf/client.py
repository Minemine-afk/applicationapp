"""Client for the public MyCareersFuture jobs API (no login needed)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx

from mcf_autoapply import __version__

API_BASE = "https://api.mycareersfuture.gov.sg"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class MCFClient:
    def __init__(
        self,
        requests_per_second: float = 1.0,
        max_retries: int = 4,
        http: httpx.Client | None = None,
    ) -> None:
        self._min_interval = 1.0 / requests_per_second
        self._max_retries = max_retries
        self._last_request = 0.0
        self._http = http or httpx.Client(
            base_url=API_BASE,
            timeout=30,
            headers={"User-Agent": f"mcf-autoapply/{__version__} (personal job search)"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MCFClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search_page(self, query: str, page: int, limit: int = 100) -> dict[str, Any]:
        """One page of search results, newest postings first."""
        return self._get(
            "/v2/jobs",
            params={"search": query, "limit": limit, "page": page, "sortBy": "new_posting_date"},
        )

    def search(self, query: str, limit: int = 100, max_pages: int = 5) -> Iterator[list[dict]]:
        """Yield pages of results until results run out or max_pages is reached."""
        for page in range(max_pages):
            results = self.search_page(query, page, limit).get("results") or []
            if not results:
                return
            yield results
            if len(results) < limit:
                return

    def get_job(self, uuid: str) -> dict[str, Any]:
        return self._get(f"/v2/jobs/{uuid}")

    def screening_questions(self, uuid: str) -> list[str]:
        """The employer's yes/no screening questions for a job (often none)."""
        data = self._get(f"/v2/jobs/{uuid}/screening-questions")
        items = data.get("results", []) if isinstance(data, dict) else data
        return [q["question"].strip() for q in items or [] if q.get("question")]

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
            else:
                if resp.status_code not in _RETRY_STATUSES or attempt == self._max_retries:
                    resp.raise_for_status()
                    return resp.json()
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def _throttle(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
