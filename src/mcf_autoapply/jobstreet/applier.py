"""Fill and submit one JobStreet (SEEK) application in a logged-in browser page.

Same contract as mcf.applier.Applier. Differences: employer questions are only discovered
inside the form, so a job whose questions aren't all answered in answers.yml comes back as
NEEDS_INPUT carrying the questions (the run parks it and Telegram asks you); "Apply" that leads
to the employer's own site is never followed (EXTERNAL); Cloudflare's interstitial that doesn't
clear counts as CAPTCHA, which pauses submissions.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from mcf_autoapply.jobstreet import selectors as S
from mcf_autoapply.mcf.applier import CAPTCHA, LOGGED_OUT, Outcome
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import normalise

MAX_STEPS = 8
STEP_TIMEOUT_MS = 30_000


class JobStreetApplier:
    def __init__(self, page: Page, screenshots_dir: Path, resume_name: str = "",
                 dry_run: bool = True, step_timeout_ms: int = STEP_TIMEOUT_MS,
                 cloudflare_wait_s: float = S.CLOUDFLARE_CLEAR_S) -> None:
        self.page = page
        self.shots = Path(screenshots_dir)
        self.resume_name = resume_name.strip()
        self.dry_run = dry_run
        self.timeout = step_timeout_ms
        self.cloudflare_wait_s = cloudflare_wait_s

    def apply(self, uuid: str, url: str, questions: list[str],
              answers: list[str | None]) -> Outcome:
        self._uuid = uuid
        self.bank = {normalise(q): a for q, a in zip(questions, answers) if a is not None}
        try:
            return self._apply(url)
        except (PlaywrightTimeout, PlaywrightError) as e:
            return self._fail(f"browser: {str(e).splitlines()[0][:200]}")

    # --- flow -----------------------------------------------------------------------------

    def _apply(self, url: str) -> Outcome:
        page = self.page
        page.goto(url, wait_until="domcontentloaded")
        if not self._clear_cloudflare():
            return self._evidence(CAPTCHA, "JobStreet's bot check didn't clear", "cloudflare")
        page.locator(f"{S.APPLY_BUTTON}, {S.SIGN_IN_LINK}, main h1, h1").first.wait_for(
            timeout=self.timeout)
        self._settle()
        body = page.locator("body").inner_text()
        if any(m.lower() in body.lower() for m in S.APPLIED_MARKERS):
            return Outcome(Q.ALREADY_APPLIED, "JobStreet shows this job as applied", source="jobstreet")
        if any(m.lower() in body.lower() for m in S.CLOSED_MARKERS):
            return self._evidence(Q.CLOSED, "no longer advertised", "closed")
        target = self._apply_target()
        if target is None:
            if self._logged_out():
                return self._evidence(LOGGED_OUT, "JobStreet session expired; send /login jobstreet",
                                      "logged-out")
            return self._evidence(Q.CLOSED, "no apply button on the job page", "no-apply-button")
        label, form_url = target
        if form_url is None:
            return self._evidence(Q.EXTERNAL, f"'{label}' leads to the employer's own site", "external")
        # Open SEEK's form in this tab: the button opens a new tab, which would leave this page
        # sitting on the job details while we look for the form's controls.
        page.goto(form_url, wait_until="domcontentloaded")
        if not self._clear_cloudflare():
            return self._evidence(CAPTCHA, "JobStreet's bot check didn't clear", "cloudflare")
        if left := self._left_jobstreet(form_url):
            return left
        if self._logged_out():
            return self._evidence(LOGGED_OUT, "JobStreet session expired; send /login jobstreet",
                                  "logged-out")

        for _ in range(MAX_STEPS):
            self._settle()
            if left := self._left_jobstreet(form_url):
                return left
            if self._success():
                return Outcome(Q.APPLIED, "submitted", self._screenshot("submitted"), source="jobstreet")
            if self._logged_out():
                return self._evidence(LOGGED_OUT, "JobStreet session expired; send /login jobstreet",
                                      "logged-out")
            heading = self._heading().lower()
            if page.locator(S.SUBMIT_BUTTON).count():
                return self._review()
            kind = self._step_kind()
            if kind == "documents":
                self._choose_resume()
            elif kind == "questions":
                outcome = self._answer_questions()
                if outcome:
                    return outcome
            # profile step and anything unrecognised: just continue
            before = page.url, heading
            button = self._continue()
            if not button.count():
                return self._fail(f"no Continue button on the '{heading or 'current'}' step")
            button.click(timeout=self.timeout)
            if not self._changed(before):
                errors = [t for t in S.FIELD_ERRORS if page.get_by_text(t).count()]
                why = (f"JobStreet wants more on the '{heading or 'current'}' step" if errors
                       else f"the '{heading or 'current'}' step did not move on")
                return self._fail(f"{why}; finish this one by hand")
        return self._fail(f"did not reach the review page in {MAX_STEPS} steps")

    def _left_jobstreet(self, form_url: str) -> Outcome | None:
        """Some jobs' JobStreet apply address forwards to the employer's own careers site."""
        from urllib.parse import urlparse

        here = urlparse(self.page.url).netloc
        if here and here != urlparse(form_url).netloc:
            return self._evidence(Q.EXTERNAL, f"JobStreet sends this one to the employer's own "
                                  f"site ({here})", "external")
        return None

    def _apply_target(self) -> tuple[str, str | None] | None:
        """(button label, SEEK form URL or None if it leads off-site), or None if no button."""
        from urllib.parse import urljoin, urlparse

        page = self.page
        for sel in S.APPLY_BUTTONS:
            loc = page.locator(sel)
            if not loc.count():
                continue
            el = loc.first
            label = (el.inner_text() or "").strip() or "Apply"
            href = el.get_attribute("href")
            if href:
                full = urljoin(page.url, href)
                here, there = urlparse(page.url), urlparse(full)
                onsite = there.netloc == here.netloc and re.search(S.ONSITE_APPLY_PATH, there.path)
                return label, (full if onsite else None)
            # A button without a link: SEEK's own form only when it says Quick apply.
            if S.QUICK_APPLY_TEXT.lower() in label.lower():
                return label, self._url_opened_by(el)
            return label, None
        return None

    def _url_opened_by(self, el: Locator) -> str | None:
        """Click a button that opens SEEK's form (maybe in a new tab); return the form's URL."""
        page = self.page
        before = page.url
        try:
            with page.context.expect_page(timeout=5_000) as new:
                el.click(timeout=self.timeout)
            tab = new.value
            tab.wait_for_load_state("domcontentloaded")
            url = tab.url
            tab.close()
            return url
        except PlaywrightTimeout:   # same tab
            page.wait_for_load_state("domcontentloaded")
            return page.url if page.url != before else None

    def _step_kind(self) -> str:
        """documents / questions / profile / other, from the step's address and its controls."""
        page, url = self.page, self.page.url.lower()
        if any(p in url for p in S.QUESTIONS_URL_PARTS):
            return "questions"
        if any(p in url for p in S.PROFILE_URL_PARTS):
            return "profile"
        if page.locator(f"{S.RESUME_SELECT}, {S.SAVED_RESUME_RADIO}").count() or \
                page.get_by_text(re.compile(S.COVER_LETTER_NONE_TEXT, re.I)).count():
            return "documents"
        if self._question_groups():
            return "questions"
        return "other"

    def _review(self) -> Outcome:
        shot = self._screenshot("review")
        if self.dry_run:
            return Outcome(Q.DRY_RUN_OK, "stopped at the review page (dry run)", shot, source="jobstreet")
        self.page.locator(S.SUBMIT_BUTTON).first.click()
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            if self._success():
                return Outcome(Q.APPLIED, "submitted", self._screenshot("submitted"), source="jobstreet")
            self.page.wait_for_timeout(500)
        return self._fail("no confirmation after Submit; check JobStreet before retrying")

    # --- steps ----------------------------------------------------------------------------

    def _choose_resume(self) -> None:
        page = self.page
        if not self._pick(page.locator(S.SAVED_RESUME_RADIO), S.SAVED_RESUME_TEXT):
            pass   # no "select a resumé" choice: the saved resume is probably preselected
        select = page.locator(S.RESUME_SELECT)
        if select.count():
            try:
                options = select.first.locator("option").all_inner_texts()
                wanted = next((o for o in options if self.resume_name and
                               self.resume_name.lower() in o.lower()), None)
                if wanted:
                    select.first.select_option(label=wanted, timeout=5_000)
                elif len(options) > 1 and not select.first.input_value():
                    select.first.select_option(index=1, timeout=5_000)   # first real resume
            except (PlaywrightTimeout, PlaywrightError):
                pass   # the step's own check reports a missing resume
        self._pick(page.locator(S.COVER_LETTER_NONE), S.COVER_LETTER_NONE_TEXT)

    def _pick(self, inputs: Locator, label_text: str | None = None) -> bool:
        """Select a radio/checkbox even when SEEK hides the input behind its label."""
        page = self.page
        if label_text:
            label = page.locator("label").filter(has_text=re.compile(label_text, re.I))
            if label.count():
                try:
                    label.first.click(timeout=5_000)
                    return True
                except (PlaywrightTimeout, PlaywrightError):
                    pass
        if inputs.count():
            el = inputs.first
            for action in (lambda: el.check(timeout=3_000), lambda: el.click(timeout=3_000),
                           lambda: el.check(force=True, timeout=3_000)):
                try:
                    action()
                    return True
                except (PlaywrightTimeout, PlaywrightError):
                    continue
        return False

    def _question_groups(self) -> list[Locator]:
        """Employer questions: groups with inputs, minus the documents step's own groups."""
        groups = self.page.locator(S.QUESTION_GROUPS)
        out = []
        for i in range(groups.count()):
            g = groups.nth(i)
            if not g.locator("input, select, textarea").count():
                continue
            title = self._question_text(g).lower()
            if any(w in title for w in S.DOCUMENT_GROUP_WORDS):
                continue
            out.append(g)
        return out

    def _answer_questions(self) -> Outcome | None:
        """Answer every employer question from the bank; else NEEDS_INPUT with the questions.

        JobStreet pre-fills questions you've answered on earlier applications (your right to
        work, your qualification); a question that already has an answer is left as it is.
        """
        found: list[dict] = []
        missing: list[dict] = []
        for group in self._question_groups():
            text = self._question_text(group)
            if not text:
                continue
            options = self._options(group, text)
            q = {"question": text, "options": options}
            found.append(q)
            answer = self._lookup(text)
            if answer is not None and self._fill(group, answer, options):
                continue
            if self._has_answer(group):
                continue   # JobStreet filled it in from your earlier applications
            missing.append(q)
        if missing:
            out = self._evidence(Q.NEEDS_INPUT, f"{len(missing)} employer question(s) need your answer",
                                 "questions")
            out.questions = missing
            return out
        return None

    def _options(self, group: Locator, question: str) -> list[str]:
        """The choices: a dropdown's options (minus its placeholder), else the option labels."""
        select = group.locator("select")
        if select.count():
            opts = select.first.locator("option")
            out = []
            for i in range(opts.count()):
                o = opts.nth(i)
                label = (o.inner_text() or "").strip()
                if label and (o.get_attribute("value") or "") != "" and \
                        not label.lower().startswith(("select", "choose", "please")):
                    out.append(label)
            return out
        return [o.strip() for o in group.locator("label").all_inner_texts()
                if o.strip() and normalise(o) != normalise(question)]

    def _has_answer(self, group: Locator) -> bool:
        try:
            if group.locator("input[type=radio]:checked, input[type=checkbox]:checked").count():
                return True
            select = group.locator("select")
            if select.count():
                return bool(select.first.input_value())
            text = group.locator("textarea, input[type='text'], input[type='number'], input:not([type])")
            return bool(text.count() and text.first.input_value().strip())
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def _question_text(self, group: Locator) -> str:
        for sel in ("legend", "label", "p", "span"):
            loc = group.locator(sel)
            if loc.count():
                text = loc.first.inner_text().strip()
                if text:
                    return text
        return ""

    def _lookup(self, question: str) -> str | None:
        key = normalise(question)
        if key in self.bank:
            return self.bank[key]
        for k, v in self.bank.items():   # the form may add a suffix like " *"
            if k and (k in key or key in k):
                return v
        return None

    def _fill(self, group: Locator, answer: str, options: list[str]) -> bool:
        """Click the option matching the answer, or type it. False if it can't be applied."""
        wanted = normalise(answer)
        radios = group.locator('input[type="radio"], input[type="checkbox"]')
        if radios.count():
            for i, label in enumerate(options[:radios.count()]):
                if normalise(label) == wanted or wanted in normalise(label):
                    labels = group.locator("label").filter(has_text=label)
                    if labels.count():
                        try:
                            labels.first.click(timeout=5_000)
                            return True
                        except (PlaywrightTimeout, PlaywrightError):
                            pass
                    return self._pick(radios.nth(i))
            return False
        select = group.locator("select")
        if select.count():
            for opt in select.first.locator("option").all_inner_texts():
                if normalise(opt) == wanted or wanted in normalise(opt):
                    select.first.select_option(label=opt)
                    return True
            return False
        text = group.locator("textarea, input[type='text'], input[type='number'], input:not([type])")
        if text.count():
            text.first.fill(answer, timeout=5_000)
            return True
        return False

    # --- page state -----------------------------------------------------------------------

    def _clear_cloudflare(self) -> bool:
        deadline = time.monotonic() + self.cloudflare_wait_s
        while S.CLOUDFLARE_TITLE.lower() in (self.page.title() or "").lower():
            if time.monotonic() > deadline:
                return False
            self.page.wait_for_timeout(1000)
        return True

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeout:
            pass

    def _logged_out(self) -> bool:
        page = self.page
        if "/login" in page.url or "login." in page.url:
            return True
        return bool(page.get_by_role("link", name=re.compile(S.SIGN_IN_TEXT, re.I)).count())

    def _success(self) -> bool:
        if S.SUCCESS_URL_PART in self.page.url:
            return True
        return any(self.page.get_by_text(t, exact=False).count() for t in S.SUCCESS_TEXTS)

    def _heading(self) -> str:
        loc = self.page.locator(S.STEP_HEADING)
        return loc.first.inner_text().strip() if loc.count() else ""

    def _continue(self) -> Locator:
        return self.page.locator(S.CONTINUE_BUTTON).first

    def _changed(self, before: tuple[str, str]) -> bool:
        deadline = time.monotonic() + self.timeout / 1000
        error_seen = 0
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
            if (self.page.url, self._heading().lower()) != before or self._success():
                return True
            if self.page.locator(S.SUBMIT_BUTTON).count():
                return True
            if any(self.page.get_by_text(t).count() for t in S.FIELD_ERRORS):
                error_seen += 250
                if error_seen >= 1000:
                    return False
        return False

    # --- evidence -------------------------------------------------------------------------

    def _screenshot(self, label: str) -> str | None:
        try:
            self.shots.mkdir(parents=True, exist_ok=True)
            stem = f"{datetime.now():%Y%m%d-%H%M%S}-{self._uuid.replace(':', '-')}-{label}"
            path = self.shots / f"{stem}.png"
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except PlaywrightError:
            return None

    def _evidence(self, state: str, detail: str, label: str) -> Outcome:
        """An outcome with a screenshot and the page's HTML saved next to it, so anything
        other than a submission can be checked (and the hooks fixed) from the files."""
        shot = self._screenshot(label)
        if shot:
            try:
                Path(shot).with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
            except PlaywrightError:
                pass
        return Outcome(state, detail, shot, source="jobstreet")

    def _fail(self, detail: str) -> Outcome:
        return self._evidence(Q.FAILED, detail, "failed")
