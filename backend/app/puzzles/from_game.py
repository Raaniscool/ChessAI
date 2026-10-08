"""Source A: a puzzle from the learner's own game — the moment they went wrong.

The weakness evidence (analysis.weaknesses) records each mistake: the position before the move
(`fen`), what the learner played (`san`) and the engine's best move. That position is the most
personal training item there is ("this is the exact moment you missed it"), so a personalized
set may open with it. It is never trusted as-is:

    1. python-chess: the position is legal and it is the learner's move
    2. Stockfish, fresh (multipv): the best move's line is the solution; the move the learner
       played must still be clearly worse (>= profile.CRITICAL_MARGIN), so the puzzle really
       tests the decision they got wrong
    3. puzzles.profile with that engine data: critical / forced / open moves, where the puzzle
       ends (the objective), the rating. A first move that is "open" (several moves about as
       good) is not a puzzle and is rejected — exactly like library puzzles.

Game puzzles are private to the learner: stored under DATA_DIR/puzzles/game_puzzles.json,
never written to the Knowledge Library, never offered by Practice or by other learners' sets
(source type "user_game" is excluded everywhere else). They're served only as the clearly
labelled "From your game" item of a personalized set.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import chess

from .model import Puzzle, _accepted_san, difficulty_of
from ..knowledge.positions import replay
from .profile import CRITICAL_MARGIN, MATE_CP, MULTIPV, _close, build

DEPTH = 14
MAX_LINE = 9          # plies of the engine line kept before the profile trims it
MAX_SOLUTIONS = 3     # accepted moves at most (the best one + 2 equally good)
MAX_TRIES = 2         # evidence moments tried per set (each costs a few seconds of Stockfish)
MATE_MOTIFS = {"missed_checkmate", "allowed_checkmate"}


def puzzle_id(evidence: dict) -> str:
    return f"game:{evidence.get('game_id')}:{evidence.get('ply')}"


def _family(evidence: dict, best_cp: int) -> str:
    if best_cp >= MATE_CP or evidence.get("motif") in MATE_MOTIFS:
        return "mate"
    # "allowed" = the learner walked into it: the right move is the one that prevents it
    return "defense" if evidence.get("family") == "allowed" else "tactic"


def _played_cp(board: chess.Board, played: chess.Move, lines, engine) -> int:
    for ln in lines:
        if ln.move == played:
            return int(ln.score.for_side(board.turn == chess.WHITE))
    after = board.copy()
    after.push(played)
    if after.is_checkmate():
        return 10 ** 4
    reply = engine.analyse_lines(after, depth=DEPTH - 2, multipv=1, fresh=True)
    return -int(reply[0].score.for_side(after.turn == chess.WHITE)) if reply else 0


def verify(evidence: dict, engine) -> tuple[dict | None, str]:
    """(record, "ok") for a position that makes a real puzzle, else (None, why not)."""
    fen = evidence.get("fen")
    if not fen or not evidence.get("san"):
        return None, "no position recorded"
    try:
        board = chess.Board(fen)
    except ValueError:
        return None, "illegal position"
    if not board.is_valid() or board.is_game_over():
        return None, "illegal or finished position"
    side = evidence.get("side")
    if side and side != ("white" if board.turn else "black"):
        return None, "not the learner's move"
    try:
        played = board.parse_san(evidence["san"])
    except ValueError:
        return None, "the played move isn't legal here"
    lines = engine.analyse_lines(board, depth=DEPTH, multipv=MULTIPV, fresh=True)
    if not lines:
        return None, "no engine line"
    best = lines[0]
    best_cp = int(best.score.for_side(board.turn == chess.WHITE))
    if best.move == played:
        return None, "the engine now agrees with the move played"
    played_cp = _played_cp(board, played, lines, engine)
    if best_cp < MATE_CP and best_cp - played_cp < CRITICAL_MARGIN:
        return None, "the move played was not clearly worse at full depth"
    line = list(best.pv_san[:MAX_LINE]) or [best.san]
    if len(line) % 2 == 0:   # a puzzle ends on the learner's move
        line = line[:-1]
    family = _family(evidence, best_cp)
    # Up to MAX_SOLUTIONS moves may solve it (all accepted) when every other engine candidate is
    # clearly worse — "stop the fork" often has two good answers. More than that: not a puzzle.
    close = [ln for ln in lines[1:] if _close(best_cp, int(ln.score.for_side(board.turn == chess.WHITE)))]
    if len(close) >= MAX_SOLUTIONS or (close and len(close) == len(lines) - 1):
        return None, "several moves are about as good: no single idea to find"
    if played in [ln.move for ln in close]:
        return None, "the move played was about as good as the best"
    accepted = {0: [ln.san for ln in close]} if close else {}
    data = {"0": [[ln.move.uci(), int(ln.score.for_side(board.turn == chess.WHITE))] for ln in lines]}
    try:
        rep = replay(fen, line)
    except ValueError:
        return None, "the engine line doesn't replay"
    for ply in range(2, len(line), 2):   # the learner's later moves (the first one is above)
        b = rep.boards[ply]
        data[str(ply)] = [[ln.move.uci(), int(ln.score.for_side(b.turn == chess.WHITE))]
                          for ln in engine.analyse_lines(b, depth=DEPTH - 2, multipv=MULTIPV, fresh=True)]
    try:
        prof = build(fen, line, 0, family=family, data=data, accepted=accepted)
    except (ValueError, IndexError):
        return None, "the engine line doesn't replay"
    if not prof.clear_start:
        return None, "several moves are about as good: no single idea to find"
    if played.uci() in prof.steps[0].accepted:
        return None, "the move played would count as correct"
    return {"evidence": {k: evidence.get(k) for k in ("game_id", "moment_id", "ply", "move_number", "side", "san",
                                                      "best_move", "severity", "motif", "family", "opponent",
                                                      "date", "url", "concept")},
            "fen": fen, "line": line, "family": family, "data": data, "accepted": [ln.san for ln in close], "played_cp": played_cp, "best_cp": best_cp,
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, "ok"


def to_puzzle(record: dict, weakness_key: str, concept: str) -> Puzzle:
    """The Puzzle for a verified record (no engine call: the stored engine data is reused)."""
    accepted = {0: list(record.get("accepted") or [])}
    prof = build(record["fen"], record["line"], 0, family=record["family"], data=record["data"], accepted=accepted)
    ev = record["evidence"]
    board = chess.Board(record["fen"])
    return Puzzle(
        id=record["id"], type="mistake_correction" if record["family"] != "mate" else "checkmate",
        concept=concept, concepts=tuple(dict.fromkeys([concept, *( [ev["concept"]] if ev.get("concept") else [])])),
        difficulty=difficulty_of(prof.rating), rating=prof.rating, fen=record["fen"],
        side_to_move="white" if board.turn else "black", solution=tuple(prof.solution),
        learner_moves=len(prof.steps),
        # the same truth the solver judges with: the key move plus everything step 0 accepts
        accepted_first=tuple(dict.fromkeys([prof.solution[0], *_accepted_san(board, prof.steps[0])])),
        uniqueness="multiple" if prof.steps[0].accepted else "unique",
        main_idea=f"Your game: move {ev.get('move_number')}", required_skill=concept, tags=("your_game",),
        source={"type": "user_game", "id": ev.get("game_id"), "license": None, "url": ev.get("url")},
        verification_state="verified", tier="personal", weakness=weakness_key,
        steps=tuple(asdict(st) for st in prof.steps), objective=prof.objective,
        critical_moves=tuple(st.san for st in prof.critical), forced_moves=tuple(st.san for st in prof.forced),
        decision_points=tuple(i for i, st in enumerate(prof.steps, start=1) if st.kind == "critical"),
        meaningful_moves=len(prof.critical), trimmed=prof.trimmed, clear_start=prof.clear_start,
        engine_profiled=True, difficulty_basis=prof.basis, facts={"game": ev},
    )


class GamePuzzleStore:
    """Verified game puzzles and rejected moments (so neither is re-analysed)."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            data = {"puzzles": {}, "rejected": {}}
            if self.path and self.path.exists():
                try:
                    data.update(json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    pass
            self._data = data
        return self._data

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data), encoding="utf-8")

    def record(self, pid: str) -> dict | None:
        with self._lock:
            return self._load()["puzzles"].get(pid)

    def same_board(self, board: str, exclude: str) -> list[str]:
        """Other stored game puzzles of the same position (a repeated mistake in another game)."""
        with self._lock:
            return [pid for pid, rec in self._load()["puzzles"].items()
                    if pid != exclude and rec["fen"].split(" ")[0] == board]

    def rejected(self, pid: str) -> str | None:
        with self._lock:
            return self._load()["rejected"].get(pid)

    def put(self, pid: str, record: dict | None, why: str, weakness_key: str, concept: str) -> None:
        with self._lock:
            data = self._load()
            if record is None:
                data["rejected"][pid] = why
            else:
                data["puzzles"][pid] = {**record, "id": pid, "weakness": weakness_key, "concept": concept}
            self._save()

    def get(self, pid: str) -> Puzzle | None:
        rec = self.record(pid)
        if rec is None:
            return None
        try:
            return to_puzzle(rec, rec["weakness"], rec["concept"])
        except (ValueError, IndexError, KeyError):
            return None


_store: GamePuzzleStore | None = None
_store_lock = threading.Lock()


def get_game_puzzles() -> GamePuzzleStore:
    global _store
    with _store_lock:
        if _store is None:
            from ..config import get_settings
            _store = GamePuzzleStore(Path(get_settings().data_dir) / "puzzles" / "game_puzzles.json")
        return _store


def set_game_puzzles(store: GamePuzzleStore | None) -> None:
    global _store
    with _store_lock:
        _store = store


def pick(weakness: dict, engine, usage=None, now: datetime | None = None,
         store: GamePuzzleStore | None = None) -> tuple[Puzzle | None, list[dict]]:
    """The learner's own mistake to open the set with: (puzzle or None, debug trail).

    Evidence is tried most-severe first; a moment is skipped when it was rejected before or
    isn't due (the same novelty rules as library puzzles: not shown today, not solved in the
    last 3 days, a miss comes back the next day)."""
    from .select import MIN_NOVELTY, novelty
    store = store or get_game_puzzles()
    now = now or datetime.now(timezone.utc)
    trail: list[dict] = []
    tried = 0
    concept = weakness.get("concept")
    for ev in weakness.get("evidence", []):
        pid = puzzle_id(ev)
        board = (ev.get("fen") or "").split(" ")[0]
        if any(novelty(usage.puzzle_stats(other) if usage is not None else {}, now)[0] < MIN_NOVELTY
               for other in store.same_board(board, pid)):
            trail.append({"id": pid, "status": "not due", "why": "the same position was just shown"})
            continue
        if store.rejected(pid):
            trail.append({"id": pid, "status": "rejected before", "why": store.rejected(pid)})
            continue
        stats = usage.puzzle_stats(pid) if usage is not None else {}
        if novelty(stats, now)[0] < MIN_NOVELTY:
            trail.append({"id": pid, "status": "not due"})
            continue
        if store.record(pid) is None:
            if engine is None or tried >= MAX_TRIES:
                trail.append({"id": pid, "status": "skipped", "why": "no engine" if engine is None else "time"})
                continue
            tried += 1
            try:
                record, why = verify(ev, engine)
            except Exception as exc:  # engine unavailable / crashed: no verdict, try again next time
                trail.append({"id": pid, "status": "skipped", "why": f"engine: {exc}"})
                continue
            store.put(pid, record, why, weakness["key"], concept)
            if record is None:
                trail.append({"id": pid, "status": "rejected", "why": why})
                continue
        p = store.get(pid)
        if p is not None and p.clear_start:
            trail.append({"id": pid, "status": "used"})
            return p, trail
    return None, trail
