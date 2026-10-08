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
PATTERN_PUZZLES = 3  # puzzles in the "learn the pattern" lesson; the rest go to practice
LICHESS_TRAINING_URL = "https://lichess.org/training/"


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


def _side(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def puzzle_steps(puzzle: dict, topic: Topic, number: int, total: int) -> list[dict]:
    """One verified puzzle -> demonstrate (opponent's move) + exercise per learner move.

    puzzle["moves"] = [opponent's setup move, learner, opponent, learner, ...] (UCI),
    already checked by Stockfish (scripts/build_puzzle_library.py). Intermediate learner
    moves were verified unique; on the last move every move in final_accepted is right.
    """
    text = topic.puzzle_text
    concepts = [topic.category, topic.title]
    board = chess.Board(puzzle["fen"])
    setup = chess.Move.from_uci(puzzle["moves"][0])
    setup_san = board.san(setup)
    opponent = _side(board.turn)
    learner = "black" if opponent == "white" else "white"
    learner_moves = len(puzzle["moves"]) // 2
    steps: list[dict] = [{
        "type": "demonstrate",
        "text": f"Puzzle {number} of {total}. You play {learner}. Your opponent plays "
                f"{_move_label_from(board, setup_san)}…",
        "fen": board.fen(),
        "moves": [setup.uci()],
    }]
    board.push(setup)
    source = f" (Lichess puzzle {LICHESS_TRAINING_URL}{puzzle['id']})"
    solution = puzzle["moves"][1:]
    for i in range(0, len(solution), 2):
        move = chess.Move.from_uci(solution[i])
        final = i + 1 >= len(solution)
        san = board.san(move)
        accepted = list(puzzle.get("final_accepted") or [san]) if final else [san]
        if i == 0:  # the task, not the idea: "Find the fork" would give the answer away
            prompt = "Find the best move."
        else:
            prompt = "Keep going — find the next move."
        hints = [text.get("hint") or move_hints(board, move)[0]] + move_hints(board, move)[1:]
        if final:
            done = "Checkmate! " if puzzle.get("mate") else "Correct! "
            continue_text = done + (text.get("done", "") or "") + source
        else:
            continue_text = f"Correct — {san}!"
        steps.append({
            "type": "exercise",
            "fen": board.fen(),
            "side": learner,
            "prompt": prompt,
            "hints": hints,
            "advance_on": "accepted_move",
            "accepted": accepted,
            "continue_text": continue_text.strip(),
            "concepts": concepts,
        })
        board.push(move)
        if not final:
            reply = chess.Move.from_uci(solution[i + 1])
            reply_san = board.san(reply)
            steps.append({
                "type": "demonstrate",
                "text": f"Your opponent replies {_move_label_from(board, reply_san)}.",
                "fen": board.fen(),
                "moves": [reply.uci()],
            })
            board.push(reply)
    return steps


def _move_label_from(board: chess.Board, san: str) -> str:
    """'...Rg8' style label for the side to move in `board`."""
    return f"{board.fullmove_number}.{san}" if board.turn == chess.WHITE else f"{board.fullmove_number}...{san}"


def puzzle_lesson(lesson_id: str, topic: Topic, puzzles: list[dict], title: str,
                  intro: str, completion: str) -> dict:
    concepts = [topic.category, topic.title]
    steps: list[dict] = [{"type": "teach", "text": intro, "board": {"fen": puzzles[0]["fen"]}}]
    for n, puzzle in enumerate(puzzles, start=1):
        steps += puzzle_steps(puzzle, topic, n, len(puzzles))
    lesson = {
        "id": lesson_id,
        "title": title,
        "description": topic.summary,
        "difficulty": topic.level,
        "concepts": concepts,
        "steps": steps,
        "completion": {"text": completion},
    }
    parse_lesson(lesson, course_id="_generated")
    return lesson


def puzzle_lessons(lesson_prefix: str, topic: Topic) -> list[dict]:
    """Topics with a verified puzzle set: learn the pattern (easier puzzles), then practice."""
    ideas_text = "\n".join(f"• {idea}" for idea in topic.ideas)
    explain = topic.summary + (f"\n\nKey ideas:\n{ideas_text}" if ideas_text else "")
    credit = "Puzzles from real games on Lichess (public domain), each move checked by Stockfish."
    lessons = []
    if topic.positions:  # hand-made intro lesson exists: puzzles are extra practice
        lessons.append(position_lesson(f"{lesson_prefix}a", topic))
        practice = topic.puzzles
    else:
        pattern, practice = topic.puzzles[:PATTERN_PUZZLES], topic.puzzles[PATTERN_PUZZLES:]
        lessons.append(puzzle_lesson(
            f"{lesson_prefix}a", topic, pattern, f"{topic.title}: learn the pattern",
            f"{explain}\n\nLet's see it in action. {credit}",
            f"Lesson complete — you've seen the {topic.title.lower()} pattern. Next: practice."))
    if practice:
        lessons.append(puzzle_lesson(
            f"{lesson_prefix}b", topic, practice, f"{topic.title}: practice",
            f"Time to practise the {topic.title.lower()}. These are harder: some take several moves. "
            f"Use hints if you get stuck. {credit}",
            f"Practice complete — {topic.title} added to your toolkit."))
    return lessons


def topic_lessons(lesson_prefix: str, topic: Topic) -> list[dict]:
    if topic.category == "opening":
        return opening_lessons(lesson_prefix, topic.title, topic.side or "white", topic.line,
                               topic.summary, topic.ideas, topic.level)
    if topic.puzzles:
        return puzzle_lessons(lesson_prefix, topic)
    return [position_lesson(f"{lesson_prefix}a", topic)]
