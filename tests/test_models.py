from mcf_autoapply.models import Job


def test_parses_api_record(job_factory):
    job = Job.from_api(job_factory())
    assert job.title == "Software Engineer"
    assert job.company_name == "ACME PTE. LTD."
    assert job.employment_types == ["Full Time"]
    assert job.skills == ["Python", "AWS"]
    assert str(job.posted_at) == "2026-10-10"
    assert job.is_agency is False


def test_prefers_hiring_company_and_flags_agency(job_factory):
    job = Job.from_api(job_factory(
        postedCompany={"name": "RECRUITERS PTE LTD", "uen": "R1", "ssicCode2020": "78104"},
        hiringCompany={"name": "REAL EMPLOYER PTE LTD", "uen": "E1"},
    ))
    assert job.company_name == "REAL EMPLOYER PTE LTD"
    assert job.company_uen == "E1"
    assert job.is_agency is True


def test_monthly_salary_conversion(job_factory):
    annual = Job.from_api(job_factory(salary={"minimum": 60000, "maximum": 120000,
                                              "type": {"salaryType": "Annual"}}))
    assert annual.monthly_salary_max() == 10000
    hidden = Job.from_api(job_factory(metadata__isHideSalary=True))
    assert hidden.monthly_salary_max() is None


def test_description_text_strips_html(job_factory):
    text = Job.from_api(job_factory()).description_text()
    assert "<" not in text
    assert "• Python" in text
