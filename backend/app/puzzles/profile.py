"""Puzzle profile: which moves of a puzzle are real decisions, where it ends, how hard it is.

Lichess-style puzzles test *the important idea*, not how many moves you can click through.
For every learner move of a verified line this module decides:

    critical  a real decision: Stockfish shows a clear gap (>= CRITICAL_MARGIN) between the
              solution (and any equally good move using the same idea) and the next-best move
    forced    the only legal move, or an obvious one: a recapture, escaping check with few
              options, or cashing in the target the tactic attacked (taking the forked queen)
    open      several moves are about as good: not a decision worth testing

Then it trims the line at the puzzle's objective:

    mate      mate concepts whose line ends in mate: play to mate
    material  the earliest learner move after which the line's material gain is banked and
              survives the reply ("find the fork -> reply -> win the queen -> done")
    defense   defensive / mistake-correction puzzles: the saving move
    idea      otherwise: the last critical decision

and never before the last critical move. Difficulty is rated from the critical decisions only
(hardest decision + 30% of the others), so an obvious move followed by a hard one is rated by the
hard one, and a forced continuation adds nothing:

    decision = 600 + 300 quiet + 90 non-check capture + 250 sacrifice + 100 backward
               - 150 free capture + 45 per plausible candidate beyond the first (checks,
               captures and engine moves within 300cp; max 10) + 35 per ply until the pay-off
               (max 8) + 3 per legal move above 25

Engine data (multipv lines at each learner move) comes from the bundled file built by
scripts/build_puzzle_profiles.py, from the runtime store for generated puzzles (computed when
they are made), or is absent: then python-chess heuristics classify the moves (obvious ->
forced, everything else critical) and nothing is called "open".
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import chess

from ..knowledge.difficulty import move_features
from ..knowledge.positions import VALUES, replay

CLOSE_CP = 120          # an alternative this close to the solution is "equally good"
CRITICAL_MARGIN = 150   # a gap this large to the next-best move makes it a real decision
PLAUSIBLE_CP = 300      # engine moves this close to the best are plausible candidates
MATE_CP = 9000          # scores at or above this are forced mates (for_side convention)
MULTIPV = 5
MIN_RATING, MAX_RATING = 400, 2400
OTHER_DECISIONS = 0.3

BUNDLED = Path(__file__).parent / "data" / "engine_profiles.json"


@dataclass
class Step:
    """One learner move of the (trimmed) puzzle."""
    ply: int                  # index into the entry's moves
    san: str
    uci: str
    kind: str                 # critical | forced | open
    reason: str
    accepted: list[str] = field(default_factory=list)   # UCI: verified equally good moves (solve it)
    good: list[str] = field(default_factory=list)       # UCI: other engine-good moves (open moves only)
    reply_san: str | None = None
    reply_uci: str | None = None
    rating: int | None = None  # decision rating (critical moves)
    features: dict = field(default_factory=dict)


@dataclass
class Profile:
    steps: list[Step]
    solution: list[str]       # SAN from the key move to the end of the trimmed puzzle
    objective: str            # mate | material | defense | idea
    rating: int
    trimmed: int              # moves cut from the stored line
    engine: bool              # classified with Stockfish data (else heuristics)
    clear_start: bool         # the first move is a decision (not "open")
    basis: dict = field(default_factory=dict)

    @property
    def critical(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "critical"]

    @property
    def forced(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "forced"]

    def as_dict(self) -> dict:
        return asdict(self)


def signature(start_fen: str, moves: list[str], key_ply: int) -> str:
    return hashlib.sha1(f"{start_fen}|{' '.join(moves)}|{key_ply}".encode()).hexdigest()[:12]


# ------------------------------------------------------------------ engine data
def engine_data(start_fen: str, moves: list[str], key_ply: int, engine, depth: int = 12) -> dict:
    """{ply: [[uci, cp], ...]} for every learner move of the full line (cp for the mover)."""
    rep = replay(start_fen, moves)
    out = {}
    for ply in range(key_ply, len(moves), 2):
        board = rep.boards[ply]
        lines = engine.analyse_lines(board, depth=depth, multipv=MULTIPV, fresh=True)
        out[str(ply)] = [[ln.move.uci(), int(ln.score.for_side(board.turn == chess.WHITE))] for ln in lines]
    return out


class EngineProfiles:
    """Engine data per puzzle: the bundled file (library) + a runtime file (generated puzzles)."""

    def __init__(self, bundled: Path = BUNDLED, runtime: Path | None = None):
        self.bundled, self.runtime = bundled, runtime
        self._lock = threading.Lock()
        self._data: dict | None = None
        self.version = 0  # bumps on every put (the puzzle index rebuilds)

    def _load(self) -> dict:
        if self._data is None:
            data: dict = {}
            for path in (self.bundled, self.runtime):
                if path and Path(path).exists():
                    try:
                        data.update(json.loads(Path(path).read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        pass
            self._data = data
        return self._data

    def get(self, eid: str, sig: str) -> dict | None:
        with self._lock:
            rec = self._load().get(eid)
        return rec["plies"] if rec and rec.get("sig") == sig else None

    def put(self, eid: str, sig: str, plies: dict) -> None:
        if self.runtime is None:
            return
        with self._lock:
            self._load()[eid] = {"sig": sig, "plies": plies}
            self.version += 1
            path = Path(self.runtime)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                current = {}
            current[eid] = {"sig": sig, "plies": plies}
            path.write_text(json.dumps(current), encoding="utf-8")


_profiles: EngineProfiles | None = None
_profiles_lock = threading.Lock()


def get_engine_profiles() -> EngineProfiles:
    global _profiles
    with _profiles_lock:
        if _profiles is None:
            from ..config import get_settings
            _profiles = EngineProfiles(runtime=Path(get_settings().data_dir) / "puzzles" / "engine_profiles.json")
        return _profiles


def set_engine_profiles(profiles: EngineProfiles | None) -> None:
    global _profiles
    with _profiles_lock:
        _profiles = profiles


# ------------------------------------------------------------------ classification
def _close(solution_cp: int, other_cp: int) -> bool:
    if solution_cp >= MATE_CP:  # a mate is only matched by a mate at least as fast
        return other_cp >= solution_cp
    return solution_cp - other_cp <= CLOSE_CP


def _balance(board: chess.Board, side: chess.Color) -> int:
    def mat(c):
        return sum(VALUES[p.piece_type] for p in board.piece_map().values() if p.color == c and p.piece_type != chess.KING)
    return mat(side) - mat(not side)


def _obvious(board: chess.Board, move: chess.Move, prev: chess.Move | None, prev_board: chess.Board | None,
             follow_up: bool) -> str | None:
    legal = board.legal_moves.count()
    if legal == 1:
        return "only legal move"
    if follow_up and board.is_capture(move):
        taken = board.piece_at(move.to_square)
        mover = board.piece_at(move.from_square)
        if taken and mover and VALUES[taken.piece_type] >= 3 and (
                VALUES[taken.piece_type] > VALUES[mover.piece_type] or
                not board.is_attacked_by(not board.turn, move.to_square)):
            return "takes the target"
    if board.is_check() and legal <= 3:
        return "escape from check"
    if prev is not None and prev_board is not None and prev_board.is_capture(prev) and \
            board.is_capture(move) and move.to_square == prev.to_square:
        return "recapture"
    return None


def _gives_mate(board: chess.Board, move: chess.Move) -> bool:
    board.push(move)
    try:
        return board.is_checkmate()
    finally:
        board.pop()


def _mates(board: chess.Board) -> set[str]:
    return {m.uci() for m in board.legal_moves if _gives_mate(board, m)}


def _plausible(board: chess.Board, lines: list | None) -> int:
    moves = {m.uci() for m in board.legal_moves if board.gives_check(m) or board.is_capture(m)}
    if lines:
        best = lines[0][1]
        moves |= {u for u, cp in lines if best - cp <= PLAUSIBLE_CP}
    return max(1, len(moves))


def decision_rating(f: dict) -> int:
    r = 600 + 300 * f["quiet"] + 90 * (f["capture"] and not f["check"]) + 250 * f["sacrifice"] \
        + 100 * f["backward"] - 150 * f["free_capture"] + 45 * min(10, f["plausible"] - 1) \
        + 35 * min(8, f["payoff_plies"]) + 3 * max(0, f["legal_moves"] - 25)
    return int(max(MIN_RATING, min(MAX_RATING, r)))


def build(start_fen: str, moves: list[str], key_ply: int, *, family: str = "tactic",
          accepted: dict[int, list[str]] | None = None, data: dict | None = None) -> Profile:
    """Profile of the puzzle in `moves` starting at `key_ply`.

    family    mate | defense | tactic (which objective the puzzle has)
    accepted  {ply: [SAN]} moves verified as equally good (they solve the puzzle too)
    data      engine data from engine_data() (None: heuristics only)
    """
    rep = replay(start_fen, moves)
    n = len(moves)
    learner = rep.boards[key_ply].turn
    accepted = accepted or {}
    steps: list[Step] = []
    for ply in range(key_ply, n, 2):
        board = rep.boards[ply]
        move = board.parse_san(moves[ply])
        prev = rep.boards[ply - 1].parse_san(moves[ply - 1]) if ply > 0 else None
        alts = []
        for san in accepted.get(ply, []):
            try:
                alts.append(board.parse_san(san).uci())
            except ValueError:
                continue
        lines = (data or {}).get(str(ply))
        equal: list[str] = []
        good: list[str] = []
        margin = None
        if lines:
            sol = dict((u, cp) for u, cp in lines).get(move.uci(), lines[0][1])
            rivals = [(u, cp) for u, cp in lines if u != move.uci() and u not in alts]
            equal = sorted(u for u, cp in rivals if _close(sol, cp))
            good = sorted(u for u, cp in rivals if u not in equal and
                          (sol - cp < CRITICAL_MARGIN or (sol >= MATE_CP and cp >= MATE_CP)))
            if rivals:
                margin = sol - max(cp for _u, cp in rivals)
            elif len(lines) < board.legal_moves.count():
                margin = 10 ** 4     # every listed rival is an accepted alternative; the rest are worse
        f = move_features(board, move)
        # the verified key move is the puzzle's decision by definition; only follow-ups can be "obvious",
        # and a move that gives material away (a sacrifice) never is
        obvious = None if ply == key_ply or f["sacrifice"] else \
            _obvious(board, move, prev, rep.boards[ply - 1] if ply > 0 else None, True)
        mates = _mates(board) if _gives_mate(board, move) else set()
        if board.legal_moves.count() == 1:
            kind, reason = "forced", "only legal move"
        elif mates:   # any checkmate solves a puzzle: other mates are accepted, not a reason to doubt it
            kind, reason = "critical", "checkmate"
            alts = sorted(set(alts) | (mates - {move.uci()}))
            equal, good = [], [u for u in good + equal if u not in alts]
        elif equal:
            kind, reason = "open", "several moves are about as good"
            good = sorted(equal + good)
        elif obvious and (margin is None or margin >= CRITICAL_MARGIN):
            kind, reason = "forced", obvious
        else:
            kind, reason = "critical", "a clear best move" if lines else "the solution move"
        if kind == "forced":
            good = []
        reply = rep.boards[ply + 1].parse_san(moves[ply + 1]) if ply + 1 < n else None
        f["plausible"] = _plausible(board, lines)
        f["margin_cp"] = margin
        steps.append(Step(ply=ply, san=moves[ply], uci=move.uci(), kind=kind, reason=reason, accepted=alts,
                          good=good, reply_san=moves[ply + 1] if reply else None,
                          reply_uci=reply.uci() if reply else None, features=f))

    objective, end = _objective(rep, moves, key_ply, learner, family, steps)
    # a decision after the objective extends the puzzle only with engine evidence (a clear best move)
    last_critical = max((s.ply for s in steps if s.kind == "critical"
                         and (s.ply <= end or s.features["margin_cp"] is not None)), default=key_ply)
    end = max(end, last_critical)
    # a fork/threat is finished by cashing it in: keep an immediate "takes the target" capture
    for s in steps:
        if s.ply == end + 2 and s.kind == "forced" and s.reason == "takes the target":
            end = s.ply
    kept = [s for s in steps if s.ply <= end]
    kept[-1].reply_san = kept[-1].reply_uci = None  # the puzzle ends on the learner's move
    for s in kept:
        s.features["payoff_plies"] = end + 1 - s.ply
    decisions = [s for s in kept if s.kind == "critical"] or [kept[0]]
    for s in decisions:
        s.rating = decision_rating(s.features)
    ratings = sorted((s.rating for s in decisions), reverse=True)
    rating = int(max(MIN_RATING, min(MAX_RATING, ratings[0] + OTHER_DECISIONS * sum(ratings[1:]))))
    hardest = max(decisions, key=lambda s: s.rating)
    return Profile(steps=kept, solution=list(moves[key_ply:end + 1]), objective=objective, rating=rating,
                   trimmed=n - (end + 1), engine=bool(data), clear_start=kept[0].kind != "open",
                   basis={"hardest": hardest.san, "ply": hardest.ply,
                          **{k: hardest.features[k] for k in ("quiet", "check", "capture", "sacrifice",
                                                              "plausible", "payoff_plies", "margin_cp")}})


def _objective(rep, moves, key_ply, learner, family, steps) -> tuple[str, int]:
    n = len(moves)
    mate_line = rep.final.is_checkmate()
    if family == "mate" and mate_line:
        return "mate", n - 1
    start = _balance(rep.boards[key_ply], learner)
    final_gain = _balance(rep.final, learner) - start
    if family == "defense" and final_gain < 2:
        return "defense", key_ply   # the threat is met with the first move; critical follow-ups stay
    if final_gain >= 2:
        for s in steps:
            after = _balance(rep.boards[s.ply + 1], learner) - start
            after_reply = _balance(rep.boards[s.ply + 2], learner) - start if s.ply + 2 <= n else after
            if after >= final_gain - 1 and after_reply >= final_gain - 1:
                return "material", s.ply
    if mate_line:
        return "mate", n - 1
    return "idea", key_ply


def remember(example, engine, depth: int = 12) -> None:
    """Compute and store engine data for a new (generated/personal) puzzle entry."""
    if not example.key_move or example.key_ply is None:
        return
    sig = signature(example.start_fen, example.moves, example.key_ply)
    store = get_engine_profiles()
    if store.get(example.id, sig) is None:
        store.put(example.id, sig, engine_data(example.start_fen, list(example.moves), example.key_ply, engine, depth))
