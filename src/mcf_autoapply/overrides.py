"""Settings changed from Telegram, stored in the database. They override config.yml."""

from __future__ import annotations

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database

MODES = ("dry_run", "manual", "auto")
MODE_KEY = "override:apply.mode"


def apply_overrides(cfg: Config, db: Database) -> Config:
    if (mode := db.get_state(MODE_KEY)) in MODES:
        cfg.apply.mode = mode
    return cfg


def set_mode(cfg: Config, db: Database, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    db.set_state(MODE_KEY, mode)
    db.log(None, "mode_changed", {"mode": mode})
    db.commit()
    cfg.apply.mode = mode
