"""Turn verified topics into lesson data (the same JSON schema as hand-written lessons).

Generated lessons go through `lessons.schema.parse_lesson`, so they get exactly
the same validation as the Italian Game course: legal demo moves, legal
accepted moves, side-to-move checks. Nothing here asks an LLM for chess content.
"""
from __future__ import annotations

import chess

from ..lessons.schema import parse_lesson
from .catalog import Topic

PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}

MAX_INTRO_EXERCISES = 4
MAX_DRILL_EXERCISES = 8


def _move_label(ply: int, san: str) -> str:
    """ply is 0-based from the starting position: 0 -> '1.e4', 1 -> '1...e5'."""
    number = ply // 2 + 1
    return f"{number}.{san}" if ply % 2 == 0 else f"{number}...{san}"


def _format_line(sans: list[str]) -> str:
    out = []
    for ply, san in enumerate(sans):
        out.append(f"{ply // 2 + 1}.{san}" if ply % 2 == 0 else san)
    return " ".join(out)


def move_hints(board: chess.Board, move: chess.Move) -> list[str]:
    """Progressive, deterministic hints derived from the move itself."""
    if board.is_castling(move):
        side = "kingside" if chess.square_file(move.to_square) == 6 else "queenside"
        return [
            "It's time to get your king safe.",
            f"Castle {side}.",
            f"Play {board.san(move)}.",
        ]
    piece = board.piece_at(move.from_square)
    name = PIECE_NAMES[piece.piece_type] if piece else "piece"
    from_sq = chess.square_name(move.from_square)
    to_sq = chess.square_name(move.to_square)
    first = f"Move a {name}."
    if board.is_capture(move):
        first = f"It's a capture with a {name}."
    return [first, f"The {name} on {from_sq}.", f"Move it to {to_sq} ({board.san(move)})."]


def _learner_plies(line: list[str], side: str) -> list[int]:
    parity = 0 if side == "white" else 1
    return [ply for ply in range(len(line)) if ply % 2 == parity]


def _positions_before(line: list[str]) -> list[tuple[chess.Board, chess.Move]]:
    board = chess.Board()
    out = []
    for san in line:
        move = board.parse_san(san)
        out.append((board.copy(), move))
        board.push(move)
    return out


def _line_exercise(title: str, line: list[str], side: str, ply: int, before: chess.Board,
                   move: chess.Move, concepts: list[str]) -> dict:
    san = before.san(move)
    if ply == 0:
        context = "It's the first move of the game."
    else:
        context = f"Your opponent just played {_move_label(ply - 1, line[ply - 1])}."
    return {
        "type": "exercise",
        "fen": before.fen(),
        "side": side,
        "prompt": f"{context} Play the next {side} move of the {title}.",
        "hints": move_hints(before, move),
        "advance_on": "accepted_move",
        "accepted": [san],
        "continue_text": f"Correct — {_move_label(ply, san)} is the move in this line.",
        "concepts": concepts,
    }


def opening_lessons(lesson_prefix: str, title: str, side: str, line: list[str],
                    summary: str, ideas: list[str], level: str = "beginner",
                    verified_note: str = "") -> list[dict]:
    """Two lessons per opening: (1) watch + first moves, (2) play the whole line."""
    concepts = ["opening", title]
    positions = _positions_before(line)
    plies = _learner_plies(line, side)
    ideas_text = "\n".join(f"• {idea}" for idea in ideas)
    intro_text = summary + (f"\n\nKey ideas:\n{ideas_text}" if ideas_text else "")
    if verified_note:
        intro_text += f"\n\n{verified_note}"

    intro_plies = plies[: max(1, min(MAX_INTRO_EXERCISES, (len(plies) + 1) // 2))]
    intro_steps: list[dict] = [
        {"type": "teach", "text": intro_text, "board": {"fen": chess.STARTING_FEN}},
        {
            "type": "demonstrate",
            "text": f"Watch the main line of the {title}: {_format_line(line)}. "
                    f"You'll play the {side} side.",
            "fen": chess.STARTING_FEN,
            "moves": [m.uci() for _, m in positions],
        },
    ]
    for ply in intro_plies:
        before, move = positions[ply]
        intro_steps.append(_line_exercise(title, line, side, ply, before, move, concepts))
    intro_steps.append({
        "type": "teach",
        "text": f"Well done! You've played the first {side} moves of the {title}. "
                "Next lesson: the whole line from memory.",
    })

    drill_plies = plies[:MAX_DRILL_EXERCISES]
    drill_steps: list[dict] = [{
        "type": "teach",
        "text": f"Now play every {side} move of the {title} from memory. "
                "Your opponent's replies are made for you. Use hints if you get stuck.",
        "board": {"fen": chess.STARTING_FEN},
    }]
    for ply in drill_plies:
        before, move = positions[ply]
        drill_steps.append(_line_exercise(title, line, side, ply, before, move, concepts))

    lessons = [
        {
            "id": f"{lesson_prefix}a",
            "title": f"{title}: moves and ideas",
            "description": summary,
            "difficulty": level,
            "concepts": concepts,
            "steps": intro_steps,
            "completion": {"text": f"Lesson complete — you know how the {title} starts."},
        },
        {
            "id": f"{lesson_prefix}b",
            "title": f"{title}: play the whole line",
            "description": f"Play every {side} move of the {title} from memory.",
            "difficulty": level,
            "concepts": concepts,
            "steps": drill_steps,
            "completion": {"text": f"You played the whole {title} line. Excellent memory work!"},
        },
    ]
    for lesson in lessons:
        parse_lesson(lesson, course_id="_generated")  # fail fast on anything invalid
    return lessons


def position_lesson(lesson_id: str, topic: Topic) -> dict:
    """One lesson for tactic/endgame/strategy topics: explain, then solve positions."""
    concepts = [topic.category, topic.title]
    ideas_text = "\n".join(f"• {idea}" for idea in topic.ideas)
    steps: list[dict] = [{
        "type": "teach",
        "text": topic.summary + (f"\n\nKey ideas:\n{ideas_text}" if ideas_text else ""),
        "board": {"fen": topic.positions[0]["fen"]},
    }]
    for pos in topic.positions:
        step = {"type": "exercise", "concepts": concepts}
        for key in ("fen", "side", "prompt", "hints", "advance_on", "accepted",
                    "min_category", "continue_text"):
            if key in pos:
                step[key] = pos[key]
        steps.append(step)
    lesson = {
        "id": lesson_id,
        "title": topic.title,
        "description": topic.summary,
        "difficulty": topic.level,
        "concepts": concepts,
        "steps": steps,
        "completion": {"text": f"Lesson complete — {topic.title} added to your toolkit."},
    }
    parse_lesson(lesson, course_id="_generated")
    return lesson


def topic_lessons(lesson_prefix: str, topic: Topic) -> list[dict]:
    if topic.category == "opening":
        return opening_lessons(lesson_prefix, topic.title, topic.side or "white", topic.line,
                               topic.summary, topic.ideas, topic.level)
    return [position_lesson(f"{lesson_prefix}a", topic)]
