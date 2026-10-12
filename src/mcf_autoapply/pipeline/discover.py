"""Discover jobs from saved searches, store them, and apply the filter rules."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from mcf_autoapply.config import Config
from mcf_autoapply.db import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_NEW, Database
from mcf_autoapply.models import job_from_row
from mcf_autoapply.pipeline.filter import JobFilter, duplicate_key
from mcf_autoapply.pipeline.score import Ranker
from mcf_autoapply.sources import PRIORITY, Source, close_sources, open_sources

# Only phase-1 states are re-filtered; jobs that moved further along are left alone.
FILTERABLE_STATUSES = (STATUS_NEW, STATUS_FILTERED, STATUS_DUPLICATE)


@dataclass
class TermResult:
    term: str
    fetched: int = 0
    new: int = 0
    source: str = "mcf"


@dataclass
class DiscoveryResult:
    terms: list[TermResult] = field(default_factory=list)
    new_uuids: set[str] = field(default_factory=set)
    new_passed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # e.g. a site blocking searches


def discover(
    cfg: Config, db: Database, client=None, today: date | None = None,
    sources: dict[str, Source] | None = None, should_stop: Callable[[], bool] | None = None,
) -> DiscoveryResult:
    """Search every enabled site for every saved search, store new jobs, then filter.

    `client` is an MCF client (kept for older callers); `sources` overrides the registry.
    `should_stop()` is checked between search terms so a Telegram command can take over.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=cfg.filters.max_age_days)
    result = DiscoveryResult()
    own = sources is None
    sources = sources or open_sources(cfg, db, mcf_client=client)
    enabled = set(cfg.enabled_sources()) | set(sources)
    skip: set[str] = set()   # sources that failed this run (e.g. blocked)

    try:
        for search in cfg.searches:
            for name in search.sources or cfg.enabled_sources():
                src = sources.get(name)
                if src is None or name not in enabled or name in skip:
                    continue
                for term in search.terms():
                    if should_stop and should_stop():
                        result.problems.append("search interrupted by your Telegram command")
                        skip.update(sources)
                        break
                    tr = TermResult(term, source=name)
                    result.terms.append(tr)
                    try:
                        _search_term(db, src, term, tr, result, cutoff)
                    except Exception as e:   # one site's trouble must not stop the others
                        result.problems.append(f"{name}: {e}")
                        skip.add(name)
                        break
    finally:
        if own:
            close_sources(sources)

    apply_filters(db, cfg, today)
    passed = {row["uuid"] for row in db.jobs([STATUS_NEW])}
    result.new_passed = sorted(result.new_uuids & passed)
    db.log(None, "discovery_run", {
        "terms": [tr.__dict__ for tr in result.terms],
        "new": len(result.new_uuids),
        "new_passed": len(result.new_passed),
    })
    db.commit()
    return result


def _search_term(db: Database, src: Source, term: str, tr: TermResult,
                 result: DiscoveryResult, cutoff: date) -> None:
    for page in src.search(term):
        jobs = [src.to_job(r) for r in page]
        known = db.existing_uuids(j.uuid for j in jobs)
        for job in jobs:
            tr.fetched += 1
            if db.upsert_job(job, term):
                tr.new += 1
                result.new_uuids.add(job.uuid)
        db.commit()
        # Results are newest first, so once a page has nothing new, or reaches postings
        # older than the cutoff, later pages only hold older jobs.
        if len(known) == len(page):
            break
        if any(j.posted_at and j.posted_at < cutoff for j in jobs):
            break


def apply_filters(db: Database, cfg: Config, today: date | None = None) -> dict[str, int]:
    """(Re-)apply filters and ranking to every stored job that hasn't moved past filtering."""
    rules = cfg.filters
    job_filter = JobFilter(rules, today, cfg.ranking.entry_level_keywords)
    ranker = Ranker(cfg.profile, cfg.ranking)
    rows = db.jobs(FILTERABLE_STATUSES)
    # The first row for an opening is kept and later ones become duplicates: prefer the site
    # with the reliable apply flow (MCF), then the oldest posting.
    rows = sorted(rows, key=lambda r: (PRIORITY.get(r["source"], 9), r["posted_at"] or "",
                                       r["first_seen_at"]))
    seen: dict[str, str] = {}
    counts = {STATUS_NEW: 0, STATUS_FILTERED: 0, STATUS_DUPLICATE: 0}

    for row in rows:
        job = job_from_row(row)
        reasons = job_filter.reasons(job)
        rank = None
        if reasons:
            status, dup_of = STATUS_FILTERED, None
        else:
            key = duplicate_key(job)
            dup_of = seen.get(key) if rules.skip_duplicate_postings else None
            if dup_of:
                status, reasons = STATUS_DUPLICATE, ["duplicate posting"]
            else:
                status = STATUS_NEW
                seen[key] = job.uuid
                rank = ranker.rank(job)
        if (status, json.dumps(reasons)) != (row["status"], row["filter_reasons"]):
            db.log(job.uuid, "filtered", {"status": status, "reasons": reasons})
        db.set_filter_result(
            job.uuid, status, reasons, dup_of,
            rank.score if rank else None, rank.to_dict() if rank else None,
        )
        counts[status] += 1

    db.commit()
    return counts
