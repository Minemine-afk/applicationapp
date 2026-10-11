"""Persistent browser session, so your Singpass login carries over between runs."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Page, sync_playwright

from mcf_autoapply.config import Apply
from mcf_autoapply.mcf import selectors as S

LOGIN_TIMEOUT_S = 600


@contextmanager
def open_page(rules: Apply, headless: bool | None = None) -> Iterator[Page]:
    rules.browser_profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(rules.browser_profile),
            headless=rules.headless if headless is None else headless,
            executable_path=str(rules.browser_executable) if rules.browser_executable else None,
            viewport={"width": 1280, "height": 900},
            locale="en-SG",
            timezone_id=rules.timezone,
        )
        try:
            yield ctx.pages[0] if ctx.pages else ctx.new_page()
        finally:
            ctx.close()


def is_logged_in(page: Page) -> bool:
    page.goto(S.HOME_URL, wait_until="domcontentloaded")
    page.locator(f"{S.NAVBAR_LOGIN}, {S.NAVBAR_APPLIED_JOBS}").first.wait_for(timeout=30_000)
    return page.locator(S.NAVBAR_LOGIN).count() == 0


def wait_for_login(page: Page, timeout_s: int = LOGIN_TIMEOUT_S) -> bool:
    """Open MCF and wait while you log in with Singpass yourself. Never types credentials."""
    page.goto(S.HOME_URL, wait_until="domcontentloaded")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if (page.url.startswith(S.HOME_URL)
                    and page.locator(S.NAVBAR_APPLIED_JOBS).count() > 0
                    and page.locator(S.NAVBAR_LOGIN).count() == 0):
                return True
        except Exception:   # page mid-navigation during the Singpass redirect
            pass
        page.wait_for_timeout(2000)
    return False
