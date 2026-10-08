"""Remembered clarification answers.

Answers are stored by *ambiguity key*, not by sentence: "knight and bishop endgames",
"endgames with a knight and a bishop" and "bishop and knight endings" all raise the
ambiguity `coordination:B+N:endgame`, so one answer covers every way of asking. The
learner can always ask to be asked again (`reclarify`), which forgets the answer.

Stored under DATA_DIR/intents.json. It holds only what the learner chose — never game
data — so it is small and private to this installation.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from ...config import get_settings


class IntentMemory:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path(get_settings().data_dir) / "intents.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._load().get(key)

    def put(self, key: str, choice: str, label: str = "", text: str | None = None, term: str = "") -> dict:
        entry = {"choice": choice, "label": label, "term": term,
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if text:
            entry["text"] = text
        with self._lock:
            data = self._load()
            data[key] = entry
            self._save(data)
        return entry

    def forget(self, key: str) -> bool:
        with self._lock:
            data = self._load()
            if key not in data:
                return False
            del data[key]
            self._save(data)
            return True

    def all(self) -> dict:
        with self._lock:
            return self._load()
