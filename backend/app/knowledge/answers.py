"""Instant answers to "what is …?" questions from the Knowledge Library.

"What's a fork?" doesn't need an AI call or a whole lesson plan: the library already has a
verified one-line definition for every concept, and the glossary has hand-written definitions
for terms without verified examples (labelled as such). The learner gets the answer at once,
plus an offer to turn it into a lesson. Questions about a position, a move or "why"/"how"
questions are left to the tutor (they need the board and the engine).
"""
from __future__ import annotations

import re

MAX_QUESTION = 90
_DEFINITION = re.compile(
    r"^\s*(?:(?:so|ok|okay|hey|please)[,\s]+)?(?:what(?:'s|’s| is| are| does| do)|whats|define|definition of|"
    r"meaning of|explain what|tell me what|what do you mean by|what is meant by)\b", re.IGNORECASE)
_NOT_DEFINITION = re.compile(
    r"\b(why|best|better|should|this position|this move|here|my move|my game|next move|play (?:now|here)|"
    r"evaluation|eval|win(?:ning)?\?|which move|how do i|how can i)\b", re.IGNORECASE)


def is_definition_question(message: str) -> bool:
    text = " ".join((message or "").split())
    return bool(text) and len(text) <= MAX_QUESTION and bool(_DEFINITION.search(text)) \
        and not _NOT_DEFINITION.search(text)


def quick_answer(message: str, library, glossary) -> dict | None:
    """{term, text, source: "library"|"glossary", concept, verified, lesson_goal} or None."""
    if not is_definition_question(message):
        return None
    for cid in library.match_concepts(message)[:1]:
        concept = library.concepts[cid]
        summary = concept.summary.strip()
        if not summary:
            continue
        return {"term": concept.name, "text": summary, "source": "library", "concept": cid, "verified": True,
                "examples": library.count_for(cid), "lesson_goal": f"I want to learn {concept.name.lower()}"}
    found = glossary.match(message)
    if found is not None:
        return {"term": found.term, "text": found.definition, "source": "glossary", "concept": found.broader,
                "verified": False, "examples": 0, "lesson_goal": f"I want to learn {found.term.lower()}"}
    return None
