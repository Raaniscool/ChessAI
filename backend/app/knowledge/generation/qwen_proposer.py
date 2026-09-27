"""Qwen as a *proposer* of positions — never as a judge.

Qwen is asked for a FEN and the move it intends as the solution. That is all that is
kept: its description of the position is thrown away (the lesson text is written from
verified facts), and the proposal is judged exactly like a constructed one — python-chess
rules, the concept validator, Stockfish, the "does it test the concept" check. A small
local model usually gets FENs or tactics wrong; those proposals are simply rejected.
"""
from __future__ import annotations

import logging

import chess

from .constructors import Proposal

log = logging.getLogger(__name__)
MAX_TOKENS = 400


def build_messages(concept_name: str, concept_summary: str, side: str, motif_hint: str) -> list[dict]:
    return [
        {"role": "system", "content": (
            "You design chess training positions. Answer with one JSON object only, no other text. "
            "Your position will be checked by a chess engine; wrong positions are discarded.")},
        {"role": "user", "content": (
            f"Create a short puzzle position for a beginner about: {concept_name} — {concept_summary}\n"
            f"Requirements: {side.capitalize()} to move; a legal position with both kings; few pieces; "
            f"{motif_hint} The solution must be a single move for {side.capitalize()} that clearly works.\n"
            'JSON format: {"fen": "<FEN>", "solution": "<the move in SAN, e.g. Nc7+>"}')},
    ]


class QwenProposalError(ValueError):
    """Qwen's answer can't even be turned into a position + move (a rejection reason)."""


def parse_proposal(data: dict, motif: str) -> Proposal:
    if not isinstance(data, dict):
        raise QwenProposalError("the answer is not a JSON object")
    fen, solution = data.get("fen"), data.get("solution") or (data.get("moves") or [None])[0]
    if not isinstance(fen, str) or not isinstance(solution, str):
        raise QwenProposalError("the answer has no FEN or no solution move")
    try:
        board = chess.Board(fen.strip())
    except ValueError as exc:
        raise QwenProposalError(f"invalid FEN: {exc}") from exc
    if not board.is_valid():
        raise QwenProposalError(f"illegal position ({board.status()!r})")
    try:
        move = board.parse_san(solution.strip())
    except ValueError as exc:
        raise QwenProposalError(f"the solution {solution!r} is not a legal move there") from exc
    return Proposal(board.fen(), move, motif, "qwen", 2, {"qwen_solution": solution.strip()[:12]})


def propose(concept_name: str, concept_summary: str, side: str, motif: str, motif_hint: str,
            teacher=None) -> Proposal:
    """One proposal from Qwen. Raises QwenProposalError (bad answer) or TeacherUnavailable."""
    from ...planner.planner import extract_json
    from ...teacher.qwen import QwenTeacher

    teacher = teacher or QwenTeacher()
    reply = teacher.complete(build_messages(concept_name, concept_summary, side, motif_hint), max_tokens=MAX_TOKENS)
    try:
        data = extract_json(reply)
    except ValueError as exc:
        raise QwenProposalError(f"no JSON in the answer: {exc}") from exc
    return parse_proposal(data, motif)
