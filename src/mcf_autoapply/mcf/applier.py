"""Fill and submit one MCF application in a logged-in browser page."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from mcf_autoapply.mcf import selectors as S
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import normalise

MAX_STEPS = 12
STEP_TIMEOUT_MS = 30_000

# Outcomes beyond the application states in pipeline.queue.
CAPTCHA = "captcha"
LOGGED_OUT = "logged_out"


@dataclass
class Outcome:
    state: str
    detail: str = ""
    screenshot: str | None = None
    application_id: str | None = None
    # NEEDS_INPUT only: employer questions found in the form, [{question, options}].
    questions: list[dict] | None = None
    source: str = "mcf"         # which site's applier produced it


class Applier:
    def __init__(
        self, page: Page, screenshots_dir: Path, resume_name: str = "", dry_run: bool = True,
        step_timeout_ms: int = STEP_TIMEOUT_MS,
    ) -> None:
        self.page = page
        self.shots = Path(screenshots_dir)
        self.resume_name = resume_name.strip()
        self.dry_run = dry_run
        self.timeout = step_timeout_ms
        self._created: list[dict] = []
        page.on("response", self._on_response)

    def apply(
        self, uuid: str, url: str, questions: list[str], answers: list[str | None],
    ) -> Outcome:
        self._uuid = uuid
        self._created.clear()
        try:
            return self._apply(url, questions, answers)
        except (PlaywrightTimeout, PlaywrightError) as e:
            return self._fail(f"browser: {str(e).splitlines()[0][:200]}")

    # --- flow -----------------------------------------------------------------------------

    def _apply(self, url: str, questions: list[str], answers: list[str | None]) -> Outcome:
        if None in answers:
            return Outcome(Q.FAILED, "a screening question has no saved answer")
        page = self.page
        page.goto(url, wait_until="domcontentloaded")
        closed = "|".join(re.escape(t) for t in S.CLOSED_TEXTS)
        page.locator(f"{S.APPLY_BUTTON}, {S.LOGIN_TO_APPLY}, {S.CAPTCHA}").or_(
            page.get_by_text(re.compile(closed))).first.wait_for(timeout=self.timeout)

        if self._captcha():
            return Outcome(CAPTCHA, "CAPTCHA on job page", self._screenshot("captcha"))
        if page.locator(S.LOGIN_TO_APPLY).count():
            return Outcome(LOGGED_OUT, "MCF session expired; run `mcf-apply login`")
        button = page.locator(S.APPLY_BUTTON)
        if not button.count():
            return Outcome(Q.CLOSED, "applications closed")
        label = button.first.inner_text().strip()
        if S.CAREERS_GOV_TEXT in label:
            return Outcome(Q.EXTERNAL, "apply on Careers@Gov")
        if label == S.APPLIED_TEXT:
            return Outcome(Q.ALREADY_APPLIED, "MCF shows this job as applied")
        button.first.click()

        for _ in range(MAX_STEPS):
            state = self._wait_for_step()
            if state == "success":
                return self._success()
            if state == "not_permitted":
                return Outcome(Q.CLOSED, "MCF says the job is closed or already applied",
                               self._screenshot("not-permitted"))
            if state == "captcha":
                return Outcome(CAPTCHA, "CAPTCHA during application", self._screenshot("captcha"))
            if state == "error":
                return self._fail("MCF showed an error banner: "
                                  + page.locator(S.ERROR_BANNER).first.inner_text()[:200])
            if state == "review":
                return self._review()

            header = self._header()
            if page.locator(S.QUESTION).count():
                if problem := self._answer_questions(questions, answers):
                    return self._fail(problem)
            elif S.STEP_RESUME in header and self.resume_name:
                card = page.get_by_text(self.resume_name, exact=False)
                if not card.count():
                    return self._fail(f"resume '{self.resume_name}' not found on MCF profile")
                card.first.click()
            self._next_button().click()
            if not self._wait_for_step_change(header):
                errors = [t for t in S.FIELD_ERRORS if page.get_by_text(t).count()]
                why = (f"MCF wants more details on the '{header}' step" if errors
                       else f"the '{header}' step did not move on")
                return self._fail(f"{why}; finish this one by hand")
        return self._fail(f"did not reach the review page in {MAX_STEPS} steps")

    def _wait_for_step_change(self, previous_header: str) -> bool:
        """After Next: wait for a new step or end state. False if MCF kept us on the step."""
        page = self.page
        terminal = ", ".join([S.SUCCESS, S.NOT_PERMITTED, S.ERROR_BANNER, S.SUBMIT_BUTTON,
                              S.CAPTCHA])
        waited, error_seen_ms = 0, 0
        while waited < self.timeout:
            page.wait_for_timeout(250)
            waited += 250
            if page.locator(terminal).count() or self._header() != previous_header:
                return True
            if any(page.get_by_text(t).count() for t in S.FIELD_ERRORS):
                error_seen_ms += 250
                if error_seen_ms >= 1000:   # a validation message that stays = needs input
                    return False
        return False

    def _wait_for_step(self) -> str:
        """Wait until the page settles on something we recognise, and say what it is."""
        page = self.page
        page.locator(", ".join([S.SUCCESS, S.NOT_PERMITTED, S.ERROR_BANNER, S.SUBMIT_BUTTON,
                                S.QUESTION, S.STEP_HEADER, S.CAPTCHA])).first.wait_for(
            timeout=self.timeout)
        try:   # let the step finish rendering; MCF's analytics can keep the network busy
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeout:
            pass
        for state, selector in (("success", S.SUCCESS), ("not_permitted", S.NOT_PERMITTED),
                                ("captcha", S.CAPTCHA), ("error", S.ERROR_BANNER),
                                ("review", S.SUBMIT_BUTTON)):
            if page.locator(selector).count() and page.locator(selector).first.is_visible():
                return state
        return "step"

    def _answer_questions(self, questions: list[str], answers: list[str | None]) -> str | None:
        rows = self.page.locator(S.QUESTION)
        if rows.count() != len(questions):
            return (f"MCF shows {rows.count()} screening questions but {len(questions)} were "
                    "saved; re-approve this job")
        for i in range(rows.count()):
            # Guard against answering a different question than the one you answered.
            if normalise(questions[i]) not in normalise(rows.nth(i).inner_text()):
                return f"screening question {i + 1} differs from the saved one; re-approve"
            self.page.locator(S.ANSWER_RADIO.format(index=i, answer=answers[i])).check()
        return None

    def _review(self) -> Outcome:
        shot = self._screenshot("review")
        if self.dry_run:
            return Outcome(Q.DRY_RUN_OK, "stopped at the review page (dry run)", shot)
        page = self.page
        page.locator(S.SUBMIT_BUTTON).click()
        try:
            page.locator(f"{S.SUCCESS}, {S.ERROR_BANNER}").first.wait_for(timeout=self.timeout)
        except PlaywrightTimeout:
            pass
        if page.locator(S.SUCCESS).count() or self._created:
            # The mutation's response counts even if the page didn't update.
            return self._success()
        if page.locator(S.ERROR_BANNER).count():
            return self._fail("MCF showed an error banner after Submit: "
                              + page.locator(S.ERROR_BANNER).first.inner_text()[:200])
        return self._fail("no confirmation after Submit; check MCF before retrying")

    def _success(self) -> Outcome:
        app_id = next((c.get("id") for c in self._created if c.get("id")), None)
        return Outcome(Q.APPLIED, "submitted", self._screenshot("submitted"), app_id)

    # --- helpers --------------------------------------------------------------------------

    def _next_button(self):
        page = self.page
        return page.locator(S.NEXT_BUTTON_CSS).or_(
            page.get_by_role("button", name=re.compile(S.NEXT_BUTTON_NAME))).first

    def _header(self) -> str:
        loc = self.page.locator(S.STEP_HEADER)
        return loc.first.inner_text().strip() if loc.count() else ""

    def _captcha(self) -> bool:
        return self.page.locator(S.CAPTCHA).count() > 0

    def _on_response(self, response: Response) -> None:
        try:
            body = response.request.post_data or ""
            if S.CREATE_APPLICATION_MUTATION not in body:
                return
            data = (response.json() or {}).get("data") or {}
            if created := data.get(S.CREATE_APPLICATION_MUTATION):
                self._created.append(created)
        except Exception:   # non-JSON or aborted response: the page text is the fallback
            pass

    def _screenshot(self, label: str) -> str | None:
        try:
            self.shots.mkdir(parents=True, exist_ok=True)
            stem = f"{datetime.now():%Y%m%d-%H%M%S}-{self._uuid[:8]}-{label}"
            path = self.shots / f"{stem}.png"
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except PlaywrightError:
            return None

    def _fail(self, detail: str) -> Outcome:
        shot = self._screenshot("failed")
        if shot:   # keep the HTML too, for fixing selectors
            try:
                Path(shot).with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
            except PlaywrightError:
                pass
        return Outcome(Q.FAILED, detail, shot)
