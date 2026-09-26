"""Importer registry. The analysis engine never imports from here — only the API does."""
from __future__ import annotations

from .base import GameImporter, ImportResult, PlayerNeeded
from .chesscom import ChessComImporter

IMPORTERS: dict[str, GameImporter] = {"chesscom": ChessComImporter()}


def get_importer(source: str = "chesscom") -> GameImporter:
    try:
        return IMPORTERS[source]
    except KeyError:
        raise ValueError(f"unsupported game source {source!r} (supported: {', '.join(IMPORTERS)})") from None


__all__ = ["GameImporter", "ImportResult", "PlayerNeeded", "IMPORTERS", "get_importer"]
