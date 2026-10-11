"""Singpass QR login against stand-in MCF and Singpass pages."""

import io
import os
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")
zxingcpp = pytest.importorskip("zxingcpp")

from PIL import Image  # noqa: E402

from mcf_autoapply.mcf.browser import login_via_qr  # noqa: E402

HOME = "https://fake-mcf.test/"
SINGPASS = "https://fake-singpass.test/main"
LINKS = ["https://app.singpass.gov.sg/qrlogin?session=first",
         "https://app.singpass.gov.sg/qrlogin?session=second"]
CHROMIUM = "/opt/pw-browsers/chromium"


def qr_svg(text: str) -> str:
    svg = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode).to_svg()
    svg = re.sub(r"<\?xml.*?\?>|<!DOCTYPE.*?>", "", svg, flags=re.S)
    return svg.replace("<svg", '<svg id="ndi-qr-canvas" width="160" height="160"', 1)


# Home: logged out until ?loggedin=1. "Log In" opens a panel with the Singpass button.
# Like the real site, "Applied Jobs" is only in the DOM while the account menu is open.
HOME_PAGE = """<!doctype html><body><nav id="nav"></nav><div id="panel"></div><script>
const nav = document.getElementById("nav");
if (new URLSearchParams(location.search).get("loggedin")) {
  nav.innerHTML = '<a id="navbar-search-jobs">Search Jobs</a><button id="account">Kumar ▾</button>';
} else {
  nav.innerHTML = '<a id="navbar-search-jobs">Search Jobs</a><button id="navbar-login">Log In</button>';
  document.getElementById("navbar-login").onclick = () => {
    document.getElementById("panel").innerHTML =
      '<button data-testid="login-with-singpass">Log in with singpass</button>';
    document.querySelector("[data-testid=login-with-singpass]").onclick =
      () => location.href = "%s";
  };
}
</script></body>""" % SINGPASS

# Singpass: QR 1 expires after 1.5 s into a "Refresh QR Code" button that shows QR 2.
# Redirects home once the test "approves" (window.__approved = "?loggedin=1" or "").
SINGPASS_PAGE = """<!doctype html><body><div id="qr">%s</div><script>
const qr = document.getElementById("qr");
setTimeout(() => {
  qr.innerHTML = '<p>QR code expired</p><button id="refresh">Refresh QR Code</button>';
  document.getElementById("refresh").onclick = () => qr.innerHTML = %r;
}, 1500);
setInterval(() => { if (window.__approved !== undefined) location.href = "%s" + window.__approved; }, 200);
</script></body>""" % (qr_svg(LINKS[0]), qr_svg(LINKS[1]), HOME)


@pytest.fixture
def page():
    with playwright.sync_playwright() as p:
        kwargs = {"executable_path": CHROMIUM} if os.path.exists(CHROMIUM) else {}
        try:
            browser = p.chromium.launch(**kwargs)
        except playwright.Error as e:
            pytest.skip(f"Chromium not available: {e}")
        page = browser.new_page()
        page.route("https://fake-mcf.test/**",
                   lambda r: r.fulfill(body=HOME_PAGE, content_type="text/html"))
        page.route("https://fake-singpass.test/**",
                   lambda r: r.fulfill(body=SINGPASS_PAGE, content_type="text/html"))
        yield page
        browser.close()


def test_sends_each_qr_with_its_link_then_detects_login(page):
    got = []

    def on_qr(qr, n):
        got.append(qr)
        img = Image.open(io.BytesIO(qr.png))
        assert img.width > 160 and zxingcpp.read_barcodes(img)   # the sent PNG scans
        if n == 1:   # approve after the refreshed QR, like tapping it in the Singpass app
            page.evaluate("window.__approved = '?loggedin=1'")

    assert login_via_qr(page, on_qr, home_url=HOME, timeout_s=20, poll_ms=300) is True
    assert [q.link for q in got] == LINKS
    assert page.url.startswith(HOME) and "loggedin=1" in page.url


def test_already_logged_in_skips_singpass(page):
    page.route("https://fake-mcf.test/**", lambda r: r.fulfill(
        body='<a id="navbar-search-jobs">Search Jobs</a>', content_type="text/html"))
    assert login_via_qr(page, lambda qr, n: pytest.fail("no QR expected"), home_url=HOME) is True


def test_times_out_without_approval(page):
    got = []
    assert login_via_qr(page, lambda qr, n: got.append(qr), home_url=HOME,
                        timeout_s=4, poll_ms=300, max_qrs=1) is False
    assert len(got) == 1   # max_qrs caps refreshes: the expired QR isn't refreshed or sent


def test_sends_a_screenshot_when_back_on_mcf_but_not_logged_in(page):
    """E.g. Singpass sent the browser back without logging in: show what the page says."""
    unclear = []

    def on_qr(qr, n):
        page.evaluate("window.__approved = ''")    # redirected home, still logged out

    ok = login_via_qr(page, on_qr, home_url=HOME, timeout_s=6, poll_ms=300,
                      on_unclear=lambda png, note: unclear.append((png, note)), unclear_after_s=1)
    assert ok is False
    notes = [n for _, n in unclear]
    assert any("can't tell if you're logged in" in n for n in notes)
    assert any("timed out" in n for n in notes)
    assert all(png.startswith(b"\x89PNG") for png, _ in unclear)
