"""Load and validate config.yml."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = "config.yml"


class _Strict(BaseModel):
    # Reject unknown keys so a typo in config.yml fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")


class Search(_Strict):
    query: str
    also: list[str] = Field(default_factory=list)

    def terms(self) -> list[str]:
        return [self.query, *self.also]


class Filters(_Strict):
    max_age_days: int = 14
    salary_min_monthly: int | None = None
    max_years_experience_required: int | None = None
    employment_types: list[str] = Field(default_factory=list)
    position_levels: list[str] = Field(default_factory=list)
    title_include_keywords: list[str] = Field(default_factory=list)
    title_exclude_keywords: list[str] = Field(default_factory=list)
    seniority_keywords: list[str] = Field(default_factory=list)
    company_blocklist: list[str] = Field(default_factory=list)
    exclude_agencies: bool = False
    skip_duplicate_postings: bool = True


class Profile(_Strict):
    skills: list[str] = Field(default_factory=list)


class Ranking(_Strict):
    entry_level_keywords: list[str] = Field(default_factory=list)
    entry_level_weight: float = Field(0.6, ge=0, le=1)
    skills_for_full_marks: int = Field(6, ge=1)


class Discovery(_Strict):
    page_size: int = Field(100, ge=1, le=100)
    max_pages: int = Field(5, ge=1)
    requests_per_second: float = Field(1.0, gt=0, le=5)


class Config(_Strict):
    db_path: Path = Path("./data/jobs.db")
    profile: Profile = Field(default_factory=Profile)
    searches: list[Search]
    filters: Filters = Field(default_factory=Filters)
    ranking: Ranking = Field(default_factory=Ranking)
    discovery: Discovery = Field(default_factory=Discovery)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("MCF_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yml to config.yml and edit it."
        )
    data = yaml.safe_load(path.read_text()) or {}
    cfg = Config.model_validate(data)
    # Resolve the DB path relative to the config file, not the current directory.
    if not cfg.db_path.is_absolute():
        cfg.db_path = (path.parent / cfg.db_path).resolve()
    return cfg
