"""Deterministic plan composition from verified content.

For each requested component the composer picks verified material and arranges it the
way every plan here is arranged: first see the idea (demonstration), then try it with
help (guided), then practise alone. Units are ordered so prerequisites come first and,
within a component, difficulty rises. Nothing that wasn't requested is added.

  topic       the catalog topic's verified lessons
  concept     verified library examples of the concept (and its sub-concepts), minus exclusions
  material    positions whose material matches (content index), topped up with newly
              generated positions for mates (parameterized constructor) — generated
              positions go through the full generation pipeline and are verified before use
  opening_as  the catalog's verified line, learned from the other side
  opening_db  the Lichess database line (CC0) — screened by Stockfish during validation
  glossary    the hand-written definition, labelled as not engine-checked
"""
from __future__ import annotations

import logging

from ..intent.model import Component, LearningIntent
from .candidate import CandidatePlan, Item, Unit
from .content import LEVEL_DIFFICULTY, ContentItem

log = logging.getLogger(__name__)

LEARN = 3       # positions in a "learn" unit
PRACTICE = 4    # positions in a "practice" unit
MIN_LINE = 6    # plies: shorter database lines are extended with the family's main continuation


def _level_filter(items: list[ContentItem], level: str | None) -> list[ContentItem]:
    if level not in LEVEL_DIFFICULTY:
        return items
    lo, hi = {"beginner": (1, 3), "intermediate": (2, 4), "advanced": (3, 5)}[level]
    fit = [i for i in items if lo <= i.difficulty <= hi]
    return fit if len(fit) >= 2 else items


def _item(ci: ContentItem, role: str) -> Item:
    ex = ci.example
    prov = "library-verified" if ci.kind == "library" else "catalog-verified"
    it = Item(kind=ci.kind, ref=ci.ref, role=role, provenance=prov, fen=ex.start_fen, moves=list(ex.moves),
              key_index=ex.key_ply, concept=ex.concept, title=ex.title, difficulty=ci.difficulty)
    it.example = ex
    return it


def _learn_practice(label: str, comp: Component, pool: list[ContentItem], objective: str) -> list[Unit]:
    """Split verified positions into a learn unit and a practice unit, easiest first."""
    pool = sorted(pool, key=lambda i: (i.difficulty, i.ref.endswith("_mistake"), i.ref))
    learn = pool[:LEARN]
    practice = pool[LEARN:LEARN + PRACTICE]
    units = []
    if learn:
        roles = ["demonstration"] + ["guided"] * (len(learn) - 1) if len(learn) > 1 else ["guided"]
        units.append(Unit(f"{label}: learn the idea", objective, comp.as_dict(), "learn",
                          [_item(ci, r) for ci, r in zip(learn, roles)]))
    if practice:
        units.append(Unit(f"{label}: practice", f"Apply it on your own: {objective[0].lower()}{objective[1:]}",
                          comp.as_dict(), "practice", [_item(ci, "practice") for ci in practice]))
    return units


def _db_line(openings, family: str) -> list[str]:
    opts = openings.by_name.get(family)
    if not opts:
        return []
    base = min(opts, key=lambda o: len(o.moves))
    line = list(base.moves)
    if len(line) < MIN_LINE:
        longer = sorted((o for name, os_ in openings.by_name.items() if name.split(":")[0].split(",")[0].strip() == family
                         for o in os_ if len(o.moves) >= MIN_LINE and tuple(o.moves[:len(line)]) == tuple(line)),
                        key=lambda o: (len(o.moves), o.name))
        if longer:
            line = list(longer[0].moves)
    return line[:14]


def compose(intent: LearningIntent, ctx, *, exclude_refs: set[str] = frozenset(), generate=None) -> CandidatePlan:
    """A candidate for `intent`. `generate(spec, n)` may return newly verified Examples for
    a material spec (mates) when verified content runs short."""
    lib, cat, content = ctx.library, ctx.catalog, ctx.content
    level = intent.level or ctx.level
    excluded_concepts: set[str] = set()
    for key in intent.exclude:
        kind, _, ident = key.partition(":")
        if kind == "concept":
            excluded_concepts |= {ident, *lib.descendants(ident)}
        elif kind == "topic":
            c = lib.concept_for_topic(ident)
            if c:
                excluded_concepts |= {c.id, *lib.descendants(c.id)}
    units: list[Unit] = []
    skipped: list[dict] = []
    notes: list[str] = []
    used: set[str] = set(exclude_refs)

    for comp in intent.components:
        if comp.kind == "topic":
            topic = cat.topics.get(comp.id)
            if topic is None:
                skipped.append({"key": comp.key(), "title": comp.label, "reason": "not in the catalog"})
                continue
            units.append(Unit(topic.title, topic.summary.split(". ")[0].rstrip(".") + ".", comp.as_dict(), "topic",
                              [Item("topic", topic.id, "lesson", "catalog-verified", title=topic.title,
                                    difficulty=LEVEL_DIFFICULTY.get(topic.level, 3))]))
        elif comp.kind == "concept":
            concept = lib.concepts.get(comp.id)
            if concept is None:
                continue
            pool = [i for i in content.concept(comp.id, used, excluded_concepts)]
            pool = _level_filter(pool, level)
            if intent.exclude and comp.id in ("tactics", "endgames", "checkmate", "openings", "basics"):
                pool = _spread(pool)
            made = _learn_practice(concept.name, comp, pool, concept.summary.split(". ")[0].rstrip(".") + ".")
            if not made:
                skipped.append({"key": comp.key(), "title": concept.name, "reason": "no verified positions yet"})
            for u in made:
                used |= {i.ref for i in u.items}
            units += made
        elif comp.kind == "material":
            spec = comp.material
            pool = [i for i in content.material(spec, used)]
            pool = _level_filter(pool, level)
            fresh = []
            if len(pool) < LEARN + 1 and generate is not None:
                fresh = generate(spec, LEARN + 2 - len(pool)) or []
            items_pool = pool
            if fresh:
                from .content import ContentItem, _key_board
                for ex in fresh:
                    board, final = _key_board(ex)
                    items_pool.append(ContentItem(ex.id, "library", ex, board, final, tuple(ex.concepts or [ex.concept]),
                                                  int(ex.difficulty or 3)))
                notes.append(f"{len(fresh)} new position{'s' if len(fresh) != 1 else ''} for “{comp.label}” "
                             "were generated and verified by python-chess + Stockfish for this plan.")
            objective = (f"Recognise and play positions with {spec.label().lower()}."
                         if spec.head == "endgame" else f"Deliver checkmate with {spec.label().lower().replace('checkmate with ', '')}.")
            made = _learn_practice(comp.label or spec.label(), comp, items_pool, objective)
            if not made:
                skipped.append({"key": comp.key(), "title": comp.label,
                                "reason": "no verified positions with this material yet"})
            for u in made:
                used |= {i.ref for i in u.items}
            units += made
        elif comp.kind == "opening_as":
            topic = cat.topics.get(comp.id)
            if topic is None or not topic.line:
                skipped.append({"key": comp.key(), "title": comp.label, "reason": "no verified line"})
                continue
            title = f"Facing the {topic.title} as {comp.side.capitalize()}"
            units.append(Unit(title, f"Know the {topic.title}'s main moves and meet them as {comp.side.capitalize()}.",
                              comp.as_dict(), "opening",
                              [Item("opening_line", f"{topic.id}@{comp.side}", "lesson", "catalog-verified",
                                    title=title, line=list(topic.line), side=comp.side,
                                    difficulty=LEVEL_DIFFICULTY.get(topic.level, 3))]))
        elif comp.kind == "opening_db":
            openings = getattr(lib, "opening_index", None)
            line = _db_line(openings, comp.id) if openings is not None else []
            if not line:
                skipped.append({"key": comp.key(), "title": comp.label, "reason": "not in the opening database"})
                continue
            side = comp.side or "white"
            units.append(Unit(comp.id, f"Learn the first moves of the {comp.id} and the ideas behind them.",
                              comp.as_dict(), "opening",
                              [Item("opening_line", f"db:{comp.id}@{side}", "lesson", "database", title=comp.id,
                                    line=line, side=side, difficulty=2)]))
        elif comp.kind == "glossary":
            from ...knowledge.glossary import get_glossary
            term = get_glossary().terms.get(comp.id)
            if term is None:
                continue
            units.append(Unit(f"{term.term}: what it is", "Understand the idea in words.", comp.as_dict(), "definition",
                              [Item("definition", term.id, "lesson", "text", title=term.term, text=term.definition)]))

    units = _order(units, ctx)
    label = intent.interpretation or ", ".join(c.label for c in intent.components[:3]) or intent.goal
    if intent.exclude:
        names = []
        for key in intent.exclude:
            kind, _, ident = key.partition(":")
            if kind == "concept" and ident in lib.concepts:
                names.append(lib.concepts[ident].name.lower())
            elif kind == "topic" and ident in cat.topics:
                names.append(cat.topics[ident].title.lower())
        if names:
            label += " — without " + ", ".join(names)
    return CandidatePlan(goal=intent.goal, intent=intent.as_dict(), title=f"Plan: {label}",
                         summary=_summary(units, skipped), units=units, proposer="composer", level=level,
                         personal=ctx.personal, learner=ctx.learner, skipped=skipped, notes=notes)


def _spread(pool: list[ContentItem]) -> list[ContentItem]:
    """Broad subjects: one position per sub-concept before any second one."""
    by: dict[str, list[ContentItem]] = {}
    for it in pool:
        by.setdefault(it.concepts[0], []).append(it)
    out = []
    while any(by.values()):
        for k in list(by):
            if by[k]:
                out.append(by[k].pop(0))
    return out


def _order(units: list[Unit], ctx) -> list[Unit]:
    """Stable topological order: a unit moves after any unit it requires; learn before practice."""
    from .validate import _requires

    out: list[Unit] = []
    pending = list(units)
    while pending:
        for u in pending:
            blockers = [v for v in pending if v is not u and v.component != u.component and _requires(ctx, u, v)]
            if not blockers:
                out.append(u)
                pending.remove(u)
                break
        else:  # a cycle in the data: keep the given order
            out += pending
            break
    return out


def _summary(units: list[Unit], skipped: list[dict]) -> str:
    parts = [u.title for u in units if u.role != "practice"]
    text = "A plan built for your request from verified material: " + "; ".join(parts[:5]) + "." if parts else ""
    if skipped:
        text += " Not included: " + "; ".join(f"{s['title']} ({s['reason']})" for s in skipped) + "."
    return text.strip()
