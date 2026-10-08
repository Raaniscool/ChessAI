"""Chess system layer.

Thin, strict helpers around python-chess. This module is the single place the
application talks to the chess rules engine, so illegal states can never be
constructed by accident (the AI model is never asked to decide legality).
"""
from __future__ import annotations

import re
from typing import Iterable

import chess

SQUARE_RE = re.compile(r"^[a-h][1-8]$")
UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")


class ChessError(ValueError):
    """Raised when a move/FEN/board command is invalid."""


def parse_fen(fen: str) -> chess.Board:
    """Parse a FEN string into a Board, raising ChessError when invalid."""
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise ChessError(f"Invalid FEN: {fen!r} ({exc})") from exc
    if not board.is_valid():
        raise ChessError(f"FEN describes an invalid position: {fen!r}")
    return board


def start_board() -> chess.Board:
    return chess.Board()


def parse_move(board: chess.Board, move_str: str) -> chess.Move:
    """Parse a move (UCI like 'e2e4' or SAN like 'Nf3') and require legality."""
    move_str = move_str.strip()
    move: chess.Move | None = None
    if UCI_RE.match(move_str):
        try:
            move = chess.Move.from_uci(move_str)
        except ValueError:  # e.g. "a1a1": well-formed but not a move
            move = None
        if move is not None and move not in board.legal_moves:
            move = None
    if move is None:
        try:
            move = board.parse_san(move_str)
        except ValueError as exc:
            raise ChessError(f"Move {move_str!r} is not legal in this position") from exc
    if move not in board.legal_moves:
        raise ChessError(f"Move {move_str!r} is not legal in this position")
    return move


def legal_uci_moves(board: chess.Board) -> list[str]:
    return [m.uci() for m in board.legal_moves]


def push_move(board: chess.Board, move_str: str) -> chess.Move:
    move = parse_move(board, move_str)
    board.push(move)
    return move


def history_uci(board: chess.Board) -> list[str]:
    return [move.uci() for move in board.move_stack]


def board_from_history(history: Iterable[str], fen: str | None = None) -> chess.Board:
    """Rebuild a board by replaying UCI moves from a starting position."""
    board = chess.Board(fen) if fen else chess.Board()
    for uci in history:
        push_move(board, uci)
    return board


def validate_square(square: str) -> str:
    if not isinstance(square, str) or not SQUARE_RE.match(square):
        raise ChessError(f"Invalid square: {square!r}")
    return square


def validate_highlights(highlights: list) -> list[dict]:
    """Validate highlight markers coming from lesson data or (future) AI output.

    Schema: [{"square": "e5", "color": "green"}]
    """
    allowed_colors = {"green", "red", "yellow", "blue", "purple", "orange", "grey"}
    validated = []
    if not isinstance(highlights, list):
        raise ChessError("highlights must be a list")
    for item in highlights:
        if not isinstance(item, dict):
            raise ChessError("highlight entries must be objects")
        square = validate_square(item.get("square", ""))
        color = item.get("color", "green")
        if color not in allowed_colors:
            raise ChessError(f"Invalid highlight color: {color!r}")
        validated.append({"square": square, "color": color})
    return validated


def validate_board_spec(spec: dict, board: chess.Board | None = None) -> dict:
    """Validate a board command coming from lesson data (or a future AI tool).

    This is the *safety gate* for board manipulation: whatever produced the
    command, it must be well-formed and legal before the frontend ever sees it.
    Returns a sanitized copy.
    """
    if not isinstance(spec, dict):
        raise ChessError("board spec must be an object")
    out: dict = {}

    if "fen" in spec:
        parsed = parse_fen(spec["fen"])
        out["fen"] = parsed.fen()
        board = parsed if board is None else board

    if "moves" in spec:
        if board is None:
            raise ChessError("board spec with moves requires a position (fen)")
        replay = board.copy()
        moves_out = []
        for uci in spec["moves"]:
            moves_out.append(parse_move(replay, uci).uci())
        out["moves"] = moves_out
        out["fen_after"] = replay.fen()

    if "highlights" in spec:
        out["highlights"] = validate_highlights(spec["highlights"])

    if "lock" in spec:
        if not isinstance(spec["lock"], bool):
            raise ChessError("lock must be a boolean")
        out["lock"] = spec["lock"]

    unknown = set(spec) - {"fen", "moves", "highlights", "lock", "fen_after"}
    if unknown:
        raise ChessError(f"Unknown board spec keys: {sorted(unknown)}")
    return out


def side_name(board: chess.Board) -> str:
    return "white" if board.turn else "black"


def moves_san(history: Iterable[str], fen: str | None = None) -> list[str]:
    """Replay UCI history and return SAN notation for display/PGN."""
    board = chess.Board(fen) if fen else chess.Board()
    sans = []
    for uci in history:
        sans.append(board.san(chess.Move.from_uci(uci)))
        board.push_uci(uci)
    return sans
