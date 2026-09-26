"""Private storage for imported games and their analyses: DATA_DIR/games/<id>.json.

Learner data stays here (DATA_DIR is gitignored and local). Nothing in this
module — or anything that reads it — writes to the Knowledge Library: user-game
positions are personal training data, never globally trusted examples.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from .model import GameRecord

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1
_ID = re.compile(r"^[a-z0-9_-]{3,80}$")
_lock = threading.Lock()


class GameNotFound(KeyError):
    pass


def games_dir() -> Path:
    from ..config import get_settings
    return get_settings().data_dir / "games"


def _path(game_id: str, directory: Path | None = None) -> Path:
    if not _ID.match(game_id or ""):
        raise GameNotFound(game_id)
    return (directory or games_dir()) / f"{game_id}.json"


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_doc(game_id: str, directory: Path | None = None) -> dict:
    path = _path(game_id, directory)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GameNotFound(game_id) from None


def save_game(game: GameRecord, directory: Path | None = None) -> bool:
    """Store a game. Re-importing the same game keeps its analysis if the moves are unchanged.

    Returns True when the game is new."""
    path = _path(game.id, directory)
    with _lock:
        analysis = None
        new = not path.exists()
        if not new:
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                same_side = old["game"].get("player_color") == game.player_color
                if old["game"].get("moves_uci") == game.moves_uci and same_side:
                    analysis = old.get("analysis")
            except (OSError, ValueError, KeyError):
                pass
        _write(path, {"schema_version": SCHEMA_VERSION, "game": game.to_dict(), "analysis": analysis})
    return new


def load_game(game_id: str, directory: Path | None = None) -> GameRecord:
    return GameRecord.from_dict(load_doc(game_id, directory)["game"])


def load_analysis(game_id: str, directory: Path | None = None) -> dict | None:
    return load_doc(game_id, directory).get("analysis")


def save_analysis(game_id: str, analysis: dict, directory: Path | None = None) -> None:
    path = _path(game_id, directory)
    with _lock:
        doc = load_doc(game_id, directory)
        doc["analysis"] = analysis
        _write(path, doc)


def delete_game(game_id: str, directory: Path | None = None) -> None:
    path = _path(game_id, directory)
    if not path.exists():
        raise GameNotFound(game_id)
    path.unlink()


def list_docs(directory: Path | None = None) -> list[dict]:
    """All stored games (newest game date first). Unreadable files are skipped, not fatal."""
    directory = directory or games_dir()
    if not directory.is_dir():
        return []
    docs = []
    for path in directory.glob("*.json"):
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("Skipping unreadable game file %s: %s", path.name, exc)
    docs.sort(key=lambda d: (d["game"].get("date") or "", d["game"].get("imported_at") or ""), reverse=True)
    return docs
