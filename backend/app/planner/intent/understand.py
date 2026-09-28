"""Understand a learning request: structure it, find real ambiguities, ask or resolve.

understand(goal) returns a LearningIntent or raises ClarificationNeeded(question).

General detectors, each looking at a *kind* of ambiguity rather than at examples:

  coordination  "A and B <endgames|mate>" with pieces A, B: taught separately, together
                (one side has both) or against each other. Readings that chess makes
                impossible are dropped (a lone bishop or knight can't force mate, so
                "bishop and knight checkmate" has only one reading and is not asked).
                Kings and pawns are never partners ("rook and pawn endgames" = rook
                endgames). "or", "vs", "together", "separately" settle it.
  relation      "together" and "separately" both said about the same pieces.
  lexical       a name that is only *part* of several unrelated subjects ("Philidor":
                an opening, an endgame position, a mating pattern). Plural family
                nouns ("Indian defenses") mean the whole family and are not asked.
  side          a named opening that belongs to the other side ("the Sicilian as
                White"). "against/facing the Sicilian" is not ambiguous: face it.
  level         two different levels in one request.
  exclusion     leaving out something that contains what was asked for ("forks but no
                tactics").
  vague         a request with no subject at all ("teach me something").

A question is asked only when at least two readings produce *different plans*
(different components or level); a remembered answer to the same ambiguity key is
reused; an answer given in this request is applied. Qwen never resolves anything here —
missing.qwen_candidates() + qwen_question() turn its suggestions into a question, never a
silent choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..catalog import _FILLER, _normalize
from .lexicon import GENERIC, Entry, Lexicon, Match, distinctive, get_lexicon, side_of_family
from .memory import IntentMemory
from .model import (ICON, LETTER_NAME, OTHER, ClarificationError, ClarificationNeeded, Component,
                    Interpretation, LearningIntent, MaterialSpec, Question, can_force_mate)
from .parse import MaterialPhrase, Parsed, parse

_NEGATION = {"without", "except", "for", "excluding", "but", "not", "no", "skip", "skipping", "the", "any", "about"}
_KIND_RANK = {"topic": 0, "concept": 1, "opening_db": 2, "glossary": 3}
_PLURAL_FAMILY = {"defenses", "defences", "openings", "gambits", "attacks", "systems", "games", "variations"}
_VAGUE = {"something", "stuff", "anything", "things", "thing", "chess", "skills", "skill", "useful", "interesting",
          "else", "today", "now", "new", "lesson", "lessons", "plan", "course"}
# Everyday words that also occur inside opening names. They are only a question when the
# learner wrote them as a name (capitalised) — "control the center" is not the Center Game.
_COMMON = {"center", "centre", "control", "space", "open", "closed", "old", "modern", "classical", "fast", "quick",
           "early", "english", "normal", "standard", "regular", "super", "mexican", "fork", "liver",
           "exchange", "advance", "symmetrical", "reversed", "hyper", "orthodox", "semi", "neo", "anti", "wing",
           "rat", "hippo", "polish", "robatsch", "war", "attacking", "attack", "positional", "defending",
           "defensive", "tactical", "strategic", "aggressive", "solid", "sharp", "quiet", "dynamic", "calculation",
           "calculating", "thinking", "style", "playing", "universal", "flexible", "simple", "simpler"}
_CATEGORY_HINT = {"opening": "an opening", "tactic": "a tactic", "checkmate": "a checkmate pattern",
                  "endgame": "an endgame", "strategy": "a strategy idea", "basics": "a basic idea",
                  "mistake": "a common mistake", "idea": "a chess idea"}


@dataclass
class _Reading:
    """A detector's result for one span of the request."""
    key: str | None                     # ambiguity key (None: nothing to ask)
    kind: str
    term: str
    span: frozenset[int]
    options: list[Interpretation]       # 1 option = resolved
    text: str = ""
    remember: bool = True
    exclude: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- components
def component_for(entry: Entry, lexicon: Lexicon | None = None) -> Component:
    if entry.kind == "opening_db" and entry.target.startswith(("topic:", "concept:")) and lexicon is not None:
        best = min(lexicon.by_target(entry.target), key=lambda e: _KIND_RANK[e.kind])
        if best.kind != "opening_db":
            return component_for(best)
    if entry.kind == "opening_db":
        return Component("opening_db", entry.id, entry.label, side=entry.side or side_of_family(entry.id))
    return Component(entry.kind, entry.id, entry.label)


def _best_entries(matches: list[Match]) -> list[Match]:
    """One match per target, preferring kinds that have verified lessons."""
    best: dict[str, Match] = {}
    for m in matches:
        cur = best.get(m.entry.target)
        if cur is None or _KIND_RANK[m.entry.kind] < _KIND_RANK[cur.entry.kind]:
            best[m.entry.target] = m
    return sorted(best.values(), key=lambda m: m.start)


def _single_piece(letter: str, head: str, lexicon: Lexicon) -> Component:
    """"knight" + endgame → the Knight endgames topic when one exists, else material."""
    name = LETTER_NAME[letter]
    for words in ([name, "endgames"], [name, "endgame"]) if head == "endgame" else \
            ([name, "mate"], [name, "checkmate"], ["king", "and", name, "vs", "king"]):
        hits = [m for m in lexicon.exact(words) if m.entry.kind == "topic" and m.start == 0 and m.end == len(words)]
        if hits:
            e = hits[0].entry
            return Component("topic", e.id, e.label)
    spec = MaterialSpec((letter,), "only", head)
    return Component("material", spec.label(), spec.label(), spec)


def _material_component(spec: MaterialSpec) -> Component:
    return Component("material", spec.label(), spec.label(), spec)


# ---------------------------------------------------------------------- detectors
def _coordination(p: Parsed, lexicon: Lexicon) -> list[_Reading]:
    out = []
    for mp in p.material:
        span = frozenset(range(mp.start, mp.end))
        real = mp.real
        head = mp.head
        term = mp.words
        if mp.group in ("minor", "major"):
            spec = MaterialSpec(tuple(real), "any", head)
            label = f"{mp.group.capitalize()}-piece {'endgames' if head == 'endgame' else 'checkmates'}"
            out.append(_Reading(None, "coordination", term, span,
                                [Interpretation("any", label, [Component("material", label, label, spec)],
                                                "".join(ICON[x] for x in real))]))
            continue
        if mp.group in ("opposite", "same"):
            spec = MaterialSpec(("B", "B"), "versus", "endgame", mp.group)
            out.append(_Reading(None, "coordination", term, span,
                                [Interpretation("versus", spec.label(), [_material_component(spec)], "♝ vs ♝")]))
            continue
        if mp.group == "pair":
            spec = MaterialSpec(tuple(real), "together", head)
            out.append(_Reading(None, "coordination", term, span,
                                [Interpretation("together", spec.label(), [_material_component(spec)], spec.icon())]))
            continue
        if len(real) < 2:
            continue  # "rook and pawn endgames" is rook endgames: the catalog knows it
        pieces = tuple(real)
        options: list[Interpretation] = []
        distinct = list(dict.fromkeys(pieces))
        if "vs" in mp.joins and len(pieces) == 2:
            spec = MaterialSpec(pieces, "versus", "endgame")
            options = [Interpretation("versus", spec.label(), [_material_component(spec)], spec.icon())]
        else:
            comps = [_single_piece(x, head, lexicon) for x in distinct]
            separate = Interpretation(
                "separate",
                " and ".join(f"{ICON[x]} " + (c.label if i == 0 else c.label[0].lower() + c.label[1:])
                             for i, (x, c) in enumerate(zip(distinct, comps))) + ", separately",
                comps, "  ".join(ICON[x] for x in distinct))
            together_spec = MaterialSpec(tuple(sorted(pieces)), "together", head)
            written = MaterialSpec(pieces, "together", head).label()  # the learner's word order
            together = Interpretation("together", written,
                                      [Component("material", written, written, together_spec)],
                                      "".join(ICON[x] for x in pieces))
            versus = None
            if len(distinct) == 2 and len(pieces) == 2 and head == "endgame":
                vs_spec = MaterialSpec(pieces, "versus", "endgame")
                versus = Interpretation("versus", vs_spec.label(), [_material_component(vs_spec)], vs_spec.icon())
            if "or" in mp.joins or p.separate and not p.together:
                options = [separate]
            elif p.together and not p.separate:
                options = [together]
            else:
                options = [separate, together] + ([versus] if versus else [])
                if head == "mate":  # chess decides which readings exist
                    options = [o for o in options
                               if (o.id == "separate" and all(can_force_mate((x,)) for x in distinct))
                               or (o.id == "together" and can_force_mate(pieces))]
                    options = options or [together]
        key = f"coordination:{'+'.join(sorted(pieces))}:{head}"
        if p.together and p.separate and len(options) > 1:
            out.append(_Reading(f"relation:{'+'.join(sorted(pieces))}:{head}", "relation", term, span,
                                [o for o in options if o.id in ("separate", "together")],
                                f"You wrote both “together” and “separately” — how should “{term}” be taught?"))
            continue
        out.append(_Reading(key, "coordination", term, span, options, f"What do you mean by “{term}”?"))
    return out


def _option_label(m: Match) -> str:
    entry = m.entry
    hint = _CATEGORY_HINT.get(entry.category, "a chess idea")
    named = m.phrase and _normalize(entry.label) != list(m.phrase)
    if named and not set(_normalize(entry.label)) >= set(m.phrase):
        return f"{' '.join(m.phrase).capitalize()} — {entry.label} ({hint})"
    return f"{entry.label} ({hint})"


def _lexical(p: Parsed, lexicon: Lexicon, exact: list[Match], taken: frozenset[int]) -> tuple[list[_Reading], list[Component]]:
    """Partial names. Returns (readings, weak suggestions for single partial matches)."""
    readings: list[_Reading] = []
    weak: list[Component] = []
    tokens = p.tokens
    covered = set(taken)
    for m in exact:
        covered |= m.span
    # Plural family nouns: "Indian defenses" = every Indian defense, not one of them.
    for m in exact:
        if m.entry.category != "opening" or m.end - m.start < 2:
            continue
        last = tokens[m.end - 1]
        if last in _PLURAL_FAMILY:
            stem = tokens[m.start:m.end - 1]
            family = [x for x in lexicon.partial(stem) if x.entry.category == "opening"] + [m]
            best = _best_entries(family)
            if len(best) >= 2:
                comps = [component_for(x.entry, lexicon) for x in
                         sorted(best, key=lambda x: (_KIND_RANK[x.entry.kind], x.entry.label))][:5]
                label = f"{' '.join(stem).title()} {last}"
                readings.append(_Reading(None, "family", " ".join(tokens[m.start:m.end]), m.span,
                                         [Interpretation("family", label, comps)]))
    has_subject = bool(exact) or bool(p.material)
    i, n = 0, len(tokens)
    while i < n:
        if i in covered or not distinctive(tokens[i]):
            i += 1
            continue
        j = i
        while j < n and j not in covered and distinctive(tokens[j]):
            j += 1
        run = tokens[i:j]
        span = frozenset(range(i, j))
        i = j
        matches = [m for m in lexicon.partial(run)]
        best = _best_entries(matches)
        if not best:
            continue
        capitalised = any(w[:1].isupper() for w in p.text.split() if _normalize(w) and _normalize(w)[0] in run)
        if len(best) == 1:
            weak.append(component_for(best[0].entry, lexicon))
            continue
        common = all(t in _COMMON for t in run)
        if (has_subject or common) and not capitalised:
            continue  # a stray word next to a clear subject is not a question
        ranked = sorted(best, key=lambda m: (_KIND_RANK[m.entry.kind], -m.entry.weight, len(m.entry.label)))[:4]
        term = " ".join(run)
        options = [Interpretation(f"opt{k + 1}", _option_label(m), [component_for(m.entry, lexicon)])
                   for k, m in enumerate(ranked)]
        readings.append(_Reading(f"lexical:{term}", "lexical", term, span, options,
                                 f"Which “{term.title()}” do you mean?"))
    return readings, weak


def _side(p: Parsed, catalog, lexicon: Lexicon, exact: list[Match]) -> list[_Reading]:
    out = []
    found = [t for t in catalog.search(p.text) if t.category == "opening" and t.side]
    if not found:
        return out
    spans = {m.entry.id: m for m in exact if m.entry.kind == "topic"}
    same_side = [t for t in found if p.side and t.side == p.side]
    if same_side:
        return out  # something on the requested side was asked for: no conflict
    for topic in found:
        m = spans.get(topic.id)
        span = m.span if m else frozenset()
        facing = m is not None and any(0 < m.start - f <= 3 for f in p.facing_at)
        other = "white" if topic.side == "black" else "black"
        flipped = Component("opening_as", topic.id, f"Facing the {topic.title} as {other.capitalize()}", side=other)
        own = Component("topic", topic.id, topic.title)
        if facing and (p.side in (None, other)):
            out.append(_Reading(None, "side", topic.title, span,
                                [Interpretation("face", flipped.label, [flipped])]))
        elif p.side and p.side != topic.side:
            out.append(_Reading(
                f"side:{topic.id}:{p.side}", "conflict_side", topic.title, span,
                [Interpretation("own", f"Learn to play the {topic.title} — it's {topic.side.capitalize()}'s opening",
                                [own], "♔" if topic.side == "white" else "♚"),
                 Interpretation("face", f"Learn how to face the {topic.title} as {p.side.capitalize()}",
                                [flipped], "♔" if p.side == "white" else "♚")],
                f"The {topic.title} is played by {topic.side.capitalize()}. What would you like?"))
    return out


def _levels(p: Parsed, exact: list[Match], negated: set[str]) -> list[_Reading]:
    in_names = set()
    for m in exact:
        in_names |= set(p.tokens[m.start:m.end])
    levels = []
    for lvl, word in zip(p.levels, p.level_words):
        if word in in_names or word in negated:
            continue
        levels.append(lvl)
    levels = list(dict.fromkeys(levels))
    if len(levels) == 1:
        return [_Reading(None, "level", levels[0], frozenset(), [Interpretation(levels[0], levels[0], [], level=levels[0])])]
    if len(levels) >= 2:
        return [_Reading(f"level:{'+'.join(sorted(levels))}", "conflict_level", " / ".join(levels), frozenset(),
                         [Interpretation(lvl, f"{lvl.capitalize()} level", [], level=lvl) for lvl in levels],
                         "You mentioned more than one level. Which one fits you?")]
    return []


def _exclusions(p: Parsed, lexicon: Lexicon, library) -> tuple[list[str], frozenset[int], set[str]]:
    """(excluded target keys, token span of the exclusion phrases, negated words)."""
    excluded: list[str] = []
    span: set[int] = set()
    negated: set[str] = set()
    for phrase in p.exclude_phrases:
        words = [w for w in _normalize(phrase) if w not in ("a", "an", "the", "any")]
        if not words:
            continue
        negated |= set(words)
        hits = [m for m in lexicon.exact(words) if m.start == 0]
        if not hits:
            continue
        m = max(hits, key=lambda h: h.end)
        excluded.append(m.entry.target)
        # locate the phrase in the request tokens
        toks = p.tokens
        for s in range(len(toks) - len(words) + 1):
            if toks[s:s + len(words)] == words:
                b = s
                while b > 0 and toks[b - 1] in _NEGATION:
                    b -= 1
                span |= set(range(b, s + m.end))
                break
    return excluded, frozenset(span), negated


def _under(target: str, ancestor: str, library, lexicon: Lexicon) -> bool:
    """Is `target` the same as, or a kind of, `ancestor`?"""
    if target == ancestor:
        return True

    def concepts_of(t: str) -> set[str]:
        if t.startswith("concept:"):
            return {t.split(":", 1)[1]}
        if t.startswith("topic:"):
            tid = t.split(":", 1)[1]
            return {cid for cid, c in library.concepts.items() if tid in (c.topics or [])}
        return set()

    anc = concepts_of(ancestor)
    for c in concepts_of(target):
        for a in anc:
            if c == a or c in library.descendants(a):
                return True
    # category words: "tactics" excludes every tactic topic
    anc_entries = lexicon.by_target(ancestor)
    tgt_entries = lexicon.by_target(target)
    if anc_entries and tgt_entries:
        cat = {e.category for e in anc_entries if e.kind == "concept" and e.id in ("tactics", "endgames", "openings",
                                                                                      "checkmate", "strategy")}
        if cat and {e.category for e in tgt_entries} & cat:
            return True
    return False


# ---------------------------------------------------------------------- main entry
def _answer_for(key: str | None, answers: dict, memory: IntentMemory | None, reclarify: bool) -> tuple[dict | None, bool]:
    if key is None:
        return None, False
    if key in answers:
        return answers[key], False
    if memory is not None and not reclarify:
        got = memory.get(key)
        if got:
            return got, True
    return None, False


def understand(goal: str, *, answers: dict | None = None, memory: IntentMemory | None = None,
               reclarify: bool = False, catalog=None, library=None, lexicon: Lexicon | None = None,
               _depth: int = 0) -> LearningIntent:
    """Structure `goal`; raise ClarificationNeeded when it has several different readings."""
    from ...knowledge.library import get_knowledge
    from ..catalog import get_catalog

    catalog = catalog or get_catalog()
    library = library or get_knowledge()
    lexicon = lexicon or get_lexicon()
    answers = dict(answers or {})
    # A reading the learner chose among Qwen's suggestions (asked earlier for these words).
    qkey = qwen_key(goal)
    answer, remembered = _answer_for(qkey, answers, memory, reclarify)
    if answer is not None:
        if answer.get("choice") == OTHER:
            text = (answer.get("text") or "").strip()
            if not text:
                raise ClarificationError("Tell me in a few words what you meant.")
            if _depth < 2:
                inner = understand(text, answers={k: v for k, v in answers.items() if k != qkey}, memory=memory,
                                   reclarify=reclarify, catalog=catalog, library=library, lexicon=lexicon,
                                   _depth=_depth + 1)
                inner.clarified.insert(0, {"key": qkey, "kind": "interpretation", "term": goal, "choice": OTHER,
                                           "text": text, "original": goal, "remembered": remembered})
                if memory is not None and not remembered:
                    memory.put(qkey, OTHER, "Something else", text, goal)
                return inner
        comp = _component_from_key(str(answer.get("choice", "")), library, catalog)
        if comp is None and not remembered:
            raise ClarificationError(f"“{answer.get('choice')}” is not one of the choices.")
        if comp is not None:
            if memory is not None and not remembered:
                memory.put(qkey, comp.key(), comp.label, None, goal)
            return LearningIntent(goal, [comp], interpretation=comp.label,
                                  clarified=[{"key": qkey, "kind": "interpretation", "term": goal,
                                              "choice": comp.key(), "label": comp.label, "remembered": remembered}],
                                  source="remembered" if remembered else "clarified")
    p = parse(goal)
    exact = lexicon.exact(p.tokens)

    excluded, excl_span, negated = _exclusions(p, lexicon, library)
    material_span = frozenset().union(*[frozenset(range(m.start, m.end)) for m in p.material]) if p.material else frozenset()
    # A subject inside the exclusion phrase or inside a material phrase isn't a separate subject.
    subjects = [m for m in exact if not (m.span & excl_span) and not (m.span & material_span)
                and m.entry.target not in excluded]
    subjects = [m for m in subjects if not all(t in GENERIC for t in p.tokens[m.start:m.end])
                or m.entry.kind in ("topic", "concept")]

    readings: list[_Reading] = []
    readings += _coordination(p, lexicon)
    lex_readings, weak = _lexical(p, lexicon, subjects, material_span | excl_span)
    readings += lex_readings
    readings += _side(p, catalog, lexicon, subjects)
    readings += _levels(p, exact, negated)

    # Exclusion conflicts: something asked for lies under something excluded.
    for m in list(subjects):
        for ex in excluded:
            if _under(m.entry.target, ex, library, lexicon):
                ex_label = lexicon.by_target(ex)[0].label if lexicon.by_target(ex) else ex
                comp = component_for(m.entry, lexicon)
                readings.append(_Reading(
                    f"exclude:{m.entry.target}:{ex}", "conflict_exclusion", f"{m.entry.label} / {ex_label}", m.span,
                    [Interpretation("keep", f"Just {m.entry.label.lower()} — none of the other {ex_label.lower()}",
                                    [comp]),
                     Interpretation("drop", f"Leave out all {ex_label.lower()}, including {m.entry.label.lower()}",
                                    [])],
                    f"You asked for {m.entry.label.lower()} but also to leave out {ex_label.lower()}. Which do you mean?",
                    exclude=[ex]))

    # Vague: no subject of any kind.
    if not exact and not p.material and not lex_readings and not weak and not catalog.search(goal):
        content = [t for t in p.tokens if t not in _FILLER]
        if p.tokens and all(t in _VAGUE for t in content):
            cats = [("openings", "Openings — how to start the game", "♙"),
                    ("tactics", "Tactics — forks, pins and other tricks", "⚔️"),
                    ("checkmate", "Checkmates — finishing the game", "♚"),
                    ("endgames", "Endgames — winning with few pieces left", "♔")]
            opts = [Interpretation(cid, label, [Component("concept", cid, label.split(" — ")[0])], icon)
                    for cid, label, icon in cats if cid in library.concepts]
            if len(opts) >= 2:
                readings.append(_Reading("vague", "vague", goal.strip(), frozenset(), opts,
                                         "What would you like to work on?", remember=False))

    intent = LearningIntent(goal, side=p.side)
    chosen: list[tuple[int, list[Component]]] = []
    consumed: set[int] = set(material_span | excl_span)
    structured = bool(excluded)
    for r in readings:
        opts = _distinct(r.options)
        if len(opts) == 1 or r.key is None:
            pick = opts[0]
            record = None
        else:
            answer, remembered = _answer_for(r.key, answers, memory if r.remember else None, reclarify)
            if answer is None:
                raise ClarificationNeeded(Question(r.key, r.kind, r.term, r.text, opts), goal)
            if answer.get("choice") == OTHER:
                text = (answer.get("text") or "").strip()
                if not text:
                    raise ClarificationError("Tell me in a few words what you meant.")
                if _depth >= 2:
                    raise ClarificationError("I still can't tell what you mean — try naming the topic directly.")
                inner = understand(text, answers={k: v for k, v in answers.items() if k != r.key}, memory=memory,
                                   reclarify=reclarify, catalog=catalog, library=library, lexicon=lexicon,
                                   _depth=_depth + 1)
                inner.clarified.insert(0, {"key": r.key, "kind": r.kind, "term": r.term, "choice": OTHER,
                                           "text": text, "original": goal, "remembered": remembered})
                inner.source = "remembered" if remembered else "clarified"
                if memory is not None and r.remember and not remembered:
                    memory.put(r.key, OTHER, "Something else", text, r.term)
                return inner
            pick = next((o for o in opts if o.id == answer.get("choice")), None)
            if pick is None:
                if remembered:  # the options changed since: ask again
                    raise ClarificationNeeded(Question(r.key, r.kind, r.term, r.text, opts), goal)
                raise ClarificationError(f"“{answer.get('choice')}” is not one of the choices.")
            record = {"key": r.key, "kind": r.kind, "term": r.term, "choice": pick.id, "label": pick.label,
                      "remembered": remembered}
            intent.clarified.append(record)
            if memory is not None and r.remember and not remembered:
                memory.put(r.key, pick.id, pick.label, None, r.term)
        if pick.level:
            intent.level = pick.level
        if r.kind in ("level", "conflict_level"):
            continue
        consumed |= set(r.span)
        if r.kind == "conflict_exclusion":
            if pick.id == "drop":
                structured = True
                subjects = [m for m in subjects if m.span != r.span]
            else:
                excluded = [e for e in excluded if e not in r.exclude]
                structured = structured or bool(excluded)
                chosen.append((min(r.span) if r.span else 0, pick.components))
            continue
        if record is not None or any(c.kind in ("material", "opening_as") for c in pick.components) \
                or r.kind in ("family", "side", "coordination", "relation"):
            structured = True
            chosen.append((min(r.span) if r.span else 0, pick.components))
            if record is not None:
                intent.interpretation = pick.label

    if structured:
        for m in _best_entries([m for m in subjects if not (m.span & consumed)]):
            chosen.append((m.start, [component_for(m.entry, lexicon)]))
        comps: list[Component] = []
        for _, cs in sorted(chosen, key=lambda x: x[0]):
            for c in cs:
                if c.key() not in {x.key() for x in comps}:
                    comps.append(c)
        intent.components = comps
        intent.exclude = excluded
        if not comps and excluded:
            intent.components = []
    if intent.clarified:
        intent.source = "remembered" if all(c.get("remembered") for c in intent.clarified) else "clarified"
    # Openings only the Lichess database names ("the Dutch Defense"): exact names first, then single
    # partial matches. The planner builds a Stockfish-screened custom plan from them when neither
    # the library nor the catalog has lessons — a recognised opening is never a dead end.
    named = [component_for(m.entry, lexicon) for m in _best_entries(subjects) if m.entry.kind == "opening_db"]
    intent.suggested = named + [w for w in weak if w.key() not in {c.key() for c in named}]  # type: ignore[attr-defined]
    return intent


def _distinct(options: list[Interpretation]) -> list[Interpretation]:
    """Readings that would give the same plan are one reading."""
    seen, out = set(), []
    for o in options:
        sig = o.signature()
        if sig not in seen:
            seen.add(sig)
            out.append(o)
    return out


# ---------------------------------------------------------------------- Qwen
def qwen_question(goal: str, ids: list[str], library, catalog, glossary=None) -> Question | None:
    """Qwen mapped an unknown request to several of our subjects: ask, never pick.

    `ids` are concept or topic ids Qwen suggested (already restricted to ids we know).
    With one id there's nothing to choose; with several distinct ones the learner does."""
    options: list[Interpretation] = []
    for cid in dict.fromkeys(ids):
        comp = _component_from_key(cid if ":" in cid else _key_for(cid, library, catalog, glossary), library,
                                   catalog, glossary)
        if comp is not None:
            # the option id *is* the subject, so the answer can be applied without asking Qwen again
            options.append(Interpretation(comp.key(), comp.label, [comp]))
    options = _distinct(options)[:4]
    if len(options) < 2:
        return None
    term = goal.strip()
    return Question(qwen_key(goal), "interpretation", term,
                    f"“{term}” could mean a few different things. Which one?", options)


def qwen_key(goal: str) -> str:
    return f"qwen:{' '.join(_normalize(goal))}"


def _key_for(cid: str, library, catalog, glossary) -> str:
    if cid in library.concepts:
        return f"concept:{cid}"
    if cid in catalog.topics:
        return f"topic:{cid}"
    return f"glossary:{cid}"


def _component_from_key(key: str, library, catalog, glossary=None) -> Component | None:
    kind, _, ident = key.partition(":")
    if kind == "concept" and ident in library.concepts:
        return Component("concept", ident, library.concepts[ident].name)
    if kind == "topic" and ident in catalog.topics:
        return Component("topic", ident, catalog.topics[ident].title)
    if kind == "glossary":
        if glossary is None:
            from ...knowledge.glossary import get_glossary
            glossary = get_glossary()
        if ident in glossary.terms:
            return Component("glossary", ident, glossary.terms[ident].term)
    return None
