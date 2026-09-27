"""What has been generated and shown to this learner: DATA_DIR/knowledge/generation_log.json.

Used to vary what comes next (a motif variant used recently is picked less often, a
position already shown is not shown again) and as an audit trail of every candidate
the generator tried: accepted, sent to review, or rejected (with the reason).
Private, local learner data like the rest of DATA_DIR.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 2000   # oldest entries are dropped beyond this
RECENT = 12          # how many recent generations count for variety


class GenerationLog:
    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from ...config import get_settings  # resolved each time: DATA_DIR can change (tests)
        return get_settings().data_dir / "knowledge" / "generation_log.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                data.setdefault("shown", {})
                return data
        except (OSError, ValueError):
            pass
        return {"entries": [], "shown": {}}

    def _save(self, data: dict) -> None:
        data["entries"] = data["entries"][-MAX_ENTRIES:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, entry: dict) -> None:
        with self._lock:
            data = self._load()
            data["entries"].append({**entry, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            self._save(data)

    def entries(self, concept: str | None = None, outcome: str | None = None) -> list[dict]:
        rows = self._load()["entries"]
        return [e for e in rows if (concept is None or e.get("concept") == concept)
                and (outcome is None or e.get("outcome") == outcome)]

    def recent_variants(self, concept: str) -> Counter:
        """How often each motif variant was *accepted* recently for this concept."""
        rows = [e for e in self.entries(concept, "verified")][-RECENT:]
        return Counter(e.get("signature") for e in rows)

    def mark_shown(self, example_ids: list[str]) -> None:
        with self._lock:
            data = self._load()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for eid in example_ids:
                data["shown"][eid] = now
            self._save(data)

    def shown(self) -> dict[str, str]:
        return dict(self._load()["shown"])


_log: GenerationLog | None = None


def get_log() -> GenerationLog:
    global _log
    if _log is None:
        _log = GenerationLog()
    return _log


def set_log(log: GenerationLog | None) -> None:
    global _log
    _log = log
