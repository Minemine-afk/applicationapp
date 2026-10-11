from datetime import date

import pytest

from mcf_autoapply.config import Filters
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline.filter import JobFilter

TODAY = date(2026, 10, 11)


def reasons(raw, **rules):
    return JobFilter(Filters(**rules), today=TODAY).reasons(Job.from_api(raw))


def test_default_job_passes(job_factory):
    assert reasons(job_factory(), salary_min_monthly=5000, title_include_keywords=["software"]) == []


@pytest.mark.parametrize("title, expected", [
    ("AI Engineer", True),
    ("Maintenance Engineer", False),     # "ai" inside a word must not match
    ("Senior LLM Engineer", True),
    ("Full-Stack Developer", True),
])
def test_include_keywords_match_whole_words(job_factory, title, expected):
    r = reasons(job_factory(title=title), title_include_keywords=["ai", "llm", "full-stack"])
    assert (r == []) is expected


def test_exclude_keyword_whole_word(job_factory):
    assert reasons(job_factory(title="Software Engineer Intern"), title_exclude_keywords=["intern"])
    assert not reasons(job_factory(title="Internal Tools Engineer"), title_exclude_keywords=["intern"])


def test_salary_floor_uses_top_of_range(job_factory):
    raw = job_factory(salary={"minimum": 3000, "maximum": 4500, "type": {"salaryType": "Monthly"}})
    assert reasons(raw, salary_min_monthly=5000) == ["salary: max 4,500 < 5,000"]
    assert reasons(raw, salary_min_monthly=4500) == []


def test_hidden_salary_is_not_rejected(job_factory):
    raw = job_factory(salary={"minimum": 1, "maximum": 2, "type": {"salaryType": "Monthly"}},
                      metadata__isHideSalary=True)
    assert reasons(raw, salary_min_monthly=5000) == []


def test_experience_age_status_and_expiry(job_factory):
    assert reasons(job_factory(minimumYearsExperience=10), max_years_experience_required=8) == [
        "experience: needs 10+ years"]
    assert reasons(job_factory(metadata__newPostingDate="2026-09-01"), max_age_days=14) == [
        "older than 14 days"]
    assert reasons(job_factory(status={"jobStatus": "Closed"})) == ["status: Closed"]
    assert reasons(job_factory(status={"jobStatus": "Re-open"})) == []
    assert reasons(job_factory(metadata__expiryDate="2026-10-01")) == ["expired"]


def test_employment_type_and_level(job_factory):
    raw = job_factory(employmentTypes=[{"employmentType": "Internship/Attachment"}])
    assert reasons(raw, employment_types=["Full Time", "Permanent"]) == [
        "employment type: Internship/Attachment"]
    assert reasons(job_factory(), position_levels=["professional"]) == []


def test_company_blocklist_and_agencies(job_factory):
    assert reasons(job_factory(), company_blocklist=["acme"]) == ["company: blocklisted"]
    assert reasons(job_factory(), company_blocklist=["201900001a"]) == ["company: blocklisted"]
    agency = job_factory(postedCompany={"name": "HIRE CO", "uen": "H1", "ssicCode2020": "78104"})
    assert reasons(agency, exclude_agencies=True) == ["company: recruitment agency"]


def test_seniority_dropped_unless_title_is_also_entry_level(job_factory):
    def r(title):
        return JobFilter(Filters(seniority_keywords=["senior", "lead"]), today=TODAY,
                         entry_level_keywords=["junior"]).reasons(Job.from_api(job_factory(title=title)))
    assert r("Senior DevOps Engineer") == ["title: too senior ('senior')"]
    assert r("Junior/ Senior DevOps Engineer") == []
    assert r("DevOps Engineer") == []
