"""Settings changed from Telegram, stored in the database. They override config.yml."""

from __future__ import annotations

from mcf_autoapply.config import Config
from mcf_autoapply.db import Database

MODES = ("dry_run", "manual", "auto")
MODE_KEY = "override:apply.mode"
HOURLY_KEY = "override:apply.hourly_cap"
DAILY_KEY = "override:apply.daily_cap"
JS_ENABLED_KEY = "override:jobstreet.enabled"
JS_HOURLY_KEY = "override:jobstreet.hourly_cap"
JS_DAILY_KEY = "override:jobstreet.daily_cap"
MAX_HOURLY, MAX_DAILY = 10, 60
MAX_JS_HOURLY, MAX_JS_DAILY = 5, 20     # JobStreet forbids automation; keep it quiet


def apply_overrides(cfg: Config, db: Database) -> Config:
    if (mode := db.get_state(MODE_KEY)) in MODES:
        cfg.apply.mode = mode
    if hourly := db.get_state(HOURLY_KEY):
        cfg.apply.hourly_cap = int(hourly)
    if daily := db.get_state(DAILY_KEY):
        cfg.apply.daily_cap = int(daily)
        cfg.apply.ramp = []   # an explicit daily limit replaces the weekly ramp
    if (enabled := db.get_state(JS_ENABLED_KEY)) in ("on", "off"):
        cfg.jobstreet.enabled = enabled == "on"
    if hourly := db.get_state(JS_HOURLY_KEY):
        cfg.jobstreet.hourly_cap = int(hourly)
    if daily := db.get_state(JS_DAILY_KEY):
        cfg.jobstreet.daily_cap = int(daily)
    return cfg


def set_site_limits(cfg: Config, db: Database, site: str, hourly: int, daily: int) -> None:
    if site != "jobstreet":
        raise ValueError("per-site limits exist for jobstreet only")
    if not (1 <= hourly <= MAX_JS_HOURLY and 1 <= daily <= MAX_JS_DAILY):
        raise ValueError(f"jobstreet hourly must be 1–{MAX_JS_HOURLY} and daily 1–{MAX_JS_DAILY}")
    db.set_state(JS_HOURLY_KEY, str(hourly))
    db.set_state(JS_DAILY_KEY, str(daily))
    db.log(None, "limits_changed", {"site": site, "hourly": hourly, "daily": daily})
    db.commit()
    apply_overrides(cfg, db)


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


def set_jobstreet_enabled(cfg: Config, db: Database, enabled: bool) -> None:
    db.set_state(JS_ENABLED_KEY, "on" if enabled else "off")
    db.log(None, "jobstreet_toggled", {"enabled": enabled})
    db.commit()
    cfg.jobstreet.enabled = enabled
