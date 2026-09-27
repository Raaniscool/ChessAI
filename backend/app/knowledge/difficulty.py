"""How hard a verified example is to solve, on a rating scale learners understand.

The library's 1-5 ``difficulty`` label is coarse: it mostly counts how many moves the
learner makes, so most real-game puzzles share the same label. Personalization needs a
finer, *comparable* number so a 700-rated beginner and a 1700-rated club player can be
given different positions for the same idea.

``puzzle_rating(example)`` estimates an Elo-like rating (400-2400) from facts about the
solution that python-chess can check, not from opinion:

    solution length      every extra move the learner must find (and foresee)
    kind of key move     checks and captures are the first things players look at;
                         quiet moves, sacrifices and backward moves are found later
    board complexity     many legal moves and many pieces mean more to consider
    the curator's label  blended in, so the estimate stays anchored to the library

The weights are deliberately simple and documented here; they order positions sensibly
(a one-move capture of a free queen is easier than a two-move quiet sacrifice), which is
all selection needs. The learner model then calibrates per learner from actual results.
"""
from __future__ import annotations

import chess

MIN_RATING, MAX_RATING = 400, 2400
LABEL_RATING = {1: 600, 2: 900, 3: 1250, 4: 1650, 5: 2050}
VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

BASE = 650
PER_EXTRA_MOVE = 260        # each further learner move in the solution
QUIET = 230                 # neither check nor capture nor promotion
NON_CHECK = 90              # a capture that isn't check is found a little later than a check
SACRIFICE = 220             # the moved piece can be taken for less than it is worth
BACKWARD = 90               # moving away from the opponent is easy to overlook
FREE_CAPTURE = -120         # taking an undefended piece worth 3+
LEGAL_MOVE_WEIGHT = 4       # per legal move above 25
CROWDED = 70                # more than 20 pieces on the board
LABEL_BLEND = 0.3           # share of the curator's label in the final estimate

_cache: dict[tuple[str, str, str], int] = {}


def _clamp(value: float) -> int:
    return int(max(MIN_RATING, min(MAX_RATING, round(value))))


def _learner_moves(example) -> int:
    if example.key_ply is None:
        return 0
    return len(range(example.key_ply, len(example.moves), 2))


def features(example) -> dict:
    """The measurable facts the estimate is built from (also shown to tests/debugging)."""
    rep = example.replay()
    key = example.key_ply
    out = {"learner_moves": _learner_moves(example), "demonstration_only": key is None}
    if key is None:
        return out
    board = rep.boards[key]
    move = board.parse_san(example.moves[key])
    mover = board.turn
    piece = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        captured = chess.Piece(chess.PAWN, not mover)
    gives_check = board.gives_check(move)
    after = board.copy(stack=False)
    after.push(move)
    moved_value = VALUE[piece.piece_type] if piece else 0
    taken_value = VALUE[captured.piece_type] if captured else 0
    attacked = after.is_attacked_by(not mover, move.to_square)
    defended = after.is_attacked_by(mover, move.to_square)
    sacrifice = bool(attacked and moved_value > taken_value + (1 if defended else 0) and not after.is_checkmate()
                     and piece.piece_type != chess.KING)
    forward = 1 if mover == chess.WHITE else -1
    rank_change = (chess.square_rank(move.to_square) - chess.square_rank(move.from_square)) * forward
    free_capture = bool(captured and taken_value >= 3 and not board.is_attacked_by(not mover, move.to_square))
    out.update({
        "check": gives_check, "capture": captured is not None, "promotion": move.promotion is not None,
        "quiet": not gives_check and captured is None and move.promotion is None,
        "sacrifice": sacrifice, "backward": rank_change < 0 and piece.piece_type not in (chess.KING, chess.PAWN),
        "free_capture": free_capture, "legal_moves": board.legal_moves.count(),
        "pieces": len(board.piece_map()), "mate": rep.final.is_checkmate(),
    })
    return out


def estimate(f: dict, label: int) -> int:
    label_rating = LABEL_RATING.get(label, 1250)
    if f.get("demonstration_only"):
        return _clamp(label_rating)
    r = BASE + PER_EXTRA_MOVE * max(0, f["learner_moves"] - 1)
    if f["quiet"]:
        r += QUIET
    elif not f["check"]:
        r += NON_CHECK
    if f["sacrifice"]:
        r += SACRIFICE
    if f["backward"]:
        r += BACKWARD
    if f["free_capture"]:
        r += FREE_CAPTURE
    r += LEGAL_MOVE_WEIGHT * max(0, f["legal_moves"] - 25)
    if f["pieces"] > 20:
        r += CROWDED
    return _clamp((1 - LABEL_BLEND) * r + LABEL_BLEND * label_rating)


def puzzle_rating(example) -> int:
    """Estimated rating needed to solve `example` about half the time (cached per example)."""
    key = (example.id, example.start_fen, " ".join(example.moves))
    if key not in _cache:
        try:
            _cache[key] = estimate(features(example), example.difficulty)
        except (ValueError, AttributeError, IndexError):  # unreplayable: fall back to the label
            _cache[key] = LABEL_RATING.get(example.difficulty, 1250)
    return _cache[key]
