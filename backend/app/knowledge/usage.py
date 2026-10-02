"""Usage statistics per example (DATA_DIR/knowledge/usage.json).

    times_used, times_completed, attempts, successes, hints_used,
    difficulty_feedback {too_easy, just_right, too_hard}, last_used, concept,
    puzzle {resolved, solved, first_try, hints, seconds_total, timed, last_result, last_resolved}

Used for personalisation ("not seen recently") and analytics. A low success
rate is *information*, not a verdict: a hard example may be hard on purpose,
so nothing here ever changes an example's status.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from pathlib import Path

FEEDBACK = ("too_easy", "just_right", "too_hard")
MAX_PUZZLE_SECONDS = 600  # an idle tab must not distort average solve times


class UsageTracker:
    def __init__(self, path: Path | None = None):
        if path is None:
            from .store import knowledge_dir
            path = knowledge_dir() / "usage.json"
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=1)
        os.replace(tmp, self.path)

    def _entry(self, example_id: str, concept: str = "") -> dict:
        data = self._load()
        entry = data.setdefault(example_id, {
            "times_used": 0, "times_completed": 0, "attempts": 0, "successes": 0, "hints_used": 0,
            "difficulty_feedback": {k: 0 for k in FEEDBACK}, "last_used": None, "concept": concept})
        if concept:
            entry["concept"] = concept
        return entry

    def record_used(self, examples) -> None:
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            for ex in examples:
                e = self._entry(ex.id, ex.concept)
                e["times_used"] += 1
                e["last_used"] = now
            self._save()

    def record_attempt(self, example_id: str, success: bool, hints: int = 0) -> None:
        with self._lock:
            e = self._entry(example_id)
            e["attempts"] += 1
            e["successes"] += int(bool(success))
            e["hints_used"] += max(0, hints)
            self._save()

    def record_completed(self, example_id: str) -> None:
        with self._lock:
            self._entry(example_id)["times_completed"] += 1
            self._save()

    def record_feedback(self, example_id: str, feedback: str) -> None:
        if feedback not in FEEDBACK:
            raise ValueError(f"feedback must be one of {FEEDBACK}")
        with self._lock:
            self._entry(example_id)["difficulty_feedback"][feedback] += 1
            self._save()

    def record_resolved(self, example_id: str, solved: bool, first_try: bool, hints: int = 0,
                        seconds: float | None = None, revealed: bool = False, concept: str = "") -> None:
        """One finished puzzle (all its moves): the per-puzzle outcome used by puzzle selection.

        `attempts`/`successes` above count single moves; this counts whole puzzles.
        """
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        result = "solved_first_try" if solved and first_try else "solved" if solved else "revealed" if revealed else "failed"
        with self._lock:
            e = self._entry(example_id, concept)
            p = e.setdefault("puzzle", {"resolved": 0, "solved": 0, "first_try": 0, "hints": 0,
                                        "seconds_total": 0.0, "timed": 0, "last_result": None, "last_resolved": None})
            p["resolved"] += 1
            p["solved"] += int(bool(solved))
            p["first_try"] += int(bool(solved and first_try))
            p["hints"] += max(0, int(hints))
            if seconds is not None and seconds >= 0:
                p["seconds_total"] = round(p["seconds_total"] + min(float(seconds), MAX_PUZZLE_SECONDS), 1)
                p["timed"] += 1
            p["last_result"] = result
            p["last_resolved"] = now
            self._save()

    def puzzle_stats(self, example_id: str) -> dict:
        """attempts (whole puzzles), success rate, average seconds, hints, seen-before, last result."""
        with self._lock:
            e = dict(self._load().get(example_id) or {})
        p = e.get("puzzle") or {}
        n = p.get("resolved", 0)
        return {
            "seen": bool(e.get("times_used") or n),
            "attempts": n,
            "success_rate": round(p["solved"] / n, 3) if n else None,
            "first_try_rate": round(p["first_try"] / n, 3) if n else None,
            "average_seconds": round(p["seconds_total"] / p["timed"], 1) if p.get("timed") else None,
            "hints_used": p.get("hints", 0),
            "last_result": p.get("last_result"),
            "last_resolved": p.get("last_resolved"),
            "last_used": e.get("last_used"),
        }

    def all_puzzle_stats(self) -> dict[str, dict]:
        with self._lock:
            ids = list(self._load())
        return {i: self.puzzle_stats(i) for i in ids}

    def stats(self, example_id: str) -> dict:
        with self._lock:
            e = dict(self._load().get(example_id) or {})
        if e:
            e["average_success_rate"] = round(e["successes"] / e["attempts"], 3) if e["attempts"] else None
        return e

    def seen_counts(self) -> dict[str, int]:
        with self._lock:
            return {k: v.get("times_used", 0) for k, v in self._load().items()}

    def last_used(self) -> dict[str, str]:
        with self._lock:
            return {k: v["last_used"] for k, v in self._load().items() if v.get("last_used")}


_usage: UsageTracker | None = None
_usage_lock = threading.Lock()


def get_usage() -> UsageTracker:
    """The learner's usage tracker (DATA_DIR/knowledge/usage.json)."""
    global _usage
    with _usage_lock:
        if _usage is None:
            _usage = UsageTracker()
        return _usage


def set_usage(tracker: UsageTracker | None) -> None:
    global _usage
    with _usage_lock:
        _usage = tracker
