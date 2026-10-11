"""JobStreet as a second source: client, record mapping, multi-source discovery, gating."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.jobstreet.client import API_URL, BLOCKED_KEY, JobStreetBlocked, JobStreetClient
from mcf_autoapply.models import Job, job_from_row, parse_salary_label
from mcf_autoapply.pipeline import queue as Q
from mcf_autoapply.pipeline.answers import AnswerBank
from mcf_autoapply.pipeline.discover import apply_filters, discover
from mcf_autoapply.sources import Source, short_id, source_of

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "jobstreet_search.json").read_text())
TODAY = date(2026, 10, 11)


def make_cfg(tmp_path, **jobstreet):
    return Config.model_validate({
        "db_path": ":memory:",
        "searches": [{"query": "software engineer", "sources": ["mcf", "jobstreet"]}],
        "filters": {"max_age_days": 14},
        "apply": {"mode": "manual", "answers_path": str(tmp_path / "answers.yml")},
        "jobstreet": {"enabled": True, "page_size": 3, "max_pages": 3,
                      "requests_per_second": 2, **jobstreet},
    })


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


# --- mapping -------------------------------------------------------------------------------

def test_record_maps_to_job():
    job = Job.from_jobstreet(FIXTURE["data"][0])
    assert job.uuid == "js:95188714" and job.source == "jobstreet"
    assert job.title == "Junior QA Engineer (Electronics Manufacturing)"
    assert job.company_name == "Search Personnel" and job.is_agency is True
    assert (job.salary_min, job.salary_max, job.salary_type) == (3500, 4500, "Monthly")
    assert job.monthly_salary_max() == 4500
    assert job.employment_types == ["Full Time"]           # MCF's spelling, for the filter
    assert Job.from_jobstreet(dict(FIXTURE["data"][0], workTypes=["Contract/Temp"])
                              ).employment_types == ["Contract"]
    assert job.posted_at == date(2026, 10, 11) and job.min_years_exp is None
    assert job.url == "https://sg.jobstreet.com/job/95188714"
    assert job.description_text().startswith(FIXTURE["data"][0]["teaser"][:20])


@pytest.mark.parametrize("label, expected", [
    ("$3,500 – $4,500 per month", (3500, 4500, "Monthly")),
    ("$60,000 – $80,000 per year", (60000, 80000, "Annual")),
    ("$25 – $30 per hour", (25, 30, "Hourly")),
    ("$5k - $7k per month", (5000, 7000, "Monthly")),
    (None, (None, None, None)),
    ("Competitive", (None, None, None)),
])
def test_salary_label_parsing(label, expected):
    assert parse_salary_label(label) == expected


def test_job_from_row_dispatches_on_source(db, job_factory):
    db.upsert_job(Job.from_jobstreet(FIXTURE["data"][1]), "x")
    db.upsert_job(Job.from_mcf(job_factory()), "x")
    rows = {r["source"]: r for r in db.jobs()}
    assert job_from_row(rows["jobstreet"]).source == "jobstreet"
    assert job_from_row(rows["mcf"]).source == "mcf"
    assert short_id("js:95188714") == "js:95188714" and len(short_id("a" * 32)) == 8
    assert source_of("js:1") == "jobstreet" and source_of("a" * 32) == "mcf"


# --- client --------------------------------------------------------------------------------

def mock_search(router, pages, status=200):
    def handler(request):
        page = int(request.url.params["page"])
        if status != 200:
            return httpx.Response(status)
        data = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(200, json={"data": data, "totalCount": sum(map(len, pages))})
    return router.get(API_URL).mock(side_effect=handler)


def test_client_pages_and_stops_on_a_short_page(tmp_path, db):
    cfg = make_cfg(tmp_path)
    recs = FIXTURE["data"]
    with respx.mock as router:
        route = mock_search(router, [recs[:3], recs[3:5]])
        with JobStreetClient(cfg.jobstreet, db) as client:
            pages = list(client.search("software engineer"))
    assert [len(p) for p in pages] == [3, 2]
    assert route.call_count == 2                 # a short page ends paging
    params = route.calls[0].request.url.params
    assert params["siteKey"] == "SG-Main" and params["where"] == "Singapore"
    assert params["sortmode"] == "ListedDate" and params["pageSize"] == "3"


def test_403_blocks_searches_for_an_hour(tmp_path, db):
    cfg = make_cfg(tmp_path)
    with respx.mock as router:
        route = mock_search(router, [], status=403)
        client = JobStreetClient(cfg.jobstreet, db)
        with pytest.raises(JobStreetBlocked, match="blocking"):
            list(client.search("x"))
        with pytest.raises(JobStreetBlocked, match="paused until"):
            list(client.search("x"))             # no request made while blocked
    assert route.call_count == 1
    until = datetime.fromisoformat(db.get_state(BLOCKED_KEY))
    assert timedelta(minutes=59) < until - datetime.now(timezone.utc) <= timedelta(hours=1)


# --- discovery across sources --------------------------------------------------------------

def fake_sources(mcf_pages, js_pages, js_error=None):
    def js_search(term):
        if js_error:
            raise js_error
        yield from js_pages
    return {
        "mcf": Source("mcf", lambda term: iter(mcf_pages), Job.from_mcf, lambda u: [], lambda: None),
        "jobstreet": Source("jobstreet", js_search, Job.from_jobstreet, None, lambda: None),
    }


def test_discover_searches_both_sites_and_mcf_wins_duplicates(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path)
    cfg.filters.title_include_keywords = []
    # The same opening on both sites: MCF posted later, but it must be the one kept.
    mcf_rec = job_factory(title="Junior QA Engineer (Electronics Manufacturing)",
                          postedCompany={"name": "Search Personnel", "uen": "S1", "ssicCode2020": "78104"},
                          metadata__newPostingDate="2026-10-11")
    js_recs = [dict(r, listingDate="2026-10-09T00:00:00Z") for r in FIXTURE["data"][:2]]
    result = discover(cfg, db, today=TODAY, sources=fake_sources([[mcf_rec]], [js_recs]))
    assert {(t.source, t.fetched) for t in result.terms} == {("mcf", 1), ("jobstreet", 2)}
    assert result.problems == []
    rows = {r["uuid"]: r for r in db.jobs()}
    assert rows[mcf_rec["uuid"]]["status"] == "new"
    assert rows["js:95188714"]["status"] == "duplicate"
    assert rows["js:95188714"]["duplicate_of"] == mcf_rec["uuid"]
    assert rows["js:94584706"]["status"] == "new"      # Apple's job, only on JobStreet


def test_a_blocked_site_is_reported_and_the_other_still_runs(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path)
    result = discover(cfg, db, today=TODAY, sources=fake_sources(
        [[job_factory()]], [], js_error=JobStreetBlocked("JobStreet is blocking searches (403)")))
    assert result.problems == ["jobstreet: JobStreet is blocking searches (403)"]
    assert [t.source for t in result.terms if t.fetched] == ["mcf"]


def test_disabled_jobstreet_is_not_searched(tmp_path, db, job_factory):
    cfg = make_cfg(tmp_path, enabled=False)
    sources = fake_sources([[job_factory()]], [FIXTURE["data"]])
    del sources["jobstreet"]          # open_sources() wouldn't create it either
    result = discover(cfg, db, today=TODAY, sources=sources)
    assert [t.source for t in result.terms] == ["mcf"]


# --- approve / auto gate -------------------------------------------------------------------

class NoQuestionsClient:
    def screening_questions(self, uuid):
        raise AssertionError("MCF questions must not be fetched for a JobStreet job")


def test_jobstreet_jobs_queue_without_questions_and_are_gated_in_auto(tmp_path, db):
    cfg = make_cfg(tmp_path)
    cfg.filters.title_include_keywords, cfg.ranking.entry_level_keywords = [], ["junior"]
    for rec in FIXTURE["data"][:2]:
        db.upsert_job(Job.from_jobstreet(rec), "x")
    apply_filters(db, cfg, TODAY)
    bank = AnswerBank(cfg.apply.answers_path)
    [d] = Q.approve(db, NoQuestionsClient(), bank, ["js:95188714"])
    assert d.state == Q.APPROVED
    assert db.application("js:95188714")["questions"] == "[]"

    cfg.apply.mode, cfg.apply.auto_apply_min_score = "auto", 0
    assert Q.auto_approve(cfg, db, NoQuestionsClient(), bank) == []      # allow_auto is false
    cfg.jobstreet.allow_auto = True
    assert [d.job_uuid for d in Q.auto_approve(cfg, db, NoQuestionsClient(), bank)] == ["js:94584706"]
