"""Stockfish-grounded Brilliant Finder over the verified sacrifice puzzle library.

The feature does not rely on a source's single expected move or on Qwen: python-chess
validates the submitted move, Stockfish evaluates its loss against best play, and the
existing learner profile/difficulty system adapts future selections. Manual levels are
relative quintiles of this collection's engine-profiled decision complexity, not human-Elo
promises or changes to the verdict policy.
"""
from __future__ import annotations

import copy
import logging
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
import chess
import chess.engine

from ..engine.classification import GOOD_CP, INACCURATE_CP, Score
from ..knowledge.difficulty import move_features
from .model import Puzzle

log = logging.getLogger(__name__)

DIFFICULTIES = ("adaptive", "beginner", "easy", "intermediate", "advanced", "expert")
MANUAL_LEVELS = DIFFICULTIES[1:]
LEVEL_LABELS = {
    "adaptive": "Adaptive",
    "beginner": "Beginner",
    "easy": "Easy",
    "intermediate": "Intermediate",
    "advanced": "Advanced",
    "expert": "Expert",
}
LEVEL_DESCRIPTIONS = {
    "beginner": "The easiest fifth of the current verified Finder collection.",
    "easy": "The next-easiest fifth of the current verified Finder collection.",
    "intermediate": "The middle fifth of the current verified Finder collection.",
    "advanced": "The next-hardest fifth of the current verified Finder collection.",
    "expert": "The hardest fifth of the current verified Finder collection.",
}

# Verdict thresholds reuse the engine's established move-quality policy. Difficulty never
# changes them. These are transparent practice labels, not Chess.com's proprietary badge.
SOUND_LOSS_CP = GOOD_CP
PLAYABLE_LOSS_CP = INACCURATE_CP
SUCCESS_SCORES = {
    "sound_sacrifice": 1.0,
    "playable_sacrifice": 0.8,
    "strong_tactical_move": 0.8,
    "strong_non_sacrifice": 0.75,
    "stronger_alternative": 0.2,
    "incorrect_sacrifice": 0.0,
}
SUCCESS_VERDICTS = frozenset({
    "sound_sacrifice", "playable_sacrifice", "strong_tactical_move", "strong_non_sacrifice",
})
VERDICT_LABELS = {
    "sound_sacrifice": "Sound sacrifice",
    "playable_sacrifice": "Playable sacrifice",
    "strong_tactical_move": "Strong tactical move — not a sacrifice",
    "strong_non_sacrifice": "Strong move — not a material sacrifice",
    "incorrect_sacrifice": "Incorrect sacrifice",
    "stronger_alternative": "A stronger alternative was available",
    "inconclusive": "Analysis inconclusive",
}
RECENT_FINDER_WINDOW = 5
MIN_FINDER_RESULTS_TO_ADAPT = 4
MAX_FINDER_SHIFT = 40
TASK_TTL_SECONDS = 30 * 60


class BrilliantFinderError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class BrilliantTask:
    task_id: str
    puzzle: Puzzle
    preference: str
    complexity_band: str
    calibration: dict
    issued_at: float = field(default_factory=time.monotonic)
    status: str = "playing"  # playing | processing | complete
    step_index: int = 0
    current_fen: str = ""
    played: list[dict] = field(default_factory=list)
    mistakes: int = 0
    sacrifice_fen: str | None = None
    sacrifice_uci: str | None = None
    sacrifice_san: str | None = None
    result: dict | None = None
    result_recorded: bool = False
    inflight_key: tuple[int, str] | None = None
    move_results: dict[tuple[int, str], dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.current_fen:
            self.current_fen = self.puzzle.fen


class BrilliantTaskStore:
    """Short-lived server-authoritative guided lines with per-turn idempotency."""

    def __init__(self):
        self._lock = threading.RLock()
        self._tasks: dict[str, BrilliantTask] = {}

    def _purge(self) -> None:
        cutoff = time.monotonic() - TASK_TTL_SECONDS
        for task_id in [key for key, task in self._tasks.items() if task.issued_at < cutoff]:
            self._tasks.pop(task_id, None)

    def issue(self, puzzle: Puzzle, preference: str, complexity_band: str, calibration: dict) -> BrilliantTask:
        task = BrilliantTask(str(uuid.uuid4()), puzzle, preference, complexity_band, copy.deepcopy(calibration))
        with self._lock:
            self._purge()
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> BrilliantTask:
        with self._lock:
            self._purge()
            task = self._tasks.get(task_id)
            if task is None:
                raise BrilliantFinderError("This Finder position is unknown or has expired. Request another position.", 404)
            return task

    def claim_move(self, task_id: str, step_index: int, move_uci: str,
                   before_fen: str | None = None) -> tuple[BrilliantTask, dict | None]:
        key = (step_index, move_uci.strip().lower())
        with self._lock:
            self._purge()
            task = self._tasks.get(task_id)
            if task is None:
                raise BrilliantFinderError("This Finder position is unknown or has expired. Request another position.", 404)
            cached = task.move_results.get(key)
            if cached is not None:
                return task, copy.deepcopy(cached)
            if task.status == "processing":
                raise BrilliantFinderError("A move is already being processed. Please wait for the reply.", 409)
            if task.status == "complete":
                raise BrilliantFinderError("This Finder line is already complete. Start another position or retry it.", 409)
            if step_index != task.step_index:
                raise BrilliantFinderError("The puzzle turn has changed. Restore the current board position and retry.", 409)
            if before_fen and before_fen != task.current_fen:
                raise BrilliantFinderError("The board is out of sync with this Finder turn. Restore the current position.", 409)
            task.status = "processing"
            task.inflight_key = key
            return task, None

    def finish_move(self, task: BrilliantTask, key: tuple[int, str], result: dict, *, done: bool = False) -> None:
        with self._lock:
            if self._tasks.get(task.task_id) is not task or task.inflight_key != key:
                return
            task.move_results[key] = copy.deepcopy(result)
            task.inflight_key = None
            task.status = "complete" if done else "playing"
            if done:
                task.result = copy.deepcopy(result.get("result") or {})

    def release_move(self, task: BrilliantTask, key: tuple[int, str]) -> None:
        with self._lock:
            if self._tasks.get(task.task_id) is task and task.inflight_key == key:
                task.inflight_key = None
                task.status = "playing"

    def claim_reveal(self, task_id: str) -> tuple[BrilliantTask, bool]:
        with self._lock:
            self._purge()
            task = self._tasks.get(task_id)
            if task is None:
                raise BrilliantFinderError("This Finder position is unknown or has expired. Request another position.", 404)
            if task.status == "complete":
                return task, False
            if task.status == "processing":
                raise BrilliantFinderError("A move is still being processed. Wait for the verified reply first.", 409)
            task.status = "processing"
            task.inflight_key = (task.step_index, "__reveal__")
            return task, True

    def finish_reveal(self, task: BrilliantTask, result: dict) -> None:
        with self._lock:
            if self._tasks.get(task.task_id) is task:
                task.result = copy.deepcopy(result)
                task.inflight_key = None
                task.status = "complete"


_task_store: BrilliantTaskStore | None = None
_task_store_lock = threading.Lock()


def get_brilliant_task_store() -> BrilliantTaskStore:
    global _task_store
    with _task_store_lock:
        if _task_store is None:
            _task_store = BrilliantTaskStore()
        return _task_store


def set_brilliant_task_store(store: BrilliantTaskStore | None) -> None:
    global _task_store
    with _task_store_lock:
        _task_store = store


def preference_of(profile) -> str:
    preferences = getattr(profile, "preferences", None)
    if not isinstance(preferences, dict):
        return "adaptive"
    finder = preferences.get("brilliant_finder")
    value = finder.get("difficulty") if isinstance(finder, dict) else None
    return value if value in DIFFICULTIES else "adaptive"


def set_preference(profile, difficulty: str) -> dict:
    if difficulty not in DIFFICULTIES:
        raise BrilliantFinderError(f"difficulty must be one of {', '.join(DIFFICULTIES)}", 422)
    if not isinstance(getattr(profile, "preferences", None), dict):
        profile.preferences = {}
    finder = profile.preferences.get("brilliant_finder")
    finder = dict(finder) if isinstance(finder, dict) else {}
    finder["difficulty"] = difficulty
    profile.preferences["brilliant_finder"] = finder
    if hasattr(profile, "touch"):
        profile.touch()
    return {"difficulty": difficulty, "label": LEVEL_LABELS[difficulty]}


def _first_step(puzzle: Puzzle) -> dict:
    return puzzle.steps[0] if puzzle.steps and isinstance(puzzle.steps[0], dict) else {}


def _feature(puzzle: Puzzle, name: str, default=0):
    return (_first_step(puzzle).get("features") or {}).get(name, default)


def _defensive_reply_count(puzzle: Puzzle) -> int:
    """Count legal replies after the verified key move; used only as a complexity tiebreaker."""
    try:
        board = chess.Board(puzzle.fen)
        move = chess.Move.from_uci(_first_step(puzzle)["uci"])
        if move not in board.legal_moves:
            return 0
        board.push(move)
        return board.legal_moves.count()
    except (KeyError, TypeError, ValueError):
        return 0


def _sacrifice_indices(puzzle: Puzzle) -> tuple[int, ...]:
    """Critical learner turns explicitly marked as sacrifices in the existing move profile."""
    return tuple(
        index for index, step in enumerate(puzzle.steps or ())
        if isinstance(step, dict) and step.get("kind") == "critical"
        and bool((step.get("features") or {}).get("sacrifice"))
    )


def _first_sacrifice_index(puzzle: Puzzle) -> int | None:
    indices = _sacrifice_indices(puzzle)
    return indices[0] if indices else None


def _complexity_key(puzzle: Puzzle) -> tuple:
    """Decision rating plus verified setup depth and measurable calculation/defence features."""
    sacrifice_index = _first_sacrifice_index(puzzle)
    return (
        int(puzzle.rating),
        int(sacrifice_index if sacrifice_index is not None else 0),
        int(puzzle.meaningful_moves),
        int(puzzle.learner_moves),
        int(_feature(puzzle, "plausible", 1)),
        int(_feature(puzzle, "payoff_plies", 1)),
        int(_feature(puzzle, "legal_moves", 0)),
        _defensive_reply_count(puzzle),
        1 if puzzle.uniqueness == "unique" else 0,
        puzzle.id,
    )


def _verified_guided_line(puzzle: Puzzle) -> list[dict] | None:
    """Validate that Puzzle.steps and opponent replies exactly replay the stored verified SAN line."""
    try:
        board = chess.Board(puzzle.fen)
        if not board.is_valid() or board.turn != (puzzle.side_to_move == "white"):
            return None
        if not puzzle.solution or not puzzle.steps:
            return None
        line = []
        cursor = 0
        for index, step in enumerate(puzzle.steps):
            if not isinstance(step, dict) or cursor >= len(puzzle.solution):
                return None
            player = board.parse_san(puzzle.solution[cursor])
            player_san = board.san(player)
            if player not in board.legal_moves or player.uci() != step.get("uci") or player_san != step.get("san"):
                return None
            line.append({"player_uci": player.uci(), "player_san": player_san,
                         "reply_uci": None, "reply_san": None})
            board.push(player)
            cursor += 1
            if index < len(puzzle.steps) - 1:
                if not step.get("reply_uci") or cursor >= len(puzzle.solution):
                    return None
                reply = board.parse_san(puzzle.solution[cursor])
                reply_san = board.san(reply)
                if reply not in board.legal_moves or reply.uci() != step.get("reply_uci") \
                        or reply_san != step.get("reply_san"):
                    return None
                line[-1]["reply_uci"] = reply.uci()
                line[-1]["reply_san"] = reply_san
                board.push(reply)
                cursor += 1
            elif step.get("reply_uci") or step.get("reply_san"):
                return None
        if cursor != len(puzzle.solution) or not _sacrifice_indices(puzzle):
            return None
        return line
    except (KeyError, TypeError, ValueError, chess.InvalidMoveError, chess.IllegalMoveError,
            chess.AmbiguousMoveError):
        return None


def _line_is_legal(puzzle: Puzzle) -> bool:
    return _verified_guided_line(puzzle) is not None


def eligible_puzzles(knowledge) -> list[Puzzle]:
    """Global, verified engine-profiled lines with a critical sacrifice at any learner turn."""
    from . import get_puzzles

    out = []
    for puzzle in get_puzzles(knowledge).all(include_personal=False):
        if puzzle.tier != "global":
            continue
        if puzzle.verification_state != "verified" or not puzzle.engine_profiled or not puzzle.clear_start:
            continue
        if puzzle.uniqueness not in ("unique", "multiple"):
            continue
        if _verified_guided_line(puzzle) is not None:
            out.append(puzzle)
    return sorted(out, key=lambda item: (item.id, item.rating))


def manual_bands(puzzles: list[Puzzle]) -> dict[str, list[Puzzle]]:
    """Assign real library positions to five stable, ordered complexity quintiles."""
    bands = {level: [] for level in MANUAL_LEVELS}
    ordered = sorted(puzzles, key=_complexity_key)
    if not ordered:
        return bands
    for index, puzzle in enumerate(ordered):
        band_index = min(len(MANUAL_LEVELS) - 1, index * len(MANUAL_LEVELS) // len(ordered))
        bands[MANUAL_LEVELS[band_index]].append(puzzle)
    return bands


def _finder_events(profile) -> list[tuple[dict, dict]]:
    out = []
    for entry in (getattr(profile, "exercise_log", None) or []):
        if not isinstance(entry, dict) or entry.get("source") != "brilliant_finder":
            continue
        metadata = entry.get("brilliant_finder")
        if isinstance(metadata, dict):
            out.append((entry, metadata))
    return out


def _recent_finder_adjustment(profile) -> tuple[int, list[tuple[dict, dict]]]:
    events = _finder_events(profile)
    scored = [(entry, meta) for entry, meta in events if meta.get("scored") is True
              and isinstance(meta.get("success_score"), (int, float))]
    recent = scored[-RECENT_FINDER_WINDOW:]
    if len(recent) < MIN_FINDER_RESULTS_TO_ADAPT:
        return 0, events
    average = sum(float(meta["success_score"]) for _entry, meta in recent) / len(recent)
    if average >= 0.85:
        return MAX_FINDER_SHIFT, events
    if average <= 0.25:
        return -MAX_FINDER_SHIFT, events
    return 0, events


def _adaptive_evidence_count(profile, calibration: dict) -> int:
    """How much relevant evidence learner.difficulty actually used for this tactic target."""
    concept = calibration.get("concept") or {}
    count = int(concept.get("puzzles") or 0) + int((concept.get("recognition") or {}).get("tested") or 0)
    game_skill = getattr(profile, "game_skill", None) or {}
    chances = (game_skill.get("chances") or {}).get("items") or []
    count += sum(1 for item in chances if item.get("kind") in ("pattern", "calculation"))
    return count


def adaptive_calibration(knowledge, profile, usage) -> dict:
    """Start from learner.difficulty and gently incorporate recent Finder verdict evidence.

    The existing difficulty estimate remains authoritative. Its change from the rating prior is
    applied as a bounded selection adjustment that expands with relevant puzzle, game, and
    unprompted Training evidence; one result therefore cannot jump the Finder several levels.
    """
    from ..learner.difficulty import ZONE_OFFSET, for_learner

    learner = for_learner(knowledge, profile, usage)
    calibration = learner.target("sacrifice", knowledge)
    calibration["prior"] = learner.prior
    shift, events = _recent_finder_adjustment(profile)
    raw_target = int(calibration["target"]) + shift
    evidence_count = _adaptive_evidence_count(profile, calibration)
    prior_target = int((learner.prior or {}).get("puzzle_scale", raw_target + ZONE_OFFSET)) - ZONE_OFFSET
    if _rating_reference(profile, calibration).get("stale"):
        prior_target = int(round((prior_target + 940) / 2))
    max_adjustment = min(240, 40 * max(1, evidence_count))
    delta = max(-max_adjustment, min(max_adjustment, raw_target - prior_target))
    target = max(400, min(2400, prior_target + delta))
    calibration = dict(calibration)
    calibration["learner_target"] = int(calibration["target"])
    calibration["target"] = target
    calibration["zone"] = [target - 120, target + 150]
    calibration["floor"] = target - 250
    calibration["finder_shift"] = shift
    calibration["finder_results"] = sum(1 for _entry, meta in events if meta.get("scored") is True)
    calibration["adaptation_evidence"] = evidence_count
    calibration["confidence"] = (calibration.get("skill_evidence") or {}).get("confidence", 0)
    if target != int(calibration["learner_target"]):
        calibration["summary"] = (calibration.get("summary") or "Adaptive from your learner profile") + \
            f"; blended gradually from {evidence_count} relevant results"
    if shift:
        calibration["summary"] = (calibration.get("summary") or "Adaptive from your learner profile") + \
            ("; a small step up after several strong Finder results" if shift > 0
             else "; easing down a little after several difficult Finder results")
    # The prior is weak and never gates the pool; game, Training, and puzzle evidence refine it.
    return calibration


def _rating_reference(profile, calibration: dict) -> dict:
    onboarding = getattr(profile, "onboarding", None) or {}
    rating = onboarding.get("rating")
    platform = onboarding.get("platform")
    source = (calibration.get("prior") or {}).get("source", "default starting estimate")
    is_chesscom = bool(rating and platform == "chesscom")
    at = onboarding.get("at")
    age_days = None
    stale = False
    if is_chesscom and at:
        try:
            then = datetime.fromisoformat(at)
            then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
            age_days = max(0, int((datetime.now(timezone.utc) - then).total_seconds() / 86400))
            stale = age_days > 365
        except (TypeError, ValueError):
            stale = True
    if is_chesscom:
        source = "Chess.com rating"
    return {
        "available": bool(is_chesscom),
        "source": source,
        "stale": stale,
        "age_days": age_days,
        "role": "weak initial reference; game, Training, and puzzle results refine it",
    }


def _band_summary(level: str, puzzles: list[Puzzle]) -> dict:
    ordered = sorted(puzzles, key=_complexity_key)
    if not ordered:
        return {"difficulty": level, "label": LEVEL_LABELS[level], "description": LEVEL_DESCRIPTIONS[level],
                "count": 0, "features": {}}
    lines = [int(p.learner_moves) for p in ordered]
    setups = [int(_first_sacrifice_index(p) or 0) for p in ordered]
    candidates = [int(_feature(p, "plausible", 1)) for p in ordered]
    defenses = [_defensive_reply_count(p) for p in ordered]
    decisions = [int(p.meaningful_moves) for p in ordered]
    return {
        "difficulty": level,
        "label": LEVEL_LABELS[level],
        "description": LEVEL_DESCRIPTIONS[level],
        "count": len(ordered),
        "features": {
            "decision_rating_range": [min(p.rating for p in ordered), max(p.rating for p in ordered)],
            "solution_moves_range": [min(lines), max(lines)],
            "preparatory_player_moves_before_sacrifice_range": [min(setups), max(setups)],
            "decision_points_range": [min(decisions), max(decisions)],
            "plausible_candidates_range": [min(candidates), max(candidates)],
            "defensive_replies_range": [min(defenses), max(defenses)],
        },
    }


def profile_summary(knowledge, profile, usage) -> dict:
    puzzles = eligible_puzzles(knowledge)
    bands = manual_bands(puzzles)
    calibration = adaptive_calibration(knowledge, profile, usage)
    events = _finder_events(profile)
    recent = []
    for entry, meta in events[-5:]:
        recent.append({
            "difficulty": meta.get("difficulty", "adaptive"),
            "complexity_band": meta.get("complexity_band"),
            "verdict": meta.get("verdict"),
            "result": entry.get("result"),
            "scored": bool(meta.get("scored")),
        })
    return {
        "difficulty": preference_of(profile),
        "available": len(puzzles),
        "bands": [_band_summary(level, bands[level]) for level in MANUAL_LEVELS],
        "adaptive": {
            "target": calibration["target"],
            "zone": calibration["zone"],
            "confidence": calibration.get("confidence", 0),
            "summary": calibration.get("summary"),
            "skill": calibration.get("skill", "tactics"),
            "finder_shift": calibration.get("finder_shift", 0),
            "finder_results": calibration.get("finder_results", 0),
            "rating_reference": _rating_reference(profile, calibration),
            "evidence": (calibration.get("skill_evidence") or {}).get("evidence", []),
        },
        "results": {"total": len(events),
                    "scored": sum(1 for _entry, meta in events if meta.get("scored") is True),
                    "recent": recent},
        "labels_note": "Manual labels rank positions by this verified collection's engine-profiled complexity; they are not human Elo promises.",
    }


def _manual_calibration(level: str, puzzles: list[Puzzle]) -> dict:
    target = int(round(statistics.median(p.rating for p in puzzles)))
    floor = min(p.rating for p in puzzles) - 1
    return {
        "target": target,
        "zone": [min(p.rating for p in puzzles), max(p.rating for p in puzzles)],
        "floor": floor,
        "skill": "tactics",
        "skill_level": level,
        "summary": f"{LEVEL_LABELS[level]}: relative engine-profiled complexity in the current verified Finder collection",
        "manual": True,
    }


class _IgnoreRecentUsage:
    """Used only as a review fallback after normal novelty has no eligible position."""
    def puzzle_stats(self, _puzzle_id: str) -> dict:
        return {}


def _selection(knowledge, puzzles: list[Puzzle], profile, usage, preference: str, exclude: set[str]):
    from .select import select

    bands = manual_bands(puzzles)
    candidates = puzzles
    if preference in MANUAL_LEVELS:
        candidates = bands[preference]
        if not candidates:
            raise BrilliantFinderError(f"No verified positions are available for {LEVEL_LABELS[preference]} yet.", 404)
        calibration = _manual_calibration(preference, candidates)
    else:
        calibration = adaptive_calibration(knowledge, profile, usage)
    available = [p for p in candidates if p.id not in exclude]
    if not available:
        label = LEVEL_LABELS.get(preference, "Adaptive")
        raise BrilliantFinderError(
            f"No unseen {label} positions remain in this session. Choose another difficulty or start a new session.", 404)
    # Finder eligibility is established by a critical sacrifice anywhere in the stored line,
    # not by the puzzle's primary taxonomy tag (many verified combinations lead to a sacrifice).
    # Explicit relevance overrides keep the existing learner calibration/novelty machinery while
    # retaining every position in this verified collection.
    overrides = {p.id: (1.0, "exact") for p in available}
    choice = select(available, knowledge, "sacrifice", count=1, profile=profile, usage=usage,
                    exclude=exclude, calibration=calibration, overrides=overrides)
    review = False
    if not choice.chosen:
        # Existing puzzle selection deliberately avoids recent repeats. Once that policy leaves
        # no usable puzzle, review an unplayed-this-session position in the same category rather
        # than letting a fresh/recent record strand a manual level.
        choice = select(available, knowledge, "sacrifice", count=1, profile=profile,
                        usage=_IgnoreRecentUsage(), exclude=exclude, calibration=calibration,
                        overrides=overrides)
        review = bool(choice.chosen)
    if not choice.chosen:
        raise BrilliantFinderError("No verified sacrifice positions fit this selection right now.", 404)
    puzzle = choice.chosen[0].puzzle
    complexity_band = next((level for level, members in bands.items() if any(p.id == puzzle.id for p in members)),
                           "intermediate")
    return puzzle, calibration, complexity_band, review


def issue_task(knowledge, profile, usage, *, exclude: list[str] | None = None,
               store: BrilliantTaskStore | None = None) -> BrilliantTask:
    puzzles = eligible_puzzles(knowledge)
    if not puzzles:
        raise BrilliantFinderError("No verified, engine-profiled sacrifice positions are available.", 404)
    preference = preference_of(profile)
    puzzle, calibration, complexity_band, _review = _selection(
        knowledge, puzzles, profile, usage, preference,
        {value for value in (exclude or []) if isinstance(value, str)},
    )
    task = (store or get_brilliant_task_store()).issue(puzzle, preference, complexity_band, calibration)
    try:
        usage.record_used([puzzle])
    except (OSError, AttributeError):
        pass
    return task


def _complexity_payload(puzzle: Puzzle) -> dict:
    # Do not disclose line length or the sacrifice's location before the player finds it.
    return {
        "decision_points": puzzle.meaningful_moves,
        "plausible_candidates": int(_feature(puzzle, "plausible", 1)),
        "legal_moves": int(_feature(puzzle, "legal_moves", 0)),
        "defensive_replies": _defensive_reply_count(puzzle),
        "uniqueness": puzzle.uniqueness,
    }


def task_payload(task: BrilliantTask) -> dict:
    """Public task data omits solution moves, turn profiles and accepted alternatives."""
    return {
        "task_id": task.task_id,
        "id": task.puzzle.id,
        "fen": task.puzzle.fen,
        "side_to_move": task.puzzle.side_to_move,
        "prompt": "Play the full combination yourself. The verified opponent reply will move automatically.",
        "difficulty": {
            "preference": task.preference,
            "label": LEVEL_LABELS[task.preference],
            "complexity_band": task.complexity_band,
            "complexity": _complexity_payload(task.puzzle),
        },
    }


def validate_position_move(fen: str, move_uci: str) -> tuple[chess.Board, chess.Move, str]:
    try:
        board = chess.Board(fen)
    except (TypeError, ValueError) as exc:
        raise BrilliantFinderError(f"Invalid Finder position: {exc}", 422) from exc
    if not board.is_valid():
        raise BrilliantFinderError("Finder position is not a valid chess position.", 422)
    if not isinstance(move_uci, str):
        raise BrilliantFinderError("Move must be UCI text.", 422)
    try:
        move = chess.Move.from_uci(move_uci.strip())
    except (TypeError, ValueError) as exc:
        raise BrilliantFinderError("Move must be a valid UCI move.", 422) from exc
    if move not in board.legal_moves:
        raise BrilliantFinderError("That move is illegal in this Finder position.", 422)
    return board, move, board.san(move)


def _relative_score(score: Score, player_is_white: bool) -> dict:
    if not isinstance(score, Score) or score.kind not in ("cp", "mate", "checkmate"):
        raise ValueError("Stockfish returned an unsupported score representation")
    raw = score.as_dict()
    if score.kind == "cp":
        value = score.for_side(player_is_white)
        label = f"{value / 100:+.2f} pawns for you"
        relative = {"kind": "cp", "value": value}
    else:
        wins_for_player = score.is_mate_for(player_is_white)
        if score.kind == "checkmate":
            value = 0
            label = "Checkmate for you" if wins_for_player else "You are checkmated"
        else:
            value = abs(score.value) if wins_for_player else -abs(score.value)
            label = f"Mate in {abs(value)} for you" if value > 0 else f"You are mated in {abs(value)}"
        relative = {"kind": "mate", "value": value}
    return {"white_perspective": raw, "player_perspective": relative, "label": label}


def _line_with_uci(board: chess.Board, san_moves: list[str], *, required_first: chess.Move | None = None) -> dict:
    if not isinstance(san_moves, (list, tuple)):
        raise ValueError("Stockfish returned a malformed continuation")
    line_board = board.copy()
    moves: list[str] = []
    ucis: list[str] = []
    positions = [line_board.fen()]
    for san in san_moves:
        move = line_board.parse_san(str(san))
        if move not in line_board.legal_moves:
            raise ValueError("Stockfish continuation contains an illegal move")
        moves.append(line_board.san(move))
        ucis.append(move.uci())
        line_board.push(move)
        positions.append(line_board.fen())
    if required_first is not None and (not ucis or ucis[0] != required_first.uci()):
        raise ValueError("Stockfish continuation does not begin with the reported move")
    return {"moves": moves, "ucis": ucis, "positions": positions}


def _line_has_tactics(board: chess.Board, line: list[str]) -> bool:
    trial = board.copy()
    for san in line:
        try:
            move = trial.parse_san(san)
        except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError):
            return False
        features = move_features(trial, move)
        if features["check"] or features["capture"] or features["promotion"] or features["sacrifice"]:
            return True
        trial.push(move)
    return False


def _verdict(*, sacrifice: bool, tactical: bool, loss_cp: int) -> str:
    """Classify quality identically at every difficulty; source expected moves are not inputs."""
    if sacrifice:
        if loss_cp <= SOUND_LOSS_CP:
            return "sound_sacrifice"
        if loss_cp <= PLAYABLE_LOSS_CP:
            return "playable_sacrifice"
        return "incorrect_sacrifice"
    if loss_cp <= SOUND_LOSS_CP:
        return "strong_tactical_move" if tactical else "strong_non_sacrifice"
    return "stronger_alternative"


def _engine_call(engine, board: chess.Board, move: chess.Move):
    from ..config import get_settings

    settings = get_settings()
    depth = int(getattr(settings, "decision_engine_depth", 10))
    timeout = float(getattr(settings, "decision_engine_timeout", 6.0))
    try:
        feedback = engine.evaluate_move(board, move, depth=depth, timeout=timeout)
    except TypeError as exc:
        # Simple legacy/test doubles may not expose the bounded keyword. Real UciEngine does;
        # keep compatibility without hiding other TypeErrors raised inside an engine adapter.
        if "timeout" not in str(exc):
            raise
        feedback = engine.evaluate_move(board, move, depth=depth)
    return feedback, timeout


def _inconclusive(reason: str, selected_move: str | None = None, line: list[str] | None = None) -> dict:
    return {
        "analysis_status": "inconclusive",
        "verdict": "inconclusive",
        "label": VERDICT_LABELS["inconclusive"],
        "reason": reason,
        "selected_move": selected_move,
        "sacrifice": None,
        "tactical": None,
        "stronger_alternative": None,
        "best_move": None,
        "score": None,
        "selected_line": {"moves": list(line or []), "ucis": [], "positions": []},
        "best_line": {"moves": [], "ucis": [], "positions": []},
        "depth": None,
    }


def _evaluate_position(puzzle: Puzzle, fen: str, move_uci: str, engine, *, expected_uci: str | None = None) -> dict:
    try:
        board, move, move_san = validate_position_move(fen, move_uci)
    except BrilliantFinderError:
        raise
    after = board.copy()
    after.push(move)
    if engine is None:
        return _inconclusive("stockfish_unavailable", move_san, [move_san])
    try:
        feedback, timeout = _engine_call(engine, board, move)
    except (TimeoutError, chess.engine.EngineTimeoutError):
        return _inconclusive("stockfish_timeout", move_san, [move_san])
    except (chess.engine.EngineError, OSError):
        return _inconclusive("stockfish_error", move_san, [move_san])
    except Exception:
        log.exception("Brilliant Finder Stockfish analysis failed")
        return _inconclusive("stockfish_unavailable", move_san, [move_san])

    try:
        if feedback is None or feedback.user_move_uci != move.uci():
            raise ValueError("Stockfish did not return a result for the submitted move")
        if feedback.fen_before != board.fen() or feedback.fen_after != after.fen():
            raise ValueError("Stockfish result does not match the submitted position")
        if feedback.best_move_uci is None or feedback.eval_before is None or feedback.eval_after is None:
            raise ValueError("Stockfish did not return a complete best-move comparison")
        best = chess.Move.from_uci(feedback.best_move_uci)
        if best not in board.legal_moves:
            raise ValueError("Stockfish best move is illegal in the submitted position")
        best_san = board.san(best)
        if feedback.best_move_san != best_san:
            raise ValueError("Stockfish best-move notation does not match its legal move")
        depth = int(getattr(feedback, "depth", None) or 0)
        if depth <= 0:
            raise ValueError("Stockfish did not report an analysis depth")
        before = _relative_score(feedback.eval_before, board.turn == chess.WHITE)
        after_score = _relative_score(feedback.eval_after, board.turn == chess.WHITE)
        before_cp = feedback.eval_before.for_side(board.turn == chess.WHITE)
        after_cp = feedback.eval_after.for_side(board.turn == chess.WHITE)
        # The shared move classifier deliberately gives Stockfish's own best move zero loss;
        # separate before/after searches can differ slightly because of engine noise.
        loss_cp = 0 if best == move else max(0, int(before_cp - after_cp))
        if int(feedback.loss_cp) != loss_cp:
            raise ValueError("Stockfish score perspective and reported move loss disagree")
        best_pv = list(getattr(feedback, "best_pv_san", None) or [best_san])
        if not best_pv:
            best_pv = [best_san]
        best_line = _line_with_uci(board, best_pv, required_first=best)
        selected_pv = list(getattr(feedback, "reply_pv_san", None) or [])
        selected_continuation = _line_with_uci(after, selected_pv)
        selected_line = {
            "moves": [move_san, *selected_continuation["moves"]],
            "ucis": [move.uci(), *selected_continuation["ucis"]],
            "positions": [board.fen(), *selected_continuation["positions"]],
        }
        # The selected continuation's first position is the pre-move position; avoid duplicating it.
        selected_line["positions"] = [board.fen(), after.fen(), *selected_continuation["positions"][1:]]
        features = move_features(board, move)
        sacrifice = bool(features["sacrifice"])
        tactical = bool(
            features["check"] or features["capture"] or features["promotion"]
            or _line_has_tactics(board, best_line["moves"])
            or _line_has_tactics(after, selected_continuation["moves"])
            or feedback.eval_before.is_mate_for(board.turn == chess.WHITE)
            or feedback.eval_after.is_mate_for(board.turn == chess.WHITE)
        )
        best_differs = best != move
        verdict = _verdict(sacrifice=sacrifice, tactical=tactical, loss_cp=loss_cp)
        alternative = {
            "move": best_san,
            "uci": best.uci(),
            "line": best_line["moves"],
            "loss_cp": loss_cp,
        } if best_differs else None
        source_expected = expected_uci
        return {
            "analysis_status": "complete",
            "position_fen": board.fen(),
            "verdict": verdict,
            "label": VERDICT_LABELS[verdict],
            "selected_move": move_san,
            "selected_uci": move.uci(),
            "sacrifice": sacrifice,
            "tactical": tactical,
            "engine_supported": loss_cp <= SOUND_LOSS_CP,
            "stronger_alternative": alternative if loss_cp > SOUND_LOSS_CP and best_differs else None,
            "best_move": {"san": best_san, "uci": best.uci()},
            "score": {
                "perspective": "white" if board.turn == chess.WHITE else "black",
                "before": before,
                "after": after_score,
                "loss_cp": loss_cp,
                "engine_category": str(getattr(feedback.category, "value", feedback.category)),
            },
            "selected_line": selected_line,
            "best_line": best_line,
            "depth": depth,
            "timeout_seconds": timeout,
            # Deliberately diagnostic only: never used to decide whether the move is supported.
            "source_expected_match": bool(source_expected and move.uci() == source_expected),
        }
    except (TypeError, ValueError, chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError,
            AttributeError, OverflowError):
        return _inconclusive("incomplete_or_untrusted_stockfish_result", move_san, [move_san])


def _evaluate_task(task: BrilliantTask, move_uci: str, engine) -> dict:
    """Compatibility helper for analysis at the task's initial position."""
    return _evaluate_position(task.puzzle, task.puzzle.fen, move_uci, engine,
                              expected_uci=_first_step(task.puzzle).get("uci"))


def _finder_context(task: BrilliantTask, result: dict, *, scored: bool) -> dict:
    analysis = result.get("analysis") or {}
    return {
        "difficulty": task.preference,
        "complexity_band": task.complexity_band,
        "verdict": result.get("outcome", "inconclusive"),
        "engine_verdict": analysis.get("verdict") if analysis.get("analysis_status") == "complete" else None,
        "analysis_status": analysis.get("analysis_status", "not_run"),
        "scored": scored,
        "success_score": (1.0 if result.get("solved") else 0.0) if scored else None,
        "sacrifice": analysis.get("sacrifice"),
        "tactical": analysis.get("tactical"),
        "selected_move": task.sacrifice_uci,
        "best_move": (analysis.get("best_move") or {}).get("uci"),
        "mistakes": task.mistakes,
        "sacrifice_step": _first_sacrifice_index(task.puzzle),
    }


def _verified_solution_payload(puzzle: Puzzle) -> dict:
    if _verified_guided_line(puzzle) is None:
        raise BrilliantFinderError("This puzzle no longer has a consistent verified guided line.", 500)
    board = chess.Board(puzzle.fen)
    positions = [board.fen()]
    moves, ucis = [], []
    for san in puzzle.solution:
        move = board.parse_san(san)
        if move not in board.legal_moves:
            raise BrilliantFinderError("A stored verified solution move is no longer legal.", 500)
        moves.append(board.san(move))
        ucis.append(move.uci())
        board.push(move)
        positions.append(board.fen())
    return {"moves": moves, "ucis": ucis, "positions": positions}


def _played_line_payload(task: BrilliantTask) -> dict:
    return {
        "moves": [item["san"] for item in task.played],
        "ucis": [item["uci"] for item in task.played],
        "positions": [task.puzzle.fen, *[item["after_fen"] for item in task.played]],
    }


def _source_explanation(knowledge, puzzle: Puzzle) -> tuple[str, str]:
    try:
        example = knowledge.get(puzzle.id) if knowledge is not None else None
    except (AttributeError, TypeError):
        example = None
    title = str(getattr(example, "title", None) or puzzle.main_idea or "Verified tactical idea")
    text = str(getattr(example, "explanation", None) or "").strip()
    if not text:
        text = "The Finder library contains a verified, engine-profiled solution line for this position."
    return title, text


def _analyze_sacrifice(task: BrilliantTask, engine) -> dict:
    puzzle = task.puzzle
    index = _first_sacrifice_index(puzzle)
    if index is None or task.sacrifice_fen is None or task.sacrifice_uci is None:
        return _inconclusive("verified_sacrifice_step_was_not_played", task.sacrifice_san)
    expected_uci = puzzle.steps[index].get("uci")
    analysis = _evaluate_position(puzzle, task.sacrifice_fen, task.sacrifice_uci, engine,
                                  expected_uci=expected_uci)
    # A completed library line remains a valid guided-puzzle completion, but a contradictory
    # runtime check must not be presented as proof that the stored sacrifice/alternative is sound.
    if analysis.get("analysis_status") == "complete" and analysis.get("verdict") not in SUCCESS_VERDICTS:
        return _inconclusive("runtime_engine_conflicts_with_verified_solution", task.sacrifice_san)
    return analysis


def _result_explanation(task: BrilliantTask, knowledge, analysis: dict | None, verified_solution: dict) -> dict:
    puzzle = task.puzzle
    sacrifice_index = _first_sacrifice_index(puzzle)
    key_step = puzzle.steps[sacrifice_index] if sacrifice_index is not None else {}
    setup = [item for item in task.played if item.get("role") == "player"
             and item.get("step_index", -1) < (sacrifice_index or 0)]
    setup_line = [item["san"] for item in setup]
    title, source_text = _source_explanation(knowledge, puzzle)
    analysis = analysis or {}
    engine_analysis = analysis.get("analysis_status") == "complete"
    pv = (analysis.get("selected_line") or {}).get("moves") or []
    strongest_defense = pv[1] if len(pv) > 1 else None
    defense_source = "stockfish" if strongest_defense else None
    if not strongest_defense and key_step.get("reply_san"):
        strongest_defense = key_step["reply_san"]
        defense_source = "verified_solution"
    return {
        "main_idea": title,
        "source_explanation": source_text,
        "preparatory_moves": setup_line,
        "preparatory_line": " → ".join(setup_line),
        "sacrifice_step": sacrifice_index,
        "sacrifice_move": key_step.get("san"),
        "played_key_move": task.sacrifice_san,
        "played_key_move_is_mainline": bool(task.sacrifice_uci and task.sacrifice_uci == key_step.get("uci")),
        "strongest_defense": strongest_defense,
        "defense_source": defense_source,
        "verified_reply_after_sacrifice": key_step.get("reply_san"),
        "verified_continuation": verified_solution["moves"],
        "engine_analysis_status": analysis.get("analysis_status", "not_run"),
        "engine_analysis_reason": analysis.get("reason"),
        "engine_verdict": analysis.get("verdict") if engine_analysis else None,
        "engine_label": analysis.get("label") if engine_analysis else None,
        "summary": (
            f"The stored line has {sacrifice_index or 0} preparatory player move(s) before its "
            f"key sacrifice, {key_step.get('san') or 'as shown in the verified line'}. "
            f"The source explanation describes the idea; the complete verified continuation is below."
        ),
    }


def _record_guided_result(task: BrilliantTask, result: dict, *, profile_store, usage,
                          solved: bool, revealed: bool) -> dict:
    if task.result_recorded:
        return result
    first_try = bool(solved and not task.mistakes and not revealed)
    scored = True  # the stored line, not optional runtime wording, establishes puzzle completion
    metadata = _finder_context(task, result, scored=scored)
    try:
        usage.record_resolved(task.puzzle.id, solved, first_try, hints=0,
                              revealed=revealed, concept=task.puzzle.concept)
        result["usage_recorded"] = True
    except (OSError, AttributeError):
        log.exception("Brilliant Finder result could not be written to shared puzzle usage")
        result["usage_recorded"] = False
    try:
        changed = profile_store.update("local", lambda profile: profile.record_attempt(
            task.puzzle.concept,
            task.puzzle.rating,
            solved=solved,
            first_try=first_try,
            hints=0,
            revealed=revealed,
            learner_moves=max(1, task.puzzle.meaningful_moves),
            context={"source": "brilliant_finder", "exercise_id": task.puzzle.id,
                     "brilliant_finder": metadata},
        ))
        result["learning"] = changed
        result["recorded"] = True
    except Exception:
        log.exception("Brilliant Finder learner result could not be saved")
        result["recorded"] = False
    result["success"] = bool(solved and not revealed)
    result["solved"] = bool(solved and not revealed)
    result["first_try"] = first_try
    result["difficulty"] = {
        "preference": task.preference,
        "label": LEVEL_LABELS[task.preference],
        "complexity_band": task.complexity_band,
    }
    task.result_recorded = True
    return result


def _build_completion_result(task: BrilliantTask, knowledge, engine, *, outcome: str,
                             revealed: bool = False, solved: bool = False) -> dict:
    verified = _verified_solution_payload(task.puzzle)
    analysis = None if revealed else _analyze_sacrifice(task, engine)
    explanation = _result_explanation(task, knowledge, analysis, verified)
    played_line = _played_line_payload(task)
    analysis = analysis or {"analysis_status": "not_run", "verdict": "inconclusive", "label": ""}
    label = "Verified solution revealed" if revealed else (
        "Verified alternative found" if outcome == "verified_alternative" else
        "Combination completed" if solved else "Combination completed after a retry")
    return {
        "outcome": outcome,
        "completed": not revealed,
        "revealed": revealed,
        "success": bool(solved and not revealed),
        "solved": bool(solved and not revealed),
        "mistakes": task.mistakes,
        "label": label,
        "analysis_status": analysis.get("analysis_status", "not_run"),
        "verdict": analysis.get("verdict", "inconclusive"),
        "score": analysis.get("score"),
        "depth": analysis.get("depth"),
        "best_move": analysis.get("best_move"),
        "stronger_alternative": analysis.get("stronger_alternative"),
        "selected_uci": task.sacrifice_uci,
        "selected_move": task.sacrifice_san,
        "selected_line": played_line,
        "best_line": analysis.get("best_line") or {"moves": [], "ucis": [], "positions": []},
        "analysis": analysis,
        "explanation": explanation,
        "played_line": played_line,
        "verified_solution": verified,
        "recorded": False,
    }


def _move_payload(task: BrilliantTask, *, outcome: str, message: str, step_index: int,
                  player_move: dict | None = None, opponent_move: dict | None = None,
                  done: bool = False, result: dict | None = None) -> dict:
    return {
        "outcome": outcome,
        "message": message,
        "step_index": step_index,
        "next_step_index": task.step_index,
        "current_fen": task.current_fen,
        "player_move": player_move,
        "opponent_move": opponent_move,
        "done": done,
        "result": result,
    }


def process_task_move(task_id: str, step_index: int, move_uci: str, *, before_fen: str | None = None,
                      knowledge, profile_store, usage, engine,
                      store: BrilliantTaskStore | None = None) -> dict:
    task_store = store or get_brilliant_task_store()
    task, cached = task_store.claim_move(task_id, step_index, move_uci, before_fen)
    if cached is not None:
        return cached
    key = (step_index, move_uci.strip().lower())
    snapshot = (task.step_index, task.current_fen, copy.deepcopy(task.played), task.mistakes,
                task.sacrifice_fen, task.sacrifice_uci, task.sacrifice_san)
    try:
        # Validate while holding this turn's reservation. Illegal input releases the reservation
        # without changing the board, and duplicates are returned from the cache before this point.
        board, move, san = validate_position_move(task.current_fen, move_uci)
        if step_index >= len(task.puzzle.steps):
            raise BrilliantFinderError("The verified line has ended. Request another position.", 409)
        step = task.puzzle.steps[step_index]
        sacrifice_index = _first_sacrifice_index(task.puzzle)
        canonical_uci = str(step.get("uci") or "").lower()
        accepted = {str(value).lower() for value in step.get("accepted") or [] if isinstance(value, str)}
        good = {str(value).lower() for value in step.get("good") or [] if isinstance(value, str)}
        is_expected = move.uci().lower() == canonical_uci
        is_accepted = move.uci().lower() in accepted
        is_good = move.uci().lower() in good
        after_player = board.copy()
        after_player.push(move)
        player_payload = {"uci": move.uci(), "san": san, "after_fen": after_player.fen()}

        # An alternate is accepted as a solution only when this is the final player turn, so no
        # unverified opponent branch or continuation needs to be invented. Earlier alternatives
        # return to the same turn without being called wrong or triggering an opponent move.
        if not is_expected and (is_accepted or is_good):
            is_terminal_alternative = (
                step_index == len(task.puzzle.steps) - 1
                and not step.get("reply_uci")
                and (is_accepted or (is_good and step.get("kind") == "open"
                                    and not any(s.get("kind") == "critical"
                                                for s in task.puzzle.steps[step_index + 1:])))
            )
            if not is_terminal_alternative:
                outcome = "unverified_alternative" if is_accepted else "playable"
                message = ("That move is engine-approved here, but the library has no verified continuation "
                           "from it. Try the stored setup move." if is_accepted else
                           "Playable, but the verified line has a stronger move. Try again.")
                response = _move_payload(task, outcome=outcome, message=message, step_index=step_index)
                task_store.finish_move(task, key, response)
                return response

            before = board.fen()
            task.played.append({"role": "player", "step_index": step_index, "uci": move.uci(),
                                "san": san, "before_fen": before, "after_fen": after_player.fen()})
            if step_index == sacrifice_index:
                task.sacrifice_fen = before
                task.sacrifice_uci = move.uci()
                task.sacrifice_san = san
            task.current_fen = after_player.fen()
            task.step_index = step_index + 1
            result = _build_completion_result(task, knowledge, engine, outcome="verified_alternative", solved=True)
            result = _record_guided_result(task, result, profile_store=profile_store, usage=usage,
                                           solved=True, revealed=False)
            response = _move_payload(task, outcome="complete", message="Verified alternative accepted.",
                                     step_index=step_index, player_move=player_payload, done=True, result=result)
            task_store.finish_move(task, key, response, done=True)
            return response

        if not is_expected:
            task.mistakes += 1
            response = _move_payload(task, outcome="incorrect", message="That misses the verified line. Try again.",
                                     step_index=step_index)
            task_store.finish_move(task, key, response)
            return response

        before = board.fen()
        task.played.append({"role": "player", "step_index": step_index, "uci": move.uci(),
                            "san": san, "before_fen": before, "after_fen": after_player.fen()})
        if step_index == sacrifice_index:
            task.sacrifice_fen = before
            task.sacrifice_uci = move.uci()
            task.sacrifice_san = san
        board.push(move)
        opponent_payload = None
        line = _verified_guided_line(task.puzzle)
        if line is None:
            raise BrilliantFinderError("The stored verified line failed its consistency check.", 500)
        reply_uci = line[step_index]["reply_uci"]
        if reply_uci:
            try:
                reply = chess.Move.from_uci(reply_uci)
            except ValueError as exc:
                raise BrilliantFinderError("The stored opponent reply is malformed.", 500) from exc
            if reply not in board.legal_moves:
                raise BrilliantFinderError("The stored opponent reply is illegal in the current line.", 500)
            reply_san = board.san(reply)
            reply_before = board.fen()
            board.push(reply)
            opponent_payload = {"uci": reply.uci(), "san": reply_san, "after_fen": board.fen()}
            task.played.append({"role": "opponent", "step_index": step_index, "uci": reply.uci(),
                                "san": reply_san, "before_fen": reply_before, "after_fen": board.fen()})
        task.current_fen = board.fen()
        task.step_index = step_index + 1
        is_last = step_index == len(task.puzzle.steps) - 1
        if is_last:
            if task.sacrifice_fen is None:
                raise BrilliantFinderError("The verified line ended before its marked sacrifice was played.", 500)
            solved = task.mistakes == 0
            result = _build_completion_result(task, knowledge, engine, outcome="guided_solution", solved=solved)
            result = _record_guided_result(task, result, profile_store=profile_store, usage=usage,
                                           solved=solved, revealed=False)
            response = _move_payload(task, outcome="complete", message="Verified line complete.",
                                     step_index=step_index, player_move=player_payload,
                                     opponent_move=opponent_payload, done=True, result=result)
            task_store.finish_move(task, key, response, done=True)
        else:
            response = _move_payload(task, outcome="correct", message="Correct. The verified reply is played.",
                                     step_index=step_index, player_move=player_payload,
                                     opponent_move=opponent_payload)
            task_store.finish_move(task, key, response)
        return response
    except Exception:
        (task.step_index, task.current_fen, task.played, task.mistakes,
         task.sacrifice_fen, task.sacrifice_uci, task.sacrifice_san) = snapshot
        task_store.release_move(task, key)
        raise


def reveal_task(task_id: str, *, knowledge, profile_store, usage,
                 store: BrilliantTaskStore | None = None) -> dict:
    task_store = store or get_brilliant_task_store()
    task, is_new = task_store.claim_reveal(task_id)
    if not is_new:
        return {"done": True, "replayed": True, "current_fen": task.current_fen,
                "result": copy.deepcopy(task.result or {})}
    key = task.inflight_key
    try:
        result = _build_completion_result(task, knowledge, None, outcome="revealed", revealed=True)
        result = _record_guided_result(task, result, profile_store=profile_store, usage=usage,
                                       solved=False, revealed=True)
        task_store.finish_reveal(task, result)
        return {"done": True, "replayed": False, "current_fen": task.current_fen, "result": result}
    except Exception:
        if key is not None:
            task_store.release_move(task, key)
        raise


def retry_task(task_id: str, *, store: BrilliantTaskStore | None = None) -> BrilliantTask:
    task_store = store or get_brilliant_task_store()
    task = task_store.get(task_id)
    if task.status != "complete":
        raise BrilliantFinderError("Finish or reveal this line before retrying the same position.", 409)
    return task_store.issue(task.puzzle, task.preference, task.complexity_band, task.calibration)


def profile_for_current_learner(knowledge=None, profile=None, usage=None) -> dict:
    from ..knowledge.library import get_knowledge
    from ..knowledge.usage import get_usage
    from ..learner import get_profile

    knowledge = knowledge if knowledge is not None else get_knowledge()
    profile = profile if profile is not None else get_profile()
    usage = usage if usage is not None else get_usage()
    return profile_summary(knowledge, profile, usage)
