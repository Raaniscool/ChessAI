"""Opening habits across a whole game: early queen, moving one piece again and again,
falling behind in development, leaving the king in the centre.

These are principles, not engine facts, so each habit is only reported when the
engine confirms it cost something: the learner came out of the opening at least
OPENING_DROP_CP worse than they went in (Stockfish's evaluation, not ours). A
learner who plays 3.Qh5 and is still fine gets no lecture about early queens.
The pattern checks themselves are the Knowledge Library's validators.
"""
from __future__ import annotations

import chess

from ..knowledge import validators as V
from ..knowledge.positions import Replay, developed_minors, move_label
from .motifs import Finding, finding

OPENING_PLIES = 24      # the first 12 moves
MIN_PLIES = 16          # shorter games: too little opening to judge habits
OPENING_DROP_CP = 100


def _replay(boards: list[chess.Board], moves: list[chess.Move], n: int) -> Replay:
    labels = [move_label(boards[i], moves[i]) for i in range(n)]
    sans = [boards[i].san(moves[i]) for i in range(n)]
    return Replay(boards[:n + 1], moves[:n], labels, sans)


def _run(name: str, ctx: V.Ctx, params: dict) -> dict | None:
    try:
        return V.run(name, ctx, params)
    except (V.Fail, ValueError, KeyError, IndexError, AttributeError):
        return None


def _castled(boards: list[chess.Board], moves: list[chess.Move], side: chess.Color, upto: int) -> bool:
    return any(boards[i].turn == side and boards[i].is_castling(moves[i]) for i in range(upto))


def detect_habits(boards: list[chess.Board], moves: list[chess.Move], side: chess.Color,
                  opening_drop_cp: int, king_trouble: bool = False) -> list[tuple[int, Finding]]:
    """[(ply the habit is anchored to, finding)] for the learner playing `side`.

    `opening_drop_cp`: how much worse (engine, learner's view) the learner stood at the end
    of the opening than at the start. `king_trouble`: a later moment was about the king
    (mate allowed, king weakened) — evidence that leaving it in the centre mattered."""
    n = min(OPENING_PLIES, len(moves))
    if n < MIN_PLIES:
        return []
    confirmed = opening_drop_cp >= OPENING_DROP_CP
    rep = _replay(boards, moves, n)
    name = "white" if side == chess.WHITE else "black"
    out: list[tuple[int, Finding]] = []

    if confirmed:
        for i in range(n):
            board = boards[i]
            if board.turn != side or board.fullmove_number > 6:
                continue
            piece = board.piece_at(moves[i].from_square)
            if piece and piece.piece_type == chess.QUEEN:
                facts = _run("early_queen", V.Ctx(rep, mistake_ply=i), {})
                if facts:
                    out.append((i, finding("early_queen", facts, lines=[
                        f"the queen came out early with {rep.labels[i]} and was chased by "
                        + (", ".join(facts["queen_chased_by"][:3]) or "the opponent's pieces")])))
                break
        early = _replay(boards, moves, min(20, n))
        repeated = _run("repeated_moves", V.Ctx(early), {"side": name, "min": 3})
        if repeated and developed_minors(early.final, side) <= 2:
            last = max(i for i in range(len(early.moves)) if boards[i].turn == side)
            out.append((last, finding("repeated_moves", repeated, lines=[
                f"one piece moved {repeated['moves_by_one_piece']} times in the first 10 moves while "
                f"only {developed_minors(early.final, side)} of the knights and bishops were developed"])))
        lead = _run("development_lead", V.Ctx(rep), {"side": "black" if name == "white" else "white",
                                                      "at": "end", "ahead_by": 2})
        if lead:
            out.append((n - 1, finding("poor_development", lead, lines=[
                f"after move {boards[n].fullmove_number - (1 if boards[n].turn == chess.WHITE else 0)}, "
                f"{lead['developed']} of the opponent's knights and bishops were developed and only "
                f"{lead['opponent_developed']} of yours"])))

    final = boards[n]
    home = chess.E1 if side == chess.WHITE else chess.E8
    queens = final.pieces(chess.QUEEN, not side)
    if (confirmed or king_trouble) and final.king(side) == home and queens and not _castled(boards, moves, side, n):
        had_rights = any(boards[i].has_castling_rights(side) for i in range(n))
        if had_rights:
            out.append((n - 1, finding("missed_castling", {"king": chess.square_name(home), "move": n // 2}, lines=[
                f"after {n // 2} moves the king was still on {chess.square_name(home)}, uncastled, "
                f"with the opponent's queen on the board"])))
    return out
