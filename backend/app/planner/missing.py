"""When the verified library has nothing for a request: never a dead end, never a guess.

Order (after the library and the catalog have both come up empty):

1. Work out what was asked for: a concept in the graph, a glossary term, or — with
   Qwen connected — Qwen maps the words to one of *our* ids (a language task only; its
   answer must be an existing id, and "sort of related" doesn't count).
2. Generate new positions for the concept (generation.generate: python-chess + Stockfish
   + concept checks, ~20 s budget). Only verified positions are used.
3. Verified examples of a broader concept, clearly labelled as broader.
4. The hand-written definition, labelled "not engine-checked", plus related verified
   material.

Returns None only when the request names nothing we recognise (then the caller keeps
its "I don't have that" answer with suggestions).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .knowledge_lessons import CATEGORY, KNOWLEDGE_VERIFIED_BY, knowledge_lesson

log = logging.getLogger(__name__)

GENERATE_COUNT = 3
GENERATE_BUDGET = 20.0  # seconds; the plan request streams progress meanwhile
TEXT_VERIFIED_BY = "hand-written definition (not engine-checked)"
GENERATED_VERIFIED_BY = "new positions checked by python-chess + Stockfish"
UNCHECKED_NOTE = ("This explanation is a hand-written definition. It hasn't been checked by the chess engine, "
                  "and I don't have engine-checked positions for it yet.")


@dataclass
class Resolution:
    term: str                 # what we understood, in words
    concept: str | None       # concept id (generation / examples)
    text: str                 # the definition shown
    text_source: str          # "concept" | "glossary"
    broader: str | None       # concept whose examples cover the term only in general
    related: list[str]        # concept ids worth practising next
    via: str                  # "text" | "qwen"
    practice: dict | None = None  # glossary skill terms: which broader positions practise it


def _qwen_map(goal: str, library, glossary, teacher=None) -> tuple[str, str] | None:
    """The one id Qwen maps the request to — or None, including when Qwen sees several
    readings: choosing between them is the learner's decision, never Qwen's."""
    found = qwen_candidates(goal, library, glossary, teacher)
    return found[0] if len(found) == 1 else None


def qwen_candidates(goal: str, library, glossary, teacher=None) -> list[tuple[str, str]]:
    """Every known term Qwen says the request could exactly mean: [(kind, id)], at most 3.
    A language task only: ids we don't have are dropped, never invented."""
    from ..config import get_settings
    from ..teacher.qwen import QwenTeacher, TeacherUnavailable
    from .planner import extract_json

    if teacher is None:
        if not get_settings().qwen_configured():
            return []
        try:
            teacher = QwenTeacher()
        except TeacherUnavailable:
            return []
    options = [f"{c.id}: {c.name}" for c in library.concepts.values()]
    options += [f"{t.id}: {t.term}" for t in glossary.terms.values()]
    messages = [
        {"role": "system", "content": "You map a chess student's request to a known chess term. Answer with one "
                                      "JSON object only."},
        {"role": "user", "content": (
            f"Request: {goal!r}\nKnown terms (id: name):\n" + "\n".join(options) + "\n\n"
            'Answer {"ids": ["<id>", ...], "match": "same"} listing every term the request could mean exactly '
            '(a synonym, translation or misspelling) — usually one, at most 3 if the words really are ambiguous. '
            'Otherwise answer {"ids": [], "match": "none"}. Never list a term that is merely related or more '
            "general, and never choose between readings: list them all.")},
    ]
    try:
        data = extract_json(teacher.complete(messages, max_tokens=80))
    except (TeacherUnavailable, ValueError) as exc:
        log.info("Qwen term mapping unavailable: %s", exc)
        return []
    if data.get("match") != "same":
        return []
    ids = data.get("ids")
    if ids is None and isinstance(data.get("id"), str):
        ids = [data["id"]]
    out: list[tuple[str, str]] = []
    for cid in ids if isinstance(ids, list) else []:
        if not isinstance(cid, str):
            continue
        if cid in library.concepts:
            out.append(("concept", cid))
        elif cid in glossary.terms:
            out.append(("glossary", cid))
        # an id we don't have: ignored, never invented
    return list(dict.fromkeys(out))[:3]


def resolve(goal: str, library, glossary, use_qwen: bool = True, teacher=None,
            clarify: bool = False) -> Resolution | None:
    term = glossary.match(goal)  # glossary terms are the named ideas the graph lacks ("greek gift")
    concept = None if term else next(iter(library.match_concepts(goal)), None)
    via = "text"
    if term is None and concept is None and use_qwen:
        found = qwen_candidates(goal, library, glossary, teacher)
        if len(found) >= 2:
            if not clarify:
                return None  # several readings and nobody to ask: don't guess
            from .catalog import get_catalog
            from .intent import ClarificationNeeded, qwen_question
            question = qwen_question(goal, [i for _, i in found], library, get_catalog(), glossary)
            if question is not None:
                raise ClarificationNeeded(question, goal)
        mapped = found[0] if len(found) == 1 else None
        if mapped:
            via = "qwen"
            if mapped[0] == "concept":
                concept = mapped[1]
            else:
                term = glossary.terms[mapped[1]]
    if term is not None:
        return Resolution(term=term.term, concept=None, text=term.definition, text_source="glossary",
                          broader=term.broader if term.broader in library.concepts else None,
                          related=[c for c in term.related if c in library.concepts], via=via,
                          practice=term.practice)
    if concept is not None:
        c = library.concepts[concept]
        return Resolution(term=c.name, concept=concept, text=c.summary, text_source="concept",
                          broader=_broader_with_examples(library, concept),
                          related=[r for r in c.related if r in library.concepts], via=via)
    return None


def _broader_with_examples(library, concept_id: str) -> str | None:
    """The nearest ancestor concept that has verified examples."""
    queue, seen = list(library.concepts[concept_id].parents), set()
    while queue:
        cid = queue.pop(0)
        if cid in seen or cid not in library.concepts:
            continue
        seen.add(cid)
        if library.count_for(cid):
            return cid
        queue.extend(library.concepts[cid].parents)
    return None


LEVEL_TARGET = {"beginner": 900, "intermediate": 1300, "advanced": 1700}  # mid-band (learner.rating.LEVEL_BANDS)
SKILL_EXAMPLES = 4


REACH = 250  # a skill position more than this above the learner's target is out of reach


def skill_examples(library, concept: str, practice: dict, target: int, usage=None,
                   count: int = SKILL_EXAMPLES) -> tuple[list, int]:
    """Verified global positions of `concept` (and its children) that practise a skill, and the
    number of the learner's own moves each needs. At least `min_learner_moves` — relaxed (never
    below 2) when no such position is within reach of the target, since longer lines are
    inherently harder. Nearest the target, unseen first, one concept at a time so the set
    isn't four of the same trick."""
    from ..knowledge.difficulty import _learner_moves, puzzle_rating

    seen = set(usage.last_used()) if usage is not None and hasattr(usage, "last_used") else set()
    candidates: dict[str, object] = {}
    for cid in [concept, *library.descendants(concept)]:
        for ex in library.examples_for(cid):
            if ex.status == "verified" and ex.tier != "personal" and _learner_moves(ex) >= 2:
                candidates[ex.id] = ex
    need = int(practice.get("min_learner_moves", 2))
    while True:
        usable = [e for e in candidates.values() if _learner_moves(e) >= need]
        close = any(puzzle_rating(e) <= target + REACH for e in usable)
        if close or need <= 2:
            break
        need -= 1
    pools: dict[str, list] = {}
    for ex in usable:
        pools.setdefault(ex.concepts[0] if ex.concepts else ex.concept, []).append(ex)
    for pool in pools.values():
        pool.sort(key=lambda e: (e.id in seen, abs(puzzle_rating(e) - target), e.id))
    order = sorted(pools, key=lambda c: (pools[c][0].id in seen, abs(puzzle_rating(pools[c][0]) - target), c))
    picked = []
    while len(picked) < count and any(pools[c] for c in order):
        for cid in order:
            if pools[cid] and len(picked) < count:
                picked.append(pools[cid].pop(0))
    return sorted(picked, key=puzzle_rating), need


def _text_lesson(lesson_id: str, title: str, text: str, related_names: list[str]) -> dict:
    from ..lessons.schema import parse_lesson

    body = f"{text}\n\n{UNCHECKED_NOTE}"
    if related_names:
        body += " Related ideas I can show you with checked positions: " + ", ".join(related_names) + "."
    lesson = {"id": lesson_id, "title": title, "description": text.split(". ")[0][:160], "difficulty": "beginner",
              "concepts": [], "steps": [{"type": "teach", "text": body}],
              "completion": {"text": "That's the idea in words. Practise the related topics to see it on the board."}}
    parse_lesson(lesson, course_id="_generated")
    return lesson


def fallback_plan(goal: str, library=None, catalog=None, engine=None, level: str | None = None,
                  use_qwen: bool = True, teacher=None, usage=None, generate_budget: float = GENERATE_BUDGET,
                  on_progress=None, engine_factory=None, clarify: bool = False,
                  target_rating: int | None = None) -> dict | None:
    """A plan for a request the verified library and the catalog couldn't serve."""
    from ..knowledge.generation import generate, supported
    from ..knowledge.glossary import get_glossary
    from ..knowledge.library import get_knowledge
    from ..knowledge.retrieval import RetrievalRequest, retrieve
    from ..knowledge.usage import get_usage
    from .catalog import get_catalog

    library = library or get_knowledge()
    catalog = catalog or get_catalog()
    usage = usage if usage is not None else get_usage()
    res = resolve(goal, library, get_glossary(), use_qwen=use_qwen, teacher=teacher, clarify=clarify)
    if res is None:
        return None

    plan_id = uuid.uuid4().hex[:8]
    units: list[dict] = []
    lessons: list[dict] = []
    notes: list[str] = []

    def unit(title, category, reason, new_lessons, verified_by, **extra):
        units.append({"topic_id": None, "title": title, "category": category,
                      "reason": f"Step {len(units) + 1}: {reason}", "verified_by": verified_by,
                      "lesson_ids": [lesson["id"] for lesson in new_lessons], **extra})
        lessons.extend(new_lessons)

    concept = library.concepts.get(res.concept) if res.concept else None
    category = CATEGORY.get(concept.category if concept else "", "tactic")
    related_names = [library.concepts[c].name for c in res.related if library.count_for(c)][:3]

    # 1. what it is, in words — always first, always labelled
    unit(f"{res.term}: what it is", category, "the idea in words (a hand-written definition, not engine-checked).",
         [_text_lesson(f"plan_{plan_id}_01", f"{res.term}: what it is", res.text, related_names)], TEXT_VERIFIED_BY)

    # 2. new positions, verified before they are shown
    generated = []
    if concept is not None and supported(concept.id):
        if engine is None and engine_factory is not None:
            from ..engine import EngineUnavailable
            try:
                engine = engine_factory()
            except EngineUnavailable:
                engine = None  # still explains; just can't build positions
        if engine is None:
            notes.append("New practice positions need the Stockfish engine, which isn't available right now.")
        else:
            try:
                result = generate(concept.id, library, engine, count=GENERATE_COUNT, time_budget=generate_budget,
                                  use_qwen=use_qwen, on_progress=on_progress)
                generated = result.accepted
                if not generated:
                    notes.append("I tried to build new practice positions, but none passed every check "
                                 "in time, so I'm not showing any.")
            except Exception as exc:  # generation must never break planning
                log.warning("generation failed for %s: %s", concept.id, exc)
                notes.append("Building new practice positions failed this time.")
    if generated:
        names = [concept.name]
        seq = [(generated[0], "demonstration")] + [(e, "practice") for e in generated[1:]]
        intro = (f"{concept.summary}\n\nMy library has no real-game examples of this yet, so I built "
                 f"{len(generated)} new position{'s' if len(generated) != 1 else ''} for you. Each one was checked "
                 "by python-chess (legal) and Stockfish (the solution works, and it's the idea being taught).")
        lesson = knowledge_lesson(f"plan_{plan_id}_02", f"{concept.name}: new practice positions", intro, seq,
                                  f"Lesson complete — you've practised {concept.name.lower()}.", names)
        unit(f"{concept.name}: new practice positions", category,
             "new positions made for this request and checked by the engine before you see them.",
             [lesson], GENERATED_VERIFIED_BY, concepts=[concept.id], example_ids=[e.id for e in generated],
             generated=True)

    # 3a. a skill ("calculation"): the broader idea's verified positions that train it
    skill = []
    if res.practice and res.broader and not generated:
        target = target_rating or LEVEL_TARGET.get(level or "", LEVEL_TARGET["intermediate"])
        skill, need = skill_examples(library, res.broader, res.practice, target, usage)
    if skill:
        what = res.practice.get("title") or f"{res.term.lower()} practice"
        intro = f"{res.text}\n\n{res.practice.get('note', '')}".strip()
        seq = [(skill[0], "guided")] + [(e, "practice") for e in skill[1:]]
        names = list(dict.fromkeys(library.concepts[c].name for e in skill for c in e.concepts[:1]
                                   if c in library.concepts)) or [library.concepts[res.broader].name]
        lesson = knowledge_lesson(f"plan_{plan_id}_03", f"{res.term}: {what}", intro, seq,
                                  f"That's the {res.term.lower()} practice done. Want another round?", names)
        unit(f"{res.term}: {what}", CATEGORY.get(library.concepts[res.broader].category, "tactic"),
             f"verified positions chosen because each needs {need}+ moves "
             "of your own — practice for the skill itself.",
             [lesson], KNOWLEDGE_VERIFIED_BY, concepts=[res.broader], example_ids=[e.id for e in skill])
        usage.record_used(skill)

    # 3. a broader idea's verified examples, labelled as broader
    if res.broader and not generated and not skill:
        broad = library.concepts[res.broader]
        retrieval = retrieve(library, RetrievalRequest(concepts=[res.broader], level=level, count=2), usage)
        if retrieval.found and retrieval.sequence:
            intro = (f"I don't have checked examples of {res.term.lower()} itself. It belongs to the broader idea "
                     f"of {broad.name.lower()}, so here are verified {broad.name.lower()} examples — the general "
                     f"idea, not {res.term.lower()} specifically.")
            lesson = knowledge_lesson(f"plan_{plan_id}_03", f"{broad.name}: the broader idea", intro,
                                      retrieval.sequence, f"Lesson complete — that's {broad.name.lower()}.",
                                      [broad.name])
            unit(f"{broad.name}: the broader idea", CATEGORY.get(broad.category, "tactic"),
                 f"verified examples of {broad.name.lower()}, the broader idea {res.term.lower()} belongs to.",
                 [lesson], KNOWLEDGE_VERIFIED_BY, concepts=[res.broader],
                 example_ids=[e.id for e, _ in retrieval.sequence], broader=True)
            usage.record_used([e for e, _ in retrieval.sequence])
            notes.append(f"{res.term} is only covered by the broader idea “{broad.name}”.")

    if res.via == "qwen":
        notes.insert(0, f"I understood your request as “{res.term}”.")
    near = [t.title for t in catalog.near_matches(goal)]
    related = list(dict.fromkeys(near + related_names))[:4]
    checked = any(u["verified_by"] != TEXT_VERIFIED_BY for u in units)
    summary = (f"No verified library examples of {res.term.lower()} yet. " + " ".join(notes) + " "
               + ("Everything shown on the board was checked by the engine; the definition is text only."
                  if checked else "This plan is an explanation only: nothing here is engine-checked.")).strip()
    title = f"Learn: {res.term}"
    plan = {
        "id": plan_id, "goal": goal, "title": title, "summary": " ".join(summary.split()),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner": "fallback", "units": units, "skipped": [], "related": related,
        "fallback": {"term": res.term, "concept": res.concept, "understood_via": res.via,
                     "text_source": res.text_source, "broader": res.broader,
                     "generated": [e.id for e in generated], "verified_examples": 0},
    }
    course = {"id": f"plan_{plan_id}", "title": title, "description": plan["summary"], "kind": "plan",
              "lessons": [{"id": lesson["id"], "title": lesson["title"]} for lesson in lessons]}
    if generated:
        usage.record_used(generated)
    return {"plan": plan, "course": course, "lessons": lessons}
