"""Practice positions for an exact material request ("two rooks against a queen").

The library rarely has verified positions for a specific material balance, so they are
built — never invented by a language model, never weakened to a neighbouring topic:

  1. construct   deterministic code places both kings, exactly the requested pieces for
                 each side (the learner gets `spec.pieces`) and 0–3 pawns, learner to move
  2. legality    python-chess: valid position, nobody in check, no piece left hanging
  3. engine      Stockfish (multipv 4): not a forced mate, not decided, inside the eval band
                 of the requested objective, no accidental tactic that wins material at once
                 (unless winning material IS the objective), the material survives the first
                 moves, and the position is instructive: the best move(s) within ACCEPT_CP,
                 at least one natural alternative clearly worse (GAP_CP)
  4. objective   the requested focus is visible in the verified facts (a check-giving queen
                 for "avoid perpetual checks", a move by the rooks for "coordinate", ...)
  5. pipeline    the example goes through the standard verification pipeline
                 (pipeline.verify_candidate): schema, rules, the concept validator — here the
                 `material` validator re-checks the exact per-side material — the engine
                 profile `practical`, the explanation check and duplicates

Words are deterministic and describe only verified facts; Qwen may later explain them.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from collections import Counter
from dataclasses import dataclass, field

import chess

log = logging.getLogger(__name__)

CONCEPT = "material_endgames"
ACCEPT_CP = 40        # moves this close to the best one are accepted answers
GAP_CP = 100          # an instructive position has an alternative at least this much worse
MAX_ACCEPTED = 2      # more good moves than this: there's nothing particular to find
DECIDED_CP = 900      # beyond this the position is already decided
MATE_CP = 9000
VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
LETTER = {"Q": chess.QUEEN, "R": chess.ROOK, "B": chess.BISHOP, "N": chess.KNIGHT, "P": chess.PAWN}
# eval window (centipawns, learner's view) of the best move for each objective
BANDS = {
    "general": (-150, 600), "convert": (150, 800), "coordinate": (0, 700), "avoid_perpetual": (100, 800),
    "attack_king": (50, 800), "trade": (-100, 600), "win_material": (-300, 800), "hold": (-350, 60),
}


@dataclass
class Screened:
    key: chess.Move
    key_san: str
    cp: int
    accepted: list[str]
    alternative: str
    alt_cp: int
    alt_checks: bool          # after the alternative, the opponent's best reply is a check
    facts: dict = field(default_factory=dict)


@dataclass
class MaterialResult:
    spec: object
    objective: str
    accepted: list = field(default_factory=list)       # verified Examples
    attempts: int = 0
    rejected: Counter = field(default_factory=Counter)  # reason → count
    stopped: str = ""
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {"spec": self.spec.slug(), "objective": self.objective, "accepted": [e.id for e in self.accepted],
                "attempts": self.attempts, "rejected": dict(self.rejected.most_common(8)),
                "stopped": self.stopped, "elapsed": round(self.elapsed, 2)}


# ------------------------------------------------------------------ 1. construct
def _empty(board: chess.Board, rng: random.Random, ok=lambda sq: True) -> int | None:
    squares = [sq for sq in chess.SQUARES if board.piece_at(sq) is None and ok(sq)]
    return rng.choice(squares) if squares else None


def construct(spec, rng: random.Random, learner: chess.Color, pawns: tuple[int, int] | None = None) -> chess.Board | None:
    """Kings + exactly the requested pieces per side (+ a few pawns), learner to move."""
    board = chess.Board(None)
    wk = rng.choice(chess.SQUARES)
    board.set_piece_at(wk, chess.Piece(chess.KING, learner))
    bk = _empty(board, rng, lambda sq: chess.square_distance(sq, wk) >= 2)
    board.set_piece_at(bk, chess.Piece(chess.KING, not learner))

    def pawn_ok(color):
        return lambda sq: 1 <= (chess.square_rank(sq) if color == chess.WHITE else 7 - chess.square_rank(sq)) <= 5

    def quiet(color, ptype):
        """Squares where this piece gives no check (the side not to move can't be in check, and
        the learner shouldn't start in check)."""
        enemy_king = board.king(not color)

        def ok(sq):
            if ptype == chess.PAWN and not pawn_ok(color)(sq):
                return False
            return not _attacks(board, sq, ptype, color) & chess.BB_SQUARES[enemy_king]
        return ok

    for color, group in ((learner, spec.pieces), (not learner, spec.against)):
        for letter in group:
            ptype = LETTER[letter]
            sq = _empty(board, rng, quiet(color, ptype))
            if sq is None:
                return None
            board.set_piece_at(sq, chess.Piece(ptype, color))
    extra = pawns or (rng.choice([0, 1, 1, 2, 2, 3]), rng.choice([0, 1, 1, 2, 2, 3]))
    for color, n, group in ((learner, extra[0], spec.pieces), (not learner, extra[1], spec.against)):
        if "P" in group:
            continue  # pawns are part of this side's exact material
        for _ in range(n):
            sq = _empty(board, rng, quiet(color, chess.PAWN))
            if sq is not None:
                board.set_piece_at(sq, chess.Piece(chess.PAWN, color))
    board.turn = learner
    for _ in range(12):  # move hanging pieces somewhere safe (a few tries); the screen rejects the rest
        loose = [sq for sq in chess.SQUARES if _hanging_at(board, sq)]
        if not loose:
            break
        sq = rng.choice(loose)
        piece = board.remove_piece_at(sq)
        to = _empty(board, rng, quiet(piece.color, piece.piece_type))
        board.set_piece_at(to if to is not None else sq, piece)
    board.castling_rights = 0
    board.fullmove_number = 40 + rng.randint(0, 20)
    if spec.bishops and not spec.matches(board):
        return None
    return board


def _attacks(board: chess.Board, sq: int, ptype: int, color: chess.Color) -> int:
    """Squares a piece of `ptype` on `sq` would attack (bitboard), with the current blockers."""
    if ptype == chess.PAWN:
        return chess.BB_PAWN_ATTACKS[color][sq]
    if ptype == chess.KNIGHT:
        return chess.BB_KNIGHT_ATTACKS[sq]
    if ptype == chess.KING:
        return chess.BB_KING_ATTACKS[sq]
    occ = board.occupied
    out = 0
    if ptype in (chess.BISHOP, chess.QUEEN):
        out |= chess.BB_DIAG_ATTACKS[sq][chess.BB_DIAG_MASKS[sq] & occ]
    if ptype in (chess.ROOK, chess.QUEEN):
        out |= chess.BB_RANK_ATTACKS[sq][chess.BB_RANK_MASKS[sq] & occ] | \
            chess.BB_FILE_ATTACKS[sq][chess.BB_FILE_MASKS[sq] & occ]
    return out


# ------------------------------------------------------------------ 2. legality
def _hanging_at(board: chess.Board, sq: int) -> bool:
    piece = board.piece_at(sq)
    if piece is None or piece.piece_type == chess.KING:
        return False
    attackers = board.attackers(not piece.color, sq)
    if not attackers:
        return False
    cheapest = min(VALUE.get(board.piece_type_at(a), 0) for a in attackers)
    return not board.attackers(piece.color, sq) or cheapest < VALUE[piece.piece_type]


def hanging(board: chess.Board) -> list[str]:
    """Pieces that can simply be taken: attacked and undefended, or attacked by something cheaper."""
    return [f"{board.piece_at(sq).symbol()}{chess.square_name(sq)}" for sq in chess.SQUARES if _hanging_at(board, sq)]


def legal_reason(board: chess.Board, spec) -> str | None:
    if not board.is_valid():
        return "illegal position"
    if board.is_check():
        return "the learner starts in check"
    if not spec.matches(board, board.turn):
        return "material does not match"
    if board.is_game_over() or len(list(board.legal_moves)) < 3:
        return "too few moves"
    if hanging(board):
        return "a piece is hanging"
    return None


# ------------------------------------------------------------------ 3/4. engine + objective
def _balance(board: chess.Board, side: chess.Color) -> int:
    return sum(VALUE.get(p.piece_type, 0) * (1 if p.color == side else -1) for p in board.piece_map().values())


def _play(board: chess.Board, sans: list[str], plies: int) -> chess.Board:
    b = board.copy(stack=False)
    for san in sans[:plies]:
        try:
            b.push_san(san)
        except ValueError:
            break
    return b


def screen(board: chess.Board, spec, objective: str, engine, depth: int = 14) -> tuple[Screened | None, str]:
    """Stockfish checks. Returns (facts, "") or (None, reason)."""
    lines = engine.analyse_lines(board, depth=depth, multipv=4, fresh=True)
    if len(lines) < 2:
        return None, "engine gave too few lines"
    side = board.turn
    best = lines[0]
    cp = best.score.for_side(side == chess.WHITE)
    if abs(cp) >= MATE_CP:
        return None, "forced mate (a tactic, not this endgame)"
    if abs(cp) > DECIDED_CP:
        return None, "already decided"
    lo, hi = BANDS.get(objective, BANDS["general"])
    if not lo <= cp <= hi:
        return None, f"eval outside the {objective} band"
    pv = list(best.pv_san or [best.san])
    swing = _balance(_play(board, pv, 4), side) - _balance(board, side)
    if objective == "win_material":
        if swing < 3:
            return None, "no material is won"
    elif swing >= 3:
        return None, "wins material at once (an accidental tactic)"
    elif swing <= -3 and objective != "hold":
        return None, "the best line gives material back at once"
    if objective != "win_material" and not spec.matches(_play(board, pv, 2), side):
        return None, "the material changes immediately"
    scores = [(l, l.score.for_side(side == chess.WHITE)) for l in lines]
    accepted = [l.san for l, s in scores[1:] if cp - s <= ACCEPT_CP]
    worse = [(l, s) for l, s in scores[1:] if cp - s >= GAP_CP]
    if not worse:
        return None, "every move is about as good (nothing to find)"
    if len(accepted) > MAX_ACCEPTED:
        return None, "too many good moves"
    alt, alt_cp = worse[0]
    alt_board = _play(board, [alt.san], 1)
    reply = list(alt.pv_san or [])[1:2]
    alt_checks = bool(reply) and _play(alt_board, reply, 1).is_check()
    key = best.move
    piece = board.piece_type_at(key.from_square)
    facts = {"cp": cp, "alt_cp": alt_cp, "check": board.gives_check(key), "capture": board.is_capture(key),
             "piece": chess.piece_name(piece), "queen_checks": _queen_checks(board, not side)}
    if objective == "avoid_perpetual" and not (facts["queen_checks"] and alt_checks):
        return None, "no perpetual-check danger to avoid"
    if objective == "coordinate" and LETTER_OF[piece] not in spec.pieces:
        return None, "the key move isn't made by the pieces to coordinate"
    if objective == "attack_king" and not (facts["check"] or any("+" in s for s in pv[:5:2])):
        return None, "no attack on the king"
    if objective == "trade" and not (facts["capture"] and len(pv) > 1 and "x" in pv[1]):
        return None, "no trade"
    if objective == "hold" and cp - alt_cp < 150:
        return None, "the alternatives don't lose enough to make holding the point"
    return Screened(key, best.san, cp, accepted, alt.san, alt_cp, alt_checks, facts), ""


LETTER_OF = {v: k for k, v in LETTER.items()}


def _queen_checks(board: chess.Board, color: chess.Color) -> int:
    """How many checks `color`'s queens could give if it were their move."""
    b = board.copy(stack=False)
    b.turn = color
    if not b.is_valid():
        return 0
    return sum(1 for mv in b.legal_moves if b.piece_type_at(mv.from_square) == chess.QUEEN and b.gives_check(mv))


# ------------------------------------------------------------------ 5. words + the pipeline
def _eval_words(cp: int) -> str:
    if cp >= 300:
        return "a winning advantage"
    if cp >= 150:
        return "a clear advantage"
    if cp >= 50:
        return "a small edge"
    if cp > -50:
        return "the balance"
    return "your best defensive chances"


def _pawns_words(board: chess.Board, side: chess.Color) -> str:
    mine, theirs = len(board.pieces(chess.PAWN, side)), len(board.pieces(chess.PAWN, not side))
    if not mine and not theirs:
        return "no pawns"
    def n(k):
        return "no pawns" if k == 0 else ("one pawn" if k == 1 else f"{k} pawns")
    return f"{n(mine)} for you, {n(theirs)} for your opponent"


HINTS = {
    "general": "Before moving, check every check and capture your opponent would have after your move.",
    "convert": "You are better: improve your position without allowing counterplay, and trade when it helps you.",
    "coordinate": "Your pieces are strongest when they protect each other and work on the same rank or file.",
    "avoid_perpetual": "Keep the queen's checks in mind: a careless move can let her check your king again and again.",
    "attack_king": "Look for moves that bring your pieces closer to the enemy king, with tempo if you can.",
    "trade": "A trade is good for you when what remains favours you — count what is left after it.",
    "win_material": "Look for a forcing sequence: checks first, then captures, then threats.",
    "hold": "Defend actively: find the move that keeps your pieces safe and your king out of danger.",
}


def build_raw(spec, objective: str, board: chess.Board, s: Screened, tier: str = "generated") -> dict:
    from ...planner.intent.material import objective_label
    from ...planner.intent.model import group_words
    from ..schema import replay
    side = board.turn
    color = "White" if side == chess.WHITE else "Black"
    mine, theirs = group_words(spec.pieces, article=True), group_words(spec.against, article=True)
    digest = hashlib.sha1(f"{board.board_fen()} {s.key_san} {objective}".encode()).hexdigest()[:10]
    kind = "check" if s.facts["check"] else ("capture" if s.facts["capture"] else "quiet move")
    loss = max(1, round((s.cp - s.alt_cp) / 100))
    explanation = (f"{s.key_san} keeps {_eval_words(s.cp)} (Stockfish). {s.alternative} looks natural but is "
                   f"clearly weaker — it gives away about {loss} pawn{'s' if loss != 1 else ''} of value")
    if s.alt_checks:
        explanation += ", and after it the opponent can start giving checks"
    explanation += "."
    if s.accepted:
        explanation += f" {' and '.join(s.accepted)} {'is' if len(s.accepted) == 1 else 'are'} about as good."
    focus = "" if objective == "general" else f" Focus: {objective_label(objective, spec).lower()}."
    return {
        "id": f"{'mygen' if tier == 'personal' else 'gen'}_material_{spec.slug()}_{digest}",
        "title": f"{spec.label()}: {color} to move", "concept": CONCEPT, "category": "endgames",
        "difficulty": 2 if kind != "quiet move" and s.cp - s.alt_cp >= 300 else (4 if kind == "quiet move" else 3),
        "description": (f"You ({color}) have {mine}; your opponent has {theirs} ({_pawns_words(board, side)}).{focus}"),
        "start_fen": board.fen(), "moves": [s.key_san], "key_move": replay(board.fen(), [s.key_san]).labels[0],
        "accepted": list(s.accepted),
        "prompt": f"You have {mine} against {theirs}. {color} to move — find the best move.{focus}",
        "hints": [HINTS.get(objective, HINTS["general"]), f"Stockfish's choice is a {s.facts['piece']} move ({kind})."],
        "explanation": explanation,
        "tags": ["material_endgame", spec.slug(), objective],
        "presentation_modes": ["interactive", "hint", "practice"], "teaching_purpose": "practice",
        "concept_params": {"material": spec.as_dict(), "objective": objective},
        "source": {"source_type": "procedural", "source_id": f"material:{spec.slug()}:{objective}",
                   "source_license": "generated", "source_url": ""},
    }


def generate(spec, library, engine, *, count: int = 5, objective: str = "general", time_budget: float = 20.0,
             max_attempts: int = 400, seed: int | None = None, depth: int = 14, save: bool = True,
             tier: str = "generated", avoid: set[str] = frozenset()) -> MaterialResult:
    """Up to `count` verified positions with exactly `spec`'s material for the learner."""
    from ..pipeline import verify_candidate
    result = MaterialResult(spec, objective)
    started = time.monotonic()
    if engine is None:
        result.stopped = "Stockfish is not available, so nothing can be verified"
        return result
    if spec.relation != "versus" or spec.head != "endgame":
        result.stopped = "only exact two-sided material requests are generated here"
        return result
    rng = random.Random(seed)
    seen: set[str] = set(avoid)
    while len(result.accepted) < count:
        if result.attempts >= max_attempts:
            result.stopped = "attempt limit"
            break
        if time.monotonic() - started > time_budget:
            result.stopped = "time budget"
            break
        result.attempts += 1
        learner = rng.choice([chess.WHITE, chess.BLACK])
        board = construct(spec, rng, learner)
        if board is None:
            result.rejected["could not place the pieces"] += 1
            continue
        reason = legal_reason(board, spec)
        if reason:
            result.rejected[reason] += 1
            continue
        if board.board_fen() in seen:
            result.rejected["duplicate"] += 1
            continue
        seen.add(board.board_fen())
        try:
            screened, reason = screen(board, spec, objective, engine, depth)
        except Exception as exc:  # an engine failure ends this candidate, not the request
            log.warning("material screening failed: %s", exc)
            result.rejected["engine error"] += 1
            continue
        if screened is None:
            result.rejected[reason] += 1
            continue
        raw = build_raw(spec, objective, board, screened, tier)
        report = verify_candidate(raw, library, engine, depth=depth, tier=tier, method="generator:material")
        if report.status != "verified":
            failed = next((st for st in report.stages if st.outcome in ("fail", "uncertain")), None)
            result.rejected[f"pipeline: {failed.name if failed else report.status}"] += 1
            continue
        if save:
            library.save_entry(report.example)
        result.accepted.append(report.example)
    result.elapsed = time.monotonic() - started
    return result
