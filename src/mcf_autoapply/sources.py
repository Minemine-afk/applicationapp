"""The job sites, behind one small interface. Everything else in the app is site-independent.

A source knows how to search (a client yielding pages of raw records), how to turn a record
into a Job, and whether an employer's screening questions can be fetched before applying
(MCF: yes, via its public API; JobStreet: no, they only appear inside the apply form).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Callable

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.models import Job

LABELS = {"mcf": "MCF", "jobstreet": "JobStreet"}
SITE_NAMES = {"mcf": "MyCareersFuture", "jobstreet": "JobStreet"}
# When the same opening is on both sites, keep the one whose apply flow is more reliable.
PRIORITY = {"mcf": 0, "jobstreet": 1}


@dataclass
class Source:
    name: str
    search: Callable[[str], Iterator[list[dict]]]      # term -> pages of raw records
    to_job: Callable[[dict[str, Any]], Job]
    questions: Callable[[str], list[str]] | None       # job uuid -> questions, or None
    close: Callable[[], None]


def open_sources(cfg: Config, db: Database, mcf_client=None, jobstreet_client=None,
                 ) -> dict[str, Source]:
    """Sources enabled in config, with clients opened (pass your own for tests)."""
    from mcf_autoapply.mcf.client import MCFClient

    out: dict[str, Source] = {}
    mcf = mcf_client or MCFClient(cfg.discovery.requests_per_second)
    out["mcf"] = Source(
        "mcf",
        search=lambda term: mcf.search(term, limit=cfg.discovery.page_size,
                                       max_pages=cfg.discovery.max_pages),
        to_job=Job.from_mcf,
        questions=mcf.screening_questions,
        close=mcf.close if mcf_client is None else (lambda: None),
    )
    if cfg.jobstreet.enabled or jobstreet_client is not None:
        from mcf_autoapply.jobstreet.client import JobStreetClient

        js = jobstreet_client or JobStreetClient(cfg.jobstreet, db)
        out["jobstreet"] = Source(
            "jobstreet",
            search=js.search,
            to_job=Job.from_jobstreet,
            questions=None,
            close=js.close if jobstreet_client is None else (lambda: None),
        )
    return out


def close_sources(sources: dict[str, Source]) -> None:
    for s in sources.values():
        s.close()


def source_of(uuid: str) -> str:
    return "jobstreet" if uuid.startswith("js:") else "mcf"


def short_id(uuid: str) -> str:
    """What to show as a job's ID: MCF uuids are long (first 8 chars are unique enough);
    JobStreet ids are short already and share prefixes, so show them whole."""
    return uuid if uuid.startswith("js:") else uuid[:8]
