"""Verified facts for Qwen, and a check that explanations don't contradict the board.

Qwen never reads a bare FEN and decides what is on the board. It receives
*verified facts*: the pieces, the move sequence, the concept validator's
findings (which piece forks what, which piece is pinned) and Stockfish's
verdict. After it writes an explanation, `check_explanation` compares the text
with the board and rejects claims that contradict it (a knight "attacking the
bishop" when it attacks a queen and a rook, a piece on a square it never
stands on, a move that is illegal in every position of the example).
"""
from __future__ import annotations

import re

import chess

from .positions import NAME_TO_TYPE, PIECE_NAMES

_PIECE = r"(king|queen|rook|bishop|knight|pawn)s?"
_PIECE_ON = re.compile(rf"\b(?:(white|black)(?:'s)?\s+)?{_PIECE}\s+(?:on|at|from)\s+([a-h][1-8])\b", re.I)
_SAN = re.compile(r"(?<![\w.])(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8][+#]?|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?|"
                  r"[a-h][1-8]=[QRBN][+#]?)(?![\w])")
_COLOR = r"(?:(white|black)(?:'s)?\s+)?"
_ADVERB = r"(?:\s+(?:now|also|then|already|still|immediately))?"
# "<piece> [on e4] forks/pins/skewers": the subject of a tactic claim must be a verified tactic piece.
_TACTIC_CLAIM = re.compile(rf"\b{_COLOR}{_PIECE}(?:\s+on\s+([a-h][1-8]))?{_ADVERB}\s+(forks|pins|skewers)\b", re.I)
# "<piece> [on e4] attacks [the] [black] <piece> [on d5] | <square>": must be true in some position.
_ATTACK_CLAIM = re.compile(
    rf"\b{_COLOR}{_PIECE}(?:\s+on\s+([a-h][1-8]))?{_ADVERB}\s+(?:attacks|threatens|hits)\s+"
    rf"(?:(?:the|a|an|both)\s+)?(?:{_COLOR}{_PIECE}(?:\s+on\s+([a-h][1-8]))?|([a-h][1-8])\b)", re.I)
_MATE_CLAIM = re.compile(r"\b(is|it'?s|delivers?|gives?|that'?s)\s+(check)?mate\b|\bcheckmate!", re.I)
_STALEMATE_CLAIM = re.compile(r"\b(is|it'?s|that'?s|gives?|giving)\s+stalemate\b", re.I)
# Words that make a mate/stalemate mention advice or a warning rather than a claim about this example.
_HEDGE = re.compile(r"\b(not|never|no|without|avoid|avoiding|danger|careful|until|can|could|would|might|"
                    r"if|when|risk|instead|before|unless)\b|n't\b", re.I)


def _collect_pieces(obj, out: set[str]) -> None:
    if isinstance(obj, dict):
        if "piece" in obj and obj.get("piece") in NAME_TO_TYPE:
            out.add(obj["piece"])
        for v in obj.values():
            _collect_pieces(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_pieces(v, out)


_TACTIC_FACT = {"forks": ("fork", "attacker"), "pins": ("pin", "pinner"), "skewers": ("skewer", "attacker")}


def _find_fact(obj, key: str, role: str) -> dict | None:
    """The verified facts for tactic `key` (possibly nested, e.g. walked_into_fork → fork)."""
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, dict) and isinstance(value.get(role), dict):
            return value
        for v in obj.values():
            found = _find_fact(v, key, role)
            if found:
                return found
    return None


def _tactic_on_board(boards, verb: str, ptype: int) -> bool:
    """Without verified facts for this tactic: does a piece of that type do it in some position?"""
    from .validators import _pins
    if verb == "skewers":
        return ptype in (chess.BISHOP, chess.ROOK, chess.QUEEN)
    for b in boards:
        if verb == "pins":
            for color in chess.COLORS:
                if any(b.piece_type_at(pinner) == ptype for pinner, _, _ in _pins(b, color)):
                    return True
            continue
        for sq, p in b.piece_map().items():  # forks: one piece attacking two valuable enemy pieces
            if p.piece_type != ptype:
                continue
            victims = [t for t in b.attacks(sq) if (q := b.piece_at(t)) and q.color != p.color
                       and q.piece_type != chess.PAWN]
            if len(victims) >= 2:
                return True
    return False


def _is_claim(text: str, match: re.Match) -> bool:
    """A mate/stalemate mention is a claim unless the clause before it hedges ("until you can give mate")."""
    start = max(text.rfind(c, 0, match.start()) for c in ".!?;:—,")
    return not _HEDGE.search(text[start + 1:match.start()] + " " + match.group(0))


def _attack_holds(boards, m: re.Match) -> bool:
    """Is "<piece> [on sq] attacks <piece [on sq] | square>" true in at least one position?"""
    a_color, a_type, a_sq = m.group(1), NAME_TO_TYPE[m.group(2).lower()], m.group(3)
    t_color, t_piece, t_sq, square = m.group(4), m.group(5), m.group(6), m.group(7)
    for b in boards:
        for sq in chess.SQUARES:
            p = b.piece_at(sq)
            if p is None or p.piece_type != a_type or (a_sq and chess.square_name(sq) != a_sq.lower()):
                continue
            if a_color and p.color != (a_color.lower() == "white"):
                continue
            attacked = b.attacks(sq)
            if square:
                if chess.parse_square(square.lower()) in attacked:
                    return True
                continue
            for t in attacked:
                q = b.piece_at(t)
                if q is None or q.color == p.color or q.piece_type != NAME_TO_TYPE[t_piece.lower()]:
                    continue
                if t_sq and chess.square_name(t) != t_sq.lower():
                    continue
                if t_color and q.color != (t_color.lower() == "white"):
                    continue
                return True
    return False


def _describe(p: dict) -> str:
    return f"{p['color']} {p['piece']} on {p['square']}"


def verified_facts(example, concept_name: str | None = None) -> list[str]:
    """Short fact lines about a verified example (fed to Qwen as ground truth)."""
    rep = example.replay()
    lines = [f"Concept: {concept_name or example.concept}",
             f"Example: {example.title}",
             f"Start position (FEN): {example.start_fen}",
             f"Moves: {' '.join(rep.labels)}"]
    board = rep.boards[example.key_ply] if example.key_ply is not None else rep.boards[0]
    lines.append(f"Side to move{' at the key moment' if example.key_ply is not None else ''}: "
                 f"{'White' if board.turn else 'Black'}")
    for color in (chess.WHITE, chess.BLACK):
        pieces = [f"{PIECE_NAMES[p.piece_type]} {chess.square_name(s)}"
                  for s, p in sorted(board.piece_map().items()) if p.color == color]
        lines.append(f"{'White' if color else 'Black'} pieces: {', '.join(pieces)}")
    if example.key_move:
        lines.append(f"Key move: {example.key_move}")
    if example.mistake_move:
        lines.append(f"Mistake: {example.mistake_move}")
    lines += render_facts(example.facts)
    eng = example.verification.get("engine") or {}
    key = eng.get("key_move") or eng.get("mistake")
    if key:
        lines.append(f"Stockfish: {key.get('label', key['move'])} is '{key['category']}' "
                     f"(loss {key['loss_cp']} cp; best {key['best_move']})")
        if key.get("alternatives"):
            lines.append("Stockfish: other good moves: " + ", ".join(key["alternatives"]))
    final = rep.final
    if final.is_checkmate():
        lines.append("Final position: checkmate")
    elif final.is_stalemate():
        lines.append("Final position: stalemate")
    return lines


def render_facts(facts: dict) -> list[str]:
    out = []
    for name, f in (facts or {}).items():
        if not isinstance(f, dict):
            continue
        if "targets" in f and "attacker" in f:
            out.append(f"{name}: after {f.get('move')}, the {_describe(f['attacker'])} attacks "
                       + " and ".join(_describe(t) for t in f["targets"]))
        elif "pinned" in f:
            out.append(f"{name}: the {_describe(f['pinner'])} pins the {_describe(f['pinned'])} to the "
                       f"{_describe(f['behind'])} ({f['kind']} pin)")
        elif "front" in f:
            out.append(f"{name}: the {_describe(f['attacker'])} skewers the {_describe(f['front'])}; "
                       f"behind it stands the {_describe(f['back'])}")
        elif "revealed_attacker" in f:
            out.append(f"{name}: {f['move']} moves the {_describe(f['moved_piece'])} out of the way and the "
                       f"{_describe(f['revealed_attacker'])} now attacks the {_describe(f['target'])}")
        elif "checking_piece" in f:
            out.append(f"{name}: {f['move']} uncovers check from the {_describe(f['checking_piece'])}")
        elif "checkers" in f and "escape_squares" in f:
            blocked = [e for e in f["escape_squares"] if "free" not in e]
            out.append(f"{name}: the {f['mated_side']} king on {f['king']} is in check from "
                       + " and ".join(_describe(c) for c in f["checkers"]) + " and has no legal move")
            for e in blocked[:8]:
                why = (f"blocked by its own {e['blocked_by']}" if "blocked_by" in e
                       else f"covered by the {_describe(e['covered_by'])}")
                out.append(f"  - {e['square']}: {why}")
        elif "checkers" in f:
            out.append(f"{name}: after {f.get('move')}, check from "
                       + " and ".join(_describe(c) for c in f["checkers"]))
        elif "stalemated_side" in f:
            out.append(f"{name}: {f['stalemated_side']} is NOT in check but has no legal move — stalemate (a draw)")
        else:
            simple = {k: v for k, v in f.items() if isinstance(v, (str, int, float, bool))}
            out.append(f"{name}: " + ", ".join(f"{k}={v}" for k, v in simple.items()))
    return out


def check_explanation(text: str, example, extra_moves: list[str] | None = None) -> list[str]:
    """Problems where `text` contradicts the verified example (empty list = consistent)."""
    problems: list[str] = []
    rep = example.replay()
    boards = rep.boards
    for m in _PIECE_ON.finditer(text):
        color, piece, square = m.group(1), m.group(2).lower(), m.group(3).lower()
        sq = chess.parse_square(square)
        ptype = NAME_TO_TYPE[piece]
        ok = any(b.piece_at(sq) and b.piece_at(sq).piece_type == ptype and
                 (color is None or b.piece_at(sq).color == (color.lower() == "white")) for b in boards)
        if not ok:
            problems.append(f"no {(color + ' ') if color else ''}{piece} ever stands on {square}")
    legal_anywhere = set(extra_moves or [])
    for b in boards:
        if not b.is_game_over():
            legal_anywhere |= {b.san(mv).rstrip("+#") for mv in b.legal_moves}
    legal_anywhere |= {s.rstrip("+#") for s in rep.sans}
    for m in _SAN.finditer(text):
        san = m.group(1).rstrip("+#")
        if san not in legal_anywhere:
            problems.append(f"{m.group(1)} is not a legal move in this example")
    for m in _TACTIC_CLAIM.finditer(text):
        piece, verb = m.group(2).lower(), m.group(4).lower()
        key, role = _TACTIC_FACT[verb]
        fact = _find_fact(example.facts, key, role)
        if fact is not None:
            if fact[role]["piece"] != piece:
                problems.append(f"'{m.group(0)}': in the verified {key} the {role} is the {fact[role]['piece']}")
        elif not _tactic_on_board(boards, verb, NAME_TO_TYPE[piece]):
            problems.append(f"'{m.group(0)}': no {piece} {verb} anything in this example")
    for m in _ATTACK_CLAIM.finditer(text):
        if not _attack_holds(boards, m):
            problems.append(f"'{m.group(0)}' is not true in any position of the example")
    for claim, holds, what in ((_MATE_CLAIM, lambda b: b.is_checkmate(), "checkmate"),
                               (_STALEMATE_CLAIM, lambda b: b.is_stalemate(), "stalemate")):
        if any(_is_claim(text, m) for m in claim.finditer(text)) and not any(holds(b) for b in boards):
            problems.append(f"claims {what}, but no position in the example is {what}")
    return problems
