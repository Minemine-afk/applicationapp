"""SQLite storage for jobs and the event log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcf_autoapply.models import Job

# Job states for phase 1. Later phases add scored, approved, applied, etc. (DESIGN.md §3.8).
STATUS_NEW = "new"  # passed the filters, waiting for scoring
STATUS_FILTERED = "filtered_out"
STATUS_DUPLICATE = "duplicate"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uuid             TEXT PRIMARY KEY,
    job_post_id      TEXT,
    title            TEXT NOT NULL,
    company_name     TEXT NOT NULL,
    company_uen      TEXT,
    is_agency        INTEGER NOT NULL DEFAULT 0,
    salary_min       INTEGER,
    salary_max       INTEGER,
    salary_type      TEXT,
    employment_types TEXT NOT NULL DEFAULT '[]',
    position_levels  TEXT NOT NULL DEFAULT '[]',
    min_years_exp    INTEGER,
    skills           TEXT NOT NULL DEFAULT '[]',
    url              TEXT NOT NULL,
    posted_at        TEXT,
    expires_at       TEXT,
    source_code      TEXT,
    search_terms     TEXT NOT NULL DEFAULT '[]',
    raw              TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    status           TEXT NOT NULL,
    filter_reasons   TEXT NOT NULL DEFAULT '[]',
    duplicate_of     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_posted ON jobs (status, posted_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}'
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def existing_uuids(self, uuids: Iterable[str]) -> set[str]:
        uuids = list(uuids)
        if not uuids:
            return set()
        marks = ",".join("?" * len(uuids))
        rows = self.conn.execute(f"SELECT uuid FROM jobs WHERE uuid IN ({marks})", uuids)
        return {row["uuid"] for row in rows}

    def upsert_job(self, job: Job, search_term: str) -> bool:
        """Insert or refresh a job. Returns True if it is new. Status is set later."""
        now = _now()
        row = self.conn.execute(
            "SELECT search_terms FROM jobs WHERE uuid = ?", (job.uuid,)
        ).fetchone()
        if row:
            terms = json.loads(row["search_terms"])
            if search_term not in terms:
                terms.append(search_term)
            self.conn.execute(
                """UPDATE jobs SET raw = ?, last_seen_at = ?, search_terms = ?, posted_at = ?,
                   expires_at = ?, salary_min = ?, salary_max = ?, title = ? WHERE uuid = ?""",
                (json.dumps(job.raw), now, json.dumps(terms), _iso(job.posted_at),
                 _iso(job.expires_at), job.salary_min, job.salary_max, job.title, job.uuid),
            )
            return False
        self.conn.execute(
            """INSERT INTO jobs (uuid, job_post_id, title, company_name, company_uen, is_agency,
               salary_min, salary_max, salary_type, employment_types, position_levels,
               min_years_exp, skills, url, posted_at, expires_at, source_code, search_terms,
               raw, first_seen_at, last_seen_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.uuid, job.job_post_id, job.title, job.company_name, job.company_uen,
             int(job.is_agency), job.salary_min, job.salary_max, job.salary_type,
             json.dumps(job.employment_types), json.dumps(job.position_levels),
             job.min_years_exp, json.dumps(job.skills), job.url, _iso(job.posted_at),
             _iso(job.expires_at), job.source_code, json.dumps([search_term]),
             json.dumps(job.raw), now, now, STATUS_NEW),
        )
        self.log(job.uuid, "discovered", {"search_term": search_term})
        return True

    def set_filter_result(
        self, uuid: str, status: str, reasons: list[str], duplicate_of: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, filter_reasons = ?, duplicate_of = ? WHERE uuid = ?",
            (status, json.dumps(reasons), duplicate_of, uuid),
        )

    def jobs(
        self, statuses: Iterable[str] | None = None, limit: int | None = None,
        search: str | None = None,
    ) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if statuses:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if search:
            sql += " AND (title LIKE ? OR company_name LIKE ?)"
            args += [f"%{search}%"] * 2
        sql += " ORDER BY posted_at DESC, first_seen_at DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def get_job(self, uuid_prefix: str) -> sqlite3.Row | None:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE uuid LIKE ? LIMIT 2", (f"{uuid_prefix}%",)
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    def reason_counts(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """SELECT j.value reason, COUNT(*) n FROM jobs, json_each(jobs.filter_reasons) j
               WHERE status = ? GROUP BY reason ORDER BY n DESC""",
            (STATUS_FILTERED,),
        )
        return [(row["reason"], row["n"]) for row in rows]

    def log(self, job_uuid: str | None, type_: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (job_uuid, ts, type, payload) VALUES (?,?,?,?)",
            (job_uuid, _now(), type_, json.dumps(payload or {})),
        )

    def commit(self) -> None:
        self.conn.commit()


def _iso(d: Any) -> str | None:
    return d.isoformat() if d else None
