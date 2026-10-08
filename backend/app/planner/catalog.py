"""Curated topic catalog: the verified building blocks of learning plans.

Topics are data (planner/data/topics.json). Every opening line and every
exercise position in the catalog is validated with python-chess on load and
checked with Stockfish by the test suite (tests/test_catalog_engine.py), so a
plan built from catalog topics never teaches an illegal or unsound move.
"""
from __future__ import annotations

import json
import re
import unicodedata
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
    # accents folded first: "Grünfeld" is "grunfeld", not "gr" + "nfeld"
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode() if not text.isascii() else text
    text = text.lower().replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", text)


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    # plurals: "relative pins" is "relative pin" (short words like "pin" never reach the fuzzy test)
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and long_ in (short + "s", short + "es"):
        return True
    if len(a) < 4 or len(b) < 4:  # short tokens (e.g. "kid", "bc4") must match exactly
        return False
    # Typos keep the first letter; without this "position" matched "opposition" (ratio 0.89)
    # and "the Philidor position" became an opposition lesson.
    if a[0] != b[0]:
        return False
    # Everyday chess words are never typos of a subject: "position" is not "positional"
    # (strategy), "moves" is not "mover".
    if a in _EXACT_ONLY or b in _EXACT_ONLY:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.84


_EXACT_ONLY = frozenset("""position positions positional move moves piece pieces player players playing
game games square squares""".split())


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


# Words that carry no subject: "I want to get better at chess" asks for the general curriculum,
# "how to play the Stonewall" does not (it asks for something specific we may not have).
_FILLER = set("""
i im id we me my you your can could would should will please help teach show tell explain want wanna like need
to learn learning study studying practice practise improve improving master get getting become be better good
great strong stronger more most much a an the of at in on for with about how what where why when and or so
really just some any all lot lots quickly fast faster overall general generally whole entire start started
beginning beginner beginners novice player players playing play games game chess basics basic fundamentals
fundamental everything win winning more stop losing lose rating elo online again first new
""".split())


_QUALIFIER_OK = _FILLER | set("""king queen rook bishop knight pawn piece pieces kings queens rooks bishops knights
pawns white black simple easy basic common typical classic famous do does did is are was were be been it its
this that these those there their them they work works use using spot spotting find finding avoid avoiding
defend defending against vs versus from into by""".split())


def _qualifier_before(tokens: list[str], span: frozenset[int], covered: frozenset[int]) -> bool:
    """An unknown word directly in front of the matched phrase ("greek gift | sacrifice")."""
    before = min(span) - 1
    return before >= 0 and before not in covered and tokens[before] not in _QUALIFIER_OK \
        and not tokens[before].isdigit()


def _only_filler(tokens: list[str], alias: list[str]) -> bool:
    return all(t in _FILLER or t in alias for t in tokens)


# "as black against e4", "what do I play vs 1.d4", "how to answer 1.e4 as black", "as white against e5"
_SAN = r"(?:[nbrqk][a-h]?[1-8]?x?[a-h][1-8]|[a-h][1-8]|o-o(?:-o)?)"
_AGAINST = re.compile(r"\b(?:against|vs\.?|versus|facing|meet|meeting|answer(?:ing)?|respond(?:ing)?\s+to|"
                      rf"reply(?:ing)?\s+to)\s+(?:the\s+move\s+)?(?:1\s*\.{{1,3}}\s*)?({_SAN})\b", re.I)


def _san_token(raw: str) -> str:
    raw = raw.strip()
    if raw.lower().startswith("o-o"):
        return raw.upper()
    return raw[0].upper() + raw[1:].lower() if raw[0].lower() in "nbrqk" and len(raw) > 2 else raw.lower()


@dataclass
class Repertoire:
    """A request for an opening to play as `side` after the opponent's `move`."""
    side: str
    move: str
    openings: list[Topic]   # best first: beginner-friendly, then catalog order

    @property
    def description(self) -> str:
        return f"as {self.side.capitalize()} against 1.{'..' if self.side == 'white' else ''}{self.move}"


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

    def repertoire(self, goal: str) -> Repertoire | None:
        """"I want to play as Black against e4" -> the catalog openings for Black that start 1.e4.

        The side comes from the goal ("as black"), or from the move itself: a legal first move
        for White (e4, d4, c4, Nf3…) means the learner is Black. White's answers to a Black
        first move ("as white against e5") are openings whose second move is that move."""
        m = _AGAINST.search(goal or "")
        if not m:
            return None
        move = _san_token(m.group(1))
        tokens = _normalize(goal)
        wants_black, wants_white = "black" in tokens, "white" in tokens
        start = chess.Board()
        try:
            white_first = start.san(start.parse_san(move)) == move
        except ValueError:
            white_first = False
        side = "black" if wants_black and not wants_white else "white" if wants_white and not wants_black else (
            "black" if white_first else "white")
        rank = {"beginner": 0, "intermediate": 1, "advanced": 2}
        found = []
        for order, topic in enumerate(self.by_category("opening")):
            line = topic.line or []
            if side == "black" and topic.side == "black" and line[:1] == [move]:
                found.append((rank.get(topic.level, 3), order, topic))
            elif side == "white" and topic.side == "white" and len(line) > 1 and line[1] == move:
                found.append((rank.get(topic.level, 3), order, topic))
        return Repertoire(side, move, [t for _, _, t in sorted(found, key=lambda x: x[:2])])

    def search(self, goal: str) -> list[Topic]:
        """Topics the goal asks for, most specific first.

        Order of precedence: explicit topic aliases → an opening repertoire request ("as
        Black against e4") → whole categories ("tactics", "endgames") → the general beginner
        curriculum, but only when the goal really is general ("I want to get better at
        chess"): a specific subject we don't know is not a request for forks and pins.
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
        # "Greek gift sacrifice" is not a request for the general Sacrifices topic: a match
        # right after an unknown qualifier is only a near match (offered as related, never
        # swapped in for what was asked).
        covered = frozenset().union(*spans) if spans else frozenset()
        scored = [s for s in scored if not _qualifier_before(tokens, s[3], covered)]
        if scored:
            scored.sort(key=lambda s: (-s[0], s[1]))
            found = [s[2] for s in scored]
            side = _side_asked(tokens)
            if side:
                preferred = [t for t in found if t.category != "opening" or t.side == side]
                found = preferred or found
            return found

        rep = self.repertoire(goal)
        if rep is not None:
            return rep.openings  # possibly empty: never swap in something that wasn't asked for

        for category, meta in self.categories.items():
            for alias in meta.get("aliases", []):
                if _phrase_in(_normalize(alias), tokens):
                    topics = self.by_category(category)
                    if category == "opening":  # don't dump 15 openings: beginner ones, for the side asked
                        side = _side_asked(tokens)
                        if side:  # "openings for Black" is not a request for the Italian Game
                            topics = [t for t in topics if t.side == side] or topics
                        topics = ([t for t in topics if t.level == "beginner"] or topics)[:4]
                    return topics

        for alias in self.general.get("aliases", []):
            phrase = _normalize(alias)
            if _phrase_in(phrase, tokens) and _only_filler(tokens, phrase):
                return [self.topics[t] for t in self.general["topics"]]
        return []

    def best_alias_size(self, goal: str) -> int:
        """Length of the longest topic alias found in the goal (0 = none)."""
        tokens = _normalize(goal)
        best = 0
        for topic in self.topics.values():
            for alias in topic.aliases:
                phrase = _normalize(alias)
                if _phrase_span(phrase, tokens) is not None:
                    best = max(best, len(" ".join(phrase)))
        return best

    def near_matches(self, goal: str) -> list[Topic]:
        """Topics named in the goal but qualified by a word we don't know ("Greek gift
        sacrifice" -> Sacrifices): related material, not an answer."""
        tokens = _normalize(goal)
        found: list[Topic] = []
        for topic in self.topics.values():
            for alias in topic.aliases:
                span = _phrase_span(_normalize(alias), tokens)
                if span is not None and topic not in found:
                    found.append(topic)
        exact = set(t.id for t in self.search(goal))
        return [t for t in found if t.id not in exact]

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


def _side_asked(tokens: list[str]) -> str | None:
    """"white" or "black" when exactly one side is named in the request."""
    black, white = "black" in tokens, "white" in tokens
    if black == white:
        return None
    return "black" if black else "white"
