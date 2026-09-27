"""Constructive proposers: build *candidate* positions for one tactical motif.

Nothing here decides that a position is good. A proposer only arranges pieces so that
the motif is geometrically possible (a knight one jump from a square that hits the king
and a rook, a rook with an open file to a boxed-in king, ...). Every proposal then goes
through the same pipeline as any library example — python-chess rules, the concept's
validator, Stockfish — plus the generator's "does this position really test the
concept" check. Most proposals are rejected there, and that is the point.

Positions are built for White to move and mirrored when the learner plays Black. They
are sparse on purpose (kings, the pieces of the idea, a few pawns and a distractor): a
beginner should see the pattern, not search a full middlegame.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

import chess

from ..positions import VALUES

W, B = chess.WHITE, chess.BLACK


@dataclass
class Proposal:
    fen: str
    key: chess.Move | None          # None = Stockfish picks the key move (then it is validated)
    motif: str
    variant: str
    difficulty: int = 1
    notes: dict = field(default_factory=dict)

    @property
    def signature(self) -> str:
        """Coarse shape of the idea, for variety tracking (not for duplicate detection)."""
        board = chess.Board(self.fen)
        return f"{self.motif}:{self.variant}:{'w' if board.turn else 'b'}"


Constructor = Callable[[random.Random, str], Proposal | None]


# ------------------------------------------------------------------------------ helpers
def _sq(file: int, rank: int) -> int | None:
    return chess.square(file, rank) if 0 <= file < 8 and 0 <= rank < 8 else None


def _empty(board: chess.Board, rng: random.Random, ok=lambda sq: True, tries: int = 64) -> int | None:
    squares = [s for s in chess.SQUARES if board.piece_at(s) is None and ok(s)]
    return rng.choice(squares) if squares else None


def _king_distance(a: int, b: int) -> int:
    return chess.square_distance(a, b)


def _place_king(board: chess.Board, color: chess.Color, rng: random.Random, near_home: bool = True,
                avoid: set[int] = frozenset()) -> bool:
    """Put `color`'s king on an empty square where it isn't attacked and isn't next to the other king."""
    other = board.king(not color)

    def ok(sq):
        if sq in avoid or other is not None and _king_distance(sq, other) < 2:
            return False
        if board.attackers(not color, sq):
            return False
        if near_home:
            rank = chess.square_rank(sq) if color == W else 7 - chess.square_rank(sq)
            if rank > 2:
                return False
        return True

    sq = _empty(board, rng, ok)
    if sq is None:
        return False
    board.set_piece_at(sq, chess.Piece(chess.KING, color))
    return True


def _pawns(board: chess.Board, rng: random.Random, color: chess.Color, count: int, forbid: set[int],
           keep_empty: set[int] = frozenset()) -> None:
    """A few pawns for realism, on their own half, never touching the squares of the idea
    (`forbid`: not on them, not attacking them) nor standing on its lines (`keep_empty`)."""
    for _ in range(count):
        def ok(sq):
            rank = chess.square_rank(sq) if color == W else 7 - chess.square_rank(sq)
            if not 1 <= rank <= 3 or sq in forbid or sq in keep_empty:
                return False
            attacks = chess.BB_PAWN_ATTACKS[color][sq]
            return not any(chess.BB_SQUARES[f] & attacks for f in forbid)
        sq = _empty(board, rng, ok)
        if sq is not None:
            board.set_piece_at(sq, chess.Piece(chess.PAWN, color))


def _valid(board: chess.Board) -> bool:
    return board.is_valid() and not board.is_check()


def _mirror_move(move: chess.Move | None) -> chess.Move | None:
    if move is None:
        return None
    return chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square), move.promotion)


def _finish(board: chess.Board, key: chess.Move | None, motif: str, variant: str, side: str,
            difficulty: int, **notes) -> Proposal | None:
    board.turn = W
    board.castling_rights = 0
    board.ep_square = None
    board.fullmove_number = 20 + len(board.piece_map())  # a plausible middlegame move number
    if not _valid(board) or (key is not None and key not in board.legal_moves):
        return None
    if side == "black":
        board = board.mirror()
        key = _mirror_move(key)
    return Proposal(board.fen(), key, motif, variant, difficulty, notes)


def _line_clear(board: chess.Board, a: int, b: int) -> bool:
    return not (chess.SquareSet.between(a, b) & chess.SquareSet(board.occupied))


def _between(*pairs: tuple[int, int]) -> set[int]:
    """Squares strictly between each pair (empty set for pairs not on a common line)."""
    out: set[int] = set()
    for a, b in pairs:
        out |= set(chess.SquareSet(chess.between(a, b)))
    return out


def _material_ok(board: chess.Board, side: chess.Color, max_lead: int = 3) -> bool:
    """The learner must not already be far ahead: then any move wins and nothing is tested."""
    def mat(c):
        return sum(VALUES[p.piece_type] for p in board.piece_map().values() if p.color == c and p.piece_type != chess.KING)
    return mat(side) - mat(not side) <= max_lead


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values() if p.color == color and p.piece_type != chess.KING)


def _even(board: chess.Board, rng: random.Random, keep_clear: set[int], max_gap: int = 1) -> bool:
    """Top up whichever side is behind until material is within `max_gap`, so the tactic is
    what decides the position. Extras never touch `keep_clear` (the tactic's squares and
    lines) or the enemy king; White's extras attack no black piece (no second tactic)."""
    for _ in range(4):
        gap = _material(board, W) - _material(board, B)
        if abs(gap) <= max_gap:
            return True
        color = B if gap > 0 else W
        ptype = min((chess.ROOK, chess.BISHOP, chess.KNIGHT), key=lambda t: (abs(VALUES[t] - abs(gap)), rng.random()))
        enemy_king = board.king(not color)
        home = range(0, 4) if color == W else range(4, 8)
        spots = [s for s in chess.SQUARES if board.piece_at(s) is None and s not in keep_clear
                 and chess.square_rank(s) in home]
        rng.shuffle(spots)
        for s in spots:
            board.set_piece_at(s, chess.Piece(ptype, color))
            reach = board.attacks(s)
            bad = reach & chess.SquareSet(keep_clear | ({enemy_king} if enemy_king is not None else set()))
            if color == W:
                bad = bad or reach & chess.SquareSet(board.occupied_co[B])
            if not bad:
                break
            board.remove_piece_at(s)
        else:
            return False
    return abs(_material(board, W) - _material(board, B)) <= max_gap


# ------------------------------------------------------------------------------- forks
FORK_VARIANTS = {
    "knight": [("king", chess.ROOK), ("king", chess.QUEEN), (chess.QUEEN, chess.ROOK), (chess.ROOK, chess.ROOK)],
    "queen": [("king", chess.ROOK), ("king", chess.KNIGHT), ("king", chess.BISHOP)],
    "pawn": [(chess.KNIGHT, chess.ROOK), (chess.ROOK, chess.ROOK), (chess.KNIGHT, chess.KNIGHT), (chess.ROOK, chess.QUEEN)],
}
PIECE = {"knight": chess.KNIGHT, "queen": chess.QUEEN, "pawn": chess.PAWN}


def _fork(piece_name: str) -> Constructor:
    ptype = PIECE[piece_name]

    def build(rng: random.Random, side: str) -> Proposal | None:
        first, second = rng.choice(FORK_VARIANTS[piece_name])
        board = chess.Board(None)
        # the forking square, then two target squares the piece would attack from there
        f = chess.square(rng.randint(1, 6), rng.randint(3, 6) if ptype != chess.PAWN else rng.randint(3, 5))
        if ptype == chess.KNIGHT:
            reach = list(chess.SquareSet(chess.BB_KNIGHT_ATTACKS[f]))
        elif ptype == chess.PAWN:
            reach = [s for s in (_sq(chess.square_file(f) - 1, chess.square_rank(f) + 1),
                                 _sq(chess.square_file(f) + 1, chess.square_rank(f) + 1)) if s is not None]
        else:  # queen: any square on its lines within 4 steps (the board is nearly empty)
            reach = [s for s in chess.SquareSet(chess.BB_RANK_ATTACKS[f][0] | chess.BB_FILE_ATTACKS[f][0]
                                                | chess.BB_DIAG_ATTACKS[f][0]) if 2 <= _king_distance(s, f) <= 4]
        if len(reach) < 2:
            return None
        t1, t2 = rng.sample(reach, 2)
        board.set_piece_at(t1, chess.Piece(chess.KING, B) if first == "king" else chess.Piece(first, B))
        board.set_piece_at(t2, chess.Piece(second, B))
        if ptype == chess.QUEEN and not _line_clear(board, f, t1) or ptype == chess.QUEEN and not _line_clear(board, f, t2):
            return None
        # where the piece comes from
        if ptype == chess.KNIGHT:
            origins = [s for s in chess.SquareSet(chess.BB_KNIGHT_ATTACKS[f]) if board.piece_at(s) is None]
        elif ptype == chess.PAWN:
            origins = [s for s in (_sq(chess.square_file(f), chess.square_rank(f) - 1),) if s is not None
                       and chess.square_rank(s) >= 1 and board.piece_at(s) is None]
        else:
            origins = [s for s in chess.SQUARES if board.piece_at(s) is None and s != f
                       and _king_distance(s, f) >= 2 and (chess.square_file(s) == chess.square_file(f)
                                                          or chess.square_rank(s) == chess.square_rank(f)
                                                          or abs(chess.square_file(s) - chess.square_file(f))
                                                          == abs(chess.square_rank(s) - chess.square_rank(f)))]
        rng.shuffle(origins)
        if not origins:
            return None
        origin = origins[0]
        board.set_piece_at(origin, chess.Piece(ptype, W))
        if ptype == chess.QUEEN and not _line_clear(board, origin, f):
            return None
        if ptype == chess.PAWN:  # a pawn fork needs its pawn protected
            guard = _sq(chess.square_file(f) + rng.choice((-1, 1)), chess.square_rank(f) - 1)
            if guard is None or board.piece_at(guard) is not None:
                return None
            board.set_piece_at(guard, chess.Piece(chess.PAWN, W))
        lines = _between((origin, f), (f, t1), (f, t2)) if ptype == chess.QUEEN else set()
        if board.king(B) is None and not _place_king(board, B, rng, near_home=False, avoid=lines):
            return None
        if not _place_king(board, W, rng, avoid=lines):
            return None
        _pawns(board, rng, W, rng.randint(1, 3), {f, origin, t1, t2}, lines)
        _pawns(board, rng, B, rng.randint(1, 3), {f, origin, t1, t2}, lines)
        key = chess.Move(origin, f)
        after = board.copy(stack=False)
        after.turn = W
        if key not in after.legal_moves:
            return None
        # the forking square must be safe (a fork that just loses the piece teaches nothing)
        after.push(key)
        if after.attackers(B, f):
            return None
        if not _even(board, rng, {f, t1, t2, origin} | lines):
            return None
        after = board.copy(stack=False)
        after.turn = W
        after.push(key)
        if after.attackers(B, f):
            return None
        check = first == "king"
        return _finish(board, key, f"{piece_name}_fork", f"{first if isinstance(first, str) else chess.piece_name(first)}"
                       f"+{chess.piece_name(second)}", side, 1 if check and piece_name == "knight" else 2)
    return build


# ---------------------------------------------------------------------- hanging piece
def _hanging(rng: random.Random, side: str) -> Proposal | None:
    board = chess.Board(None)
    victim = rng.choice([chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN])
    attacker = rng.choice([chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.PAWN])
    t = chess.square(rng.randint(0, 7), rng.randint(2, 6))
    board.set_piece_at(t, chess.Piece(victim, B))
    probe = chess.Board(None)
    probe.set_piece_at(t, chess.Piece(victim, B))
    options = []
    for s in chess.SQUARES:
        if s == t or _king_distance(s, t) > 4:
            continue
        probe.set_piece_at(s, chess.Piece(attacker, W))
        if attacker == chess.PAWN and chess.square_rank(s) in (0, 7):
            probe.remove_piece_at(s)
            continue
        if probe.is_attacked_by(W, t) and t in chess.SquareSet(probe.attacks_mask(s)):
            options.append(s)
        probe.remove_piece_at(s)
    if not options:
        return None
    a = rng.choice(options)
    board.set_piece_at(a, chess.Piece(attacker, W))
    if not _place_king(board, B, rng) or not _place_king(board, W, rng):
        return None
    distractor = rng.random() < 0.6
    if distractor:  # a defended piece next to the free one: taking it would lose material
        d = _empty(board, rng, lambda s: 2 <= chess.square_rank(s) <= 5 and _king_distance(s, t) <= 3)
        if d is not None:
            board.set_piece_at(d, chess.Piece(rng.choice([chess.KNIGHT, chess.BISHOP]), B))
            guard = _sq(chess.square_file(d) + rng.choice((-1, 1)), chess.square_rank(d) + 1)
            if guard is not None and board.piece_at(guard) is None and chess.square_rank(guard) < 7:
                board.set_piece_at(guard, chess.Piece(chess.PAWN, B))
    _pawns(board, rng, W, rng.randint(1, 3), {t, a})
    _pawns(board, rng, B, rng.randint(1, 3), {t, a})
    if board.attackers(B, t):  # the victim must really be undefended
        return None
    key = chess.Move(a, t)
    if attacker == chess.PAWN and chess.square_rank(t) == 7:
        key = chess.Move(a, t, chess.QUEEN)
    if not _material_ok(board, W, max_lead=0):
        return None
    return _finish(board, key, "hanging_piece", chess.piece_name(victim), side,
                   1 if not distractor else 2)


# ---------------------------------------------------------------------- mates in one
def _back_rank(rng: random.Random, side: str) -> Proposal | None:
    board = chess.Board(None)
    kf = rng.choice([0, 1, 2, 5, 6, 7])
    k = chess.square(kf, 7)
    board.set_piece_at(k, chess.Piece(chess.KING, B))
    for df in (-1, 0, 1):
        s = _sq(kf + df, 6)
        if s is not None:
            board.set_piece_at(s, chess.Piece(chess.PAWN, B))
    piece = rng.choice([chess.ROOK, chess.ROOK, chess.QUEEN])
    files = [f for f in range(8) if abs(f - kf) >= 2]
    mf = rng.choice(files)
    origin = chess.square(mf, rng.randint(0, 4))
    if board.piece_at(origin) is not None:
        return None
    board.set_piece_at(origin, chess.Piece(piece, W))
    target = chess.square(mf, 7)
    if not _line_clear(board, origin, target) or not _line_clear(board, target, k):
        return None
    # the defender: a black piece that is busy elsewhere (not guarding the back rank)
    defender = rng.choice([chess.ROOK, chess.QUEEN, chess.KNIGHT, chess.BISHOP])
    d = _empty(board, rng, lambda s: 2 <= chess.square_rank(s) <= 5)
    if d is not None:
        board.set_piece_at(d, chess.Piece(defender, B))
    if not _place_king(board, W, rng):
        return None
    _pawns(board, rng, W, rng.randint(2, 3), {origin, target})
    key = chess.Move(origin, target)
    probe = board.copy(stack=False)
    probe.turn = W
    if key not in probe.legal_moves:
        return None
    probe.push(key)
    if not probe.is_checkmate():  # cheap pre-filter; the validator and Stockfish still decide
        return None
    if not _material_ok(board, W, max_lead=2):
        return None
    return _finish(board, key, "back_rank_mate", chess.piece_name(piece), side, 1 if d is None else 2)


def _support_mate(rng: random.Random, side: str, pawns: bool = True) -> Proposal | None:
    """Queen mate supported by the king: the black king on the edge, the queen lands next to it."""
    board = chess.Board(None)
    kf = rng.randint(0, 7)
    k = chess.square(kf, 7)
    board.set_piece_at(k, chess.Piece(chess.KING, B))
    wk = chess.square(kf, 5)
    board.set_piece_at(wk, chess.Piece(chess.KING, W))
    target = chess.square(kf, 6)
    starts = [s for s in chess.SQUARES if board.piece_at(s) is None and s != target and _king_distance(s, target) >= 2
              and (chess.square_file(s) == kf or chess.square_rank(s) == 6
                   or abs(chess.square_file(s) - kf) == abs(chess.square_rank(s) - 6))]
    rng.shuffle(starts)
    origin = next((s for s in starts if _line_clear(board, s, target)), None)
    if origin is None:
        return None
    board.set_piece_at(origin, chess.Piece(chess.QUEEN, W))
    if pawns:
        _pawns(board, rng, W, rng.randint(0, 2), {origin, target, wk})
        _pawns(board, rng, B, rng.randint(1, 2), {origin, target, wk})
    key = chess.Move(origin, target)
    probe = board.copy(stack=False)
    probe.turn = W
    if key not in probe.legal_moves:
        return None
    probe.push(key)
    if not probe.is_checkmate():
        return None
    return _finish(board, key, "support_mate", "queen" if pawns else "bare", side, 1)


# --------------------------------------------------------------------- threat defence
def _threat(rng: random.Random, side: str) -> Proposal | None:
    """Black threatens to take a white piece; White must see it before doing anything else.
    The key move is chosen by Stockfish (key=None) and must then parry the threat."""
    board = chess.Board(None)
    victim = rng.choice([chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN])
    attacker = rng.choice([p for p in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK)
                           if VALUES[p] < VALUES[victim]] or [chess.PAWN])
    s = chess.square(rng.randint(0, 7), rng.randint(2, 5))
    board.set_piece_at(s, chess.Piece(victim, W))
    options = []
    for a in chess.SQUARES:
        if a == s or board.piece_at(a) is not None or _king_distance(a, s) > 3:
            continue
        if attacker == chess.PAWN and chess.square_rank(a) in (0, 7):
            continue
        board.set_piece_at(a, chess.Piece(attacker, B))
        if s in chess.SquareSet(board.attacks_mask(a)):
            options.append(a)
        board.remove_piece_at(a)
    if not options:
        return None
    a = rng.choice(options)
    board.set_piece_at(a, chess.Piece(attacker, B))
    # the attacker is defended, so "just take it back" is not an answer
    guard = _sq(chess.square_file(a) + rng.choice((-1, 1)), chess.square_rank(a) + 1)
    if guard is None or board.piece_at(guard) is not None or chess.square_rank(guard) >= 7:
        return None
    board.set_piece_at(guard, chess.Piece(chess.PAWN, B))
    if not _place_king(board, B, rng) or not _place_king(board, W, rng):
        return None
    # a tempting move elsewhere: a white piece that could grab a pawn
    extra = _empty(board, rng, lambda q: 1 <= chess.square_rank(q) <= 4)
    if extra is not None:
        board.set_piece_at(extra, chess.Piece(rng.choice([chess.KNIGHT, chess.BISHOP, chess.ROOK]), W))
    _pawns(board, rng, W, rng.randint(1, 3), {s, a})
    _pawns(board, rng, B, rng.randint(1, 3), {s, a})
    if board.attackers(W, s) and VALUES[attacker] >= VALUES[victim]:
        return None
    if not _material_ok(board, W, max_lead=4):
        return None
    return _finish(board, None, "threat", f"{chess.piece_name(attacker)}_vs_{chess.piece_name(victim)}", side, 1)


# ------------------------------------------------------------------- skewers and pins
_DIRECTIONS = {"orthogonal": [(1, 0), (-1, 0), (0, 1), (0, -1)], "diagonal": [(1, 1), (1, -1), (-1, 1), (-1, -1)]}


def _ray(start: int, d: tuple[int, int]) -> list[int]:
    out, f, r = [], chess.square_file(start), chess.square_rank(start)
    while True:
        f, r = f + d[0], r + d[1]
        s = _sq(f, r)
        if s is None:
            return out
        out.append(s)


def _line_attack(kind: str) -> Constructor:
    """kind: skewer (king in front, queen/rook behind) or absolute_pin (piece in front of the
    king). No relative pins: the pinned piece is usually defended by the queen behind it, so
    they rarely win material by force and Stockfish rejects nearly all of them."""
    def build(rng: random.Random, side: str) -> Proposal | None:
        board = chess.Board(None)
        geometry = rng.choice(["orthogonal", "diagonal"])
        slider = chess.ROOK if geometry == "orthogonal" else chess.BISHOP
        d = rng.choice(_DIRECTIONS[geometry])
        front_sq = chess.square(rng.randint(1, 6), rng.randint(2, 6))
        if kind == "skewer":
            front, back = chess.KING, rng.choice([chess.QUEEN, chess.ROOK] if slider == chess.BISHOP else [chess.QUEEN])
        else:
            front, back = rng.choice([chess.QUEEN, chess.ROOK] if slider == chess.BISHOP else [chess.QUEEN]), chess.KING
        behind = _ray(front_sq, d)
        if len(behind) < 1:
            return None
        back_sq = rng.choice(behind[:3])
        ahead = _ray(front_sq, (-d[0], -d[1]))
        if len(ahead) < 2:
            return None
        target = rng.choice(ahead[1:4] if len(ahead) > 1 else ahead)
        board.set_piece_at(front_sq, chess.Piece(front, B))
        board.set_piece_at(back_sq, chess.Piece(back, B))
        if not _line_clear(board, front_sq, back_sq) or not _line_clear(board, target, front_sq):
            return None
        # the slider arrives on the line from somewhere off it
        starts = [s for s in chess.SQUARES if board.piece_at(s) is None and s != target
                  and s not in ahead and s not in behind and _king_distance(s, target) >= 1]
        rng.shuffle(starts)
        origin = None
        for s in starts:
            board.set_piece_at(s, chess.Piece(slider, W))
            if target in chess.SquareSet(board.attacks_mask(s)):
                origin = s
                break
            board.remove_piece_at(s)
        if origin is None:
            return None
        line = _between((origin, target), (target, front_sq), (front_sq, back_sq))
        if kind != "skewer" and front == chess.QUEEN:
            # the pinned piece may be able to take the pinner: the pinner gets a pawn guard
            guard = _empty(board, rng, lambda g: 1 <= chess.square_rank(g) < 7 and g not in line and target in
                           chess.SquareSet(chess.BB_PAWN_ATTACKS[W][g]))
            if guard is None:
                return None
            board.set_piece_at(guard, chess.Piece(chess.PAWN, W))
        if board.king(B) is None and not _place_king(board, B, rng, near_home=False, avoid=line):
            return None
        if not _place_king(board, W, rng, avoid=line):
            return None
        _pawns(board, rng, W, rng.randint(1, 2), {origin, target, front_sq, back_sq}, line)
        _pawns(board, rng, B, rng.randint(1, 2), {origin, target, front_sq, back_sq}, line)
        key = chess.Move(origin, target)
        probe = board.copy(stack=False)
        probe.turn = W
        if key not in probe.legal_moves:
            return None
        probe.push(key)
        if probe.attackers(B, target) and not probe.attackers(W, target):
            return None
        if not _even(board, rng, {origin, target, front_sq, back_sq} | line):
            return None
        return _finish(board, key, kind, f"{chess.piece_name(slider)}:{chess.piece_name(front)}-{chess.piece_name(back)}",
                       side, 2)
    return build


# ------------------------------------------------------------------------ endgames
def _opposition(rng: random.Random, side: str) -> Proposal | None:
    """King and pawn against king, White to move: Stockfish picks the move (key=None); the
    validator then requires it to take the opposition and the endgame check to keep the win."""
    board = chess.Board(None)
    pf = rng.randint(1, 6)  # no rook pawns (those are usually draws)
    pr = rng.randint(1, 4)
    pawn = chess.square(pf, pr)
    bk = chess.square(pf + rng.randint(-1, 1), min(7, pr + rng.randint(3, 5)))
    if bk is None or chess.square_rank(bk) <= pr + 1:
        return None
    # White's king one step from the square in direct opposition to Black's king
    opp = _sq(chess.square_file(bk), chess.square_rank(bk) - 2)
    if opp is None:
        return None
    around = [s for s in chess.SquareSet(chess.BB_KING_ATTACKS[opp]) if chess.square_rank(s) <= chess.square_rank(opp)]
    rng.shuffle(around)
    wk = next((s for s in around if s not in (pawn, bk) and _king_distance(s, bk) >= 2), None)
    if wk is None:
        return None
    board.set_piece_at(pawn, chess.Piece(chess.PAWN, W))
    board.set_piece_at(bk, chess.Piece(chess.KING, B))
    board.set_piece_at(wk, chess.Piece(chess.KING, W))
    return _finish(board, None, "opposition", "kpk", side, 2)


CONSTRUCTORS: dict[str, Constructor] = {
    "knight_fork": _fork("knight"),
    "queen_fork": _fork("queen"),
    "pawn_fork": _fork("pawn"),
    "hanging_piece": _hanging,
    "back_rank_mate": _back_rank,
    "support_mate": _support_mate,
    "bare_queen_mate": lambda rng, side: _support_mate(rng, side, pawns=False),
    "threat": _threat,
    "skewer": _line_attack("skewer"),
    "absolute_pin": _line_attack("absolute_pin"),
    "opposition": _opposition,
}
