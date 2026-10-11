"""Your saved answers to employers' yes/no screening questions (answers.yml).

Answers only ever come from this file. A question with no confident match is never
guessed: the job waits in `needs_input` until you answer it with `mcf-apply answer`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import yaml

# Employers word the same question slightly differently; this catches small edits
# (punctuation, a changed word) without matching questions that mean something else.
MATCH_THRESHOLD = 0.92
VALID_ANSWERS = ("yes", "no")

HEADER = """\
# Your answers to employers' yes/no screening questions on MyCareersFuture.
# These are sent to employers as your answers, so keep them truthful.
# Add new ones with:  mcf-apply answer <question-id> yes|no
"""


def normalise(question: str) -> str:
    q = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    return re.sub(r"\s+", " ", q).strip()


def _as_answer(value: object) -> str:
    # YAML reads bare yes/no as booleans.
    if isinstance(value, bool):
        return "yes" if value else "no"
    answer = str(value).strip().lower()
    if answer not in VALID_ANSWERS:
        raise ValueError(f"answer must be yes or no, got {value!r}")
    return answer


@dataclass
class Entry:
    question: str
    answer: str


class AnswerBank:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: list[Entry] = []
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text()) or []
            for i, item in enumerate(data):
                try:
                    self.entries.append(Entry(str(item["question"]).strip(),
                                              _as_answer(item["answer"])))
                except (KeyError, TypeError, ValueError) as e:
                    raise ValueError(f"{self.path}: entry {i + 1} is invalid ({e})") from e

    def lookup(self, question: str) -> str | None:
        target = normalise(question)
        best, best_ratio = None, 0.0
        for entry in self.entries:
            candidate = normalise(entry.question)
            if candidate == target:
                return entry.answer
            ratio = SequenceMatcher(None, candidate, target).ratio()
            if ratio > best_ratio:
                best, best_ratio = entry, ratio
        return best.answer if best and best_ratio >= MATCH_THRESHOLD else None

    def answer_all(self, questions: list[str]) -> list[str | None]:
        return [self.lookup(q) for q in questions]

    def add(self, question: str, answer: str) -> None:
        answer = _as_answer(answer)
        target = normalise(question)
        for entry in self.entries:
            if normalise(entry.question) == target:
                entry.answer = answer
                break
        else:
            self.entries.append(Entry(question.strip(), answer))
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(
            [{"question": e.question, "answer": e.answer} for e in self.entries],
            sort_keys=False, allow_unicode=True, width=1000,
        )
        self.path.write_text(HEADER + "\n" + body)
