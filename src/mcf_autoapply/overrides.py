"""Settings changed from Telegram, stored in the database. They override config.yml."""

from __future__ import annotations

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database

MODES = ("dry_run", "manual", "auto")
MODE_KEY = "override:apply.mode"
HOURLY_KEY = "override:apply.hourly_cap"
DAILY_KEY = "override:apply.daily_cap"
MAX_HOURLY, MAX_DAILY = 10, 60


def apply_overrides(cfg: Config, db: Database) -> Config:
    if (mode := db.get_state(MODE_KEY)) in MODES:
        cfg.apply.mode = mode
    if hourly := db.get_state(HOURLY_KEY):
        cfg.apply.hourly_cap = int(hourly)
    if daily := db.get_state(DAILY_KEY):
        cfg.apply.daily_cap = int(daily)
        cfg.apply.ramp = []   # an explicit daily limit replaces the weekly ramp
    return cfg


def set_limits(cfg: Config, db: Database, hourly: int, daily: int) -> None:
    if not (1 <= hourly <= MAX_HOURLY and 1 <= daily <= MAX_DAILY):
        raise ValueError(f"hourly must be 1–{MAX_HOURLY} and daily 1–{MAX_DAILY}")
    db.set_state(HOURLY_KEY, str(hourly))
    db.set_state(DAILY_KEY, str(daily))
    db.log(None, "limits_changed", {"hourly": hourly, "daily": daily})
    db.commit()
    apply_overrides(cfg, db)


def set_mode(cfg: Config, db: Database, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    db.set_state(MODE_KEY, mode)
    db.log(None, "mode_changed", {"mode": mode})
    db.commit()
    cfg.apply.mode = mode
