"""One index of everything a learner can name, across every source we have.

Catalog topics, Knowledge Library concepts, glossary terms and opening families from
the Lichess opening database (CC0) all become Entries. Entries that name the same
thing are merged into one *target* (the Sicilian topic and the "Sicilian Defense"
database family; the "forks" topic and the `fork` concept), so "two sources know this
word" never looks like "this word has two meanings".

Matching is contiguous (a phrase must appear as consecutive words, allowing plurals and
small typos through catalog._token_match), which is stricter than the catalog's
skip-gram search on purpose: "knight endgames" is *not* found inside "knight and
bishop endgames" — that coordination is analysed by the parser instead.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from ..catalog import _FILLER, _QUALIFIER_OK, _normalize, _token_match

# Words that describe *kind* rather than name a subject. A word that is only one of these
# never makes a lexical ambiguity ("defense" is in 60 opening names — that isn't a question).
GENERIC = _QUALIFIER_OK | set("""
opening openings defense defence defenses defences game games attack attacks gambit gambits system systems
variation variations line lines mate mates checkmate checkmates checkmating mating endgame endgames ending
endings position positions tactic tactics move moves theory idea ideas plan plans structure structures
strategy strategic trick tricks trap traps pattern patterns principle principles lesson lessons course
courses puzzle puzzles exercise exercises middlegame middlegames sacrifice sacrifices main accepted declined
counter countergambit advanced intermediate hard harder difficult expert experts opposite same colored
coloured color colour minor major pair two both together separately separate without except not no but as
side perspective facing face meet meeting answer answering beat beating counter countering respond
responding versus vs v one single piece pieces pawns pawn kings queens rooks bishops knights king queen
rook bishop knight white black mistake mistakes blunder blunders concept concepts thing things stuff
something anything classic modern accelerated reversed extended declined deferred refused
""".split())

_FAMILY_SUFFIX = ("defense", "defence", "opening", "game", "attack", "gambit", "system", "variation")


@dataclass(frozen=True)
class Entry:
    kind: str          # topic | concept | glossary | opening_db
    id: str
    label: str
    category: str      # opening | tactic | checkmate | endgame | strategy | basics | mistake | idea
    target: str        # merged identity: entries with the same target mean the same thing
    phrases: tuple[tuple[str, ...], ...]
    side: str | None = None
    weight: int = 0    # opening families: how many database lines they have (a popularity signal)


@dataclass(frozen=True)
class Match:
    entry: Entry
    start: int
    end: int           # exclusive
    exact: bool        # the whole phrase is in the request (else: the request has part of it)
    phrase: tuple[str, ...] = ()   # the entry phrase that matched

    @property
    def span(self) -> frozenset[int]:
        return frozenset(range(self.start, self.end))


def _contiguous(phrase: tuple[str, ...], tokens: list[str]) -> tuple[int, int] | None:
    n = len(phrase)
    for i in range(len(tokens) - n + 1):
        if all(_token_match(phrase[k], tokens[i + k]) for k in range(n)):
            return i, i + n
    return None


def _strict(a: str, b: str) -> bool:
    """Equal or a plural (no typo tolerance: part of a name must be spelled like the name)."""
    return a == b or (len(min(a, b, key=len)) >= 3 and max(a, b, key=len) in (min(a, b, key=len) + "s",
                                                                               min(a, b, key=len) + "es"))


def _contains_strict(run: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(run)
    return any(all(_strict(run[k], phrase[i + k]) for k in range(n)) for i in range(len(phrase) - n + 1))


def _category(raw: str) -> str:
    raw = (raw or "").lower()
    for key, cat in (("opening", "opening"), ("tactic", "tactic"), ("checkmate", "checkmate"), ("mate", "checkmate"),
                     ("endgame", "endgame"), ("strateg", "strategy"), ("basic", "basics"), ("mistake", "mistake")):
        if key in raw:
            return cat
    return raw or "idea"


def family_of(name: str) -> str:
    """'Sicilian Defense: Najdorf Variation' → 'Sicilian Defense';
    "King's Indian Attack, with Bf5" → "King's Indian Attack"."""
    return name.split(":")[0].split(",")[0].strip()


def side_of_family(family: str) -> str:
    """Which side "plays" a named opening: defenses are Black's, the rest White's."""
    low = family.lower()
    return "black" if ("defense" in low or "defence" in low or "countergambit" in low) else "white"


class Lexicon:
    def __init__(self, catalog, library, glossary=None, openings=None):
        self.entries: list[Entry] = []
        # A topic and a concept are the same subject when the concept lists the topic AND they
        # share a name. Concepts also list topics
        # just for practice (stalemate lists K+Q mate): that doesn't make them one subject.
        concept_phrases = {cid: {tuple(_normalize(n)) for n in [c.name, cid.replace("_", " ")] + list(c.aliases)}
                           for cid, c in library.concepts.items()}
        owners: dict[str, list[str]] = {}
        for cid, concept in library.concepts.items():
            for tid in getattr(concept, "topics", ()) or ():
                owners.setdefault(tid, []).append(cid)
        topic_concept: dict[str, str] = {}
        for topic in catalog.topics.values():
            cands = owners.get(topic.id, [])
            tp = {tuple(_normalize(a)) for a in list(topic.aliases) + [topic.title, topic.id.replace("_", " ")]}
            named = [c for c in cands if concept_phrases[c] & tp]
            if named:
                topic_concept[topic.id] = named[0]
        topic_target: dict[tuple[str, ...], str] = {}
        for topic in catalog.topics.values():
            target = f"concept:{topic_concept[topic.id]}" if topic.id in topic_concept else f"topic:{topic.id}"
            phrases = {tuple(_normalize(a)) for a in list(topic.aliases) + [topic.title]}
            phrases.discard(())
            for p in phrases:
                topic_target.setdefault(p, target)
            self.entries.append(Entry("topic", topic.id, topic.title, _category(topic.category), target,
                                      tuple(sorted(phrases)), topic.side if topic.category == "opening" else None))
        for cid, concept in library.concepts.items():
            names = [concept.name] + list(getattr(concept, "aliases", ()) or ())
            phrases = {tuple(_normalize(n)) for n in names}
            phrases.discard(())
            self.entries.append(Entry("concept", cid, concept.name, _category(concept.category), f"concept:{cid}",
                                      tuple(sorted(phrases))))
        if glossary is not None:
            for term in glossary.terms.values():
                phrases = {tuple(_normalize(n)) for n in [term.term] + list(term.aliases)}
                phrases.discard(())
                broader = term.broader[0] if term.broader else None
                cat = _category(library.concepts[broader].category) if broader in library.concepts else "idea"
                # A glossary word that is exactly a topic/concept name is that topic, not a homonym.
                target = next((topic_target[p] for p in phrases if p in topic_target),
                              next((f"concept:{e.id}" for e in self.entries if e.kind == "concept"
                                    and set(e.phrases) & phrases), f"glossary:{term.id}"))
                self.entries.append(Entry("glossary", term.id, term.term, cat, target, tuple(sorted(phrases))))
        if openings is not None:
            sizes: dict[str, int] = {}
            for name, lines in openings.by_name.items():
                sizes[family_of(name)] = sizes.get(family_of(name), 0) + len(lines)
            for fam in sorted(sizes):
                phrase = tuple(_normalize(fam))
                if not phrase:
                    continue
                target = topic_target.get(phrase, f"opening_db:{fam}")
                self.entries.append(Entry("opening_db", fam, fam, "opening", target, (phrase,), side_of_family(fam),
                                          sizes[fam]))

    # ------------------------------------------------------------------ matching
    def exact(self, tokens: list[str]) -> list[Match]:
        """Every entry phrase found as consecutive words. Matches inside a longer match
        of a *different* target are dropped ("checkmate" inside "smothered checkmate")."""
        found: list[Match] = []
        for e in self.entries:
            best = None
            for p in e.phrases:
                hit = _contiguous(p, tokens)
                if hit and (best is None or hit[1] - hit[0] > best[1] - best[0]):
                    best = hit
            if best:
                found.append(Match(e, best[0], best[1], True))
        return [m for m in found
                if not any(o.entry.target != m.entry.target and m.span < o.span for o in found)]

    def partial(self, run: list[str]) -> list[Match]:
        """Entries with a phrase that *contains* these words (in order) plus more:
        "philidor" → Philidor Defense, the Philidor position, Philidor's legacy."""
        out = []
        n = len(run)
        for e in self.entries:
            for p in e.phrases:
                if len(p) <= n:
                    continue
                if _contains_strict(tuple(run), p):
                    out.append(Match(e, 0, n, False, p))
                    break
        return out

    def by_target(self, target: str) -> list[Entry]:
        return [e for e in self.entries if e.target == target]


def distinctive(token: str) -> bool:
    """Could this word be (part of) a name? Generic chess words and filler can't."""
    return len(token) >= 3 and not token.isdigit() and token not in GENERIC and token not in _FILLER


_lexicon: Lexicon | None = None
_lock = threading.Lock()


def get_lexicon() -> Lexicon:
    global _lexicon
    with _lock:
        if _lexicon is None:
            from ...knowledge.glossary import get_glossary
            from ...knowledge.library import get_knowledge
            from ..catalog import get_catalog
            library = get_knowledge()
            try:
                openings = library.opening_index
            except Exception:  # the opening database is optional data
                openings = None
            _lexicon = Lexicon(get_catalog(), library, get_glossary(), openings)
        return _lexicon


def reset_lexicon() -> None:
    global _lexicon
    with _lock:
        _lexicon = None
