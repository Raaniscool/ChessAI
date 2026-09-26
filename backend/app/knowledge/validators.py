"""Concept validators: proof that an example really shows its concept.

A legal position is not automatically a correct teaching example. Each concept
names a validator (knowledge/data/concepts.json → "validator"), and each
validator checks that concept's actual geometry with python-chess — a fork
attacks two worthwhile targets, a pin has a slider, a pinned piece and a more
valuable piece (or the king) behind it on one line, a back-rank mate has the
king boxed in on its first rank by its own pieces, and so on.

Validators return *facts* (JSON-friendly dicts). The facts are stored with the
example and are what Qwen receives when it explains the example, so the
explanation is grounded in the verified geometry rather than in the model's
reading of a FEN string.

Adding a concept = registering a function here (or reusing one with params).

    @validator("my_motif")
    def my_motif(ctx: Ctx, params: dict) -> dict:  # facts
        ...raise Fail("why not") when the motif isn't there

Validators are deterministic: same example, same verdict, no engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import chess

from .positions import (NAME_TO_TYPE, PIECE_NAMES, SLIDERS, VALUES, Replay, developed_minors,
                        is_worth_attacking, king_escape_report, material, piece_fact,
                        threatened_targets)


class Fail(Exception):
    """The example does not show what it claims."""


@dataclass
class Ctx:
    replay: Replay
    key_ply: int | None = None  # the move the learner must find
    mistake_ply: int | None = None  # the move that is the mistake (mistake concepts)
    opening_index: object | None = None  # sources.lichess_openings.OpeningIndex

    @property
    def learner(self) -> chess.Color:
        if self.key_ply is not None:
            return self.replay.boards[self.key_ply].turn
        if self.mistake_ply is not None:  # the side that punishes the mistake
            return not self.replay.boards[self.mistake_ply].turn
        return self.replay.boards[0].turn

    def label(self, ply: int) -> str:
        return self.replay.labels[ply]


Validator = Callable[[Ctx, dict], dict]
REGISTRY: dict[str, Validator] = {}


def validator(name: str) -> Callable[[Validator], Validator]:
    def deco(fn: Validator) -> Validator:
        REGISTRY[name] = fn
        return fn
    return deco


def run(name: str, ctx: Ctx, params: dict | None = None) -> dict:
    """Run validator `name`; returns its facts or raises Fail."""
    fn = REGISTRY.get(name)
    if fn is None:
        raise Fail(f"unknown validator {name!r}")
    try:
        return fn(ctx, dict(params or {}))
    except Fail:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise Fail(f"{name}: bad parameters or data ({exc})") from exc


# ------------------------------------------------------------------ helpers

def _plies(ctx: Ctx, params: dict, side: str = "learner") -> list[int]:
    """Plies to look at: an explicit 'at', else the learner's moves from the key move on."""
    r = ctx.replay
    if params.get("at") not in (None, "start", "end"):
        return [r.ply(params["at"])]
    start = ctx.key_ply if ctx.key_ply is not None else 0
    if side == "any":
        return list(range(start, len(r.moves)))
    color = ctx.learner
    return [i for i in range(start, len(r.moves)) if r.boards[i].turn == color]


def _position(ctx: Ctx, params: dict, default: str = "end") -> tuple[chess.Board, str]:
    at = params.get("at", default)
    return ctx.replay.position(at), at


def _color(name: str) -> chess.Color:
    if name not in ("white", "black"):
        raise Fail(f"side must be white or black, not {name!r}")
    return name == "white"


def _side_name(color: chess.Color) -> str:
    return "white" if color else "black"


def _want_piece(params: dict) -> int | None:
    name = params.get("piece")
    if name is None:
        return None
    if name not in NAME_TO_TYPE:
        raise Fail(f"unknown piece {name!r}")
    return NAME_TO_TYPE[name]


def _check_targets(params: dict, found: list[str]) -> None:
    for sq in params.get("targets", []) or []:
        if sq not in found:
            raise Fail(f"{sq} is not among the attacked targets ({', '.join(found) or 'none'})")


# -------------------------------------------------------------- basic rules

@validator("check")
def v_check(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    for i in _plies(ctx, params, "any"):
        board = r.boards[i + 1]
        if board.is_check():
            return {"move": ctx.label(i), "king": chess.square_name(board.king(board.turn)),
                    "checkers": [piece_fact(board, s) for s in board.checkers()]}
    raise Fail("no move gives check")


@validator("checkmate")
def v_checkmate(ctx: Ctx, params: dict) -> dict:
    board, at = _position(ctx, params)
    if not board.is_check():
        raise Fail(f"the king is not in check after {at} — not checkmate")
    if any(board.legal_moves):
        escape = next(iter(board.legal_moves))
        raise Fail(f"not checkmate after {at}: {board.san(escape)} is legal")
    return {"mated_side": _side_name(board.turn), "king": chess.square_name(board.king(board.turn)),
            "checkers": [piece_fact(board, s) for s in board.checkers()],
            "escape_squares": king_escape_report(board),
            "mating_move": ctx.replay.labels[-1] if at == "end" else at}


@validator("stalemate")
def v_stalemate(ctx: Ctx, params: dict) -> dict:
    board, at = _position(ctx, params)
    if board.is_check():
        raise Fail(f"the king is in check after {at} — that is not stalemate")
    if any(board.legal_moves):
        raise Fail(f"not stalemate after {at}: {board.san(next(iter(board.legal_moves)))} is legal")
    return {"stalemated_side": _side_name(board.turn), "king": chess.square_name(board.king(board.turn)),
            "in_check": False, "legal_moves": 0, "king_squares": king_escape_report(board)}


def _find_move(ctx: Ctx, params: dict, test, what: str) -> tuple[int, chess.Board, chess.Move]:
    r = ctx.replay
    for i in _plies(ctx, params, "any"):
        if test(r.boards[i], r.moves[i]):
            return i, r.boards[i], r.moves[i]
    raise Fail(f"no {what} in the moves")


@validator("capture")
def v_capture(ctx: Ctx, params: dict) -> dict:
    i, board, move = _find_move(ctx, params, lambda b, m: b.is_capture(m), "capture")
    captured = chess.square_name(move.to_square) if not board.is_en_passant(move) else None
    victim = board.piece_at(move.to_square)
    return {"move": ctx.label(i), "capturing_piece": piece_fact(board, move.from_square),
            "captured": PIECE_NAMES[victim.piece_type] if victim else "pawn", "square": captured
            or chess.square_name(move.to_square)}


@validator("castling")
def v_castling(ctx: Ctx, params: dict) -> dict:
    wing = params.get("wing")

    def test(b, m):
        if not b.is_castling(m):
            return False
        return wing is None or (b.is_kingside_castling(m) == (wing == "kingside"))

    i, board, move = _find_move(ctx, params, test, f"{wing or ''} castling move".strip())
    after = ctx.replay.boards[i + 1]
    rook = [s for s in after.pieces(chess.ROOK, board.turn) if not board.piece_at(s)]
    return {"move": ctx.label(i), "side": _side_name(board.turn),
            "wing": "kingside" if board.is_kingside_castling(move) else "queenside",
            "king_to": chess.square_name(after.king(board.turn)),
            "rook_to": chess.square_name(rook[0]) if rook else None}


@validator("cannot_castle")
def v_cannot_castle(ctx: Ctx, params: dict) -> dict:
    board, at = _position(ctx, params, default="end")
    color = _color(params["side"])
    wing = params.get("wing", "kingside")
    if board.turn != color:
        raise Fail(f"it isn't {params['side']}'s move after {at}")
    has_right = board.has_kingside_castling_rights(color) if wing == "kingside" \
        else board.has_queenside_castling_rights(color)
    test = board.is_kingside_castling if wing == "kingside" else board.is_queenside_castling
    if any(test(m) for m in board.legal_moves):
        raise Fail(f"{params['side']} CAN castle {wing} after {at}")
    if not has_right:
        return {"side": params["side"], "wing": wing, "reason": "castling right lost (king or rook has moved)"}
    king = board.king(color)
    rank = chess.square_rank(king)
    path = [chess.square(f, rank) for f in ((5, 6) if wing == "kingside" else (3, 2))]
    between = [chess.square(f, rank) for f in ((5, 6) if wing == "kingside" else (1, 2, 3))]
    if board.is_check():
        reason = "the king is in check"
    elif any(board.piece_at(s) for s in between):
        reason = "pieces stand between the king and the rook"
    else:
        hit = [chess.square_name(s) for s in path if board.is_attacked_by(not color, s)]
        reason = f"the king would pass through or land on an attacked square ({', '.join(hit)})"
    return {"side": params["side"], "wing": wing, "reason": reason}


@validator("en_passant")
def v_en_passant(ctx: Ctx, params: dict) -> dict:
    i, board, move = _find_move(ctx, params, lambda b, m: b.is_en_passant(m), "en passant capture")
    captured = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
    return {"move": ctx.label(i), "pawn_from": chess.square_name(move.from_square),
            "lands_on": chess.square_name(move.to_square), "captured_pawn_on": chess.square_name(captured),
            "previous_move": ctx.label(i - 1) if i else None}


@validator("promotion")
def v_promotion(ctx: Ctx, params: dict) -> dict:
    want = params.get("promote_to")
    i, board, move = _find_move(
        ctx, params, lambda b, m: m.promotion is not None and
        (want is None or PIECE_NAMES[m.promotion] == want), "promotion")
    return {"move": ctx.label(i), "square": chess.square_name(move.to_square),
            "promoted_to": PIECE_NAMES[move.promotion], "side": _side_name(board.turn)}


@validator("illegal_move")
def v_illegal_move(ctx: Ctx, params: dict) -> dict:
    board, at = _position(ctx, params, default="end")
    text = params["move"]
    try:
        board.parse_san(text)
    except ValueError:
        pass
    else:
        raise Fail(f"{text} is legal after {at}")
    try:
        move = chess.Move.from_uci(params["uci"]) if params.get("uci") else None
    except ValueError:
        move = None
    reason = "that piece cannot move that way"
    if move is not None and board.is_pseudo_legal(move) and board.is_into_check(move):
        reason = "it would leave the king in check"
    return {"move": text, "position_after": at, "legal": False, "reason": params.get("reason_hint") or reason}


# ------------------------------------------------------------------ tactics

@validator("fork")
def v_fork(ctx: Ctx, params: dict) -> dict:
    """The moved piece attacks two or more worthwhile targets at once."""
    r, want = ctx.replay, _want_piece(params)
    best = None
    for i in _plies(ctx, params):
        board, move = r.boards[i + 1], r.moves[i]
        attacker = board.piece_at(move.to_square)
        if want is not None and attacker.piece_type != want:
            continue
        targets = threatened_targets(board, move.to_square)
        if len(targets) >= 2:
            names = [chess.square_name(t) for t in targets]
            if params.get("targets") and not set(params["targets"]) <= set(names):
                continue
            best = (i, board, move, targets)
            break
    if best is None:
        kind = PIECE_NAMES[want] + " " if want else ""
        raise Fail(f"no {kind}fork: no move attacks two worthwhile targets at once")
    i, board, move, targets = best
    return {"move": ctx.label(i), "attacker": piece_fact(board, move.to_square),
            "targets": [piece_fact(board, t) for t in targets],
            "gives_check": board.is_check()}


@validator("double_attack")
def v_double_attack(ctx: Ctx, params: dict) -> dict:
    """After one move, two worthwhile targets are newly attacked (by one or more pieces)."""
    r = ctx.replay
    for i in _plies(ctx, params):
        before, board = r.boards[i], r.boards[i + 1]
        mover = before.turn
        found = []
        for sq in chess.SquareSet(board.occupied_co[not mover]):
            attackers = [a for a in board.attackers(mover, sq) if is_worth_attacking(board, a, sq)]
            new = [a for a in attackers if a not in before.attackers(mover, sq) or a == r.moves[i].to_square]
            if new:
                found.append((sq, new[0]))
        if len(found) >= 2:
            return {"move": ctx.label(i),
                    "attacks": [{"target": piece_fact(board, t), "by": piece_fact(board, a)} for t, a in found]}
    raise Fail("no move creates two new threats at once")


def _pins(board: chess.Board, pinner_side: chess.Color) -> list[tuple[int, int, int]]:
    """(pinner, pinned, behind) triples for pins by `pinner_side`."""
    out = []
    for s in chess.SquareSet(board.occupied_co[pinner_side]):
        piece = board.piece_at(s)
        if piece.piece_type not in SLIDERS:
            continue
        for target in board.attacks(s):
            victim = board.piece_at(target)
            if victim is None or victim.color == pinner_side or victim.piece_type == chess.KING:
                continue
            ray = chess.ray(s, target)
            beyond = [q for q in chess.SquareSet(ray)
                      if chess.square_distance(q, s) > chess.square_distance(target, s)
                      and _on_same_side(s, target, q)]
            beyond.sort(key=lambda q: chess.square_distance(q, s))
            behind = next((q for q in beyond if board.piece_at(q)), None)
            if behind is None:
                continue
            bp = board.piece_at(behind)
            if bp.color == pinner_side:
                continue
            if bp.piece_type == chess.KING or VALUES[bp.piece_type] > VALUES[victim.piece_type]:
                out.append((s, target, behind))
    return out


def _on_same_side(origin: int, mid: int, far: int) -> bool:
    """`far` lies beyond `mid` when looking from `origin` along one line."""
    df1 = chess.square_file(mid) - chess.square_file(origin)
    dr1 = chess.square_rank(mid) - chess.square_rank(origin)
    df2 = chess.square_file(far) - chess.square_file(origin)
    dr2 = chess.square_rank(far) - chess.square_rank(origin)
    sign = lambda x: (x > 0) - (x < 0)  # noqa: E731
    return sign(df1) == sign(df2) and sign(dr1) == sign(dr2)


def _pin_facts(board: chess.Board, pin: tuple[int, int, int]) -> dict:
    pinner, pinned, behind = pin
    absolute = board.piece_at(behind).piece_type == chess.KING
    return {"pinner": piece_fact(board, pinner), "pinned": piece_fact(board, pinned),
            "behind": piece_fact(board, behind), "kind": "absolute" if absolute else "relative"}


@validator("pin")
def v_pin(ctx: Ctx, params: dict) -> dict:
    """A slider pins an enemy piece to its king (absolute) or a more valuable piece (relative).

    The move must create the pin or exploit it (attack the pinned piece)."""
    r = ctx.replay
    kind = params.get("kind")
    demo = ctx.key_ply is None and ctx.mistake_ply is None  # a demonstration just has to show a pin
    for i in _plies(ctx, params, "any" if demo else "learner"):
        board, move = r.boards[i + 1], r.moves[i]
        mover = r.boards[i].turn
        for pin in _pins(board, mover):
            facts = _pin_facts(board, pin)
            if kind and facts["kind"] != kind:
                continue
            if params.get("pinned") and facts["pinned"]["square"] != params["pinned"]:
                continue
            created = pin not in _pins(r.boards[i], mover) or pin[0] == move.to_square
            exploits = pin[1] in board.attacks(move.to_square)
            if created or exploits or demo:
                facts.update({"move": ctx.label(i), "created_by_move": created})
                return facts
    raise Fail("no pin is created or exploited")


@validator("skewer")
def v_skewer(ctx: Ctx, params: dict) -> dict:
    """The moved slider attacks a valuable piece; behind it, on the same line, a lesser one."""
    r = ctx.replay
    for i in _plies(ctx, params):
        board, move = r.boards[i + 1], r.moves[i]
        s = move.to_square
        piece = board.piece_at(s)
        if piece.piece_type not in SLIDERS:
            continue
        for front in board.attacks(s):
            fp = board.piece_at(front)
            if fp is None or fp.color == piece.color:
                continue
            beyond = sorted((q for q in chess.SquareSet(chess.ray(s, front))
                             if chess.square_distance(q, s) > chess.square_distance(front, s)
                             and _on_same_side(s, front, q)), key=lambda q: chess.square_distance(q, s))
            back = next((q for q in beyond if board.piece_at(q)), None)
            if back is None:
                continue
            bp = board.piece_at(back)
            if bp.color == piece.color or bp.piece_type == chess.PAWN and fp.piece_type != chess.KING:
                continue
            if fp.piece_type == chess.KING or VALUES[fp.piece_type] > VALUES[bp.piece_type]:
                return {"move": ctx.label(i), "attacker": piece_fact(board, s),
                        "front": piece_fact(board, front), "back": piece_fact(board, back),
                        "front_is_king": fp.piece_type == chess.KING}
    raise Fail("no skewer: no slider hits a valuable piece with a lesser one behind it")


@validator("discovered_attack")
def v_discovered_attack(ctx: Ctx, params: dict) -> dict:
    """Moving one piece uncovers an attack by another piece on a worthwhile target."""
    r = ctx.replay
    for i in _plies(ctx, params):
        before, board, move = r.boards[i], r.boards[i + 1], r.moves[i]
        mover = before.turn
        for target in chess.SquareSet(board.occupied_co[not mover]):
            for a in board.attackers(mover, target):
                if a == move.to_square or a in before.attackers(mover, target):
                    continue
                if board.piece_at(a).piece_type in SLIDERS and is_worth_attacking(board, a, target):
                    return {"move": ctx.label(i), "moved_piece": piece_fact(board, move.to_square),
                            "revealed_attacker": piece_fact(board, a), "target": piece_fact(board, target),
                            "is_check": board.piece_at(target).piece_type == chess.KING}
    raise Fail("no discovered attack: no move uncovers a new threat by another piece")


@validator("discovered_check")
def v_discovered_check(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    for i in _plies(ctx, params):
        board, move = r.boards[i + 1], r.moves[i]
        others = [c for c in board.checkers() if c != move.to_square]
        if others:
            return {"move": ctx.label(i), "moved_piece": piece_fact(board, move.to_square),
                    "checking_piece": piece_fact(board, others[0]),
                    "double_check": len(board.checkers()) > 1}
    raise Fail("no discovered check: no move reveals a check by another piece")


@validator("double_check")
def v_double_check(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    for i in _plies(ctx, params):
        board = r.boards[i + 1]
        checkers = list(board.checkers())
        if len(checkers) >= 2:
            return {"move": ctx.label(i), "checkers": [piece_fact(board, c) for c in checkers],
                    "king": chess.square_name(board.king(board.turn)),
                    "only_king_moves": all(board.piece_at(m.from_square).piece_type == chess.KING
                                           for m in board.legal_moves)}
    raise Fail("no double check: no move gives check with two pieces at once")


@validator("hanging_piece")
def v_hanging_piece(ctx: Ctx, params: dict) -> dict:
    """The learner captures an undefended piece (or a piece worth more than the capturer)."""
    r = ctx.replay
    for i in _plies(ctx, params):
        before, move = r.boards[i], r.moves[i]
        victim = before.piece_at(move.to_square)
        if victim is None or victim.piece_type == chess.PAWN and not params.get("pawns"):
            continue
        undefended = not before.attackers(victim.color, move.to_square)
        cheaper = VALUES[before.piece_at(move.from_square).piece_type] < VALUES[victim.piece_type]
        if undefended or cheaper:
            return {"move": ctx.label(i), "captured": piece_fact(before, move.to_square),
                    "capturer": piece_fact(before, move.from_square), "was_defended": not undefended}
    raise Fail("no free piece is captured")


@validator("trapped_piece")
def v_trapped_piece(ctx: Ctx, params: dict) -> dict:
    """After a learner move an enemy piece is attacked and every escape square is covered."""
    r = ctx.replay
    for i in _plies(ctx, params):
        board = r.boards[i + 1]
        mover = r.boards[i].turn
        for sq in chess.SquareSet(board.occupied_co[not mover]):
            piece = board.piece_at(sq)
            if piece.piece_type in (chess.PAWN, chess.KING) or not board.attackers(mover, sq):
                continue
            if _trapped(board, sq):
                return {"move": ctx.label(i), "trapped": piece_fact(board, sq),
                        "attacked_by": [piece_fact(board, a) for a in board.attackers(mover, sq)]}
    raise Fail("no trapped piece")


def _trapped(board: chess.Board, sq: int) -> bool:
    piece = board.piece_at(sq)
    probe = board.copy(stack=False)
    probe.turn = piece.color
    if probe.is_check():
        return False
    for move in probe.legal_moves:
        if move.from_square != sq:
            continue
        captured = probe.piece_at(move.to_square)
        if captured and VALUES[captured.piece_type] >= VALUES[piece.piece_type]:
            return False
        after = probe.copy(stack=False)
        after.push(move)
        attackers = after.attackers(not piece.color, move.to_square)
        if not attackers:
            return False
        cheapest = min(VALUES[after.piece_at(a).piece_type] for a in attackers)
        defended = bool(after.attackers(piece.color, move.to_square))
        if defended and cheapest >= VALUES[piece.piece_type]:
            return False
    return True


@validator("material_gain")
def v_material_gain(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    color = _color(params["side"]) if params.get("side") else ctx.learner
    start = r.boards[ctx.key_ply] if ctx.key_ply is not None else r.boards[0]
    end = r.position(params.get("to", "end"))
    delta = (material(end, color) - material(end, not color)) - (material(start, color) - material(start, not color))
    if delta < params.get("min", 2):
        raise Fail(f"{_side_name(color)} gains only {delta} points of material")
    return {"side": _side_name(color), "material_gained": delta}


@validator("sacrifice")
def v_sacrifice(ctx: Ctx, params: dict) -> dict:
    """The learner gives up material on purpose, and the line ends in mate or a net gain."""
    r = ctx.replay
    color = ctx.learner
    start = r.boards[ctx.key_ply or 0]
    lowest, low_at = 0, None
    base = material(start, color) - material(start, not color)
    for i in range(ctx.key_ply or 0, len(r.moves)):
        b = r.boards[i + 1]
        bal = material(b, color) - material(b, not color) - base
        if bal < lowest:
            lowest, low_at = bal, i
    if low_at is None or lowest > -2:
        raise Fail("no material is sacrificed")
    end = r.final
    final = material(end, color) - material(end, not color) - base
    mate = end.is_checkmate() and end.turn != color
    if not mate and final < 0 and not params.get("positional"):
        raise Fail("the sacrifice is never paid back (no mate, no material regained)")
    return {"material_given": -lowest, "given_after": ctx.label(low_at),
            "ends_in_mate": mate, "final_material_balance": final}


@validator("deflection")
def v_deflection(ctx: Ctx, params: dict) -> dict:
    """A forcing move drags a defender away from what it guards; the guarded point then falls."""
    r = ctx.replay
    for i in _plies(ctx, params):
        if i + 2 >= len(r.moves):
            break
        before, board, reply, follow = r.boards[i], r.boards[i + 1], r.moves[i + 1], r.moves[i + 2]
        defender_from = reply.from_square
        # the defender left a square from which it guarded the square the learner uses next
        if follow.to_square in before.attacks(defender_from) and \
                follow.to_square not in r.boards[i + 2].attacks(reply.to_square):
            return {"move": ctx.label(i), "defender": piece_fact(board, defender_from),
                    "dragged_to": chess.square_name(reply.to_square),
                    "undefended_square": chess.square_name(follow.to_square),
                    "follow_up": ctx.label(i + 2)}
    raise Fail("no deflection: no defender is lured away from the square the attack then uses")


@validator("removing_defender")
def v_removing_defender(ctx: Ctx, params: dict) -> dict:
    """The learner captures a piece that was defending something the learner then wins."""
    r = ctx.replay
    for i in _plies(ctx, params):
        before, move = r.boards[i], r.moves[i]
        victim = before.piece_at(move.to_square)
        if victim is None:
            continue
        guarded = [s for s in before.attacks(move.to_square)
                   if before.piece_at(s) and before.piece_at(s).color == victim.color]
        for j in range(i + 2, len(r.moves), 2):
            target = r.moves[j].to_square
            if target in guarded or (r.boards[j + 1].is_checkmate() and target in before.attacks(move.to_square)):
                return {"move": ctx.label(i), "removed_defender": piece_fact(before, move.to_square),
                        "then": ctx.label(j), "won_square": chess.square_name(target)}
            if target in before.attacks(move.to_square) and r.boards[j].piece_at(target):
                return {"move": ctx.label(i), "removed_defender": piece_fact(before, move.to_square),
                        "then": ctx.label(j), "won_square": chess.square_name(target)}
    raise Fail("no defender is removed")


@validator("zwischenzug")
def v_zwischenzug(ctx: Ctx, params: dict) -> dict:
    """Instead of the expected recapture, the learner first inserts a forcing move."""
    r = ctx.replay
    for i in _plies(ctx, params):
        if i == 0:
            continue
        before, move, prev = r.boards[i], r.moves[i], r.moves[i - 1]
        was_capture = r.boards[i - 1].is_capture(prev)
        recapture_available = any(m.to_square == prev.to_square for m in before.legal_moves
                                  if before.is_capture(m))
        forcing = r.boards[i + 1].is_check() or bool(threatened_targets(r.boards[i + 1], move.to_square))
        if was_capture and recapture_available and move.to_square != prev.to_square and forcing:
            return {"move": ctx.label(i), "instead_of_recapturing_on": chess.square_name(prev.to_square),
                    "forcing": "check" if r.boards[i + 1].is_check() else "threat"}
    raise Fail("no in-between move: the expected recapture is never delayed by a forcing move")


@validator("overloaded_piece")
def v_overloaded(ctx: Ctx, params: dict) -> dict:
    """One enemy piece guards two things; the learner makes it choose (a deflection of an overloaded guard)."""
    facts = v_deflection(ctx, params)
    r = ctx.replay
    i = r.ply(facts["move"])
    before = r.boards[i]
    guard = before.piece_at(chess.parse_square(facts["defender"]["square"]))
    duties = [chess.square_name(s) for s in before.attacks(chess.parse_square(facts["defender"]["square"]))
              if (before.piece_at(s) and before.piece_at(s).color == guard.color) or
              s == r.moves[i].to_square or s == chess.parse_square(facts["undefended_square"])]
    if len(duties) < 2:
        raise Fail("the defender had only one duty — not overloaded")
    facts["duties"] = duties
    return facts


# ------------------------------------------------------------- mate patterns

def _mate(ctx: Ctx, params: dict) -> tuple[chess.Board, dict]:
    facts = v_checkmate(ctx, params)
    return ctx.replay.position(params.get("at", "end")), facts


@validator("back_rank_mate")
def v_back_rank(ctx: Ctx, params: dict) -> dict:
    board, facts = _mate(ctx, params)
    color = board.turn
    king = board.king(color)
    home = 0 if color == chess.WHITE else 7
    if chess.square_rank(king) != home:
        raise Fail("the mated king is not on its back rank")
    checkers = list(board.checkers())
    if not any(board.piece_at(c).piece_type in (chess.ROOK, chess.QUEEN) and chess.square_rank(c) == home
               for c in checkers):
        raise Fail("the mate is not delivered along the back rank by a rook or queen")
    forward = [s for s in chess.SquareSet(chess.BB_KING_ATTACKS[king]) if chess.square_rank(s) != home]
    own = [s for s in forward if board.piece_at(s) and board.piece_at(s).color == color]
    if not own:
        raise Fail("the king's own pieces don't box it in")
    facts["pattern"] = "back-rank mate"
    facts["boxed_in_by"] = [piece_fact(board, s) for s in own]
    return facts


@validator("smothered_mate")
def v_smothered(ctx: Ctx, params: dict) -> dict:
    board, facts = _mate(ctx, params)
    color = board.turn
    checkers = list(board.checkers())
    if len(checkers) != 1 or board.piece_at(checkers[0]).piece_type != chess.KNIGHT:
        raise Fail("a smothered mate is delivered by a knight alone")
    king = board.king(color)
    around = list(chess.SquareSet(chess.BB_KING_ATTACKS[king]))
    own = [s for s in around if board.piece_at(s) and board.piece_at(s).color == color]
    if len(own) < len(around) - 1 or not all(
            (board.piece_at(s) and board.piece_at(s).color == color) or board.is_attacked_by(not color, s)
            for s in around):
        raise Fail("the king is not smothered by its own pieces")
    facts["pattern"] = "smothered mate"
    facts["smothered_by"] = [piece_fact(board, s) for s in own]
    return facts


def _piece_mate(ctx: Ctx, params: dict, types: set[int], name: str) -> dict:
    board, facts = _mate(ctx, params)
    if not any(board.piece_at(c).piece_type in types for c in board.checkers()):
        raise Fail(f"{name}: the mating piece is not a {' or '.join(PIECE_NAMES[t] for t in types)}")
    facts["pattern"] = name
    return facts


@validator("queen_mate")
def v_queen_mate(ctx: Ctx, params: dict) -> dict:
    """King and queen against a lone king, ending in mate."""
    r = ctx.replay
    start = r.boards[0]
    winner = ctx.learner
    if start.occupied_co[not winner] != chess.BB_SQUARES[start.king(not winner)] or \
            len(start.pieces(chess.QUEEN, winner)) != 1 or \
            chess.popcount(start.occupied_co[winner]) != 2:
        raise Fail("the start position is not king and queen against a lone king")
    return _piece_mate(ctx, params, {chess.QUEEN}, "king and queen mate")


@validator("rook_mate")
def v_rook_mate(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    start = r.boards[0]
    winner = ctx.learner
    if start.occupied_co[not winner] != chess.BB_SQUARES[start.king(not winner)] or \
            len(start.pieces(chess.ROOK, winner)) != 1 or \
            chess.popcount(start.occupied_co[winner]) != 2:
        raise Fail("the start position is not king and rook against a lone king")
    return _piece_mate(ctx, params, {chess.ROOK}, "king and rook mate")


@validator("ladder_mate")
def v_ladder_mate(ctx: Ctx, params: dict) -> dict:
    """Two heavy pieces take turns checking the king up to the edge."""
    board, facts = _mate(ctx, params)
    color = board.turn
    heavies = list(board.pieces(chess.ROOK, not color) | board.pieces(chess.QUEEN, not color))
    if len(heavies) < 2:
        raise Fail("a ladder mate needs two rooks/queens")
    king = board.king(color)
    edge = chess.square_rank(king) in (0, 7) or chess.square_file(king) in (0, 7)
    if not edge:
        raise Fail("the king is not mated on the edge")
    r = ctx.replay
    checks = sum(1 for i in range(len(r.moves)) if r.boards[i].turn != color and r.boards[i + 1].is_check())
    if checks < 2:
        raise Fail("a ladder needs at least two checks in a row")
    facts["pattern"] = "ladder mate"
    facts["heavy_pieces"] = [piece_fact(board, s) for s in heavies]
    return facts


@validator("boden_mate")
def v_boden(ctx: Ctx, params: dict) -> dict:
    board, facts = _mate(ctx, params)
    color = board.turn
    bishops = board.pieces(chess.BISHOP, not color)
    if len(bishops) < 2 or not any(board.piece_at(c).piece_type == chess.BISHOP for c in board.checkers()):
        raise Fail("Boden's mate is delivered by two bishops on crossing diagonals")
    facts["pattern"] = "Boden's mate"
    return facts


@validator("arabian_mate")
def v_arabian(ctx: Ctx, params: dict) -> dict:
    board, facts = _mate(ctx, params)
    color = board.turn
    king = board.king(color)
    rooks = [c for c in board.checkers() if board.piece_at(c).piece_type == chess.ROOK]
    if not rooks or chess.square_distance(rooks[0], king) != 1:
        raise Fail("in an Arabian mate a rook checks from right next to the king")
    if not any(board.piece_at(a).piece_type == chess.KNIGHT for a in board.attackers(not color, rooks[0])):
        raise Fail("in an Arabian mate a knight protects the rook")
    facts["pattern"] = "Arabian mate"
    return facts


@validator("anastasia_mate")
def v_anastasia(ctx: Ctx, params: dict) -> dict:
    board, facts = _mate(ctx, params)
    color = board.turn
    king = board.king(color)
    if chess.square_file(king) not in (0, 7):
        raise Fail("in Anastasia's mate the king is mated on the edge file")
    heavy = [c for c in board.checkers() if board.piece_at(c).piece_type in (chess.ROOK, chess.QUEEN)]
    knights = board.pieces(chess.KNIGHT, not color)
    if not heavy or not knights:
        raise Fail("Anastasia's mate uses a rook or queen on the edge and a knight")
    facts["pattern"] = "Anastasia's mate"
    return facts


@validator("scholars_mate")
def v_scholars(ctx: Ctx, params: dict) -> dict:
    board, facts = _mate(ctx, params)
    color = board.turn
    target = chess.F7 if color == chess.BLACK else chess.F2
    piece = board.piece_at(target)
    if piece is None or piece.piece_type != chess.QUEEN or piece.color == color:
        raise Fail("Scholar's mate ends with the queen capturing on f7 (or f2)")
    if not any(board.piece_at(a).piece_type == chess.BISHOP for a in board.attackers(not color, target)):
        raise Fail("in Scholar's mate a bishop supports the queen")
    facts["pattern"] = "Scholar's mate"
    return facts


@validator("early_mate")
def v_early_mate(ctx: Ctx, params: dict) -> dict:
    """Mate within the first few moves from the starting position (Fool's mate, Legal's mate)."""
    r = ctx.replay
    if r.boards[0].board_fen() != chess.Board().board_fen():
        raise Fail("this mate pattern starts from the initial position")
    facts = v_checkmate(ctx, params)
    if len(r.moves) > params.get("max_plies", 20):
        raise Fail("the mate comes too late for this pattern")
    facts["plies"] = len(r.moves)
    return facts


# ------------------------------------------------------------------ endgames

@validator("opposition")
def v_opposition(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    for i in _plies(ctx, params):
        board = r.boards[i + 1]
        wk, bk = board.king(chess.WHITE), board.king(chess.BLACK)
        df = abs(chess.square_file(wk) - chess.square_file(bk))
        dr = abs(chess.square_rank(wk) - chess.square_rank(bk))
        if (df, dr) in ((0, 2), (2, 0)) and board.turn != r.boards[i].turn:
            return {"move": ctx.label(i), "kind": "direct opposition",
                    "kings": [chess.square_name(wk), chess.square_name(bk)],
                    "side_with_opposition": _side_name(r.boards[i].turn),
                    "side_to_move": _side_name(board.turn)}
    raise Fail("no move takes the opposition")


def _passed(board: chess.Board, sq: int, color: chess.Color) -> bool:
    f, rank = chess.square_file(sq), chess.square_rank(sq)
    for s in board.pieces(chess.PAWN, not color):
        if abs(chess.square_file(s) - f) <= 1:
            ahead = chess.square_rank(s) > rank if color == chess.WHITE else chess.square_rank(s) < rank
            if ahead:
                return False
    return True


@validator("passed_pawn")
def v_passed_pawn(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    color = ctx.learner
    start = r.boards[ctx.key_ply or 0]
    passers = [s for s in start.pieces(chess.PAWN, color) if _passed(start, s, color)]
    if not passers:
        raise Fail("the learner has no passed pawn")
    promoted = any(m.promotion for i, m in enumerate(r.moves) if r.boards[i].turn == color)
    if params.get("must_promote", True) and not promoted:
        raise Fail("the passed pawn never promotes")
    return {"passed_pawns": [chess.square_name(s) for s in passers], "promotes": promoted,
            "side": _side_name(color)}


# ------------------------------------------------------------------ openings

@validator("opening_line")
def v_opening_line(ctx: Ctx, params: dict) -> dict:
    """The moves follow a named line from the opening database (no invented theory)."""
    r = ctx.replay
    if r.boards[0].fen() != chess.STARTING_FEN:
        raise Fail("opening examples start from the initial position")
    index = ctx.opening_index
    if index is None:
        from .sources.lichess_openings import get_opening_index
        index = get_opening_index()
    match = index.longest_prefix(r.sans)
    if match is None:
        raise Fail("the moves don't follow any named opening line")
    names = params.get("names", [])
    if names and not any(match.name.startswith(n) for n in names):
        raise Fail(f"the line is {match.name!r}, not {' / '.join(names)}")
    book, extra = len(match.moves), len(r.moves) - len(match.moves)
    if book < params.get("min_book_plies", 5):
        raise Fail(f"only {book} moves come from the opening database")
    if extra > params.get("max_extra_plies", 6):
        raise Fail(f"{extra} moves go beyond the database line")
    return {"eco": match.eco, "name": match.name, "book_plies": book, "extra_plies": extra}


# ------------------------------------------------------------------ mistakes

def _mistake(ctx: Ctx) -> tuple[int, chess.Board, chess.Move, chess.Color]:
    if ctx.mistake_ply is None:
        raise Fail("a mistake example must name its mistake_move")
    i = ctx.mistake_ply
    return i, ctx.replay.boards[i], ctx.replay.moves[i], ctx.replay.boards[i].turn


@validator("mistake")
def v_mistake(ctx: Ctx, params: dict) -> dict:
    """The rule part of a mistake: the mistake is followed by the opponent punishing it.
    (Stockfish confirms the mistake really loses — see engine_check.py.)"""
    i, board, move, side = _mistake(ctx)
    r = ctx.replay
    if i + 1 >= len(r.moves):
        raise Fail("show the punishment: the opponent's reply must follow the mistake")
    end = r.final
    swing = (material(end, not side) - material(end, side)) - (material(board, not side) - material(board, side))
    mated = end.is_checkmate() and end.turn == side
    if swing < params.get("min_swing", 2) and not mated:
        raise Fail("the line doesn't show the mistake being punished (no mate, no material won)")
    return {"mistake": ctx.label(i), "mistake_by": _side_name(side), "punishment": ctx.label(i + 1),
            "material_lost": swing, "ends_in_mate": mated}


@validator("hung_piece")
def v_hung_piece(ctx: Ctx, params: dict) -> dict:
    facts = v_mistake(ctx, params)
    i, board, move, side = _mistake(ctx)
    r = ctx.replay
    after = r.boards[i + 1]
    want = _want_piece(params)
    for sq in chess.SquareSet(after.occupied_co[side]):
        piece = after.piece_at(sq)
        if piece.piece_type == chess.KING or (want and piece.piece_type != want):
            continue
        attackers = after.attackers(not side, sq)
        if not attackers:
            continue
        cheapest = min(VALUES[after.piece_at(a).piece_type] for a in attackers)
        en_prise = not after.attackers(side, sq) or cheapest < VALUES[piece.piece_type]
        taken = any(r.moves[j].to_square == sq for j in range(i + 1, len(r.moves), 2))
        if en_prise and taken:
            facts["hung"] = piece_fact(after, sq)
            facts["attacked_by"] = [piece_fact(after, a) for a in attackers]
            return facts
    raise Fail("the mistake doesn't leave a piece hanging that then gets taken")


@validator("walked_into_fork")
def v_walked_into_fork(ctx: Ctx, params: dict) -> dict:
    facts = v_mistake(ctx, params)
    i, *_ = _mistake(ctx)
    sub = Ctx(ctx.replay, key_ply=i + 1)
    fork = v_fork(sub, {})
    facts["fork"] = fork
    return facts


@validator("missed_threat")
def v_missed_threat(ctx: Ctx, params: dict) -> dict:
    """The punishing move was already threatened before the mistake, and the mistake ignored it."""
    facts = v_mistake(ctx, params)
    i, board, move, side = _mistake(ctx)
    reply = ctx.replay.moves[i + 1]
    probe = board.copy(stack=False)
    probe.push(chess.Move.null())
    if reply not in probe.legal_moves:
        raise Fail("the punishing move wasn't possible before the mistake — it's a new threat, not a missed one")
    facts["ignored_threat"] = probe.san(reply)
    return facts


@validator("early_queen")
def v_early_queen(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    i, board, move, side = _mistake(ctx)
    if board.piece_at(move.from_square).piece_type != chess.QUEEN or board.fullmove_number > 6:
        raise Fail("the mistake is not an early queen move")
    tempo = []
    for j in range(i + 1, len(r.moves)):
        if r.boards[j].turn == side:
            continue
        after = r.boards[j + 1]
        queen = [s for s in after.pieces(chess.QUEEN, side)]
        if queen and after.attackers(not side, queen[0]) and r.moves[j].to_square in after.attackers(not side, queen[0]):
            tempo.append(r.labels[j])
    lost = not r.final.pieces(chess.QUEEN, side)
    if not tempo and not lost:
        raise Fail("the early queen is never chased or lost")
    return {"mistake": ctx.label(i), "queen_chased_by": tempo, "queen_lost": lost,
            "mistake_by": _side_name(side)}


@validator("repeated_moves")
def v_repeated_moves(ctx: Ctx, params: dict) -> dict:
    r = ctx.replay
    side = _color(params["side"])
    counts: dict[int, int] = {}
    where: dict[int, int] = {}
    ident = 0
    for board, move in zip(r.boards, r.moves):
        if board.turn != side:
            where.pop(move.to_square, None)
            continue
        pid = where.pop(move.from_square, None)
        if pid is None:
            pid, ident = ident, ident + 1
        where[move.to_square] = pid
        counts[pid] = counts.get(pid, 0) + 1
    most = max(counts.values(), default=0)
    if most < params.get("min", 3):
        raise Fail(f"no {params['side']} piece moves {params.get('min', 3)} times")
    end = r.final
    return {"side": params["side"], "moves_by_one_piece": most,
            "development": {"white": developed_minors(end, chess.WHITE), "black": developed_minors(end, chess.BLACK)}}


@validator("development_lead")
def v_development_lead(ctx: Ctx, params: dict) -> dict:
    board, at = _position(ctx, params)
    side = _color(params["side"])
    lead = developed_minors(board, side) - developed_minors(board, not side)
    if lead < params.get("ahead_by", 2):
        raise Fail(f"{params['side']} is only {lead} developed pieces ahead after {at}")
    return {"side": params["side"], "developed": developed_minors(board, side),
            "opponent_developed": developed_minors(board, not side), "lead": lead}


@validator("king_safety_mistake")
def v_king_safety(ctx: Ctx, params: dict) -> dict:
    """The mistake weakens the king (pawn shield / king walk) and the line ends in mate or a big loss."""
    facts = v_mistake(ctx, params)
    i, board, move, side = _mistake(ctx)
    piece = board.piece_at(move.from_square)
    king = board.king(side)
    near_king = chess.square_distance(move.from_square, king) <= 2
    if not (piece.piece_type == chess.KING or (piece.piece_type == chess.PAWN and near_king)):
        raise Fail("the mistake doesn't weaken the king (no king move, no pawn in front of the king)")
    facts["weakening_move"] = ctx.label(i)
    facts["king"] = chess.square_name(king)
    return facts


@validator("poisoned_pawn")
def v_poisoned_pawn(ctx: Ctx, params: dict) -> dict:
    facts = v_mistake(ctx, params)
    i, board, move, side = _mistake(ctx)
    victim = board.piece_at(move.to_square)
    if victim is None or victim.piece_type != chess.PAWN:
        raise Fail("the mistake doesn't grab a pawn")
    facts["grabbed"] = piece_fact(board, move.to_square)
    return facts


@validator("all")
def v_all(ctx: Ctx, params: dict) -> dict:
    """Combine validators: {"type": "all", "of": [{"type": "fork"}, {"type": "check"}]}."""
    facts = {}
    for spec in params["of"]:
        spec = dict(spec)
        facts[spec["type"]] = run(spec.pop("type"), ctx, spec)
    return facts
