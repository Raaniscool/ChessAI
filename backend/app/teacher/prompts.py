"""Prompt construction for the Qwen teacher.

Prompts carry *facts only* — the structured output of the engine and the
lesson data — plus strict instructions against hallucinating analysis.
"""
from __future__ import annotations

from ..engine import MoveFeedback
from ..engine.classification import Classification
from .base import LessonContext, _fmt_eval

# Kept byte-identical across requests so local servers (Ollama, llama.cpp) can reuse
# its processed tokens from cache: only the short per-move facts are read each time.
SYSTEM_PROMPT = """You are a friendly, precise chess teacher inside an interactive chess tutor.
You get FACTS from a chess engine (Stockfish) and the app. They are the truth about the position; \
your job is to explain them in clear, encouraging, beginner-friendly language.
Rules:
- Never invent evaluations, moves, variations or tactics that are not in the facts.
- If facts are missing, say engine analysis is needed instead of guessing.
- Prefer plain language to engine numbers.
- Teach the idea behind the move (development, king safety, center, tempo, tactics...).
- Be brief: 2-3 short sentences for move feedback unless asked for more.
- Reply in the student's language."""

# Verified example facts sent per request (a few hundred tokens at most).
MAX_FACT_LINES = 40

# Recent chat turns sent with each question (each costs prompt-reading time on a CPU).
CHAT_HISTORY = 6

# Plies of each engine line to include: enough to show the idea, cheap to read.
LINE_PLIES = 4
GOOD_VERDICTS = {Classification.EXCELLENT, Classification.GOOD}


def _line(moves: list[str]) -> str:
    return " ".join(moves[:LINE_PLIES]) or "none"


def _eval(score, student_is_white: bool) -> str:
    return _fmt_eval(score.as_dict() if score else None, for_white=student_is_white)


def _facts_block(context: LessonContext) -> str:
    if not context.facts:
        return ""
    lines = context.facts[:MAX_FACT_LINES]
    return ("\nVerified example from the lesson library (checked by python-chess and Stockfish; "
            "these facts are true):\n" + "\n".join(f"- {line}" for line in lines))


def build_move_feedback_messages(
    feedback: MoveFeedback, context: LessonContext, level: str = "beginner"
) -> list[dict]:
    """Compact prompt: facts as short lines, evaluations from the student's side.

    No FENs — models can't read them reliably (and must not calculate from them),
    and on a CPU every prompt token delays the first word.
    """
    student_is_white = feedback.fen_before.split()[1] == "w"
    same_as_best = feedback.best_move_uci in (None, feedback.user_move_uci)
    facts = [
        f"Student ({'White' if student_is_white else 'Black'}) played: {feedback.user_move_san} "
        f"- verdict: {feedback.category.value}",
        "Engine's best move: " + ("the same move" if same_as_best else (feedback.best_move_san or "unknown")),
        f"Evaluation for the student (pawns, + = good for them): {_eval(feedback.eval_before, student_is_white)} "
        f"with best play, {_eval(feedback.eval_after, student_is_white)} after their move",
    ]
    if not same_as_best:
        facts.append(f"Engine line after the best move: {_line(feedback.best_pv_san)}")
    facts.append(f"Likely continuation after the student's move: {_line(feedback.reply_pv_san)}")
    if feedback.notes:
        facts.append("Notes: " + "; ".join(feedback.notes))

    if feedback.category in GOOD_VERDICTS:
        task = ("Say why the move is good and which lesson idea it uses"
                + ("." if same_as_best else "; mention the engine's move only if it teaches something."))
    else:
        task = "Say what the move allows or misses, why the engine's move is better, and which lesson idea applies."
    if context.accepted_moves and feedback.user_move_san not in context.accepted_moves:
        task += (f" The lesson's planned move is {', '.join(context.accepted_moves)}: if the student's move "
                 "is playable, say so and explain how it differs from the plan.")

    user = (
        f"Lesson: {context.lesson_title} ({', '.join(context.concepts) or 'general play'}). "
        f"Goal: {(context.exercise_prompt or 'find the best move').rstrip(' .')}. Level: {level}.\n"
        + "\n".join(facts)
        + _facts_block(context)
        + f"\nTask: {task} 2-3 sentences, under 60 words."
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
        + _facts_block(context)
    )
    messages.append({"role": "user", "content": intro})
    messages.append({"role": "assistant", "content": "Understood. I will teach from these facts."})
    for entry in transcript[-CHAT_HISTORY:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": message})
    return messages


def build_example_messages(context: LessonContext, level: str = "beginner",
                           question: str | None = None) -> list[dict]:
    """Explain a verified library example. Correctness is settled; Qwen only explains."""
    task = (f"The student asks: {question.strip()}\nAnswer using only the facts."
            if question else
            "Explain what happens in this example and the idea behind the key move, "
            "so the student can recognise the pattern in their own games.")
    user = (
        f"Lesson: {context.lesson_title} ({', '.join(context.concepts) or 'general play'}). Level: {level}."
        + _facts_block(context)
        + f"\nTask: {task} Every move and evaluation above is already verified: do not judge "
          "whether moves are correct, and do not add moves, variations or evaluations that are "
          "not in the facts. Plain language, 3-5 sentences, under 110 words."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
