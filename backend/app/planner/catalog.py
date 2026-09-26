"""Curated topic catalog: the verified building blocks of learning plans.

Topics are data (planner/data/topics.json). Every opening line and every
exercise position in the catalog is validated with python-chess on load and
checked with Stockfish by the test suite (tests/test_catalog_engine.py), so a
plan built from catalog topics never teaches an illegal or unsound move.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import chess

from .. import chess_system
from ..chess_system import ChessError

CATALOG_FILE = Path(__file__).resolve().parent / "data" / "topics.json"
PUZZLE_FILE = Path(__file__).resolve().parent / "data" / "puzzles.json"
MIN_PUZZLES = 3
CATEGORIES = ("opening", "tactic", "endgame", "strategy")


class CatalogError(ValueError):
    pass


@dataclass
class Topic:
    id: str
    title: str
    category: str
    level: str
    summary: str
    ideas: list[str]
    aliases: list[str]
    prerequisites: list[str] = field(default_factory=list)
    side: str | None = None  # openings: the side the learner plays
    line: list[str] = field(default_factory=list)  # openings: SAN from the start
    positions: list[dict] = field(default_factory=list)  # exercise dicts
    puzzle_theme: str | None = None  # Lichess theme tag of the verified puzzle set
    puzzle_text: dict = field(default_factory=dict)  # {"task", "hint", "done"}
    puzzles: list[dict] = field(default_factory=list)  # records from puzzles.json

    def as_summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "level": self.level,
            "side": self.side,
            "summary": self.summary,
        }


def validate_line(san_moves: list[str], start_fen: str = chess.STARTING_FEN) -> list[str]:
    """Replay SAN moves; return them normalized. Raises ChessError on the first illegal move."""
    board = chess_system.parse_fen(start_fen)
    out = []
    for i, san in enumerate(san_moves):
        try:
            move = board.parse_san(san)
        except ValueError as exc:
            raise ChessError(f"move {i + 1} ({san!r}) is illegal") from exc
        out.append(board.san(move))
        board.push(move)
    return out


def _normalize(text: str) -> list[str]:
    text = text.lower().replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", text)


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:  # short tokens (e.g. "kid", "bc4") must match exactly
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.84


def _phrase_span(phrase: list[str], goal: list[str]) -> frozenset[int] | None:
    """Goal token positions matched by the alias phrase (fuzzy, in order), or None."""
    if not phrase:
        return None
    pos = 0
    hits = []
    for token in phrase:
        while pos < len(goal) and not _token_match(token, goal[pos]):
            pos += 1
        if pos >= len(goal):
            return None
        hits.append(pos)
        pos += 1
    return frozenset(hits)


def _phrase_in(phrase: list[str], goal: list[str]) -> bool:
    """Every token of the alias phrase fuzzily appears, in order, in the goal."""
    return _phrase_span(phrase, goal) is not None


class Catalog:
    def __init__(self, path: Path | None = None, puzzle_path: Path | None = None):
        self.path = Path(path) if path else CATALOG_FILE
        self.puzzle_path = Path(puzzle_path) if puzzle_path else PUZZLE_FILE
        self.puzzle_meta: dict = {}
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.categories: dict[str, dict] = raw.get("categories", {})
        self.general: dict = raw.get("general_curriculum", {"aliases": [], "topics": []})
        self.topics: dict[str, Topic] = {}
        for entry in raw.get("topics", []):
            topic = self._parse_topic(entry)
            if topic.id in self.topics:
                raise CatalogError(f"duplicate topic id {topic.id}")
            self.topics[topic.id] = topic
        self._attach_puzzles()
        for topic in self.topics.values():
            for pre in topic.prerequisites:
                if pre not in self.topics:
                    raise CatalogError(f"{topic.id}: unknown prerequisite {pre}")
        for tid in self.general.get("topics", []):
            if tid not in self.topics:
                raise CatalogError(f"general curriculum: unknown topic {tid}")

    @staticmethod
    def _parse_topic(entry: dict) -> Topic:
        for key in ("id", "title", "category", "summary"):
            if not entry.get(key):
                raise CatalogError(f"topic missing {key}: {entry.get('id')}")
        if entry["category"] not in CATEGORIES:
            raise CatalogError(f"{entry['id']}: bad category {entry['category']}")
        line = entry.get("line", "")
        line_moves = line.split() if isinstance(line, str) else list(line)
        if entry["category"] == "opening":
            if entry.get("side") not in ("white", "black"):
                raise CatalogError(f"{entry['id']}: openings need side white|black")
            if len(line_moves) < 6:
                raise CatalogError(f"{entry['id']}: opening line too short")
            try:
                line_moves = validate_line(line_moves)
            except ChessError as exc:
                raise CatalogError(f"{entry['id']}: {exc}") from exc
        elif not entry.get("positions") and not entry.get("puzzle_theme"):
            raise CatalogError(f"{entry['id']}: non-opening topics need positions or a puzzle_theme")
        return Topic(
            id=entry["id"],
            title=entry["title"],
            category=entry["category"],
            level=entry.get("level", "beginner"),
            summary=entry["summary"],
            ideas=list(entry.get("ideas", [])),
            aliases=list(entry.get("aliases", [])) + [entry["title"]],
            prerequisites=list(entry.get("prerequisites", [])),
            side=entry.get("side"),
            line=line_moves,
            positions=list(entry.get("positions", [])),
            puzzle_theme=entry.get("puzzle_theme"),
            puzzle_text=dict(entry.get("puzzle", {})),
        )

    def _attach_puzzles(self) -> None:
        """Give each puzzle_theme topic its verified puzzles (see scripts/build_puzzle_library.py)."""
        themes: dict = {}
        if self.puzzle_path.exists():
            with open(self.puzzle_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            themes = raw.get("themes", {})
            self.puzzle_meta = {k: v for k, v in raw.items() if k != "themes"}
        for topic in self.topics.values():
            if not topic.puzzle_theme:
                continue
            topic.puzzles = list(themes.get(topic.puzzle_theme, []))
            if len(topic.puzzles) < MIN_PUZZLES and not topic.positions:
                raise CatalogError(
                    f"{topic.id}: only {len(topic.puzzles)} puzzles for theme {topic.puzzle_theme} "
                    f"in {self.puzzle_path.name} (need {MIN_PUZZLES}); rebuild the puzzle library")

    def get(self, topic_id: str) -> Topic | None:
        return self.topics.get(topic_id)

    def by_category(self, category: str) -> list[Topic]:
        return [t for t in self.topics.values() if t.category == category]

    def search(self, goal: str) -> list[Topic]:
        """Topics the goal asks for, most specific first.

        Order of precedence: explicit topic aliases → whole categories
        ("tactics", "endgames") → the general beginner curriculum ("chess").
        """
        tokens = _normalize(goal)
        if not tokens:
            return []

        scored: list[tuple[int, int, Topic, frozenset[int]]] = []
        for order, topic in enumerate(self.topics.values()):
            best, best_span = 0, frozenset()
            for alias in topic.aliases:
                phrase = _normalize(alias)
                span = _phrase_span(phrase, tokens)
                if span is not None and len(" ".join(phrase)) > best:
                    best, best_span = len(" ".join(phrase)), span
            if best:
                scored.append((best, order, topic, best_span))
        # A match that is only part of a longer match ("checkmate" inside "smothered
        # checkmate", "attack" inside "discovered attack") isn't what was asked for.
        spans = [s[3] for s in scored]
        scored = [s for s in scored if not any(s[3] < other for other in spans)]
        if scored:
            scored.sort(key=lambda s: (-s[0], s[1]))
            found = [s[2] for s in scored]
            wants_black = "black" in tokens
            wants_white = "white" in tokens
            if wants_black != wants_white:
                side = "black" if wants_black else "white"
                preferred = [t for t in found if t.category != "opening" or t.side == side]
                found = preferred or found
            return found

        for category, meta in self.categories.items():
            for alias in meta.get("aliases", []):
                if _phrase_in(_normalize(alias), tokens):
                    topics = self.by_category(category)
                    if category == "opening":  # don't dump 15 openings: pick beginner ones
                        topics = [t for t in topics if t.level == "beginner"][:4]
                    return topics

        for alias in self.general.get("aliases", []):
            if _phrase_in(_normalize(alias), tokens):
                return [self.topics[t] for t in self.general["topics"]]
        return []

    def with_prerequisites(self, topics: list[Topic], limit: int = 8) -> list[tuple[Topic, str | None]]:
        """Order topics so prerequisites come first. Returns (topic, required_by)."""
        ordered: list[tuple[Topic, str | None]] = []
        seen: set[str] = set()

        def visit(topic: Topic, required_by: str | None, depth: int = 0) -> None:
            if topic.id in seen or depth > 5:
                return
            for pre in topic.prerequisites:
                visit(self.topics[pre], topic.title, depth + 1)
            if topic.id not in seen:
                seen.add(topic.id)
                ordered.append((topic, required_by))

        for t in topics:
            visit(t, None)
        return ordered[:limit]


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog
