"""One-time settings changes applied on the home PC at start-up.

config.yml lives only on the PC (git ignores it), so a change you ask for in chat can't be
pushed to it directly. A patch here edits the file once, right after the update that carries
it, and records in the database that it ran so it never repeats. Each patch returns the lines
to tell you in Telegram.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database
from mcf_autoapply.overrides import set_jobstreet_enabled, set_limits
from mcf_autoapply.setup import replace_section, section_block, set_section_value

STATE_PREFIX = "patch:"


def edit_config(cfg: Config, changes: list[tuple[str, str, str]]) -> list[str]:
    """Set section.key values in config.yml (keeping comments). Returns what changed."""
    path = Path(cfg.project_dir) / "config.yml"
    if not path.exists():
        return [f"config.yml not found at {path}"]
    text = path.read_text(encoding="utf-8")
    done = []
    for section, key, value in changes:
        text, changed = set_section_value(text, section, key, value)
        if changed:
            done.append(f"{section}.{key}: {value}")
    if done:
        path.with_suffix(".yml.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text, encoding="utf-8")
    return done


def requested_settings_2026_10_12(cfg: Config, db: Database) -> list[str]:
    """Requested in chat: auto_apply_min_score 55, delay 45–100 s, JobStreet on, limits 12/100."""
    done = edit_config(cfg, [("apply", "auto_apply_min_score", "55"),
                             ("apply", "delay_seconds", "[45, 100]"),
                             ("jobstreet", "enabled", "true")])
    # Apply them to the running program too, so no restart is needed.
    cfg.apply.auto_apply_min_score = 55
    cfg.apply.delay_seconds = (45, 100)
    set_jobstreet_enabled(cfg, db, True)
    set_limits(cfg, db, 12, 100)
    return done + ["jobstreet.enabled: on (also as a Telegram override)",
                   "limits: 12/hour, 100/day (as /limits 12 100 would)"]


def copy_sections_from_example(cfg: Config, sections: list[str]) -> list[str]:
    """Replace whole sections of config.yml with the current config.example.yml's versions."""
    project = Path(cfg.project_dir)
    path, example = project / "config.yml", project / "config.example.yml"
    if not path.exists() or not example.exists():
        return []
    text, ex = path.read_text(encoding="utf-8"), example.read_text(encoding="utf-8")
    done = []
    for section in sections:
        block = section_block(ex, section)
        if block is None:
            continue
        text, changed = replace_section(text, section, block)
        if changed:
            done.append(f"{section}: replaced with the new version")
    if done:
        path.with_suffix(".yml.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text, encoding="utf-8")
    return done


def broader_search_2026_10_12b(cfg: Config, db: Database) -> list[str]:
    """Requested in chat: many more search terms (software developer, devops, data, cloud,
    backend, AI engineer in every family), looser title/experience filters, lower auto
    threshold. Re-filters and re-scores the jobs already stored so they can be queued now."""
    from mcf_autoapply.config import load_config
    from mcf_autoapply.pipeline.discover import apply_filters

    done = copy_sections_from_example(cfg, ["searches", "filters", "ranking"])
    done += edit_config(cfg, [("apply", "auto_apply_min_score", "35"),
                              ("telegram", "quick_review_min_score", "20")])
    path = Path(cfg.project_dir) / "config.yml"
    if path.exists():
        fresh = load_config(path)                      # validated; raises if the merge went wrong
        cfg.searches, cfg.filters, cfg.ranking = fresh.searches, fresh.filters, fresh.ranking
    cfg.apply.auto_apply_min_score = 35
    cfg.telegram.quick_review_min_score = 20
    counts = apply_filters(db, cfg)
    done.append(f"{sum(len(s.terms()) for s in cfg.searches)} search terms; stored jobs re-filtered "
                f"({', '.join(f'{n} {k}' for k, n in counts.items())})")
    return done


def experience_0_to_1_2026_10_12c(cfg: Config, db: Database) -> list[str]:
    """Requested in chat: only jobs asking for 0–1 years of experience."""
    from mcf_autoapply.pipeline.discover import apply_filters

    done = edit_config(cfg, [("filters", "max_years_experience_required", "1")])
    cfg.filters.max_years_experience_required = 1
    counts = apply_filters(db, cfg)
    return done + [f"stored jobs re-filtered ({', '.join(f'{n} {k}' for k, n in counts.items())})"]


def jobstreet_auto_2026_10_12d(cfg: Config, db: Database) -> list[str]:
    """Requested in chat: JobStreet applications should happen on their own in Auto mode."""
    from mcf_autoapply.overrides import set_jobstreet_auto

    done = edit_config(cfg, [("jobstreet", "allow_auto", "true"),
                             ("jobstreet", "auto_min_score", "20")])
    set_jobstreet_auto(cfg, db, True)
    return done + ["jobstreet.allow_auto: on (Auto mode now queues JobStreet jobs, within its "
                   f"{cfg.jobstreet.hourly_cap}/hour, {cfg.jobstreet.daily_cap}/day limits)"]


def limits_20_per_hour_2026_10_12e(cfg: Config, db: Database) -> list[str]:
    """Requested in chat: MCF 20/hour, daily raised in proportion (100 → 170), same 45–100 s gap."""
    done = edit_config(cfg, [("apply", "hourly_cap", "20"), ("apply", "daily_cap", "170"),
                             ("apply", "ramp", "[85, 130, 170]")])
    set_limits(cfg, db, 20, 170)
    return done + ["limits: 20/hour, 170/day (as /limits 20 170 would); gap between submissions "
                   f"stays {int(cfg.apply.delay_seconds[0])}–{int(cfg.apply.delay_seconds[1])} s"]


def requeue_jobstreet_2026_10_13(cfg: Config, db: Database) -> list[str]:
    """The first JobStreet form code mistook the resume/cover-letter choices for employer
    questions and timed out on SEEK's hidden radio buttons and new-tab apply link. Put the
    JobStreet jobs it gave up on back in the queue so the fixed code tries them."""
    import json

    from mcf_autoapply.pipeline import queue as Q

    words = ("resum", "cover letter")
    requeued = 0
    for app in db.applications([Q.FAILED, Q.EXTERNAL, Q.CLOSED, Q.NEEDS_INPUT]):
        if not app["job_uuid"].startswith("js:"):
            continue
        if app["state"] == Q.NEEDS_INPUT:
            questions = json.loads(app["questions"] or "[]")
            if not any(any(w in q.lower() for w in words) for q in questions):
                continue   # real employer questions: leave them waiting for your answers
        db.update_application(app["job_uuid"], state=Q.APPROVED, attempts=0, error=None,
                              questions=[], answers=[])
        requeued += 1
    db.commit()
    return [f"JobStreet: {requeued} job(s) the old form code gave up on are queued again"]


def tighter_titles_2026_10_13b(cfg: Config, db: Database) -> list[str]:
    """Requested in chat (off-target cards): no bare "engineer", construction/design/project/
    business-developer titles excluded, MCF jobs need one of your skills. Re-filters, and takes
    auto-queued jobs that no longer qualify out of the queue."""
    import json

    from mcf_autoapply.config import load_config
    from mcf_autoapply.pipeline import queue as Q
    from mcf_autoapply.pipeline.discover import apply_filters

    done = copy_sections_from_example(cfg, ["filters"])
    path = Path(cfg.project_dir) / "config.yml"
    if path.exists():
        cfg.filters = load_config(path).filters
    counts = apply_filters(db, cfg)
    dropped = 0
    for app in db.applications([Q.APPROVED]):
        if app["approved_by"] != "auto":
            continue
        job = db.get_job(app["job_uuid"])
        if app["job_status"] != "new" or (job is not None and not Q.enough_skills(cfg, job)):
            db.update_application(app["job_uuid"], state=Q.SKIPPED,
                                  error="no longer matches your filters or skills")
            dropped += 1
    db.commit()
    return done + [f"stored jobs re-filtered ({', '.join(f'{n} {k}' for k, n in counts.items())})",
                   f"{dropped} off-target auto-queued job(s) taken out of the queue"]


PATCHES: list[tuple[str, Callable[[Config, Database], list[str]]]] = [
    ("2026-10-12-requested-settings", requested_settings_2026_10_12),
    ("2026-10-12b-broader-search", broader_search_2026_10_12b),
    ("2026-10-12c-experience-0-1", experience_0_to_1_2026_10_12c),
    ("2026-10-12d-jobstreet-auto", jobstreet_auto_2026_10_12d),
    ("2026-10-12e-limits-20-170", limits_20_per_hour_2026_10_12e),
    ("2026-10-13-requeue-jobstreet", requeue_jobstreet_2026_10_13),
    ("2026-10-13b-tighter-titles", tighter_titles_2026_10_13b),
]


def apply_patches(cfg: Config, db: Database) -> list[str]:
    """Run every patch that hasn't run on this machine yet. Returns the lines to report."""
    report = []
    if not (Path(cfg.project_dir) / "config.yml").exists():
        return report   # not an installed copy (tests, a bare checkout): nothing to patch
    for name, patch in PATCHES:
        if db.get_state(STATE_PREFIX + name):
            continue
        try:
            lines = patch(cfg, db)
        except Exception as e:   # a bad patch must never stop the service
            lines = [f"failed: {type(e).__name__}: {e}"]
        db.set_state(STATE_PREFIX + name, "done")
        db.log(None, "patch_applied", {"name": name, "lines": lines})
        db.commit()
        if lines:
            report.append(f"⚙️ Settings applied as requested ({name}):\n• " + "\n• ".join(lines))
    return report
