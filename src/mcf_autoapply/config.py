"""Load and validate config.yml."""

from __future__ import annotations

import os
import sys
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = "config.yml"


class _Strict(BaseModel):
    # Reject unknown keys so a typo in config.yml fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")


SOURCE_NAMES = ("mcf", "jobstreet")


class Search(_Strict):
    query: str
    also: list[str] = Field(default_factory=list)
    # Which sites to search. Leave it out to search every enabled site (MCF, plus JobStreet
    # when jobstreet.enabled is true); set it to restrict a search to some sites.
    sources: list[Literal["mcf", "jobstreet"]] | None = None

    def terms(self) -> list[str]:
        return [self.query, *self.also]


class Filters(_Strict):
    max_age_days: int = 14
    salary_min_monthly: int | None = None
    max_years_experience_required: int | None = None
    employment_types: list[str] = Field(default_factory=list)
    position_levels: list[str] = Field(default_factory=list)
    title_include_keywords: list[str] = Field(default_factory=list)
    title_exclude_keywords: list[str] = Field(default_factory=list)
    seniority_keywords: list[str] = Field(default_factory=list)
    company_blocklist: list[str] = Field(default_factory=list)
    exclude_agencies: bool = False
    skip_duplicate_postings: bool = True


class Profile(_Strict):
    skills: list[str] = Field(default_factory=list)


class Ranking(_Strict):
    entry_level_keywords: list[str] = Field(default_factory=list)
    entry_level_weight: float = Field(0.6, ge=0, le=1)
    skills_for_full_marks: int = Field(6, ge=1)


class Apply(_Strict):
    # dry_run: go through the whole form, screenshot the review page, never submit.
    # manual:  submit only jobs you approve with `mcf-apply approve`.
    # auto:    also approve jobs at or above auto_apply_min_score automatically.
    mode: Literal["dry_run", "manual", "auto"] = "dry_run"
    auto_apply_min_score: int = Field(70, ge=0, le=100)
    auto_apply_fresh_grad_only: bool = False
    # MCF jobs must mention at least this many of your skills to be auto-queued or sent as a
    # card (a 1-year-experience job with none of your skills still scores 20 on fit alone).
    # JobStreet listings carry only a short teaser, so this doesn't apply to them.
    min_matched_skills: int = Field(1, ge=0)
    # Part of the file name of the resume on your MCF profile to attach. Empty = MCF's default.
    resume_name: str = ""
    answers_path: Path = Path("./answers.yml")
    daily_cap: int = Field(340, ge=1)
    hourly_cap: int = Field(40, ge=1)
    # Daily cap steps up one level per clean week; restarts from the first level after a pause.
    ramp: list[int] = Field(default_factory=lambda: [170, 255, 340])
    # When submissions may happen. The same start and end (e.g. ["00:00", "00:00"]) means any
    # time of day; an end before the start (["20:00", "02:00"]) runs past midnight.
    active_hours: tuple[time, time] = (time(0, 0), time(0, 0))
    timezone: str = "Asia/Singapore"
    delay_seconds: tuple[float, float] = (45, 120)
    max_failures_per_hour: int = Field(3, ge=1)   # per site
    browser_profile: Path = Path("./data/browser-profile")
    browser_executable: Path | None = None
    # Prefer the real Google Chrome installed on the PC ("chrome"; "msedge" also works) over
    # Playwright's bundled Chromium: sites' bot checks (reCAPTCHA, Cloudflare) trust it far more.
    # Falls back to the bundled browser if it isn't installed. "" disables.
    browser_channel: str = "chrome"
    headless: bool = False
    screenshots_dir: Path = Path("./data/screenshots")


class Telegram(_Strict):
    enabled: bool = True   # still needs the bot token (`mcf-apply setup`)
    # The bot token is a secret: it is read from this environment variable, never from config.
    token_env: str = "TELEGRAM_BOT_TOKEN"
    # Set by `mcf-apply telegram-setup`; only this chat can press buttons.
    chat_id: int | None = None
    # Jobs scoring from here up to apply.auto_apply_min_score (or any score >= this in
    # manual mode) are sent to your phone with Apply / Skip buttons.
    quick_review_min_score: int = Field(60, ge=0, le=100)
    max_cards_per_run: int = Field(8, ge=0)
    alert_every_hours: float = Field(6, gt=0)   # don't repeat the same alert more often


class JobStreet(_Strict):
    """JobStreet (sg.jobstreet.com). Off by default: its robots.txt disallows the search API and
    its terms forbid automated applications, so an account doing this can be suspended."""
    enabled: bool = False
    site_key: str = "SG-Main"
    where: str = "Singapore"
    page_size: int = Field(30, ge=1, le=100)
    max_pages: int = Field(3, ge=1)
    requests_per_second: float = Field(0.5, gt=0, le=2)
    hourly_cap: int = Field(20, ge=1)
    daily_cap: int = Field(170, ge=1)
    allow_auto: bool = False          # never auto-approve JobStreet jobs until this is true
    # JobStreet listings carry no skills list or years-of-experience field, so they score lower
    # than MCF ones; auto mode uses this lower bar for them (when allow_auto is on).
    auto_min_score: int = Field(20, ge=0, le=100)
    # JobStreet searches and applies on its own schedule, independent of MCF's runs.
    interval_minutes: int = Field(60, ge=15)
    resume_name: str = ""             # part of the saved-resume name on JobStreet; "" = first
    # JobStreet's bot check passes far more often in a visible browser than a hidden one, so
    # runs that include JobStreet jobs open a window on the PC unless this is true.
    headless: bool = False


class Serve(_Strict):
    """`mcf-apply serve`: the always-on mode for a home machine."""
    interval_minutes: int = Field(60, ge=15)
    # Where you can see the machine's browser to scan the Singpass QR code, e.g. the
    # Docker setup's http://<machine-ip>:6080/vnc.html. Included in login alerts.
    login_view_url: str = ""


class Discovery(_Strict):
    page_size: int = Field(100, ge=1, le=100)
    max_pages: int = Field(5, ge=1)
    requests_per_second: float = Field(2.0, gt=0, le=5)


class Config(_Strict):
    db_path: Path = Path("./data/jobs.db")
    profile: Profile = Field(default_factory=Profile)
    searches: list[Search]
    filters: Filters = Field(default_factory=Filters)
    ranking: Ranking = Field(default_factory=Ranking)
    apply: Apply = Field(default_factory=Apply)
    telegram: Telegram = Field(default_factory=Telegram)
    serve: Serve = Field(default_factory=Serve)
    jobstreet: JobStreet = Field(default_factory=JobStreet)
    project_dir: Path = Path(".")     # set by load_config: where config.yml lives (the checkout)

    def enabled_sources(self) -> list[str]:
        return ["mcf"] + (["jobstreet"] if self.jobstreet.enabled else [])
    discovery: Discovery = Field(default_factory=Discovery)


def get_env(name: str) -> str | None:
    """An environment variable, or on Windows the value `setx` saved for your user.

    `setx` only reaches windows opened afterwards, so read the saved value directly too.
    """
    if value := os.environ.get(name):
        return value
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value) or None
        except OSError:
            return None
    return None


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("MCF_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yml to config.yml and edit it."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)
    # Resolve relative paths against the config file, not the current directory.
    base = path.parent
    cfg.project_dir = base.resolve()
    cfg.db_path = _resolve(base, cfg.db_path)
    for name in ("answers_path", "browser_profile", "browser_executable", "screenshots_dir"):
        setattr(cfg.apply, name, _resolve(base, getattr(cfg.apply, name)))
    return cfg


def _resolve(base: Path, p: Path | None) -> Path | None:
    if p is None or str(p) == ":memory:" or p.is_absolute():
        return p
    return (base / p).resolve()
