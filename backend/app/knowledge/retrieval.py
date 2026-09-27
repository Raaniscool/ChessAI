"""Retrieval layer: how the tutor, planner and chat ask the Knowledge Library for material.

    request ("Teach me checkmates", level, seen history, ...)
        -> concepts (via the concept graph: names, aliases, parents/children)
        -> verified examples only, picked by KnowledgeLibrary.select (variety, level, freshness)
        -> a short teaching SEQUENCE: demonstration -> guided example -> practice

Nothing here verifies or creates examples; the library's pipeline already did that.
This layer only chooses, and it only ever hands out entries whose status is
``verified`` (never candidate / verifying / needs_review / rejected / deprecated).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

from ..planner.catalog import _normalize, _phrase_span
from .facts import verified_facts
from .library import DEFAULT_COUNT, MAX_COUNT, KnowledgeLibrary, Query
from .schema import Example

ROLES = ("demonstration", "guided", "practice")
LEVELS = ("beginner", "intermediate", "advanced")
MAX_PRACTICE = 3
MIN_ATTEMPTS_FOR_LEVEL = 3

# Words that can surround a concept in a request without changing what is asked for.
# A request whose only concept is a broad root ("openings", "tactics") must not
# contain other content words: "opening principles" is not a request for openings.
FILLER = set("""
a an the some any few more other another few couple several one two three four five
me my i we us you your please can could would will do does how what which why
teach show give let see learn learning study studying practice practise practicing
improve master understand get got better at about on in of for with to and or
want wanna need like id im really very super simple simplest easy easiest basic basics
beginner beginners intermediate advanced hard harder tricky difficult challenging
lesson lessons example examples position positions puzzle puzzles exercise exercises
pattern patterns kind kinds type types way ways idea ideas common typical classic famous
avoid stop making make prevent spot spotting find finding play playing
chess game games quiz test try now today
""".split())

_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:teach|show|give|explain|quiz\s+me|test\s+me|let\s+me\s+(?:see|practi[cs]e|try)|"
    r"i\s*(?:really\s+)?(?:want|would\s+like|'d\s+like|d\s+like|wanna|need)\s+(?:to\s+)?"
    r"(?:learn|study|practi[cs]e|see|improve|master|get\s+better\s+at)|"
    r"learn|practi[cs]e|help\s+me\s+(?:learn|with|understand))\b", re.I)
_COUNT = re.compile(r"(?<!\bin )\b([1-5]|two|three|four|five)\s+(?:[a-z-]+\s+){0,2}?"
                    r"(?:examples?|positions?|puzzles?|mates|checkmates|forks|pins|skewers|[a-z]+s)\b", re.I)
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}
_QUESTION = re.compile(r"\b(why|this|that|my move|the move|last move|it|here)\b", re.I)


@dataclass
class RetrievalRequest:
    """Everything the tutor can tell the library about what to fetch."""

    text: str = ""
    concepts: list[str] = field(default_factory=list)  # explicit concept ids (skip text matching)
    category: str | None = None           # basics | checkmates | tactics | endgames | openings | mistakes
    subcategory: str | None = None
    level: str | None = None              # learner skill: beginner | intermediate | advanced
    difficulty: tuple[int, int] | None = None  # explicit difficulty band (1-5), inclusive
    mode: str | None = None               # required presentation mode (e.g. "interactive")
    tags: list[str] = field(default_factory=list)
    count: int | None = None              # examples in the teaching sequence (default 3)
    practice_count: int = 0               # extra interactive examples for a practice lesson
    exclude: set[str] = field(default_factory=set)
    avoid_seen: bool = True               # prefer examples the learner hasn't seen
    include_prerequisites: bool | None = None  # None = only for beginners
    weak_concepts: list[str] = field(default_factory=list)  # personalization signal
    # Learner-model personalization (learner.views.lesson_shape); all optional:
    target_rating: int | None = None      # choose examples near this puzzle rating (knowledge.difficulty)
    practice_rating: int | None = None    # ... and the extra practice near this one
    roles: list[str] | None = None        # explicit role pattern, e.g. ["guided", "practice", "practice"]
    known_concepts: set[str] = field(default_factory=set)  # already met: no prerequisite demo needed
    prefer_real: bool = False             # prefer positions from real games (puzzles → games transfer)


@dataclass
class Retrieval:
    concepts: list[str]
    sequence: list[tuple[Example, str]]   # (example, role) in teaching order
    practice: list[Example] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)  # prerequisite concepts shown first
    related: list[str] = field(default_factory=list)        # related concept ids with examples
    level: str | None = None
    level_source: str = "none"            # request | text | history | none
    confident: bool = True

    @property
    def found(self) -> bool:
        return bool(self.sequence)

    @property
    def examples(self) -> list[Example]:
        return [e for e, _ in self.sequence] + self.practice


# ----------------------------------------------------------------- concepts ----

def resolve_concepts(library: KnowledgeLibrary, text: str) -> tuple[list[str], bool]:
    """Concepts (with verified examples) named in `text`, and whether the match is confident.

    Aliases work through the concept graph ("checkmates" -> checkmate, "knight forks" ->
    knight_fork). A match on a broad root concept only counts when nothing else of
    substance is left in the request.
    """
    matched = [c for c in library.match_concepts(text) if library.count_for(c)]
    if not matched:
        return [], False
    tokens = _normalize(text)
    covered: set[int] = set()
    for cid in matched:
        concept = library.concepts[cid]
        for alias in [concept.name, cid.replace("_", " ")] + concept.aliases:
            span = _phrase_span(_normalize(alias), tokens)
            if span:
                covered |= span
    leftover = [t for i, t in enumerate(tokens) if i not in covered and t not in FILLER
                and not t.isdigit()]
    specific = any(library.concepts[c].parents for c in matched)
    return matched, (specific or not leftover) and not _qualified(library, matched, tokens, covered, text)


_PIECE_WORDS = {"king", "queen", "rook", "bishop", "knight", "pawn", "piece", "white", "black", "kings", "queens",
                "rooks", "bishops", "knights", "pawns", "pieces"}
_MATE_IN_N = re.compile(r"\bmate\s+in\s+(?:([2-9])|two|three|four|five)\b", re.I)


def _qualified(library: KnowledgeLibrary, matched: list[str], tokens: list[str], covered: set[int],
               text: str) -> bool:
    """Is the request a *named variety* the library doesn't have ("Greek gift sacrifice",
    "epaulette mate", "mate in two")? Answering it with the generic concept would quietly
    teach something else, so such a match is not confident (the caller then says what it has)."""
    if _MATE_IN_N.search(text or ""):
        return True
    for cid in matched:
        concept = library.concepts[cid]
        spans = [_phrase_span(_normalize(a), tokens) for a in [concept.name, cid.replace("_", " ")] + concept.aliases]
        spans = [sp for sp in spans if sp]
        if not spans:
            continue
        before = min(min(sp) for sp in spans) - 1
        if before >= 0 and before not in covered:
            word = tokens[before]
            if word not in FILLER and word not in _PIECE_WORDS and not word.isdigit():
                return True
    return False


def lesson_request(library: KnowledgeLibrary, text: str) -> list[str]:
    """Concept ids when `text` asks to be taught/shown something the library covers.

    "Show me checkmates" -> ["checkmate"]; "show me why this move is bad" -> [].
    """
    if not _REQUEST.search(text or ""):
        return []
    rest = _REQUEST.sub("", text, count=1)
    if _QUESTION.search(rest) and not library.match_concepts(rest):
        return []
    concepts, confident = resolve_concepts(library, text)
    return concepts if confident else []


# ------------------------------------------------------------ personalization ----

def _stats_by_concept(library: KnowledgeLibrary, usage, concepts: list[str]) -> dict[str, list[int]]:
    """{concept: [attempts, successes]} over the learner's history, within `concepts`' trees."""
    if usage is None:
        return {}
    tree = {d for c in concepts for d in library.descendants(c)}
    out: dict[str, list[int]] = {}
    for eid, entry in usage._load().items():  # read-only view of the usage file
        example = library.entries.get(eid)
        if example is None or example.concept not in tree:
            continue
        row = out.setdefault(example.concept, [0, 0])
        row[0] += entry.get("attempts", 0)
        row[1] += entry.get("successes", 0)
    return out


def personal_level(library: KnowledgeLibrary, usage, concepts: list[str]) -> str | None:
    """Learner level for these concepts from past attempts (None = not enough history)."""
    stats = _stats_by_concept(library, usage, concepts)
    attempts = sum(a for a, _ in stats.values())
    if attempts < MIN_ATTEMPTS_FOR_LEVEL:
        return None
    rate = sum(s for _, s in stats.values()) / attempts
    if rate < 0.4:
        return "beginner"
    if rate >= 0.8:
        return "advanced" if attempts >= 2 * MIN_ATTEMPTS_FOR_LEVEL else "intermediate"
    return None


def weak_concepts(library: KnowledgeLibrary, usage, concepts: list[str]) -> list[str]:
    """Sub-concepts the learner has struggled with (success rate under 50%, 2+ attempts)."""
    stats = _stats_by_concept(library, usage, concepts)
    return [c for c, (a, s) in sorted(stats.items()) if a >= 2 and s / a < 0.5 and library.count_for(c)]


# ---------------------------------------------------------------- retrieval ----

def _band_ok(example: Example, band: tuple[int, int] | None) -> bool:
    return band is None or band[0] <= example.difficulty <= band[1]


def _interactive(example: Example) -> bool:
    return example.key_move is not None and "interactive" in example.presentation_modes


def _assign_roles(examples: list[Example], mode: str | None,
                  roles: list[str] | None = None) -> list[tuple[Example, str]]:
    """Easiest first: watch one, find the key move in the next, solve the last alone.
    An explicit `roles` pattern (from the learner model) replaces the default; examples
    that can't be interactive are always demonstrations."""
    if roles:
        return [(e, roles[i] if i < len(roles) and _interactive(e) else "demonstration")
                for i, e in enumerate(examples)]
    out: list[tuple[Example, str]] = []
    last = len(examples) - 1
    for i, example in enumerate(examples):
        if not _interactive(example):
            role = "demonstration"
        elif mode == "interactive":
            role = "guided" if i == 0 else "practice"
        elif i == 0 and last > 0:
            role = "demonstration"
        elif i == last and last > 0:
            role = "practice"
        else:
            role = "guided"
        out.append((example, role))
    return out


def retrieve(library: KnowledgeLibrary, request: RetrievalRequest, usage=None) -> Retrieval:
    """Pick a small, level-appropriate teaching sequence of verified examples."""
    parsed = library.parse_query(request.text) if request.text else Query()
    if request.concepts:
        concepts = [c for c in request.concepts if c in library.concepts and library.count_for(c)]
        confident = bool(concepts)
    else:
        concepts, confident = resolve_concepts(library, request.text)
    if not concepts or not confident:
        return Retrieval(concepts=concepts, sequence=[], confident=confident)

    level, source = request.level, "request"
    if level not in LEVELS:
        level, source = parsed.level, "text"
    if level is None:
        level = personal_level(library, usage, concepts)
        source = "history" if level else "none"

    seen = usage.seen_counts() if (usage and request.avoid_seen) else {}
    last_used = usage.last_used() if (usage and request.avoid_seen) else {}

    # Filters the library's select() doesn't know about become exclusions.
    exclude = set(request.exclude)
    for cid in concepts:
        for e in library.examples_for(cid):
            if not _band_ok(e, request.difficulty) or (
                    request.subcategory and e.subcategory.lower() != request.subcategory.lower()):
                exclude.add(e.id)

    if request.prefer_real:
        real = {e.id for cid in concepts for e in library.examples_for(cid) if _from_real_game(e)}
        if len(real - exclude) >= 2:  # only when enough real positions remain to build a lesson
            for cid in concepts:
                exclude |= {e.id for e in library.examples_for(cid) if e.id not in real}

    asked = _requested_count(request.text, parsed.count)
    default = len(request.roles) if request.roles else DEFAULT_COUNT
    count = max(1, min(MAX_COUNT, request.count or (asked if asked != DEFAULT_COUNT else default)))
    mode = request.mode or parsed.mode
    # an explicit level in the words ("hard forks") beats the learner model's target
    target = request.target_rating if parsed.level is None and not parsed.simplest else None

    def query(cids: list[str], n: int, **kw) -> Query:
        return Query(concepts=cids, count=n, level=kw.get("level", level), simplest=parsed.simplest,
                     mode=kw.get("mode", mode), tags=request.tags, category=request.category,
                     exclude=exclude | kw.get("exclude", set()), not_seen_recently=request.avoid_seen,
                     target_rating=kw.get("target", target))

    picked: list[Example] = []
    # Personalization: one example of something the learner found hard, when it fits the request.
    weak = [c for c in (request.weak_concepts or weak_concepts(library, usage, concepts))
            if c in library.concepts and any(c in library.descendants(r) for r in concepts)]
    if weak and count > 1:
        picked += library.select(query(weak, 1), seen, last_used)
    picked += library.select(query(concepts, count - len(picked), exclude={e.id for e in picked}),
                             seen, last_used)
    if target is not None:
        from .difficulty import puzzle_rating
        picked = _trusted(sorted(picked, key=lambda e: (puzzle_rating(e), e.id)))
    else:
        picked = _trusted(sorted(picked, key=lambda e: (e.difficulty, e.id)))

    # Prerequisites the learner hasn't met yet: one easy demonstration each, shown first.
    prereqs: list[str] = []
    prereq_examples: list[Example] = []
    wants_prereqs = request.include_prerequisites
    if wants_prereqs is None:
        wants_prereqs = level == "beginner"
    if wants_prereqs and picked:
        met = {library.entries[eid].concept for eid in seen if eid in library.entries} | set(request.known_concepts)
        for cid in concepts:
            for pre in library.concepts[cid].prerequisites:
                if pre in prereqs or not library.count_for(pre):
                    continue
                if any(d in met for d in library.descendants(pre)):
                    continue
                taken = {e.id for e in picked + prereq_examples}
                found = _trusted(library.select(Query(concepts=[pre], count=1, simplest=True,
                                                      exclude=exclude | taken), seen, last_used))
                if found:
                    prereqs.append(pre)
                    prereq_examples += found

    sequence = [(e, "demonstration") for e in prereq_examples] + _assign_roles(picked, mode, request.roles)

    practice: list[Example] = []
    if request.practice_count and picked:
        n = max(0, min(MAX_PRACTICE, request.practice_count))
        harder = LEVELS[min(LEVELS.index(level) + 1, 2)] if level in LEVELS else None
        taken = {e.id for e, _ in sequence}
        practice_target = request.practice_rating if target is not None else None
        practice = _trusted(library.select(query(concepts, n, level=harder, mode="interactive",
                                                 exclude=taken, target=practice_target), seen, last_used))
        practice = [e for e in practice if _interactive(e)]

    related: list[str] = []
    for cid in concepts:
        for rel in library.concepts[cid].related:
            if rel not in concepts and rel not in related and library.count_for(rel):
                related.append(rel)

    return Retrieval(concepts=concepts, sequence=sequence, practice=practice, prerequisites=prereqs,
                     related=related, level=level, level_source=source, confident=True)


def _requested_count(text: str, parsed: int) -> int:
    """Examples asked for: the library's parse ("three examples"), or "2 hard checkmates"."""
    if parsed != DEFAULT_COUNT:
        return parsed
    m = _COUNT.search(text or "")
    if not m:
        return DEFAULT_COUNT
    word = m.group(1).lower()
    return int(word) if word.isdigit() else _COUNT_WORDS[word]


REAL_GAME_SOURCES = ("lichess_puzzle", "historical_game", "chesscom_game")


def _from_real_game(example: Example) -> bool:
    """Positions that arose in real games (puzzle databases, historical games), not constructed."""
    kind = (example.source or {}).get("source_type", "")
    return kind in REAL_GAME_SOURCES


def _trusted(examples: list[Example]) -> list[Example]:
    """Belt and braces: the library only indexes verified entries, but never hand out anything else."""
    return [e for e in examples if e.status == "verified" and e.tier != "personal"]


# -------------------------------------------------------------- teacher facts ----

MAX_LEGAL_MOVES = 60
MATE_CP = 9000  # engine_check stores mates as +-10000 cp


def teaching_facts(example: Example, library: KnowledgeLibrary, board: chess.Board | None = None,
                   level: str | None = None, reveal: bool = True) -> list[str]:
    """Ground truth about a verified example for the AI teacher (never the whole library).

    FEN, move sequence, key move, Stockfish verdict/eval and good alternatives, the
    concept/motif facts proven by the validators, the verified explanation, the
    learner's level, and — for the position on the board — the legal moves.
    With ``reveal=False`` (the learner is still solving) nothing that gives the
    solution away is included.
    """
    if example.status != "verified":
        return []
    concept = library.concepts.get(example.concept)
    if not reveal:
        lines = [f"Concept: {concept.name if concept else example.concept}", f"Example: {example.title}"]
        if concept:
            lines.append(f"Concept summary: {concept.summary}")
        lines.append("The student is solving this exercise right now: do NOT name or hint at the "
                     "solution move; help with questions about the idea and what to look for.")
        if example.hints:
            lines.append(f"First hint from the lesson: {example.hints[0]}")
        lines += _board_lines(board, level)
        return lines
    lines = verified_facts(example, concept.name if concept else None)
    eng = example.verification.get("engine") or {}
    key = eng.get("key_move") or eng.get("mistake")
    if key and (key.get("mate_after") or abs(key.get("eval_cp") or 0) >= MATE_CP):
        lines.append("Stockfish: the key move leads to a forced mate")
    elif key and key.get("eval_cp") is not None:
        lines.append(f"Stockfish eval after {key.get('label', key['move'])}: {key['eval_cp'] / 100:+.2f} "
                     "pawns for the side that played it")
    for later in eng.get("learner_moves") or []:
        extra = f"; also good: {', '.join(later['alternatives'])}" if later.get("alternatives") else ""
        lines.append(f"Stockfish: {later.get('label', later['move'])} is '{later['category']}'{extra}")
    if eng.get("final_eval_white") is not None:
        lines.append(f"Stockfish eval at the end of the line: {eng['final_eval_white'] / 100:+.2f} (White's view)")
    if concept:
        lines.append(f"Concept summary: {concept.summary}")
    if example.explanation:
        lines.append(f"Verified explanation: {example.explanation}")
    return lines + _board_lines(board, level, known_fen=example.start_fen)


def _board_lines(board: chess.Board | None, level: str | None, known_fen: str = "") -> list[str]:
    lines = []
    if board is not None:
        legal = sorted(board.san(m) for m in board.legal_moves)
        side = "White" if board.turn == chess.WHITE else "Black"
        shown = legal[:MAX_LEGAL_MOVES]
        more = f" (+{len(legal) - len(shown)} more)" if len(legal) > len(shown) else ""
        if board.fen() != known_fen:
            lines.append(f"Position on the board now (FEN): {board.fen()}")
        lines.append(f"Legal moves for {side} here: {', '.join(shown) or 'none'}{more}")
    if level:
        lines.append(f"Learner level: {level}")
    return lines
