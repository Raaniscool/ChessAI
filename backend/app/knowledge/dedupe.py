"""Duplicate and near-duplicate detection.

The library must not fill up with thousands of essentially identical positions.

* exact duplicate: the same teaching position (piece placement, side to move,
  castling/en-passant rights) with the same key move, or the same start
  position with the same move sequence. Always rejected.
* near duplicate: same concept, same key move and a position that differs in only
  a few pieces (placement similarity >= NEAR_THRESHOLD). Allowed only with a
  legitimate teaching reason: a different difficulty, a different variation /
  subcategory, or an explicit `teaching_purpose` that no near twin shares.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import chess

from .positions import material_signature

NEAR_THRESHOLD = 0.85


def _position_key(fen: str) -> str:
    return " ".join(fen.split()[:4])  # without the move counters


def teaching_position(example) -> str:
    """The position the lesson is about: before the key/mistake move, else the final one."""
    rep_index = example.key_ply if example.key_ply is not None else example.mistake_ply
    if rep_index is None:
        return example.final_fen
    return example.replay().boards[rep_index].fen()


def teaching_move(example) -> str:
    """The move played in the teaching position (key move, else the mistake), "" if none."""
    index = example.key_ply if example.key_ply is not None else example.mistake_ply
    return example.moves[index] if index is not None else ""


def fingerprints(example) -> set[str]:
    key_pos = _position_key(teaching_position(example))
    move = teaching_move(example)
    sequence = _position_key(example.start_fen) + "|" + " ".join(example.moves)
    return {hashlib.sha1(f"pos:{key_pos}|{move}".encode()).hexdigest()[:16],
            hashlib.sha1(f"seq:{sequence}".encode()).hexdigest()[:16]}


def placement_similarity(fen_a: str, fen_b: str) -> float:
    a, b = chess.Board(fen_a), chess.Board(fen_b)
    if a.turn != b.turn:
        return 0.0
    sa = {(sq, p.symbol()) for sq, p in a.piece_map().items()}
    sb = {(sq, p.symbol()) for sq, p in b.piece_map().items()}
    return len(sa & sb) / max(1, len(sa | sb))


@dataclass
class DuplicateReport:
    kind: str  # "none" | "exact" | "near"
    matches: list[dict]
    allowed: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def check(candidate, existing) -> DuplicateReport:
    """Compare a candidate against an iterable of library examples."""
    fps = fingerprints(candidate)
    cand_pos = teaching_position(candidate)
    cand_move = teaching_move(candidate)
    cand_sig = material_signature(chess.Board(cand_pos))
    near = []
    for other in existing:
        if other.id == candidate.id:
            continue
        if fps & fingerprints(other):
            return DuplicateReport("exact", [{"id": other.id, "similarity": 1.0}], False,
                                   f"same teaching position and moves as {other.id}")
        if other.concept != candidate.concept:
            continue
        other_move = teaching_move(other)
        if other_move != cand_move:
            continue
        other_pos = teaching_position(other)
        if material_signature(chess.Board(other_pos)) != cand_sig:
            continue
        sim = placement_similarity(cand_pos, other_pos)
        if sim >= NEAR_THRESHOLD:
            near.append((other, sim))
    if not near:
        return DuplicateReport("none", [], True)
    matches = [{"id": o.id, "similarity": round(s, 3)} for o, s in near]
    reasons = []
    if all(o.difficulty != candidate.difficulty for o, _ in near):
        reasons.append("different difficulty")
    if candidate.subcategory and all(o.subcategory != candidate.subcategory for o, _ in near):
        reasons.append("different variation")
    if candidate.teaching_purpose and all(o.teaching_purpose != candidate.teaching_purpose for o, _ in near):
        reasons.append("different teaching purpose: " + candidate.teaching_purpose)
    if reasons:
        return DuplicateReport("near", matches, True, "; ".join(reasons))
    return DuplicateReport("near", matches, False, "nearly identical to " + ", ".join(m["id"] for m in matches))
