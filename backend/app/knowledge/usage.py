"""Usage statistics per example (DATA_DIR/knowledge/usage.json).

    times_used, times_completed, attempts, successes, hints_used,
    difficulty_feedback {too_easy, just_right, too_hard}, last_used, concept

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
