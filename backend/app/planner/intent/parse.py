"""Slot extraction: the structure of a learning request, without deciding its meaning.

Pulls out the pieces of a request that change what a plan must contain:
  * material phrases  — pieces + an endgame/mate head, and how the pieces are joined
                        ("knight and bishop endgames", "mate with bishop and knight",
                        "rook vs bishop", "minor-piece endgames", "the bishop pair")
  * relation markers  — together / separately
  * learner side      — "as White", "with the black pieces", "for Black"
  * facing marker     — "against", "facing", "how to meet"
  * level words       — beginner / intermediate / advanced (all of them, to spot conflicts)
  * exclusions        — "without", "except", "but not", "no"
Named subjects come from the Lexicon; the detectors in ambiguity.py decide what is
ambiguous. Nothing here is specific to one example request.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..catalog import _normalize
from .material import PositionRequest, extract as extract_position
from .model import PIECE_LETTER

_PIECE_WORDS = {w: l for n, l in PIECE_LETTER.items() for w in (n, n + "s")}
HEAD_WORDS = {
    **{w: "endgame" for w in ("endgame", "endgames", "ending", "endings", "endgam", "endgaems")},
    **{w: "mate" for w in ("mate", "mates", "checkmate", "checkmates", "mating", "checkmating")},
}
_JOIN_AND = {"and", "plus", "with"}
_JOIN_VS = {"vs", "versus", "against", "v"}
_JOIN_OR = {"or"}
_ARTICLES = {"a", "an", "the", "one", "single", "lone", "1"}
_COUNTS = {"two": 2, "2": 2, "three": 3, "3": 3}  # "2 rooks and a queen" keeps both rooks
TOGETHER = {"together", "both", "combined", "cooperating", "cooperation", "coordinating", "coordination",
            "teamwork", "jointly", "working"}
SEPARATE = {"separately", "separate", "individually", "each", "respectively", "independently", "apart"}
LEVELS = {
    "beginner": "beginner", "beginners": "beginner", "novice": "beginner", "novices": "beginner",
    "easy": "beginner", "simple": "beginner", "basic": "beginner", "intro": "beginner",
    "introductory": "beginner", "intermediate": "intermediate", "advanced": "advanced", "hard": "advanced",
    "difficult": "advanced", "expert": "advanced", "master": "advanced", "harder": "advanced",
}
FACING = {"against", "facing", "vs", "versus", "meet", "meeting", "answer", "answering", "beat", "beating",
          "counter", "countering", "refute", "refuting", "stop", "stopping", "handle", "handling"}
_SIDE_RE = re.compile(r"\b(?:as|with|playing|for|from|on)\s+(?:the\s+)?(white|black)(?:'s)?(?:\s+(?:pieces|side))?\b"
                      r"|\b(white|black)(?:'s)?\s+(?:side|perspective|pieces)\b", re.I)
_EXCLUDE_RE = re.compile(r"\b(?:without|except(?:\s+for)?|excluding|but\s+not|not|no|skip(?:ping)?)\s+"
                         r"(?:the\s+|any\s+|about\s+)?([a-z][a-z'\- ]*?)(?=$|[,.;!?]|\s+(?:and|but|please|or)\b)", re.I)


@dataclass
class MaterialPhrase:
    pieces: list[str]            # letters in the order written, kings/pawns included
    joins: list[str]             # "and" | "vs" | "or" between consecutive pieces
    head: str                    # endgame | mate
    start: int
    end: int                     # token span (exclusive), head included
    group: str | None = None     # "minor" | "major" | "pair" | "opposite" | "same"
    words: str = ""

    @property
    def real(self) -> list[str]:
        """The pieces that define the material: kings never do; pawns only when they are
        what the other side has ("rook vs pawn"), never as a partner ("rook and pawn")."""
        pcs = [p for p in self.pieces if p != "K"]
        if "vs" in self.joins:
            return pcs
        non_pawn = [p for p in pcs if p != "P"]
        return non_pawn or pcs


@dataclass
class Parsed:
    text: str
    tokens: list[str]
    side: str | None = None
    side_span: tuple[int, int] | None = None
    facing_at: list[int] = field(default_factory=list)   # token positions of facing words
    levels: list[str] = field(default_factory=list)      # distinct levels, in order
    level_words: list[str] = field(default_factory=list)
    together: bool = False
    separate: bool = False
    material: list[MaterialPhrase] = field(default_factory=list)
    exclude_phrases: list[str] = field(default_factory=list)
    position: PositionRequest | None = None              # per-side material ("I have two rooks, they a queen")


def _group_at(tokens: list[str], i: int) -> tuple[str, list[str], int] | None:
    """Multi-word piece groups starting at i: (group, letters, length)."""
    t = tokens
    nxt = t[i + 1] if i + 1 < len(t) else ""
    nxt2 = t[i + 2] if i + 2 < len(t) else ""
    if t[i] in ("minor", "major") and nxt in ("piece", "pieces"):
        return t[i], (["N", "B"] if t[i] == "minor" else ["Q", "R"]), 2
    if t[i] in ("bishop", "bishops") and nxt == "pair":
        return "pair", ["B", "B"], 2
    if t[i] == "pair" and nxt == "of" and nxt2 in ("bishops", "knights", "rooks"):
        return "pair", [_PIECE_WORDS[nxt2]] * 2, 3
    if t[i] in _COUNTS and nxt in ("bishops", "knights", "rooks", "queens"):
        return "pair", [_PIECE_WORDS[nxt]] * _COUNTS[t[i]], 2
    if t[i] in ("opposite", "same") and nxt in ("colored", "coloured", "color", "colour") and nxt2 in ("bishop", "bishops"):
        return t[i], ["B", "B"], 3
    return None


def _material(tokens: list[str]) -> list[MaterialPhrase]:
    out: list[MaterialPhrase] = []
    i, n = 0, len(tokens)
    while i < n:
        group = _group_at(tokens, i)
        if not group and tokens[i] not in _PIECE_WORDS:
            i += 1
            continue
        start = i
        pieces: list[str] = []
        joins: list[str] = []
        grp = None
        if group:
            grp, pieces, size = group[0], list(group[1]), group[2]
            i += size
            joins = ["and"] * (len(pieces) - 1) if grp == "pair" else (["vs"] if grp in ("opposite", "same") else ["and"])
        else:
            pieces.append(_PIECE_WORDS[tokens[i]])
            i += 1
        # extend: [article]* join [article]* piece
        while i < n:
            j = i
            join = None
            if tokens[j] in _JOIN_AND | _JOIN_VS | _JOIN_OR:
                join = "vs" if tokens[j] in _JOIN_VS else ("or" if tokens[j] in _JOIN_OR else "and")
                j += 1
            while j < n and tokens[j] in _ARTICLES:
                j += 1
            times = 1
            if join and j + 1 < n and tokens[j] in _COUNTS and tokens[j + 1] in _PIECE_WORDS:
                times = _COUNTS[tokens[j]]
                j += 1
            if join and j < n and tokens[j] in _PIECE_WORDS:
                pieces += [_PIECE_WORDS[tokens[j]]] * times
                joins += [join] + ["and"] * (times - 1)
                if grp == "pair":  # "two rooks and a queen" is no longer just a pair
                    grp = None
                i = j + 1
                continue
            break
        end = i
        head = None
        # head after: "knight and bishop endgames" / "... endgame" / "... checkmate"
        k = end
        while k < n and tokens[k] in ("piece", "pieces", "together", "only"):
            k += 1
        if k < n and tokens[k] in HEAD_WORDS:
            head, end = HEAD_WORDS[tokens[k]], k + 1
        else:
            # head before: "endgames with (a) knight and (a) bishop", "mate with ...", "checkmating with ..."
            b = start - 1
            while b >= 0 and tokens[b] in _ARTICLES:
                b -= 1
            if b >= 0 and tokens[b] in ("with", "using", "of", "involving", "featuring", "where", "vs", "versus"):
                b -= 1
                while b >= 0 and tokens[b] in _ARTICLES:
                    b -= 1
            if b >= 0 and tokens[b] in HEAD_WORDS:
                head, start = HEAD_WORDS[tokens[b]], b
        if head is None and "vs" in joins and len([x for x in pieces if x not in ("K",)]) >= 2:
            head = "endgame"  # "rook vs bishop": pieces against each other are an endgame
        if head and (len(pieces) >= 2 or grp):
            out.append(MaterialPhrase(pieces, joins, head, start, end, grp, " ".join(tokens[start:end])))
    return out


def parse(goal: str) -> Parsed:
    tokens = _normalize(goal.replace("&", " and ").replace("+", " and "))
    p = Parsed(goal, tokens)
    m = _SIDE_RE.search(goal)
    if m:
        p.side = (m.group(1) or m.group(2)).lower()
        before = len(_normalize(goal[:m.start()]))
        p.side_span = (before, before + len(_normalize(m.group(0))))
    p.facing_at = [i for i, t in enumerate(tokens) if t in FACING]
    for t in tokens:
        lvl = LEVELS.get(t)
        if lvl and lvl not in p.levels:
            p.levels.append(lvl)
            p.level_words.append(t)
    p.together = any(t in TOGETHER for t in tokens)
    p.separate = any(t in SEPARATE for t in tokens)
    p.material = _material(tokens)
    p.position = extract_position(tokens)
    for m in _EXCLUDE_RE.finditer(goal):
        phrase = m.group(1).strip(" -'")
        if phrase and phrase.lower() not in ("sure", "idea", "clue", "matter", "problem", "one"):
            p.exclude_phrases.append(phrase)
    return p
