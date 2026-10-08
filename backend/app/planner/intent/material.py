"""Who has what: the material of a requested position, per side, with counts.

"I have two rooks and they only have a queen", "beat a queen with two rooks",
"up two rooks but they have a queen", "2R vs Q", "queen against two rooks" all describe the
same kind of thing: a group of pieces for each side. This module finds them as general
structure, not phrases:

  mention  a piece word with its count       "2 rooks", "a queen", "pair of rooks", "2R"
  group    mentions joined by and / plus     "two rooks and a queen"
  owner    the nearest marker before a group:
             learner   I, me, my, I'm, I've, we, our ("I'm up two rooks", "my rooks")
             opponent  opponent, they, their, he, she, enemy, other side
             facing    against, vs, beat, face, defend against ... (the group faced is the opponent's)
             with      "with"/"using" after a facing group ("beat a queen WITH two rooks")
           groups joined by vs/against with no marker: the first is the learner's.

The result is a structured reading (PositionRequest) that the semantic interpreter and the
clarification step check. Nothing here decides alone that a reading is right when the words
don't say so: guessed counts (bare plurals) and multi-piece requests are confirmed with the
learner, and a request whose groups contradict each other is asked about.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import PIECE_LETTER, MaterialSpec, canon

_PIECE_WORDS = {w: l for n, l in PIECE_LETTER.items() if l != "K" for w in (n, n + "s")}
_NOTATION = re.compile(r"^([1-4]?)([qrbn])$")
COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "1": 1, "single": 1, "lone": 1, "the": 1, "just": None, "only": None,
               "two": 2, "2": 2, "both": 2, "three": 3, "3": 3, "four": 4, "4": 4}
# possessives own the next group directly ("my rooks", "their queen", "the opponent's queen");
# subjects only with a possession word in between ("I have", "they only have", "I'm up") —
# "I want to learn the rook endgame" says nothing about who has the rook
LEARNER_OWN = {"my", "mine", "our", "ours", "ive", "weve"}
LEARNER_SUBJ = {"i", "im", "id", "we", "were"}
OPPONENT_OWN = {"their", "theirs", "his", "her", "opponents", "enemys", "theyve", "hes", "shes"}
OPPONENT_SUBJ = {"opponent", "they", "theyre", "he", "she", "enemy", "other", "rival", "black", "white"}
POSSESS = {"have", "has", "had", "having", "got", "get", "gets", "keep", "keeps", "hold", "holds", "own", "owns",
           "up", "with", "play", "playing", "plays", "left", "remaining", "still", "ve", "s", "m"}
FACING = {"against", "vs", "versus", "v", "facing", "face", "beat", "beating", "defeat", "defeating", "fight",
          "fighting", "battle", "battling", "meet", "meeting", "stop", "stopping", "handle", "handling"}
WITH = {"with", "using"}
VS = {"vs", "versus", "against", "v"}
AND = {"and", "plus", "with"}  # inside one group: "two rooks and a queen", "rook plus bishop"
# a piece word followed by one of these is not material ("queen sacrifice", "rook lift", "queen's gambit")
NOT_MATERIAL = {"sacrifice", "sacrifices", "sac", "sacs", "trap", "traps", "move", "moves", "fork", "forks",
                "check", "checks", "attack", "attacks", "side", "lift", "lifts", "maneuver", "manoeuvre", "opening",
                "gambit", "gambits", "pin", "pins", "skewer", "skewers", "trade", "trades", "exchange", "sortie",
                "battery", "file", "files", "development", "activity", "placement", "outpost", "outposts"}
SPEC_HEADS = {"endgame", "endgames", "ending", "endings", "endgam", "position", "positions", "material", "up",
              "down", "have", "has", "got", "having", "keep", "hold", "left", "remaining", "vs", "versus",
              "against", "beat", "beating", "defeat", "face", "facing"}


@dataclass
class Mention:
    letter: str
    count: int
    start: int
    end: int
    guessed: bool = False  # a bare plural ("rooks"): the count is a guess


@dataclass
class Group:
    pieces: list[str]
    start: int
    end: int
    owner: str | None = None          # "learner" | "opponent" | "with" (pending) | None
    marker: str | None = None         # the word that decided the owner
    joined_vs: bool = False           # this group follows a vs/against join
    guessed: bool = False

    @property
    def canon(self) -> tuple[str, ...]:
        return canon(self.pieces)


@dataclass
class PositionRequest:
    """The material of the requested position, per side. `spec` is None when the words don't
    pin it down (e.g. only one side mentioned)."""
    spec: MaterialSpec | None
    groups: list[Group] = field(default_factory=list)
    evidence: str = ""                # "ownership" | "versus"
    guessed: bool = False             # some count was inferred from a bare plural
    conflict: str | None = None       # the words contradict each other — ask
    words: str = ""

    @property
    def specific(self) -> bool:
        return self.spec is not None and self.spec.specific()

    def as_dict(self) -> dict:
        return {"spec": self.spec.describe_dict() if self.spec else None, "evidence": self.evidence,
                "guessed": self.guessed, "conflict": self.conflict,
                "groups": [{"pieces": list(g.canon), "owner": g.owner, "marker": g.marker} for g in self.groups]}


def _mention_at(tokens: list[str], i: int, notation_ok: bool) -> Mention | None:
    """A counted piece starting at token i (count words included in the span)."""
    t, n = tokens, len(tokens)
    j, count, guessed = i, None, False
    if t[j] == "pair" and j + 2 < n and t[j + 1] == "of" and t[j + 2] in _PIECE_WORDS:
        return Mention(_PIECE_WORDS[t[j + 2]], 2, i, j + 3)
    while j < n and t[j] in COUNT_WORDS:  # "just one", "only a", "only 1"
        count = COUNT_WORDS[t[j]] or count
        j += 1
    if j < n and t[j] in _PIECE_WORDS:
        word = t[j]
        if j + 1 < n and t[j + 1] in NOT_MATERIAL:
            return None
        if word.endswith("s") and count is None:  # bare plural "rooks": at least two, count guessed
            count, guessed = 2, True
        if word.endswith("s") and count == 1:  # "one rooks" — trust the number
            pass
        if j + 1 < n and t[j + 1] == "pair" and word.startswith("bishop"):
            return Mention("B", 2, i, j + 2)
        return Mention(_PIECE_WORDS[word], count or 1, i, j + 1, guessed)
    if notation_ok and j == i:
        m = _NOTATION.match(t[i])
        if m:
            return Mention(m.group(2).upper(), int(m.group(1) or 1), i, i + 1)
    return None


def _mentions(tokens: list[str]) -> list[Mention]:
    notation_ok = any(t in VS for t in tokens)
    out, i = [], 0
    while i < len(tokens):
        m = _mention_at(tokens, i, notation_ok)
        if m:
            out.append(m)
            i = m.end
        else:
            i += 1
    return out


def _groups(tokens: list[str], mentions: list[Mention]) -> list[Group]:
    groups: list[Group] = []
    for m in mentions:
        gap = tokens[groups[-1].end:m.start] if groups else None
        if groups and gap is not None and len(gap) == 1 and gap[0] in AND - {"with"}:
            g = groups[-1]  # "two rooks and a queen": one side's group
            g.pieces += [m.letter] * m.count
            g.end = m.end
            g.guessed = g.guessed or m.guessed
            continue
        g = Group([m.letter] * m.count, m.start, m.end, guessed=m.guessed)
        g.joined_vs = bool(gap) and any(w in VS for w in gap) and len(gap) <= 3
        groups.append(g)
    return groups


def _assign(tokens: list[str], groups: list[Group]) -> None:
    prev_end = 0
    for g in groups:
        window = tokens[max(prev_end, g.start - 7):g.start]
        for k in range(len(window) - 1, -1, -1):  # nearest marker wins
            w = window[k]
            possessed = any(x in POSSESS for x in window[k + 1:])
            if w in LEARNER_OWN or w in LEARNER_SUBJ and possessed:
                g.owner, g.marker = "learner", w
            elif w in OPPONENT_OWN or w in OPPONENT_SUBJ and possessed:
                g.owner, g.marker = "opponent", w
            elif w in FACING:
                g.owner, g.marker = "opponent", w
            elif w in WITH:
                g.owner, g.marker = "with", w
            else:
                continue
            break
        prev_end = g.end
    owners = {g.owner for g in groups}
    for g in groups:  # "beat a queen WITH two rooks": with + a faced group = the learner's pieces
        if g.owner == "with":
            g.owner = "learner" if "opponent" in owners else None
    # "I have two rooks against a queen" / "two rooks vs a queen": a vs-joined group with no
    # marker of its own is the other side of the group before it
    for a, b in zip(groups, groups[1:]):
        if b.joined_vs and b.marker in VS and a.owner in (None, "learner") and b.owner == "opponent":
            a.owner = a.owner or "first"


def extract(tokens: list[str]) -> PositionRequest | None:
    """The per-side material of a request, or None when it doesn't describe one."""
    groups = _groups(tokens, _mentions(tokens))
    if len(groups) < 2 or not any(t in SPEC_HEADS for t in tokens):
        return None
    _assign(tokens, groups)
    words = " ".join(tokens[groups[0].start:groups[-1].end])
    mine = [g for g in groups if g.owner == "learner"]
    theirs = [g for g in groups if g.owner == "opponent"]
    loose = [g for g in groups if g.owner not in ("learner", "opponent")]
    guessed = any(g.guessed for g in groups)
    if mine and theirs:
        a = canon([p for g in mine for p in g.pieces])
        b = canon([p for g in theirs for p in g.pieces])
        if len(mine) > 1 and len({g.canon for g in mine}) > 1 or len(theirs) > 1 and len({g.canon for g in theirs}) > 1:
            return PositionRequest(None, groups, "ownership", guessed, "the same side is described two different ways", words)
        a, b = mine[0].canon, theirs[0].canon  # repeated descriptions of the same group count once
        conflict = None
        for g in loose:  # "the 2 rooks and queen endgame, where I have 2 rooks ...": must agree
            if sorted(g.canon) not in (sorted(a), sorted(b), sorted(a + b)):
                conflict = f"“{' '.join(tokens[g.start:g.end])}” doesn't fit the rest of the request"
        if "P" in a + b and not all(p == "P" for p in a) and not all(p == "P" for p in b):
            a, b = tuple(p for p in a if p != "P") or a, tuple(p for p in b if p != "P") or b
        spec = MaterialSpec(a, "versus", "endgame", against=b, owner="learner")
        return PositionRequest(spec, groups, "ownership", guessed, conflict, words)
    # no ownership words: a plain "X vs Y" (first group = the learner's when the request is specific)
    for x, y in zip(groups, groups[1:]):
        if y.joined_vs and x.owner in (None, "first") and y.owner in (None, "opponent"):
            a, b = x.canon, y.canon
            if "P" in a or ("P" in b and len(b) > 1):
                return None  # pawn groups ("rook and pawn vs rook") are handled as named topics
            single = len(a) == 1 and len(b) == 1 and not guessed
            spec = MaterialSpec(a, "versus", "endgame", against=b, owner=None if single else "learner")
            return PositionRequest(spec, groups, "versus", guessed, None, words)
    return None


# ------------------------------------------------------------------ objectives
# Material is not an objective: "two rooks against a queen" can be practised as converting,
# coordinating, avoiding perpetual check, ... The confirmation card offers the ones the
# material makes meaningful; general practical play is the default.
VALUE = {"Q": 9, "R": 5, "B": 3, "N": 3, "P": 1}
OBJECTIVES = {
    "general": "General practical play",
    "convert": "Converting the advantage",
    "coordinate": "Coordinating your pieces",
    "avoid_perpetual": "Avoiding perpetual checks",
    "attack_king": "Attacking the king",
    "trade": "Trading safely",
    "win_material": "Winning material",
    "hold": "Holding the draw",
}
_OBJECTIVE_WORDS = [
    ("avoid_perpetual", {"perpetual", "perpetuals"}),
    ("coordinate", {"coordinate", "coordinating", "coordination", "cooperate", "cooperating", "teamwork"}),
    ("convert", {"convert", "converting", "conversion"}),
    ("hold", {"hold", "holding", "draw", "drawing", "defend", "defending", "save", "saving", "survive"}),
    ("attack_king", {"attack", "attacking", "mating", "checkmating"}),
    ("trade", {"trade", "trading", "exchange", "exchanging", "simplify", "simplifying"}),
]


def balance(spec: MaterialSpec) -> int:
    """Piece value of the learner's group minus the opponent's (versus specs)."""
    return sum(VALUE[p] for p in spec.pieces) - sum(VALUE[p] for p in spec.against or ())


def objective_label(objective: str, spec: MaterialSpec | None = None) -> str:
    if spec is not None and spec.relation == "versus":
        from .model import group_words, LETTER_NAME
        if objective == "coordinate" and len(set(spec.pieces)) == 1:
            return f"Coordinating the {group_words(spec.pieces).split(' ', 1)[-1]}"
        if objective == "win_material" and len(spec.against) == 1:
            return f"Winning the {LETTER_NAME[spec.against[0]]}"
    return OBJECTIVES[objective]


def objectives_for(spec: MaterialSpec) -> list[str]:
    """The focuses this material makes meaningful, general first."""
    out = ["general"]
    if spec.relation != "versus":
        return out
    diff = balance(spec)
    if len(spec.pieces) >= 2:
        out.append("coordinate")
    if "Q" in spec.against:
        out.append("avoid_perpetual")
    if diff >= 2:
        out.append("convert")
    if diff <= -2:
        out.append("hold")
    return out[:4]


def objective_in(tokens: list[str]) -> str | None:
    """A focus the learner stated in words ("... and avoid perpetual checks")."""
    for objective, words in _OBJECTIVE_WORDS:
        if any(t in words for t in tokens):
            return objective
    return None


def wants_interpretation(tokens: list[str]) -> bool:
    """Gate for the (slow) semantic interpreter: a piece is named AND the request talks about
    counts, sides or an endgame — "I have two rooks...", "queen vs rook", "two rooks endgame"."""
    mentions = _mentions(tokens)
    if not mentions:
        return False
    signals = SPEC_HEADS | set(COUNT_WORDS) - {"a", "an", "the"} | LEARNER_OWN | OPPONENT_OWN | OPPONENT_SUBJ
    return len(mentions) >= 2 or any(t in signals for t in tokens)
