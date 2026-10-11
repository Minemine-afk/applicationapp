"""Persistent browser session, so your Singpass login carries over between runs."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

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


@dataclass
class LoginQR:
    png: bytes              # the QR with a white margin, ready to send or scan
    link: str | None        # what it encodes: https://app.singpass.gov.sg/qrlogin?...


def on_mcf(page: Page, home_url: str = S.HOME_URL) -> bool:
    return page.url.startswith(home_url.rstrip("/"))


def logged_in(page: Page, home_url: str = S.HOME_URL) -> bool:
    """On MCF, the menu bar has loaded, and it has no "Log In" button."""
    try:
        return (on_mcf(page, home_url)
                and page.locator(S.NAVBAR_SEARCH_JOBS).count() > 0
                and page.locator(S.NAVBAR_LOGIN).count() == 0)
    except Exception:   # page mid-navigation during the Singpass redirect
        return False


def is_logged_in(page: Page, home_url: str = S.HOME_URL) -> bool:
    page.goto(home_url, wait_until="domcontentloaded")
    page.locator(S.NAVBAR_SEARCH_JOBS).first.wait_for(timeout=30_000)
    page.wait_for_timeout(1500)   # let the login state settle before reading the menu
    return logged_in(page, home_url)


def capture_qr(page: Page) -> LoginQR:
    """Screenshot the Singpass QR and read the link it encodes."""
    import io

    import zxingcpp
    from PIL import Image, ImageOps

    raw = page.locator(S.SINGPASS_QR).first.screenshot()
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    # Upscale and add the quiet zone scanners need around a QR code.
    img = ImageOps.expand(img.resize((img.width * 3, img.height * 3), Image.NEAREST),
                          border=60, fill="white")
    found = [r.text for r in zxingcpp.read_barcodes(img) if r.text]
    out = io.BytesIO()
    img.save(out, format="PNG")
    return LoginQR(out.getvalue(), found[0] if found else None)


def login_via_qr(
    page: Page, on_qr: Callable[[LoginQR, int], None], home_url: str = S.HOME_URL,
    timeout_s: int = LOGIN_TIMEOUT_S, max_qrs: int = 10, poll_ms: int = 2000,
    on_unclear: Callable[[bytes, str], None] | None = None, unclear_after_s: float = 20,
) -> bool:
    """Go to the Singpass QR page and wait for you to approve it in the Singpass app.

    `on_qr(qr, n)` is called with each new QR. Singpass QRs expire after about a minute;
    the expired one is refreshed automatically, up to `max_qrs` QRs in total.
    `on_unclear(screenshot, note)` is called (once each) if the browser leaves Singpass but
    doesn't look logged in, and on timeout, so you can see what the page shows.
    Your Singpass password is never typed: approval happens on your phone.
    """

    def report(note: str) -> None:
        if on_unclear:
            try:
                on_unclear(page.screenshot(full_page=False), note)
            except Exception:
                pass

    if is_logged_in(page, home_url):
        return True
    page.locator(S.NAVBAR_LOGIN).first.click()
    page.locator(S.SINGPASS_BUTTON).first.click()
    page.locator(S.SINGPASS_QR).first.wait_for(timeout=60_000)

    deadline = time.monotonic() + timeout_s
    last_link, sent = None, 0
    left_singpass_at, reported = None, False
    while time.monotonic() < deadline:
        if logged_in(page, home_url):
            # Read twice, a moment apart, so a half-loaded menu isn't mistaken for a login.
            page.wait_for_timeout(1000)
            if logged_in(page, home_url):
                return True
        if on_mcf(page, home_url):
            left_singpass_at = left_singpass_at or time.monotonic()
            if not reported and time.monotonic() - left_singpass_at > unclear_after_s:
                report("Back on MyCareersFuture after Singpass, but I can't tell if you're "
                       "logged in. This is what the browser shows.")
                reported = True
        else:
            left_singpass_at = None
        try:
            refresh = page.get_by_text(S.SINGPASS_REFRESH_TEXT)
            if refresh.count() and refresh.first.is_visible():
                if sent >= max_qrs:
                    break   # stop refreshing; you can run login again
                refresh.first.click()
            elif page.locator(S.SINGPASS_QR).count():
                qr = capture_qr(page)
                # Only send readable QRs, and each one once.
                if qr.link and qr.link != last_link and sent < max_qrs:
                    on_qr(qr, sent)
                    last_link, sent = qr.link, sent + 1
        except Exception:   # the page changed under us (redirect after approval)
            pass
        page.wait_for_timeout(poll_ms)
    report("Login timed out. This is what the browser shows.")
    return False
