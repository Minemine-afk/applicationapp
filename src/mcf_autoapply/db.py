"""SQLite storage for jobs and the event log."""

from __future__ import annotations

import json
import re
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
    duplicate_of     TEXT,
    score            INTEGER,
    rank_details     TEXT,
    source           TEXT NOT NULL DEFAULT 'mcf'
);
CREATE INDEX IF NOT EXISTS jobs_status_posted ON jobs (status, posted_at DESC);

-- One row per job you have decided on. job_uuid as primary key = never apply twice.
CREATE TABLE IF NOT EXISTS applications (
    job_uuid      TEXT PRIMARY KEY REFERENCES jobs(uuid),
    state         TEXT NOT NULL,
    approved_by   TEXT,                       -- user | auto
    questions     TEXT NOT NULL DEFAULT '[]', -- screening questions from MCF
    answers       TEXT NOT NULL DEFAULT '[]', -- yes/no per question, null = unanswered
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    submitted_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    screenshot    TEXT,
    mcf_application_id TEXT
);
CREATE INDEX IF NOT EXISTS applications_state ON applications (state);

-- Your applications as each site lists them (synced from its applied-jobs page), including
-- ones made by hand. Jobs here, and reposts of them on any site, are never applied to again.
CREATE TABLE IF NOT EXISTS applied_on_site (
    job_uuid      TEXT PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'mcf',
    title         TEXT NOT NULL DEFAULT '',
    employer      TEXT NOT NULL DEFAULT '',
    status        TEXT,
    submitted_on  TEXT,
    mcf_application_id TEXT,
    synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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
        # serve uses one connection for Telegram and one for the background run; wait for
        # each other's short writes instead of failing with "database is locked".
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for name, type_ in (("score", "INTEGER"), ("rank_details", "TEXT"),
                            ("source", "TEXT NOT NULL DEFAULT 'mcf'")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {type_}")
        # applied_on_mcf became applied_on_site (with a source column) when JobStreet arrived.
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "applied_on_mcf" in tables:
            self.conn.execute(
                """INSERT OR IGNORE INTO applied_on_site (job_uuid, source, title, employer,
                   status, submitted_on, mcf_application_id, synced_at)
                   SELECT job_uuid, 'mcf', title, employer, status, submitted_on,
                          mcf_application_id, synced_at FROM applied_on_mcf""")
            self.conn.execute("DROP TABLE applied_on_mcf")
        self.conn.commit()

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
               raw, first_seen_at, last_seen_at, status, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.uuid, job.job_post_id, job.title, job.company_name, job.company_uen,
             int(job.is_agency), job.salary_min, job.salary_max, job.salary_type,
             json.dumps(job.employment_types), json.dumps(job.position_levels),
             job.min_years_exp, json.dumps(job.skills), job.url, _iso(job.posted_at),
             _iso(job.expires_at), job.source_code, json.dumps([search_term]),
             json.dumps(job.raw), now, now, STATUS_NEW, job.source),
        )
        self.log(job.uuid, "discovered", {"search_term": search_term, "source": job.source})
        return True

    def set_filter_result(
        self, uuid: str, status: str, reasons: list[str], duplicate_of: str | None = None,
        score: int | None = None, rank_details: dict | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE jobs SET status = ?, filter_reasons = ?, duplicate_of = ?, score = ?,
               rank_details = ? WHERE uuid = ?""",
            (status, json.dumps(reasons), duplicate_of, score,
             json.dumps(rank_details) if rank_details else None, uuid),
        )

    def jobs(
        self, statuses: Iterable[str] | None = None, limit: int | None = None,
        search: str | None = None, sort: str = "date",
    ) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if statuses:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if search:
            sql += " AND (title LIKE ? OR company_name LIKE ?)"
            args += [f"%{search}%"] * 2
        if sort == "score":
            sql += " ORDER BY score IS NULL, score DESC, posted_at DESC"
        else:
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

    # --- applications -------------------------------------------------------------------

    def application(self, job_uuid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM applications WHERE job_uuid = ?", (job_uuid,)
        ).fetchone()

    def create_application(
        self, job_uuid: str, state: str, approved_by: str | None,
        questions: list[str], answers: list[str | None], error: str | None = None,
    ) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO applications (job_uuid, state, approved_by, questions, answers,
               created_at, updated_at, error) VALUES (?,?,?,?,?,?,?,?)""",
            (job_uuid, state, approved_by, json.dumps(questions), json.dumps(answers),
             now, now, error),
        )
        self.log(job_uuid, "application_" + state, {"approved_by": approved_by})

    def update_application(self, job_uuid: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        for key in ("questions", "answers"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE applications SET {sets} WHERE job_uuid = ?", (*fields.values(), job_uuid)
        )
        if "state" in fields:
            payload = {k: v for k, v in fields.items() if k in ("error", "mcf_application_id")}
            payload["source"] = "jobstreet" if job_uuid.startswith("js:") else "mcf"
            self.log(job_uuid, "application_" + fields["state"], payload)

    def applications(
        self, states: Iterable[str] | None = None, limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Applications joined with their job, best score first."""
        sql = """SELECT a.*, j.title, j.company_name, j.url, j.score, j.posted_at, j.raw,
                        j.status AS job_status, j.source, j.filter_reasons, j.rank_details
                 FROM applications a JOIN jobs j ON j.uuid = a.job_uuid WHERE 1=1"""
        args: list[Any] = []
        if states:
            states = list(states)
            sql += f" AND a.state IN ({','.join('?' * len(states))})"
            args += states
        sql += " ORDER BY j.score IS NULL, j.score DESC, j.posted_at DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def application_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) n FROM applications GROUP BY state")
        return {row["state"]: row["n"] for row in rows}

    def count_events(self, type_: str, since: datetime, source: str | None = None,
                     exclude: Iterable[str] = ()) -> int:
        """Events of a type since a time; for one source, or for all but `exclude`."""
        sql = "SELECT COUNT(*) FROM events WHERE type = ? AND ts >= ?"
        args: list[Any] = [type_, since.astimezone(timezone.utc).isoformat(timespec="seconds")]
        if source:
            sql += " AND json_extract(payload, '$.source') = ?"
            args.append(source)
        for name in exclude:
            sql += " AND COALESCE(json_extract(payload, '$.source'), 'mcf') != ?"
            args.append(name)
        return self.conn.execute(sql, args).fetchone()[0]

    def first_event_ts(self, type_: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT MIN(ts) FROM events WHERE type = ?", (type_,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    # --- applications as MCF lists them ---------------------------------------------------

    def upsert_applied_on_site(self, uuid: str, title: str = "", employer: str = "",
                               status: str | None = None, submitted_on: str | None = None,
                               mcf_application_id: str | None = None, source: str | None = None,
                               ) -> bool:
        """Record an application a site knows about. Returns True if it was new to us."""
        source = source or ("jobstreet" if uuid.startswith("js:") else "mcf")
        new = self.conn.execute(
            "SELECT 1 FROM applied_on_site WHERE job_uuid = ?", (uuid,)).fetchone() is None
        self.conn.execute(
            """INSERT INTO applied_on_site (job_uuid, source, title, employer, status,
               submitted_on, mcf_application_id, synced_at) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(job_uuid) DO UPDATE SET title = excluded.title,
               employer = excluded.employer, status = excluded.status,
               submitted_on = excluded.submitted_on,
               mcf_application_id = excluded.mcf_application_id, synced_at = excluded.synced_at""",
            (uuid, source, title, employer, status, submitted_on, mcf_application_id, _now()))
        return new

    upsert_applied_on_mcf = upsert_applied_on_site   # older name

    def applied_on_site_uuids(self) -> set[str]:
        return {r["job_uuid"] for r in self.conn.execute("SELECT job_uuid FROM applied_on_site")}

    def applied_on_site_keys(self) -> set[str]:
        """Normalised employer|title of everything applied to, on any site, to catch reposts."""
        return {applied_key(r["employer"], r["title"]) for r in self.conn.execute(
            "SELECT employer, title FROM applied_on_site WHERE title != ''")}

    def applied_on_site_count(self, source: str | None = None) -> int:
        if source:
            return self.conn.execute("SELECT COUNT(*) FROM applied_on_site WHERE source = ?",
                                     (source,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM applied_on_site").fetchone()[0]

    applied_on_mcf_uuids = applied_on_site_uuids
    applied_on_mcf_keys = applied_on_site_keys
    applied_on_mcf_count = applied_on_site_count

    # --- key/value state ------------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM state WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value),
            )

    def log(self, job_uuid: str | None, type_: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (job_uuid, ts, type, payload) VALUES (?,?,?,?)",
            (job_uuid, _now(), type_, json.dumps(payload or {})),
        )

    def commit(self) -> None:
        self.conn.commit()


def applied_key(employer: str, title: str) -> str:
    """Same employer + same title = the same opening, whichever uuid it's posted under."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()   # noqa: E731
    return f"{norm(employer)}|{norm(title)}"


def _iso(d: Any) -> str | None:
    return d.isoformat() if d else None
