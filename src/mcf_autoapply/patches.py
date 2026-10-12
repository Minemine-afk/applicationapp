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
from mcf_autoapply.setup import set_section_value

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


PATCHES: list[tuple[str, Callable[[Config, Database], list[str]]]] = [
    ("2026-10-12-requested-settings", requested_settings_2026_10_12),
]


def apply_patches(cfg: Config, db: Database) -> list[str]:
    """Run every patch that hasn't run on this machine yet. Returns the lines to report."""
    report = []
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
