"""Lesson schema: structured, validated, engine-safe lesson data.

A lesson is data, not code — dozens/hundreds of lessons can be added by
dropping JSON files into lessons/data/<course>/. Every lesson is validated on
load: FENs must parse, demo moves must be legal, exercises must have hints.
Invalid lessons fail fast instead of reaching the learner.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import chess

from .. import chess_system
from ..chess_system import ChessError

STEP_TYPES = {"teach", "demonstrate", "exercise"}
ADVANCE_MODES = {"any_legal", "accepted_move", "min_classification"}


class LessonError(ValueError):
    pass


@dataclass
class TeachStep:
    text: str
    board: dict | None = None  # validated board spec (fen/highlights/lock)

    type = "teach"


@dataclass
class DemonstrateStep:
    text: str
    fen: str
    moves: list[str]  # UCI, validated legal in sequence
    comments: list[str] = field(default_factory=list)
    board: dict | None = None  # highlights/lock applied after the moves

    type = "demonstrate"


@dataclass
class ExerciseStep:
    fen: str
    side: str  # "white" | "black" — the side the learner plays
    prompt: str
    hints: list[str]
    advance_on: str = "any_legal"
    accepted_san: list[str] | None = None  # required when advance_on == accepted_move
    min_category: str | None = None  # required when advance_on == min_classification
    continue_text: str = ""
    concepts: list[str] = field(default_factory=list)  # exercise-level concept tags

    type = "exercise"


@dataclass
class Lesson:
    id: str
    course_id: str
    title: str
    description: str
    difficulty: str
    concepts: list[str]
    steps: list
    completion_text: str


@dataclass
class LessonPlan:
    """Entry in a course's lesson list (may be 'planned' = not written yet)."""

    id: str
    title: str
    status: str  # "available" | "planned"
    file: str | None = None


@dataclass
class Course:
    id: str
    title: str
    description: str
    lessons: list[LessonPlan]
    kind: str = "course"  # "course" (curated) | "plan" (generated for a learner)
    meta: dict = field(default_factory=dict)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LessonError(message)


def _parse_teach(data: dict) -> TeachStep:
    _require(isinstance(data.get("text"), str) and data["text"].strip(), "teach step needs text")
    board = None
    if "board" in data:
        board = chess_system.validate_board_spec(data["board"])
    return TeachStep(text=data["text"], board=board)


def _parse_demonstrate(data: dict) -> DemonstrateStep:
    _require(isinstance(data.get("text"), str) and data["text"].strip(), "demonstrate step needs text")
    fen = data.get("fen", chess.STARTING_FEN)
    board = chess_system.parse_fen(fen)
    moves = data.get("moves")
    _require(isinstance(moves, list) and moves, "demonstrate step needs a non-empty moves list")
    replay = board.copy()
    try:
        for uci in moves:
            chess_system.push_move(replay, uci)
    except ChessError as exc:
        raise LessonError(f"illegal demonstration move: {exc}") from exc
    comments = data.get("comments", [])
    _require(
        isinstance(comments, list) and len(comments) <= len(moves),
        "comments must be a list no longer than moves",
    )
    for c in comments:
        _require(isinstance(c, str), "comments must be strings")
    board_spec = chess_system.validate_board_spec(data["board"], board=replay) if "board" in data else None
    return DemonstrateStep(text=data["text"], fen=fen, moves=[m for m in moves], comments=comments, board=board_spec)


def _parse_exercise(data: dict) -> ExerciseStep:
    fen = data.get("fen")
    _require(isinstance(fen, str) and fen, "exercise needs a fen")
    board = chess_system.parse_fen(fen)
    side = data.get("side")
    _require(side in ("white", "black"), "exercise side must be white or black")
    _require(
        chess_system.side_name(board) == side,
        f"exercise side ({side}) does not match position to move ({chess_system.side_name(board)})",
    )
    prompt = data.get("prompt")
    _require(isinstance(prompt, str) and prompt.strip(), "exercise needs a prompt")
    hints = data.get("hints", [])
    _require(isinstance(hints, list) and hints and all(isinstance(h, str) and h for h in hints),
             "exercise needs at least one hint")
    advance_on = data.get("advance_on", "any_legal")
    _require(advance_on in ADVANCE_MODES, f"advance_on must be one of {sorted(ADVANCE_MODES)}")

    accepted_san = data.get("accepted")
    if advance_on == "accepted_move":
        _require(isinstance(accepted_san, list) and accepted_san, "accepted_move requires accepted moves")
        for san in accepted_san:
            try:
                board.parse_san(san)
            except ValueError as exc:
                raise LessonError(f"accepted move {san!r} is illegal in exercise position") from exc
    else:
        accepted_san = None

    min_category = data.get("min_category")
    if advance_on == "min_classification":
        _require(
            min_category in ("excellent", "good", "inaccurate", "mistake"),
            "min_classification must be a valid category",
        )

    continue_text = data.get("continue_text", "")
    _require(isinstance(continue_text, str), "continue_text must be a string")

    concepts_raw = data.get("concepts", [])
    _require(isinstance(concepts_raw, list) and all(isinstance(c, str) for c in concepts_raw),
             "concepts must be a list of strings")

    return ExerciseStep(
        fen=fen,
        side=side,
        prompt=prompt,
        hints=hints,
        advance_on=advance_on,
        accepted_san=accepted_san,
        min_category=min_category,
        continue_text=continue_text,
        concepts=list(concepts_raw),
    )


_PARSERS = {"teach": _parse_teach, "demonstrate": _parse_demonstrate, "exercise": _parse_exercise}


def parse_lesson(data: dict, course_id: str) -> Lesson:
    _require(isinstance(data, dict), "lesson must be an object")
    for key in ("id", "title", "description", "steps"):
        _require(key in data, f"lesson missing required field: {key}")
    steps_raw = data["steps"]
    _require(isinstance(steps_raw, list) and steps_raw, "lesson needs at least one step")
    steps = []
    for i, raw in enumerate(steps_raw):
        _require(isinstance(raw, dict), f"step {i} must be an object")
        step_type = raw.get("type")
        _require(step_type in STEP_TYPES, f"step {i} has unknown type {step_type!r}")
        try:
            steps.append(_PARSERS[step_type](raw))
        except ChessError as exc:
            raise LessonError(f"step {i}: {exc}") from exc
    completion = data.get("completion", {})
    completion_text = completion.get("text", "Lesson complete — nice work!") if isinstance(completion, dict) else str(completion)
    return Lesson(
        id=data["id"],
        course_id=course_id,
        title=data["title"],
        description=data.get("description", ""),
        difficulty=data.get("difficulty", "beginner"),
        concepts=list(data.get("concepts", [])),
        steps=steps,
        completion_text=completion_text,
    )


def parse_course(data: dict) -> Course:
    for key in ("id", "title", "lessons"):
        _require(key in data, f"course missing required field: {key}")
    plans = []
    for raw in data["lessons"]:
        plan = LessonPlan(
            id=raw["id"],
            title=raw.get("title", raw["id"]),
            status=raw.get("status", "available"),
            file=raw.get("file"),
        )
        _require(plan.status in ("available", "planned"), f"bad lesson status for {plan.id}")
        if plan.status == "available":
            _require(bool(plan.file), f"available lesson {plan.id} needs a file")
        plans.append(plan)
    return Course(
        id=data["id"],
        title=data["title"],
        description=data.get("description", ""),
        lessons=plans,
    )
