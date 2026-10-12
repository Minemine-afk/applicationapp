"""JobStreet login (you sign in yourself in a visible window) and the applied-jobs sync."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import Page

from mcf_autoapply.db import Database
from mcf_autoapply.jobstreet import selectors as S
from mcf_autoapply.mcf.applied import AppliedSyncError, mark_already_applied

LOGIN_TIMEOUT_S = 600


def is_logged_in(page: Page, home_url: str = S.HOME_URL) -> bool:
    """On JobStreet, no "Sign in" link, and the account links are there."""
    try:
        if not page.url.startswith(home_url.rstrip("/")):
            return False
        if page.get_by_role("link", name=re.compile(S.SIGN_IN_TEXT, re.I)).count():
            return False
        return page.locator(S.ACCOUNT_MENU).count() > 0
    except Exception:
        return False


def check_logged_in(page: Page, home_url: str = S.HOME_URL, settle_s: float = 15) -> bool:
    """Open the home page and read the header. Waits out Cloudflare's interstitial and gives
    the page time to render the signed-in header."""
    page.goto(home_url, wait_until="domcontentloaded")
    deadline = time.monotonic() + S.CLOUDFLARE_CLEAR_S
    while time.monotonic() < deadline and _on_cloudflare(page):
        page.wait_for_timeout(1000)
    deadline = time.monotonic() + settle_s
    while True:
        page.wait_for_timeout(1500)
        if is_logged_in(page, home_url) or time.monotonic() > deadline:
            return is_logged_in(page, home_url)


def _on_cloudflare(page: Page) -> bool:
    try:
        return S.CLOUDFLARE_TITLE.lower() in (page.title() or "").lower()
    except Exception:
        return False


def login(page: Page, on_status: Callable[[str], None], home_url: str = S.HOME_URL,
          timeout_s: int = LOGIN_TIMEOUT_S, poll_ms: int = 2000) -> bool:
    """Open JobStreet's sign-in and wait while you log in. Nothing is typed for you."""
    if check_logged_in(page, home_url):
        return True
    link = page.get_by_role("link", name=re.compile(S.SIGN_IN_TEXT, re.I))
    if link.count():
        link.first.click()
    else:
        page.goto(S.LOGIN_URL, wait_until="domcontentloaded")
    on_status("JobStreet's sign-in page is open in the browser window on the PC. "
              "Sign in there (email + the code it sends, or Continue with Google). Nothing is "
              "typed for you. If it says \"Please complete the recaptcha\" or \"Verification "
              "failed\", that's its bot check: wait a few seconds and try once more, or use "
              "Continue with Google instead.")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_logged_in(page, home_url):
            page.wait_for_timeout(1000)
            if is_logged_in(page, home_url):
                return True
        # After SEEK's login the browser lands on the home page; if it lands elsewhere on
        # the site, go home so the header can be read.
        if (page.url.startswith(home_url.rstrip("/")) and "/login" not in page.url
                and not page.get_by_role("link", name=re.compile(S.SIGN_IN_TEXT, re.I)).count()
                and not page.locator(S.ACCOUNT_MENU).count()):
            page.goto(home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(poll_ms)
    return False


# --- applied jobs -----------------------------------------------------------------------------

@dataclass
class SyncResult:
    total: int
    new: int = 0
    released: list[str] = field(default_factory=list)


JOB_LINK = re.compile(r"/job/(\d+)")


def fetch_applied_jobs(page: Page, url: str = S.APPLIED_JOBS_URL, max_scrolls: int = 30,
                       ) -> list[dict]:
    """Collect job ids (and titles) from JobStreet's applied-jobs page, loading all of it."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    if "/login" in page.url or page.get_by_role("link", name=re.compile(S.SIGN_IN_TEXT, re.I)).count():
        raise AppliedSyncError("not logged in")
    seen: dict[str, dict] = {}
    last_height = -1
    for _ in range(max_scrolls):
        for a in page.locator("a[href*='/job/']").all():
            href = a.get_attribute("href") or ""
            m = JOB_LINK.search(href)
            if not m or m.group(1) in seen:
                continue
            title = (a.inner_text() or "").strip().split("\n")[0]
            card = a.locator("xpath=ancestor::*[self::article or self::li or self::div][1]")
            employer = ""
            if card.count():
                lines = [ln.strip() for ln in card.first.inner_text().split("\n") if ln.strip()]
                if len(lines) > 1 and lines[0] == title:
                    employer = lines[1]
            seen[m.group(1)] = {"id": m.group(1), "title": title, "employer": employer}
        # Load more (infinite scroll) until the page stops growing.
        height = page.evaluate("() => document.body.scrollHeight")
        if height == last_height:
            more = page.get_by_role("button", name=re.compile(r"(show|load) more", re.I))
            if more.count():
                more.first.click()
                page.wait_for_timeout(1500)
                continue
            break
        last_height = height
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
    return list(seen.values())


def sync_applied(page: Page, db: Database, url: str = S.APPLIED_JOBS_URL) -> SyncResult:
    applied = fetch_applied_jobs(page, url)
    result = SyncResult(total=len(applied))
    for a in applied:
        if db.upsert_applied_on_site(f"js:{a['id']}", a["title"], a["employer"], source="jobstreet"):
            result.new += 1
    result.released = mark_already_applied(db)
    db.log(None, "applied_sync", {"source": "jobstreet", "total": result.total, "new": result.new})
    db.commit()
    return result
