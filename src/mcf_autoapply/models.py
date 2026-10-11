"""Job record parsed from the MCF jobs API."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# SSIC 781xx = employment placement agencies.
_AGENCY_SSIC_PREFIX = "781"

# Rough conversions to a monthly figure for salary filtering.
_MONTHLY_FACTOR = {
    "Monthly": 1.0,
    "Annual": 1 / 12,
    "Yearly": 1 / 12,
    "Daily": 21.75,
    "Hourly": 174.0,
}


@dataclass
class Job:
    uuid: str
    title: str
    company_name: str
    company_uen: str | None
    is_agency: bool
    salary_min: int | None
    salary_max: int | None
    salary_type: str | None
    salary_hidden: bool
    employment_types: list[str]
    position_levels: list[str]
    min_years_exp: int | None
    skills: list[str]
    status: str
    source_code: str | None
    url: str
    posted_at: date | None
    expires_at: date | None
    job_post_id: str | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, r: dict[str, Any]) -> Job:
        meta = r.get("metadata") or {}
        # Agencies post on behalf of a hiring company; prefer the real employer's name.
        posted = r.get("postedCompany") or {}
        hiring = r.get("hiringCompany") or {}
        company = hiring if hiring.get("name") else posted
        salary = r.get("salary") or {}
        ssic = str(posted.get("ssicCode2020") or posted.get("ssicCode") or "")
        return cls(
            uuid=r["uuid"],
            title=(r.get("title") or "").strip(),
            company_name=(company.get("name") or "").strip(),
            company_uen=company.get("uen"),
            is_agency=ssic.startswith(_AGENCY_SSIC_PREFIX),
            salary_min=salary.get("minimum"),
            salary_max=salary.get("maximum"),
            salary_type=(salary.get("type") or {}).get("salaryType"),
            salary_hidden=bool(meta.get("isHideSalary")),
            employment_types=[e["employmentType"] for e in r.get("employmentTypes") or []],
            position_levels=[p["position"] for p in r.get("positionLevels") or []],
            min_years_exp=r.get("minimumYearsExperience"),
            skills=[s["skill"] for s in r.get("skills") or []],
            status=(r.get("status") or {}).get("jobStatus") or "",
            source_code=r.get("sourceCode"),
            url=meta.get("jobDetailsUrl")
            or f"https://www.mycareersfuture.gov.sg/job/{r['uuid']}",
            posted_at=_parse_date(meta.get("newPostingDate")),
            expires_at=_parse_date(meta.get("expiryDate")),
            job_post_id=meta.get("jobPostId"),
            raw=r,
        )

    def monthly_salary_max(self) -> float | None:
        factor = _MONTHLY_FACTOR.get(self.salary_type or "")
        if self.salary_hidden or self.salary_max is None or factor is None:
            return None
        return self.salary_max * factor

    def description_text(self) -> str:
        text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", self.raw.get("description") or "")
        text = re.sub(r"<li[^>]*>", "• ", text)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
