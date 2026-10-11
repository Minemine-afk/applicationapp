"""The MCF login must survive closing and reopening the browser (every run reopens it)."""

import json
import os
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from mcf_autoapply.config import Apply  # noqa: E402
from mcf_autoapply.mcf.browser import open_page, session_path  # noqa: E402

CHROMIUM = "/opt/pw-browsers/chromium"
SITE = "https://fake-mcf.test/"


def serve_site(page):
    """A site that sets a *session* cookie (no expiry) on /login, like MCF after Singpass."""
    def handle(route):
        headers = {}
        if route.request.url.endswith("/login"):
            headers["Set-Cookie"] = "mcf_session=abc123; Path=/; Secure; HttpOnly"
        route.fulfill(body="<html><body>ok</body></html>", content_type="text/html",
                      headers=headers)
    page.route("https://fake-mcf.test/**", handle)


@pytest.fixture
def rules(tmp_path):
    if not os.path.exists(CHROMIUM):
        pytest.skip("Chromium not available")
    return Apply(browser_profile=tmp_path / "profile", browser_executable=Path(CHROMIUM),
                 headless=True)


def names(page):
    return {c["name"]: c["value"] for c in page.context.cookies(SITE)}


def test_session_cookie_is_lost_without_saving(rules):
    with open_page(rules, keep_session=False) as page:
        serve_site(page)
        page.goto(SITE + "login")
        assert names(page) == {"mcf_session": "abc123"}
    with open_page(rules, keep_session=False) as page:
        serve_site(page)
        page.goto(SITE)
        assert names(page) == {}          # Chromium dropped it: the bug this guards against


def test_session_cookie_is_restored_on_the_next_launch(rules):
    with open_page(rules) as page:
        serve_site(page)
        page.goto(SITE + "login")
    saved = json.loads(session_path(rules).read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["cookies"]] == ["mcf_session"]

    with open_page(rules) as page:          # a fresh launch, like every run
        serve_site(page)
        page.goto(SITE)
        assert names(page) == {"mcf_session": "abc123"}
        page.context.clear_cookies()        # e.g. MCF logged us out server-side
    assert json.loads(session_path(rules).read_text(encoding="utf-8"))["cookies"] == []


def test_corrupt_session_file_is_ignored(rules):
    session_path(rules).parent.mkdir(parents=True)
    session_path(rules).write_text("not json", encoding="utf-8")
    with open_page(rules) as page:
        serve_site(page)
        page.goto(SITE)
        assert names(page) == {}
