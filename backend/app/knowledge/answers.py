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
    r"meaning of|explain what|tell me what|what do you mean by|what is meant by|"
    r"how (?:does|do|can) (?!i\b|you\b|we\b)[\w' -]{2,40}? (?:move|moves|work|works|capture|captures))\b", re.IGNORECASE)
_GENERIC_RULES = {"captures", "legal_moves"}  # "how do pawns capture?" is about pawns, not captures in general
_PIECE_MOVE = re.compile(r"\b(king|queen|rook|bishop|knight|pawn)s?\b[\w' ]{0,12}\b(move|moves|capture|captures|go|jump)\b",
                         re.IGNORECASE)
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
    piece = _PIECE_MOVE.search(message)
    if piece and not set(library.match_concepts(message)) - _GENERIC_RULES:
        from ..learner.personalize import PIECES  # the same hand-written rule text lessons use
        name = piece.group(1).lower()
        text = PIECES[name]
        return {"term": f"How the {name} moves", "text": text[0].upper() + text[1:] + ".", "source": "rules",
                "concept": None, "verified": True, "examples": 0,
                "lesson_goal": None}
    matched = library.match_concepts(message)
    for cid in ([c for c in matched if c not in _GENERIC_RULES] or matched)[:1]:
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
