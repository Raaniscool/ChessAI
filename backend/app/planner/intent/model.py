"""Structured intent: what a learning request means, as data the planner can act on.

A request ("I want to learn knight and bishop endgames") is turned into a
LearningIntent: the *components* the plan has to cover, plus constraints (learner
side, level, exclusions). When a request has several materially different readings,
each reading is an Interpretation; the learner picks one through a Question.

Nothing here decides chess facts. MaterialSpec.matches only reads which pieces stand
on a board (python-chess); whether a position is correct is the validator's job.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import chess

PIECE_LETTER = {"king": "K", "queen": "Q", "rook": "R", "bishop": "B", "knight": "N", "pawn": "P"}
LETTER_NAME = {v: k for k, v in PIECE_LETTER.items()}
ICON = {"K": "♚", "Q": "♛", "R": "♜", "B": "♝", "N": "♞", "P": "♟"}
_PIECE_TYPE = {"Q": chess.QUEEN, "R": chess.ROOK, "B": chess.BISHOP, "N": chess.KNIGHT}
# The largest number of non-pawn pieces (kings excluded) a position may have and still count as an
# endgame "with these pieces": beyond that it is a middlegame that happens to contain them.
ENDGAME_PIECES = 4
RELATIONS = ("only", "together", "versus", "any")
HEADS = ("endgame", "mate")


def side_pieces(board: chess.Board, color: chess.Color) -> list[str]:
    """Non-king, non-pawn pieces of one side as sorted letters, e.g. ['B', 'N']."""
    return sorted(letter for letter, t in _PIECE_TYPE.items() for _ in board.pieces(t, color))


def can_force_mate(pieces: tuple[str, ...]) -> bool:
    """Can a king plus these pieces force mate against a lone king? (General chess knowledge:
    a lone bishop or knight can't, two knights can't force it, anything with Q/R/B+B/B+N can.)"""
    p = sorted(pieces)
    if any(x in ("Q", "R") for x in p):
        return True
    if p.count("B") >= 2 or ("B" in p and "N" in p):
        return True
    return False


@dataclass(frozen=True)
class MaterialSpec:
    """Which pieces a position must contain, and how they relate.

    only     every non-pawn piece on the board is `pieces[0]` ("knight endgames")
    together one side has all of `pieces`; the other side has at most one piece
             (for mates: none — a lone king)
    versus   one side has exactly pieces[0], the other exactly pieces[1] ("knight vs bishop")
    any      every non-pawn piece is one of `pieces` ("minor-piece endgames")
    """
    pieces: tuple[str, ...]
    relation: str
    head: str = "endgame"
    bishops: str | None = None  # "opposite" | "same" (bishop vs bishop only)

    def __post_init__(self):
        if self.relation not in RELATIONS or self.head not in HEADS:
            raise ValueError(f"bad material spec {self.relation}/{self.head}")

    # ------------------------------------------------------------------ words
    def label(self) -> str:
        names = [LETTER_NAME[p] for p in self.pieces]
        head = "endgames" if self.head == "endgame" else "checkmates"
        if self.relation == "only":
            return f"{names[0].capitalize()} {head}"
        if self.relation == "versus":
            kind = {"opposite": " (opposite-coloured bishops)", "same": " (same-coloured bishops)"}.get(self.bishops, "")
            return f"{names[0].capitalize()} against {names[1]} {head}{kind}"
        if self.relation == "together":
            joined = " and ".join(f"{'a ' if n != 'king' else ''}{n}" for n in names)
            if self.head == "mate":
                return f"Checkmate with {joined} together"
            return f"Endgames where one side has both {joined}"
        return f"{' and '.join(n.capitalize() if i == 0 else n for i, n in enumerate(names))} {head}"

    def icon(self) -> str:
        sep = " vs " if self.relation == "versus" else ""
        return sep.join(ICON[p] for p in self.pieces)

    def as_dict(self) -> dict:
        out = {"pieces": list(self.pieces), "relation": self.relation, "head": self.head}
        if self.bishops:
            out["bishops"] = self.bishops
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "MaterialSpec":
        return cls(tuple(d["pieces"]), d["relation"], d.get("head", "endgame"), d.get("bishops"))

    # ------------------------------------------------------------------ boards
    def matches(self, board: chess.Board) -> bool:
        """Does this position have the requested material? (pawns never matter)"""
        w, b = side_pieces(board, chess.WHITE), side_pieces(board, chess.BLACK)
        if len(w) + len(b) > ENDGAME_PIECES:
            return False
        want = sorted(self.pieces)
        if self.relation == "only":
            return bool(w or b) and set(w + b) == {self.pieces[0]}
        if self.relation == "any":
            return bool(w or b) and set(w + b) <= set(self.pieces)
        if self.relation == "versus":
            x, y = [self.pieces[0]], [self.pieces[1]]
            if not ((w == x and b == y) or (w == y and b == x)):
                return False
            if self.bishops and self.pieces == ("B", "B"):
                sq = [next(iter(board.pieces(chess.BISHOP, c))) for c in (chess.WHITE, chess.BLACK)]
                same = (chess.square_file(sq[0]) + chess.square_rank(sq[0])) % 2 == \
                       (chess.square_file(sq[1]) + chess.square_rank(sq[1])) % 2
                return same == (self.bishops == "same")
            return True
        # together
        for mine, theirs in ((w, b), (b, w)):
            if mine == want and len(theirs) <= (0 if self.head == "mate" else 1):
                return True
        return False

    def winner(self, board: chess.Board) -> chess.Color | None:
        """For 'together' / 'only' specs: the side that has the requested pieces."""
        for color in (chess.WHITE, chess.BLACK):
            mine = side_pieces(board, color)
            if mine and set(mine) <= set(self.pieces):
                return color
        return None


# ---------------------------------------------------------------------- components
COMPONENT_KINDS = ("topic", "concept", "glossary", "material", "opening_as", "opening_db")


@dataclass(frozen=True)
class Component:
    """One thing the plan must teach.

    topic       a catalog topic (verified lessons)          id = topic id
    concept     a Knowledge Library concept                  id = concept id
    glossary    a named idea with only a written definition  id = glossary term id
    material    positions with given material                material = MaterialSpec
    opening_as  a catalog opening, learned from `side`       id = topic id
    opening_db  an opening family from the Lichess database  id = family name
    """
    kind: str
    id: str = ""
    label: str = ""
    material: MaterialSpec | None = None
    side: str | None = None

    def __post_init__(self):
        if self.kind not in COMPONENT_KINDS:
            raise ValueError(f"unknown component kind {self.kind!r}")

    def key(self) -> str:
        if self.kind == "material":
            return "material:" + json.dumps(self.material.as_dict(), sort_keys=True)
        return f"{self.kind}:{self.id}" + (f":{self.side}" if self.side else "")

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "id": self.id, "label": self.label}
        if self.material:
            out["material"] = self.material.as_dict()
        if self.side:
            out["side"] = self.side
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Component":
        mat = MaterialSpec.from_dict(d["material"]) if d.get("material") else None
        return cls(d["kind"], d.get("id", ""), d.get("label", ""), mat, d.get("side"))


@dataclass
class Interpretation:
    """One reading of a request: the components it asks for, in order."""
    id: str
    label: str
    components: list[Component]
    icon: str = ""
    level: str | None = None

    def signature(self) -> str:
        return "|".join(sorted(c.key() for c in self.components)) + f"|{self.level or ''}"

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "icon": self.icon,
                "components": [c.as_dict() for c in self.components], "level": self.level}

    @classmethod
    def from_dict(cls, d: dict) -> "Interpretation":
        return cls(d["id"], d["label"], [Component.from_dict(c) for c in d.get("components", [])],
                   d.get("icon", ""), d.get("level"))


OTHER = "other"


@dataclass
class Question:
    """A clarification the learner answers before a plan is built."""
    key: str                 # ambiguity key: the same ambiguity in other words has the same key
    kind: str                # coordination | lexical | conflict_side | conflict_level | ...
    term: str                # the words that are ambiguous
    text: str                # "What do you mean by “knight and bishop endgames”?"
    options: list[Interpretation]
    allow_other: bool = True

    def as_dict(self) -> dict:
        opts = [{"id": o.id, "label": o.label, "icon": o.icon} for o in self.options]
        if self.allow_other:
            opts.append({"id": OTHER, "label": "Something else — let me explain", "icon": "✏️"})
        return {"key": self.key, "kind": self.kind, "term": self.term, "question": self.text, "options": opts,
                "allow_other": self.allow_other}

    def option(self, choice: str) -> Interpretation | None:
        return next((o for o in self.options if o.id == choice), None)


@dataclass
class LearningIntent:
    """What the plan must contain. `components` is empty for plain requests, which the
    existing planners handle directly from the words; it is filled when the request has
    structure they can't express (material, a flipped side, exclusions) or was clarified."""
    goal: str
    components: list[Component] = field(default_factory=list)
    level: str | None = None
    side: str | None = None
    exclude: list[str] = field(default_factory=list)      # concept ids
    interpretation: str | None = None                     # label of the reading used
    clarified: list[dict] = field(default_factory=list)   # answers that led here
    source: str = "parsed"                                # parsed | clarified | remembered | qwen

    @property
    def structured(self) -> bool:
        return bool(self.components) or bool(self.exclude)

    def signature(self) -> str:
        """Same meaning → same signature (used to find an equivalent verified plan)."""
        data = {"c": sorted(c.key() for c in self.components), "x": sorted(self.exclude),
                "s": self.side or "", "l": self.level or ""}
        return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {"goal": self.goal, "components": [c.as_dict() for c in self.components], "level": self.level,
                "side": self.side, "exclude": self.exclude, "interpretation": self.interpretation,
                "clarified": self.clarified, "source": self.source, "signature": self.signature()}

    @classmethod
    def from_dict(cls, d: dict) -> "LearningIntent":
        return cls(d.get("goal", ""), [Component.from_dict(c) for c in d.get("components", [])], d.get("level"),
                   d.get("side"), list(d.get("exclude", [])), d.get("interpretation"), list(d.get("clarified", [])),
                   d.get("source", "parsed"))


class ClarificationNeeded(Exception):
    """The request has several materially different readings: ask before planning."""

    def __init__(self, question: Question, goal: str):
        super().__init__(question.text)
        self.question = question
        self.goal = goal


class ClarificationError(ValueError):
    """A clarification answer that can't be used (unknown question/option, empty text)."""
