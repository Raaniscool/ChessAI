"""AI teacher layer.

Philosophy: Stockfish determines WHAT happened; Qwen explains it. The teacher
never invents chess facts — it receives structured engine facts and produces
language. If engine facts are missing, it must say analysis is needed rather
than making something up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..engine import MoveFeedback


@dataclass
class LessonContext:
    course_title: str
    lesson_title: str
    concepts: list[str]
    exercise_prompt: str = ""
    accepted_moves: list[str] | None = None


class Teacher(Protocol):
    name: str

    def explain_move(self, feedback: MoveFeedback, context: LessonContext) -> str: ...

    def chat(self, message: str, context: LessonContext, transcript: list[dict]) -> str: ...


def _fmt_eval(score_dict: dict | None, for_white: bool = True) -> str:
    if not score_dict:
        return "unknown"
    if score_dict["kind"] == "mate":
        m = score_dict["value"]
        if (m > 0) == for_white:
            return f"forced mate in {abs(m)} for {'White' if for_white else 'Black'}"
        return f"forced mate in {abs(m)} against {'White' if for_white else 'Black'}"
    cp = score_dict["value"] if for_white else -score_dict["value"]
    pawns = cp / 100.0
    return f"{pawns:+.2f}"
