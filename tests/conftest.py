from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest

_ids = itertools.count(1)

BASE_JOB: dict[str, Any] = {
    "uuid": "",
    "title": "Software Engineer",
    "description": "<p>Build <strong>things</strong>.</p><ul><li>Python</li><li>AWS</li></ul>",
    "minimumYearsExperience": 3,
    "skills": [{"skill": "Python"}, {"skill": "AWS"}],
    "employmentTypes": [{"employmentType": "Full Time"}],
    "positionLevels": [{"position": "Professional"}],
    "status": {"jobStatus": "Open"},
    "sourceCode": "Employer Portal",
    "salary": {"minimum": 6000, "maximum": 9000, "type": {"salaryType": "Monthly"}},
    "postedCompany": {"name": "ACME PTE. LTD.", "uen": "201900001A", "ssicCode2020": "62011"},
    "hiringCompany": None,
    "metadata": {
        "jobPostId": "MCF-2026-0000001",
        "newPostingDate": "2026-10-10",
        "expiryDate": "2026-11-09",
        "isHideSalary": False,
        "jobDetailsUrl": "https://www.mycareersfuture.gov.sg/job/x",
    },
}


def make_job(**overrides: Any) -> dict[str, Any]:
    """An MCF API job record with sensible defaults. Nested keys use '__', e.g. metadata__newPostingDate."""
    job = copy.deepcopy(BASE_JOB)
    job["uuid"] = f"{next(_ids):032x}"
    for key, value in overrides.items():
        target = job
        *parents, leaf = key.split("__")
        for p in parents:
            target = target[p]
        target[leaf] = value
    return job


@pytest.fixture
def job_factory():
    return make_job
