from datetime import date

import httpx
import pytest
import respx

from mcf_autoapply.config import Config, load_config
from mcf_autoapply.db import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_NEW, Database
from mcf_autoapply.mcf.client import API_BASE, MCFClient
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline.discover import apply_filters, discover

TODAY = date(2026, 10, 11)


def make_config(**filters) -> Config:
    return Config.model_validate({
        "db_path": ":memory:",
        "searches": [{"query": "software engineer"}],
        "filters": {"max_age_days": 14, **filters},
        "discovery": {"page_size": 2, "max_pages": 5, "requests_per_second": 5},
    })


def mock_pages(router, pages):
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"results": pages[page] if page < len(pages) else []})
    return router.get(f"{API_BASE}/v2/jobs").mock(side_effect=handler)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def test_discover_stores_and_filters(job_factory, db):
    pages = [
        [job_factory(title="Software Engineer"), job_factory(title="Sales Engineer")],
        [job_factory(title="Backend Developer")],
    ]
    with respx.mock as router:
        route = mock_pages(router, pages)
        with MCFClient(requests_per_second=50) as client:
            result = discover(make_config(title_exclude_keywords=["sales"]), db, client, TODAY)
    assert route.call_count == 2           # short second page ends paging
    assert result.terms[0].fetched == 3 and result.terms[0].new == 3
    assert len(result.new_passed) == 2
    assert db.status_counts() == {STATUS_NEW: 2, STATUS_FILTERED: 1}


def test_discover_stops_at_known_or_old_pages(job_factory, db):
    known = [job_factory(), job_factory()]
    with respx.mock as router:
        mock_pages(router, [known])
        with MCFClient(requests_per_second=50) as client:
            discover(make_config(), db, client, TODAY)

    old_page = [job_factory(), job_factory(metadata__newPostingDate="2026-09-01")]
    with respx.mock as router:
        route = mock_pages(router, [old_page, [job_factory()], [job_factory()]])
        with MCFClient(requests_per_second=50) as client:
            discover(make_config(), db, client, TODAY)
    assert route.call_count == 1           # page reached postings older than max_age_days

    with respx.mock as router:
        route = mock_pages(router, [known, [job_factory()]])
        with MCFClient(requests_per_second=50) as client:
            result = discover(make_config(), db, client, TODAY)
    assert route.call_count == 1           # nothing new on the first page
    assert result.terms[0].new == 0


def test_reposts_are_marked_duplicate(job_factory, db):
    original = job_factory(title="DevOps Engineer", metadata__newPostingDate="2026-10-05")
    repost = job_factory(title="DEVOPS ENGINEER", metadata__newPostingDate="2026-10-09")
    other_company = job_factory(title="DevOps Engineer",
                                postedCompany={"name": "OTHER", "uen": "O1", "ssicCode2020": "62011"})
    for raw in (repost, original, other_company):
        db.upsert_job(Job.from_api(raw), "devops")
    counts = apply_filters(db, make_config().filters, TODAY)
    assert counts == {STATUS_NEW: 2, STATUS_FILTERED: 0, STATUS_DUPLICATE: 1}
    assert db.get_job(repost["uuid"])["duplicate_of"] == original["uuid"]


def test_client_retries_on_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr("mcf_autoapply.mcf.client.time.sleep", sleeps.append)
    with respx.mock as router:
        route = router.get(f"{API_BASE}/v2/jobs").mock(side_effect=[
            httpx.Response(429), httpx.Response(200, json={"results": []})])
        with MCFClient(requests_per_second=50) as client:
            assert client.search_page("x", 0) == {"results": []}
    assert route.call_count == 2
    assert 2 in sleeps                     # backed off before retrying


def test_config_rejects_unknown_keys_and_resolves_db_path(tmp_path):
    (tmp_path / "config.yml").write_text("searches: [{query: x}]\nfiltres: {}\n")
    with pytest.raises(Exception, match="filtres"):
        load_config(tmp_path / "config.yml")
    (tmp_path / "config.yml").write_text("searches: [{query: x}]\ndb_path: data/j.db\n")
    assert load_config(tmp_path / "config.yml").db_path == tmp_path / "data" / "j.db"
