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


def logged_in(page: Page, home_url: str = S.HOME_URL) -> bool:
    try:
        return (page.url.startswith(home_url)
                and page.locator(S.NAVBAR_APPLIED_JOBS).count() > 0
                and page.locator(S.NAVBAR_LOGIN).count() == 0)
    except Exception:   # page mid-navigation during the Singpass redirect
        return False


def is_logged_in(page: Page, home_url: str = S.HOME_URL) -> bool:
    page.goto(home_url, wait_until="domcontentloaded")
    page.locator(f"{S.NAVBAR_LOGIN}, {S.NAVBAR_APPLIED_JOBS}").first.wait_for(timeout=30_000)
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
) -> bool:
    """Go to the Singpass QR page and wait for you to approve it in the Singpass app.

    `on_qr(qr, n)` is called with each new QR. Singpass QRs expire after about a minute;
    the expired one is refreshed automatically, up to `max_qrs` QRs in total.
    Your Singpass password is never typed: approval happens on your phone.
    """
    if is_logged_in(page, home_url):
        return True
    page.locator(S.NAVBAR_LOGIN).first.click()
    page.locator(S.SINGPASS_BUTTON).first.click()
    page.locator(S.SINGPASS_QR).first.wait_for(timeout=60_000)

    deadline = time.monotonic() + timeout_s
    last_link, sent = None, 0
    while time.monotonic() < deadline:
        if logged_in(page, home_url):
            return True
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
    return False
