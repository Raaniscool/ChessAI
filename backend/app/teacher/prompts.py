"""Prompt construction for the Qwen teacher.

Prompts carry *facts only* — the structured output of the engine and the
lesson data — plus strict instructions against hallucinating analysis.
"""
from __future__ import annotations

import json

from ..engine import MoveFeedback
from .base import LessonContext, _fmt_eval

SYSTEM_PROMPT = """You are a friendly, precise chess teacher inside an interactive chess tutor.

You receive FACTS computed by a chess engine (Stockfish) and by the application. \
These facts are the source of truth about the position. Your job is to EXPLAIN them \
in clear, encouraging, level-appropriate language.

Hard rules:
- Never invent evaluations, moves, variations, or tactical claims that are not in the facts.
- If the facts are incomplete or missing, say that engine analysis is needed — do not guess.
- Do not dump raw engine numbers unless they help the point; prefer plain language.
- Focus on teaching: name the idea or principle behind the move (development, king safety, \
center, tempo, tactics...), say what the player did well or missed, and point toward the better idea.
- Keep it short: 2-5 sentences for move feedback unless asked for more.
- Write in the same language the student used.
"""


def build_move_feedback_messages(
    feedback: MoveFeedback, context: LessonContext, level: str = "beginner"
) -> list[dict]:
    facts = {
        "fen_before": feedback.fen_before,
        "fen_after": feedback.fen_after,
        "student_move": feedback.user_move_san,
        "engine_best_move": feedback.best_move_san,
        "classification": feedback.category.value,
        "evaluation_before_best_play": _fmt_eval(
            feedback.eval_before.as_dict() if feedback.eval_before else None
        ),
        "evaluation_after_student_move": _fmt_eval(
            feedback.eval_after.as_dict() if feedback.eval_after else None
        ),
        "best_line": feedback.best_pv_san,
        "line_after_student_move": feedback.reply_pv_san,
        "notes": feedback.notes,
        "depth": feedback.depth,
    }
    user = (
        "Explain the student's move to them now.\n"
        f"Lesson: {context.lesson_title} (course: {context.course_title}). "
        f"Concepts in focus: {', '.join(context.concepts) or 'general play'}.\n"
        f"Current exercise goal: {context.exercise_prompt or 'find the best move'}\n"
        f"Student level: {level}.\n"
        f"Facts:\n{json.dumps(facts, indent=2)}\n\n"
        "Cover: what the student did correctly (if anything), what they missed, "
        "why the engine's move is better, and which idea from the lesson applies."
    )
    if context.accepted_moves:
        user += (
            "\nNote: this exercise specifically teaches a planned line; the goal move(s) are "
            f"{', '.join(context.accepted_moves)}. If the student's move is sound but leaves "
            "the taught line, acknowledge it is playable but explain how it differs from the "
            "lesson's plan."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_chat_messages(
    message: str, context: LessonContext, transcript: list[dict], level: str = "beginner"
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    intro = (
        f"Lesson context — course: {context.course_title}, lesson: {context.lesson_title}, "
        f"concepts: {', '.join(context.concepts) or 'none'}. Student level: {level}."
    )
    messages.append({"role": "user", "content": intro})
    messages.append({"role": "assistant", "content": "Understood. I will teach from these facts."})
    for entry in transcript[-10:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": message})
    return messages
