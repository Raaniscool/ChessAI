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

# Several phrasings per situation, picked deterministically per position so the same
# move always reads the same, but a lesson doesn't repeat one sentence ten times.
_PHRASES = {
    "obvious": ["Right.", "Yes: {move}.", "{move}, of course.", "That's it."],
    "supporting": ["Correct, {move}.", "Good: {move} keeps it going.", "{move} is right.", "Yes, {move}. Keep going."],
    "solved": ["Correct: {move}!", "Well found: {move}.", "Yes, {move} is the move.", "That's it: {move}!"],
    "excellent": ["Excellent, {move} is exactly the engine's choice too.", "{move}: the best move here.",
                  "Spot on. {move} is what the engine plays."],
    "good": ["Good move. {move} holds up well; the engine slightly prefers {best}.",
             "{move} is sound. {best} was a touch more precise."],
    "inaccurate": ["Not quite the most precise. {move} is playable, but {best} is stronger.",
                   "{move} is okay, but {best} was better here."],
    "mistake": ["That's a mistake. After {move}, {best} was the move to find.",
                "{move} is a mistake: {best} was the move here."],
    "blunder": ["Careful: {move} is a blunder. The move was {best}.",
                "That's a blunder. {move} lets the advantage go; {best} was the move."],
}

_PRAISE_BY_CONCEPT = {
    "development": "You're getting a piece into the game, which is exactly what this position asks for.",
    "king safety": "King safety first: that's a habit of strong players.",
    "center": "Fighting for the center is the right idea here.",
    "tactics": "Sharp tactical vision: you spotted the concrete chance.",
}


def _pick(kind: str, seed: str, **fmt) -> str:
    import random
    return random.Random(f"{kind}:{seed}").choice(_PHRASES[kind]).format(**fmt)


def _cost(loss_cp: int | None) -> str:
    """What an evaluation drop means, in words a beginner understands."""
    loss = loss_cp or 0
    if loss >= 900:
        return "It hands your opponent a winning position."
    if loss >= 450:
        return "It costs about a piece."
    if loss >= 220:
        return "It costs about two pawns."
    if loss >= 90:
        return "It gives away about a pawn's worth."
    return "It gives your opponent an easier game."


class FallbackTeacher:
    name = "fallback"

    def explain_move(self, feedback: MoveFeedback, context: LessonContext) -> str:
        """Engine facts in a coach's words. Length follows the move's importance
        (teacher.importance); raw evaluations and engine lines only for players past
        the beginner stage, and only when the moment is worth it."""
        if feedback.eval_before is None or feedback.eval_after is None:
            return (
                "I don't have engine analysis for this position yet, so I can't "
                "give you a trustworthy evaluation of the move. Let's get the "
                "engine's read first."
            )

        category = feedback.category.value
        best = feedback.best_move_san or "the engine's choice"
        move = feedback.user_move_san
        importance = context.importance or "important"
        seed = f"{feedback.fen_before}|{move}"
        beginner = context.level == "beginner"
        brief = context.style == "brief"

        praise = ""
        for concept in context.concepts:
            key = concept.lower()
            if key in _PRAISE_BY_CONCEPT:
                praise = _PRAISE_BY_CONCEPT[key]
                break

        if "engine_prefers" in feedback.notes:
            text = (f"Correct — {move} solves the exercise. The engine's top choice, {best}, "
                    "is even stronger, so it's worth a look.")
            if importance in ("supporting", "obvious"):
                text = f"Correct — {move} solves the exercise ({best} is even stronger)."
            return text
        if context.move_accepted and category in ("excellent", "good"):
            if importance == "obvious":
                return _pick("obvious", seed, move=move)
            if importance == "supporting":
                return _pick("supporting", seed, move=move)
            text = _pick("solved", seed, move=move)
            if praise and not brief:
                text += f" {praise}"
            return text

        if category in ("excellent", "good") and feedback.best_move_uci == feedback.user_move_uci:
            text = _pick("excellent", seed, move=move)
            if praise and not brief and importance in ("critical", "important"):
                text += f" {praise}"
        else:
            text = _pick(category if category in _PHRASES else "good", seed, move=move, best=best)
            if category in ("inaccurate", "mistake", "blunder"):
                if beginner or brief:
                    text += " " + _cost(feedback.loss_cp)
                else:
                    mover_is_white = feedback.fen_before.split()[1] == "w"
                    before = _fmt_eval(feedback.eval_before.as_dict(), for_white=mover_is_white)
                    after = _fmt_eval(feedback.eval_after.as_dict(), for_white=mover_is_white)
                    text += f" The evaluation goes from {before} to {after} for you."

        if "missed_mate" in feedback.notes:
            text += " There was a forced checkmate, and this move lets it slip away."
        if "decided_position" in feedback.notes and not brief and importance == "critical":
            text += " The game was already decided, but precision still matters for learning."

        # The engine's line: only for a critical moment, for players who can read one.
        pv = feedback.best_pv_san[:4]
        if pv and importance == "critical" and not beginner and not brief and \
                feedback.best_move_uci != feedback.user_move_uci:
            text += f" Main line: {' '.join(pv)}."

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
        """Answer simple board-state questions from verified facts even when Qwen is offline."""
        import re
        from ..planner.intent.conversation import LESSON_STATE_QUESTION

        route = context.route_intent or {}
        state = context.learning_state or {}
        active_topic = state.get("topic")
        if route.get("action") == "greeting":
            if active_topic:
                return f"Hi! We can continue your {active_topic} lesson, or switch to something else whenever you like."
            return "Hi! I'm your chess coach. I can explain a concept, build a lesson, or give you a puzzle. What would you like to work on?"
        if route.get("action") == "discovery":
            options = "openings, tactics, strategy, endgames, checkmate patterns, and chess fundamentals"
            if active_topic:
                return f"I can teach {options}. Your {active_topic} lesson is still here if you'd like to return to it."
            return f"I can teach {options}. Name a topic, ask for an explanation, or request a puzzle."
        if route.get("action") == "lesson_question" and LESSON_STATE_QUESTION.search(message):
            progress = state.get("progress") if isinstance(state.get("progress"), dict) else {}
            current = progress.get("current_step", "?")
            total = progress.get("total_steps", "?")
            objective = state.get("objective") or "work through the current lesson"
            return f"We're working on {state.get('topic') or context.lesson_title}. " \
                   f"Right now the goal is: {objective} (step {current} of {total})."
        if route.get("action") == "board_question":
            facts = context.board_facts or []
            current = next((line for line in facts if line.startswith("Current authoritative position:")), None)
            if current is None:
                return "I don't have an authoritative lesson-board position to read right now."
            turn = re.search(r"(White|Black) to move; move (\d+)", current)
            if re.search(r"\b(?:whose|who's) (?:move|turn)|\bwho moves\b", message, re.I) and turn:
                return f"It's {turn.group(1)} to move, on move {turn.group(2)}."
            capture_question = re.search(r"\b(?:can't|cannot|can not)\b", message, re.I) and \
                re.search(r"\b(?:take|capture|captures|capturing)\b", message, re.I)
            if capture_question:
                named_side = re.search(r"\b(white|black)\b", message, re.I)
                side = named_side.group(1).title() if named_side else (turn.group(1) if turn else None)
                if turn and side and side != turn.group(1):
                    return f"It's {turn.group(1)} to move, so {side} can't make a move in this position yet."
                legal_line = next((line for line in facts
                                   if line.startswith(f"Python-chess verified legal captures for {side} to move:")), None)
                if legal_line:
                    legal = legal_line.split(":", 1)[1].strip()
                    if legal == "none":
                        return f"Python-chess confirms {side} has no legal captures in the current position. " \
                               "If you mean a particular pawn, tell me its square and I'll check that specific capture."
                    return f"Python-chess verifies these legal captures for {side}: {legal}. " \
                           "If you mean a different pawn, tell me its square and I can check that capture."
            if route.get("requires_engine"):
                move_feedback = next((line for line in facts
                                      if line.startswith("Verified Stockfish feedback on the latest graded move")), None)
                if move_feedback:
                    return move_feedback
                analysis = next((line for line in facts if line.startswith("Stockfish analysis of the authoritative")), None)
                if analysis and "unavailable" not in analysis.lower():
                    return analysis
                return "I can't give a trustworthy best move or evaluation without Stockfish analysis of this verified position."
            white = next((line for line in facts if line.startswith("White pieces:")), None)
            black = next((line for line in facts if line.startswith("Black pieces:")), None)
            if white and black:
                return "The verified board is " + current.removeprefix("Current authoritative position: ").rstrip(".") + ". " + \
                       white + " " + black
            return current
        active_note = (f" Your lesson on {active_topic} is still active; we can pick it up whenever you're ready."
                       if active_topic else "")
        return (
            "The AI teacher (Qwen) isn't available right now, so I can't answer that in my own words. "
            "I can still explain verified chess terms, show lesson examples, and check moves on the board."
            + active_note
        )
