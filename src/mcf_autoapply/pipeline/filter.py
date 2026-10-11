"""Deterministic filter rules. Cheap, runs before any scoring."""

from __future__ import annotations

import re
from datetime import date, timedelta

from mcf_autoapply.config import Filters
from mcf_autoapply.models import Job

OPEN_STATUSES = {"Open", "Re-open"}


def keyword_pattern(keywords: list[str]) -> re.Pattern | None:
    keywords = [k.strip() for k in keywords if k.strip()]
    if not keywords:
        return None
    # Whole-word match so "ai" doesn't hit "maintenance" and "intern" doesn't hit "internal".
    # Longest first so "data engineering" wins over "data engineer" in reported matches.
    alternatives = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"(?<![\w&+#])(?:{alternatives})(?![\w&+#])", re.IGNORECASE)


def duplicate_key(job: Job) -> str:
    """Same employer + same normalised title = the same opening posted again.

    Compares company *names* (normalised), not registration numbers, so the same opening on
    two sites matches; a name like "ACME PTE. LTD." and "Acme Pte Ltd" matches too.
    """
    company = _norm(job.company_name) or (job.company_uen or "").lower()
    return f"{company}|{_norm(job.title)}"


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return re.sub(r"\b(pte|ltd|limited|llp|inc|co|company|singapore|branch)\b", "", text).strip()


class JobFilter:
    def __init__(
        self, rules: Filters, today: date | None = None, entry_level_keywords: list[str] = (),
    ) -> None:
        self.rules = rules
        self.today = today or date.today()
        self._include = keyword_pattern(rules.title_include_keywords)
        self._exclude = keyword_pattern(rules.title_exclude_keywords)
        self._seniority = keyword_pattern(rules.seniority_keywords)
        self._entry = keyword_pattern(list(entry_level_keywords))
        self._employment = {t.lower() for t in rules.employment_types}
        self._levels = {lvl.lower() for lvl in rules.position_levels}
        self._blocklist = [b.lower() for b in rules.company_blocklist]

    def reasons(self, job: Job) -> list[str]:
        """Every rule the job fails. An empty list means the job passes."""
        r = self.rules
        out: list[str] = []

        if job.status not in OPEN_STATUSES:
            out.append(f"status: {job.status or 'unknown'}")
        if job.expires_at and job.expires_at < self.today:
            out.append("expired")
        if job.posted_at and job.posted_at < self.today - timedelta(days=r.max_age_days):
            out.append(f"older than {r.max_age_days} days")

        if self._include and not self._include.search(job.title):
            out.append("title: no target keyword")
        if self._exclude and (m := self._exclude.search(job.title)):
            out.append(f"title: excluded keyword '{m.group(0).lower()}'")
        if self._seniority and (m := self._seniority.search(job.title)):
            if not (self._entry and self._entry.search(job.title)):
                out.append(f"title: too senior ('{m.group(0).lower()}')")

        monthly_max = job.monthly_salary_max()
        if r.salary_min_monthly and monthly_max is not None and monthly_max < r.salary_min_monthly:
            out.append(f"salary: max {monthly_max:,.0f} < {r.salary_min_monthly:,}")

        if (
            r.max_years_experience_required is not None
            and job.min_years_exp is not None
            and job.min_years_exp > r.max_years_experience_required
        ):
            out.append(f"experience: needs {job.min_years_exp}+ years")

        if self._employment and job.employment_types:
            if not self._employment & {t.lower() for t in job.employment_types}:
                out.append(f"employment type: {', '.join(job.employment_types)}")
        if self._levels and job.position_levels:
            if not self._levels & {p.lower() for p in job.position_levels}:
                out.append(f"position level: {', '.join(job.position_levels)}")

        name = job.company_name.lower()
        uen = (job.company_uen or "").lower()
        if any(b == uen or b in name for b in self._blocklist):
            out.append("company: blocklisted")
        if r.exclude_agencies and job.is_agency:
            out.append("company: recruitment agency")

        return out
