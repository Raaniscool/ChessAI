"""Where Training positions come from, which phase they belong to, and which one comes next.

Two verified sources, nothing invented:

  opening trees     positions inside the verified opening trees (Lichess opening database, CC0,
                    every move Stockfish-checked). A node qualifies when its whole path is sound
                    for the side to move (no opponent blunder handing over a won game), it is
                    OPENING_PLIES deep, and Stockfish rates it roughly balanced. No hidden idea:
                    these measure general opening play.
  puzzle positions  the Puzzle Library's verified, non-personal positions (Lichess puzzles from real
                    games, historical games, endgame theory, curated patterns). Each carries one
                    verified idea (the library's concept, proven by its validator) and the
                    accepted first moves: the hidden test. The learner is never told it is there.

Phase = analysis.analyzer.phase_of(board) (the game analyzer's own rule, from the position's
material and real move number), so Training and game analysis always agree on what an endgame is.

Choosing the next position (`choose`):
  - never a position from the recent Training history (SEEN_KEEP ids), never a puzzle the learner
    has already met in the Puzzles tab (they would know the answer: not a fair observation);
  - the kind (theory position / verified idea position) is drawn in proportion to how many of each
    the phase has, then
  - puzzle positions are scored by rating fit to the learner's level for that phase, plus a bonus
    for ideas the profile is unsure about (measure them) or flags as a need (re-check them),
    minus a penalty for an idea tested in the last few segments; a seeded jitter keeps it varied;
  - when everything has been seen, the positions seen longest ago come back (effectively infinite).
"""
from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field

import chess

PHASES = ("opening", "middlegame", "endgame")
MODES = {"opening": "Beginning Game", "middlegame": "Middlegame", "endgame": "Endgame", "mixed": "Mixed Games"}
OPENING_PLIES = (6, 16)          # a beginning-game position: a few moves of real theory on the board
OPENING_MAX_EVAL = 120           # cp: roughly balanced, so the segment tests play, not a lost position
SEEN_KEEP = 300                  # Training positions remembered (no repeat while unseen ones exist)
RECENT_CONCEPTS = 3              # segments: don't test the same hidden idea again this soon
FIT_WIDTH = 220                  # rating fit (puzzle scale)
UNCERTAIN_BONUS = 0.35           # an idea with little evidence yet: worth measuring
NEED_BONUS = 0.35                # an idea the profile flags: worth re-checking
REPEAT_PENALTY = 0.6
JITTER = 0.25


@dataclass
class TrainingPosition:
    id: str
    fen: str
    phase: str
    side: str                                    # the learner: the side to move
    source: dict                                 # shown after the segment (never before)
    rating: int | None = None                    # puzzle scale (puzzle positions only)
    hidden: dict | None = None                   # {concept, accepted (SAN), rating, puzzle_id}: never sent
    tags: dict = field(default_factory=dict)

    def public(self) -> dict:
        """What the learner may see while playing: the board, nothing about its idea or origin."""
        return {"fen": self.fen, "side": self.side, "phase": self.phase}


def _side(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def opening_positions(trees) -> list[TrainingPosition]:
    from ..analysis.analyzer import phase_of
    out, seen = [], set()
    lo, hi = OPENING_PLIES
    for tree in trees.trees.values():
        for node in tree.nodes[1:]:
            if not lo <= node.depth <= hi or abs(node.eval) > OPENING_MAX_EVAL:
                continue
            board = tree.board(node.i)
            side = _side(board)
            if not all(tree.move_ok(n, side) for n in tree.path(node.i)) or board.is_game_over():
                continue
            if board.epd() in seen or phase_of(board) != "opening":
                continue
            seen.add(board.epd())
            out.append(TrainingPosition(
                id=f"opening:{tree.id}:{node.i}", fen=board.fen(), phase="opening", side=side,
                source={"type": "opening_tree", "label": node.name or tree.title, "opening": tree.id,
                        "line": tree.sans(node.i)},
                tags={"opening": tree.id}))
    return out


def puzzle_positions(puzzles) -> list[TrainingPosition]:
    from ..analysis.analyzer import phase_of
    from ..puzzles.dashboard import candidates, source_label
    out, seen = [], set()
    for p in candidates(puzzles):
        if p.tier == "personal" or (p.source or {}).get("type") == "user_game" or not p.clear_start:
            continue
        board = chess.Board(p.fen)
        if board.epd() in seen or board.is_game_over():
            continue
        seen.add(board.epd())
        out.append(TrainingPosition(
            id=f"puzzle:{p.id}", fen=p.fen, phase=phase_of(board), side=_side(board),
            source={"type": (p.source or {}).get("type") or "library", "label": source_label(p),
                    "url": (p.source or {}).get("url")},
            rating=p.rating,
            hidden={"concept": p.concept, "accepted": list(p.accepted_first), "rating": p.rating,
                    "puzzle_id": p.id, "key_move": p.solution[0] if p.solution else None}))
    return out


class PositionPool:
    """All Training positions by phase (built once per knowledge library / puzzle index state)."""

    def __init__(self, positions: list[TrainingPosition]):
        self.by_phase: dict[str, list[TrainingPosition]] = {p: [] for p in PHASES}
        self.by_id: dict[str, TrainingPosition] = {}
        for pos in positions:
            self.by_phase.setdefault(pos.phase, []).append(pos)
            self.by_id[pos.id] = pos

    def phases(self) -> list[str]:
        return [p for p in PHASES if self.by_phase.get(p)]

    def counts(self) -> dict[str, int]:
        return {p: len(self.by_phase.get(p, [])) for p in PHASES}


_pool: tuple[object, PositionPool] | None = None
_lock = threading.Lock()


def get_pool(knowledge=None) -> PositionPool:
    from ..knowledge.opening_trees import get_opening_trees
    from ..puzzles import get_puzzles
    index = get_puzzles(knowledge)
    puzzles = index.all(include_personal=False)
    key = (id(index), frozenset(p.id for p in puzzles))
    global _pool
    with _lock:
        if _pool is None or _pool[0] != key:
            _pool = (key, PositionPool(opening_positions(get_opening_trees()) + puzzle_positions(puzzles)))
        return _pool[1]


def reset_pool() -> None:
    global _pool
    with _lock:
        _pool = None


# ---------------------------------------------------------------------- choosing
def next_phase(mode: str, history: list[dict], available: list[str], rng: random.Random) -> str:
    """The phase of the next segment. Mixed Games: the phase played least in the last few segments
    (ties: the one played longest ago, then the seeded coin), so all three keep coming in turn
    without a fixed order."""
    if mode in available:
        return mode
    if mode in PHASES or not available:
        raise LookupError(f"no Training positions for {mode}")
    recent = [h.get("phase") for h in history[-2 * len(PHASES):]]

    def key(phase: str):
        last = max((i for i, p in enumerate(recent) if p == phase), default=-1)
        return (recent.count(phase), last, rng.random())
    return min(available, key=key)


def choose(pool: PositionPool, phase: str, *, seen: list[str], used_puzzles: set[str], recent_concepts: list[str],
           target: int | None, needs: dict | None, rng: random.Random) -> TrainingPosition:
    """The next position of `phase` (see the module doc for the rules)."""
    candidates = pool.by_phase.get(phase) or []
    if not candidates:
        raise LookupError(f"no Training positions for {phase}")
    fair = [c for c in candidates if not (c.hidden and c.hidden["puzzle_id"] in used_puzzles)]
    seen_set = set(seen)
    fresh = [c for c in fair if c.id not in seen_set]
    if not fresh:
        # everything met already: the ones seen longest ago come back (position ids in `seen`, oldest first)
        order = {pid: i for i, pid in enumerate(seen)}
        pool_ = fair or candidates
        oldest = min(order.get(c.id, -1) for c in pool_)
        fresh = [c for c in pool_ if order.get(c.id, -1) < oldest + max(1, len(pool_) // 10)]  # oldest tenth
    hidden = [c for c in fresh if c.hidden]
    plain = [c for c in fresh if not c.hidden]
    if hidden and plain:
        # the kind of position in proportion to what the phase has (the opening is mostly theory
        # positions, the middlegame and endgame verified positions): no source crowds out the other
        fresh = hidden if rng.random() < len(hidden) / len(fresh) else plain
    needs = needs or {}
    last_opening = [c for c in recent_concepts if c.startswith("opening:")]

    def score(c: TrainingPosition) -> float:
        s = rng.random() * JITTER
        if c.hidden:
            if target is not None and c.rating is not None:
                s += math.exp(-((c.rating - target) / FIT_WIDTH) ** 2)
            concept = c.hidden["concept"]
            need = needs.get(concept)
            if need is None or need.get("confidence", 0) < 0.3:
                s += UNCERTAIN_BONUS
            elif need.get("status") in ("needs_work", "improving"):
                s += NEED_BONUS
            if concept in recent_concepts:
                s -= REPEAT_PENALTY
        else:
            s += 0.5
            if f"opening:{c.tags.get('opening')}" in last_opening:
                s -= REPEAT_PENALTY
        return s
    return max(fresh, key=lambda c: (score(c), c.id))
