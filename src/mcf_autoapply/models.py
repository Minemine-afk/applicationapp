"""Job record, parsed from the MCF jobs API or JobStreet's search API."""

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
    source: str = "mcf"            # which site it came from: "mcf" or "jobstreet"

    @classmethod
    def from_api(cls, r: dict[str, Any]) -> Job:   # kept for older call sites
        return cls.from_mcf(r)

    @classmethod
    def from_jobstreet(cls, r: dict[str, Any]) -> Job:
        """A record from JobStreet's search API (sg.jobstreet.com/api/jobsearch/v5/search)."""
        company = (r.get("companyName") or (r.get("advertiser") or {}).get("description") or "")
        salary_min, salary_max, salary_type = parse_salary_label(r.get("salaryLabel"))
        job_id = str(r["id"])
        return cls(
            uuid=f"js:{job_id}",
            title=(r.get("title") or "").strip(),
            company_name=company.strip(),
            company_uen=None,
            is_agency=any(w in company.lower() for w in _AGENCY_WORDS),
            salary_min=salary_min, salary_max=salary_max, salary_type=salary_type,
            salary_hidden=False,
            employment_types=[_WORK_TYPES.get(w, w) for w in r.get("workTypes") or []],
            position_levels=[],
            min_years_exp=None,          # JobStreet doesn't publish this
            skills=[],
            status="Open",
            source_code="JobStreet",
            url=f"https://sg.jobstreet.com/job/{job_id}",
            posted_at=_parse_date(r.get("listingDate")),
            expires_at=None,
            job_post_id=job_id,
            raw=r,
            source="jobstreet",
        )

    @classmethod
    def from_mcf(cls, r: dict[str, Any]) -> Job:
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
        if self.source == "jobstreet":
            parts = [self.raw.get("teaser") or ""] + [f"• {b}" for b in self.raw.get("bulletPoints") or []]
            return "\n".join(p for p in parts if p).strip()
        text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", self.raw.get("description") or "")
        text = re.sub(r"<li[^>]*>", "• ", text)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        return re.sub(r"\n{3,}", "\n\n", text).strip()


# JobStreet's work types in MCF's vocabulary, so one employment_types filter covers both.
_WORK_TYPES = {"Full time": "Full Time", "Part time": "Part Time", "Contract/Temp": "Contract",
               "Casual/Vacation": "Part Time"}

_AGENCY_WORDS = ("recruit", "personnel", "staffing", "manpower", "headhunt", "talent ")

_SALARY_UNITS = {"month": "Monthly", "year": "Annual", "annum": "Annual", "hour": "Hourly",
                 "day": "Daily", "week": "Weekly"}
_MONTHLY_FACTOR["Weekly"] = 52 / 12


def parse_salary_label(label: str | None) -> tuple[int | None, int | None, str | None]:
    """'$3,500 – $4,500 per month' → (3500, 4500, 'Monthly'); '$60k per year' style too."""
    if not label:
        return None, None, None
    nums = []
    for m in re.finditer(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?", label):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        nums.append(int(round(n * (1000 if m.group(2) else 1))))
    if not nums:
        return None, None, None
    unit = next((u for word, u in _SALARY_UNITS.items() if word in label.lower()), None)
    lo, hi = min(nums), max(nums)
    return lo, hi, unit or "Monthly"


def job_from_row(row) -> Job:
    """Rebuild a Job from a jobs-table row, whichever site it came from."""
    import json

    raw = json.loads(row["raw"])
    source = row["source"] if "source" in row.keys() else "mcf"
    return Job.from_jobstreet(raw) if source == "jobstreet" else Job.from_mcf(raw)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
