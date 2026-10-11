"""Discover jobs from saved searches, store them, and apply the filter rules."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from mcf_autoapply.config import Config
from mcf_autoapply.db import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_NEW, Database
from mcf_autoapply.mcf.client import MCFClient
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline.filter import JobFilter, duplicate_key
from mcf_autoapply.pipeline.score import Ranker

# Only phase-1 states are re-filtered; jobs that moved further along are left alone.
FILTERABLE_STATUSES = (STATUS_NEW, STATUS_FILTERED, STATUS_DUPLICATE)


@dataclass
class TermResult:
    term: str
    fetched: int = 0
    new: int = 0


@dataclass
class DiscoveryResult:
    terms: list[TermResult] = field(default_factory=list)
    new_uuids: set[str] = field(default_factory=set)
    new_passed: list[str] = field(default_factory=list)


def discover(
    cfg: Config, db: Database, client: MCFClient, today: date | None = None
) -> DiscoveryResult:
    today = today or date.today()
    cutoff = today - timedelta(days=cfg.filters.max_age_days)
    result = DiscoveryResult()

    for search in cfg.searches:
        for term in search.terms():
            tr = TermResult(term)
            result.terms.append(tr)
            for page in client.search(
                term, limit=cfg.discovery.page_size, max_pages=cfg.discovery.max_pages
            ):
                known = db.existing_uuids(r["uuid"] for r in page)
                jobs = [Job.from_api(r) for r in page]
                for job in jobs:
                    tr.fetched += 1
                    if db.upsert_job(job, term):
                        tr.new += 1
                        result.new_uuids.add(job.uuid)
                db.commit()
                # Results are newest first, so once a page has nothing new, or reaches
                # postings older than the cutoff, later pages only hold older jobs.
                if len(known) == len(page):
                    break
                if any(j.posted_at and j.posted_at < cutoff for j in jobs):
                    break

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


def apply_filters(db: Database, cfg: Config, today: date | None = None) -> dict[str, int]:
    """(Re-)apply filters and ranking to every stored job that hasn't moved past filtering."""
    rules = cfg.filters
    job_filter = JobFilter(rules, today, cfg.ranking.entry_level_keywords)
    ranker = Ranker(cfg.profile, cfg.ranking)
    rows = db.jobs(FILTERABLE_STATUSES)
    # Oldest posting first, so the original posting is kept and later reposts are duplicates.
    rows = sorted(rows, key=lambda r: (r["posted_at"] or "", r["first_seen_at"]))
    seen: dict[str, str] = {}
    counts = {STATUS_NEW: 0, STATUS_FILTERED: 0, STATUS_DUPLICATE: 0}

    for row in rows:
        job = Job.from_api(json.loads(row["raw"]))
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
