"""The Training opponent: a bot that plays near the learner's level.

Strength comes from the learner's own difficulty profile (learner.difficulty): the skill estimate
for the segment's phase (opening / endgame, else overall), turned from the puzzle scale back into a
game rating with the documented inverse of difficulty.puzzle_scale. No rating bands, no fixed table.

Move choice uses only AnalysisEngine.analyse_lines (Stockfish's top candidate moves), so it works
with the WASM engine and any test engine, without UCI strength options:

    loss_i    = how much worse candidate i is than the best one, for the bot (cp, mate = large)
    tolerance = TOL_FLOOR + TOL_SPAN * exp(-(rating - 400) / TOL_DECAY)
                (about 185 cp at 400, 70 cp at 1200, 30 cp at 2000: weaker bots drift further
                from the best move, strong ones rarely do)
    weight_i  = exp(-loss_i / tolerance), picked with the segment's seeded random generator

A losing alternative is never impossible, just increasingly unlikely as the rating rises — like a
human of that strength. The bot never sees the hidden idea of the position and never plays an
illegal move (python-chess checks every candidate).
"""
from __future__ import annotations

import math
import random

import chess

from ..engine.classification import Score

MULTIPV = 4
TOL_FLOOR, TOL_SPAN, TOL_DECAY = 15.0, 170.0, 700.0
MIN_RATING, MAX_RATING = 400, 2400
MATE_LOSS = 2000
PHASE_SKILL = {"opening": "opening", "endgame": "endgame"}


def game_rating(puzzle_scale_estimate: float) -> int:
    """Inverse of learner.difficulty.puzzle_scale (puzzle scale -> game rating)."""
    from ..learner.difficulty import PRIOR_BASE, PRIOR_SLOPE
    return int(max(MIN_RATING, min(MAX_RATING, round((puzzle_scale_estimate - PRIOR_BASE) / PRIOR_SLOPE))))


def strength_for(difficulty, phase: str) -> dict:
    """The bot's rating for a segment of `phase`, with where it comes from."""
    skill = PHASE_SKILL.get(phase, "overall")
    est = difficulty.skills[skill].estimate
    return {"rating": game_rating(est), "skill": skill, "estimate": est,
            "basis": f"your {skill} estimate {est} on the puzzle scale -> about {game_rating(est)} as a game rating"}


def tolerance(rating: float) -> float:
    return TOL_FLOOR + TOL_SPAN * math.exp(-(max(MIN_RATING, rating) - MIN_RATING) / TOL_DECAY)


def depth_for(rating: float) -> int:
    """Search depth: deep enough that the candidate list is meaningful, cheaper for weak bots."""
    return int(max(6, min(14, 6 + rating // 300)))


def _value(score: Score, side: chess.Color) -> int:
    if score.kind == "mate":
        sign = 1 if score.is_mate_for(side == chess.WHITE) else -1
        return sign * (MATE_LOSS * 2 - min(abs(score.value), 50))
    return max(-MATE_LOSS, min(MATE_LOSS, score.for_side(side == chess.WHITE)))


def choose_move(engine, board: chess.Board, rating: float, rng: random.Random) -> dict:
    """The bot's move in `board` (its side to move): {uci, san, loss_cp, candidates}."""
    legal = list(board.legal_moves)
    if not legal:
        raise ValueError("no legal move")
    if len(legal) == 1:
        return {"uci": legal[0].uci(), "san": board.san(legal[0]), "loss_cp": 0, "candidates": 1}
    lines = []
    if hasattr(engine, "analyse_lines"):
        try:
            lines = [ln for ln in engine.analyse_lines(board, depth=depth_for(rating), multipv=MULTIPV)
                     if ln.move in board.legal_moves]
        except Exception:
            lines = []
    if not lines:
        best = engine.analyse(board, depth=depth_for(rating))
        move = chess.Move.from_uci(best.best_move_uci) if best.best_move_uci else legal[0]
        if move not in board.legal_moves:
            move = legal[0]
        return {"uci": move.uci(), "san": board.san(move), "loss_cp": 0, "candidates": 1}
    side = board.turn
    values = [_value(ln.score, side) for ln in lines]
    top = max(values)
    tol = tolerance(rating)
    weights = [math.exp(-(top - v) / tol) for v in values]
    pick = rng.random() * sum(weights)
    for ln, v, w in zip(lines, values, weights):
        pick -= w
        if pick <= 0:
            break
    return {"uci": ln.move.uci(), "san": board.san(ln.move), "loss_cp": int(top - v), "candidates": len(lines)}
