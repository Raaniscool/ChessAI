"""What every game importer provides (Chess.com today; other sites = another importer)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..model import GameRecord
from ..pgn import ParseIssue


class PlayerNeeded(ValueError):
    """The learner's side can't be determined: ask which of these players they are."""

    def __init__(self, players: list[str]):
        super().__init__("Which player are you? Enter your username.")
        self.players = players


@dataclass
class ImportResult:
    games: list[GameRecord] = field(default_factory=list)
    errors: list[ParseIssue] = field(default_factory=list)  # games that were rejected, and why


class GameImporter(Protocol):
    source: str   # id stored on every GameRecord
    label: str    # shown to the learner

    def parse(self, text: str, username: str | None = None) -> ImportResult: ...
