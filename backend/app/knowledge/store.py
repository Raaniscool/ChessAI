"""Where library entries live.

Three tiers, kept separate on purpose:

    global     knowledge/data/examples/<category>/*.json   in the repo: the curated,
               source-backed seed library. Every entry is `verified` (or `deprecated`).
    generated  DATA_DIR/knowledge/generated/<id>.json     candidates proposed at runtime
               (by Qwen or submitted), with whatever status the pipeline gave them.
    personal   DATA_DIR/knowledge/personal/<id>.json      examples made from the
               learner's own games; used for that learner only.

Runtime tiers store one JSON file per entry, so a status change rewrites one
small file and the directories scale to tens of thousands of entries. Every
status change is appended to the entry's `review_log`, which is what a future
review dashboard will read and write (see `set_status`).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

from .schema import STATUSES, KnowledgeError

RUNTIME_TIERS = ("generated", "personal")

# Allowed manual transitions (automated verification may set any status).
TRANSITIONS = {
    "candidate": {"verifying", "rejected"},
    "verifying": {"verified", "rejected", "needs_review"},
    "needs_review": {"verified", "rejected"},
    "verified": {"deprecated", "needs_review"},
    "rejected": {"candidate"},
    "deprecated": {"verified"},
}


def knowledge_dir() -> Path:
    from ..config import get_settings
    return get_settings().data_dir / "knowledge"


class RuntimeStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else knowledge_dir()

    def _dir(self, tier: str) -> Path:
        if tier not in RUNTIME_TIERS:
            raise KnowledgeError(f"unknown tier {tier!r}")
        return self.root / tier

    def path(self, tier: str, entry_id: str) -> Path:
        return self._dir(tier) / f"{entry_id}.json"

    def save(self, tier: str, record: dict) -> Path:
        directory = self._dir(tier)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record['id']}.json"
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)  # atomic: a crash never leaves half an entry
        return path

    def load(self, tier: str, entry_id: str) -> dict | None:
        path = self.path(tier, entry_id)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def all(self, tier: str) -> list[dict]:
        directory = self._dir(tier)
        if not directory.exists():
            return []
        out = []
        for path in sorted(directory.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                continue  # a corrupt file is skipped, never served
        return out

    def exists(self, entry_id: str) -> bool:
        return any(self.path(t, entry_id).exists() for t in RUNTIME_TIERS)

    def set_status(self, tier: str, entry_id: str, status: str, note: str = "",
                   actor: str = "reviewer", force: bool = False) -> dict:
        record = self.load(tier, entry_id)
        if record is None:
            raise KeyError(entry_id)
        if status not in STATUSES:
            raise KnowledgeError(f"unknown status {status!r}")
        current = record.get("status", "candidate")
        if not force and status not in TRANSITIONS.get(current, set()):
            raise KnowledgeError(f"cannot move an entry from {current} to {status}")
        record["status"] = status
        record.setdefault("review_log", []).append({
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "from": current, "to": status, "by": actor, "note": note})
        self.save(tier, record)
        return record
