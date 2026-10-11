"""Rule-based ranking: entry-level fit plus skill overlap with your profile.

This runs without an API key. The LLM fit assessment (DESIGN.md §3.3) layers on top later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from mcf_autoapply.config import Profile, Ranking
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline.filter import keyword_pattern

ENTRY_LEVEL_POSITIONS = {"fresh/entry level"}
JUNIOR_POSITIONS = {"junior executive"}

# Points for the entry-level part of the score (capped at 100).
_TITLE_POINTS = 60
_ENTRY_POSITION_POINTS = 40
_JUNIOR_POSITION_POINTS = 20
_YEARS_POINTS = {0: 40, 1: 30, 2: 10}
_UNKNOWN_YEARS_POINTS = 20


@dataclass
class RankResult:
    score: int
    entry_score: int
    skills_score: int
    fresh_grad: bool
    entry_signals: list[str]
    matched_skills: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


class Ranker:
    def __init__(self, profile: Profile, ranking: Ranking) -> None:
        self.ranking = ranking
        self._entry = keyword_pattern(ranking.entry_level_keywords)
        self._skills = keyword_pattern(profile.skills)
        # Map lower-cased match back to how you wrote the skill in config.
        self._skill_names = {s.strip().lower(): s.strip() for s in profile.skills}

    def rank(self, job: Job) -> RankResult:
        signals: list[str] = []  # strong signals: these mark a job as fresh-grad friendly
        points = 0

        if self._entry and (m := self._entry.search(job.title)):
            points += _TITLE_POINTS
            signals.append(f"title: {m.group(0).lower()}")
        levels = {p.lower() for p in job.position_levels}
        if levels & ENTRY_LEVEL_POSITIONS:
            points += _ENTRY_POSITION_POINTS
            signals.append("level: fresh/entry")
        elif levels & JUNIOR_POSITIONS:
            points += _JUNIOR_POSITION_POINTS
        if job.min_years_exp is None:
            points += _UNKNOWN_YEARS_POINTS
        else:
            points += _YEARS_POINTS.get(job.min_years_exp, 0)
            if job.min_years_exp == 0:
                signals.append("0 years required")
        entry_score = min(points, 100)
        fresh_grad = bool(signals)

        matched: list[str] = []
        if self._skills:
            text = " ".join([job.title, *job.skills, job.description_text()])
            found = {m.group(0).lower() for m in self._skills.finditer(text)}
            matched = sorted(self._skill_names.get(f, f) for f in found)
        skills_score = min(100, round(100 * len(matched) / self.ranking.skills_for_full_marks))

        w = self.ranking.entry_level_weight
        score = round(w * entry_score + (1 - w) * skills_score)
        return RankResult(score, entry_score, skills_score, fresh_grad, signals, matched)
