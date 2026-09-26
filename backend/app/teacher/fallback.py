"""Deterministic fallback teacher.

Used when no Qwen endpoint is configured or when Qwen is unreachable. It only
ever states facts derived from the engine output and the lesson data — it
cannot hallucinate chess analysis, which is exactly the behaviour we want from
the product even in degraded mode.
"""
from __future__ import annotations

from ..engine import MoveFeedback
from ..engine.classification import LABELS
from .base import LessonContext, _fmt_eval

_CATEGORY_TEMPLATES = {
    "excellent": "Excellent move — {move} is what the engine plays here too. {praise}",
    "good": "Good move. {move} holds up well; the engine's preference is {best}, but yours is fully sound. {praise}",
    "inaccurate": "Not quite the most precise. {move} is playable, but {best} is stronger. {miss}",
    "mistake": "That's a mistake. After {move}, {best} was the move to find. {miss}",
    "blunder": "Careful — {move} is a blunder. The engine's move was {best}, and here's why it matters: {miss}",
}

_PRAISE_BY_CONCEPT = {
    "development": "You're getting a piece into the game, which is exactly what this position asks for.",
    "king safety": "King safety first — that's a habit of strong players.",
    "center": "Fighting for the center is the right idea here.",
    "tactics": "Sharp tactical vision — you spotted the concrete chance.",
}


class FallbackTeacher:
    name = "fallback"

    def explain_move(self, feedback: MoveFeedback, context: LessonContext) -> str:
        if feedback.eval_before is None or feedback.eval_after is None:
            return (
                "I don't have engine analysis for this position yet, so I can't "
                "give you a trustworthy evaluation of the move. Let's get the "
                "engine's read first."
            )

        category = feedback.category.value
        best = feedback.best_move_san or "the engine's choice"
        move = feedback.user_move_san

        praise = ""
        for concept in context.concepts:
            key = concept.lower()
            if key in _PRAISE_BY_CONCEPT:
                praise = _PRAISE_BY_CONCEPT[key]
                break

        if category in ("excellent", "good") and feedback.best_move_uci == feedback.user_move_uci:
            text = (
                f"Excellent move — {move} is exactly what the engine plays here. "
                + (praise or "It matches the plan we're learning.")
            )
        else:
            template = _CATEGORY_TEMPLATES.get(category, _CATEGORY_TEMPLATES["good"])
            # The mover is the side that played the move; report eval from their POV.
            mover_is_white = feedback.fen_before.split()[1] == "w"
            before = _fmt_eval(feedback.eval_before.as_dict(), for_white=mover_is_white)
            after = _fmt_eval(feedback.eval_after.as_dict(), for_white=mover_is_white)
            miss = f"The evaluation drops from {before} to {after} for the player."
            if category == "inaccurate":
                miss = f"It lets the opponent answer comfortably (eval {before} → {after})."
            text = template.format(move=move, best=best, praise=praise, miss=miss)

        if "missed_mate" in feedback.notes:
            text += " Warning: the engine saw a forced mate and this move let it slip away."
        if "decided_position" in feedback.notes:
            text += " The position was already decided, but precision still matters for learning."

        pv = " ".join(feedback.best_pv_san[:6])
        if pv:
            text += f" Engine's line: {pv}."

        # Lesson context: sound move, but not the taught line?
        if (
            context.accepted_moves
            and feedback.user_move_san not in context.accepted_moves
            and category in ("excellent", "good", "inaccurate")
        ):
            goals = " or ".join(context.accepted_moves)
            text += (
                f" It's a playable move, but this exercise is specifically about"
                f" {goals} — try the lesson's move to complete it."
            )
        return text

    def chat(self, message: str, context: LessonContext, transcript: list[dict]) -> str:
        return (
            "I can't reach the local Qwen model right now, so I'm answering in "
            "limited offline mode. I can still analyze your moves with the chess "
            "engine — try making a move on the board, or ask me after the engine "
            "has analyzed a position. (Set QWEN_MODEL and QWEN_BASE_URL to enable "
            "the full AI teacher.)"
        )
