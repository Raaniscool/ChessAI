"""Intent-first routing for short chess questions.

A basic concept question should not be answered by whichever related keyword happens to rank
highest in a search. Classify the request first (definition, example, position, opening, or
personal coaching), then retrieve only from the source appropriate to that request. This module is
fully local: it does not call Qwen or change a lesson session.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

perf_log = logging.getLogger("chessai.performance")

from ..basic_explanations.library import (
    LEVELS,
    get_basic_explanations,
)
from ..planner.catalog import _normalize, _phrase_span

MAX_QUESTION = 90
_BASIC_REQUEST = re.compile(
    r"^\s*(?:(?:wait|well|so|ok|okay|hey|please)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:"
    r"what(?:'s|’s| is| are| does| do)|whats|define|definition of|meaning of|"
    r"explain(?:\s+(?:to me|for me))?(?:\s+what)?|tell me (?:what|about)|"
    r"remind me (?:what|about)|i (?:just )?(?:forgot|forget|can't remember|cannot remember) (?:what|that)|"
    r"can you (?:please )?(?:go over|review)|could you (?:please )?(?:go over|review)|"
    r"(?:key|main) ideas? (?:of|behind)|(?:common )?misconception(?:s)? (?:about|around)|"
    r"what misconception(?:s)? (?:do|does) (?:people|beginners|players) (?:often )?have about|"
    r"what do (?:people|beginners|players) (?:often )?(?:get wrong|misunderstand) about|"
    r"how (?:does|do|can) (?!i\b|you\b|we\b)[\w' -]{2,40}? (?:move|moves|work|works|capture|captures)|"
    r"(?:what's|what is|what are) .{1,55}? again)\b",
    re.IGNORECASE,
)
_GENERIC_RULES = {"captures", "legal_moves"}  # "how do pawns capture?" is about pawns, not captures in general
_PIECE_MOVE = re.compile(
    r"\b(king|queen|rook|bishop|knight|pawn)s?\b[\w' ]{0,18}\b(move|moves|capture|captures|go|jump)\b",
    re.IGNORECASE,
)
_NOT_DEFINITION = re.compile(
    r"\b(why|best|better|should|this position|that position|my position|this game|last game|my game|"
    r"this move|that move|my move|last move|next move|play (?:now|here)|evaluation|eval|win(?:ning)?\?|"
    r"which move|how do i|how can i|how to|compare|comparison|difference|versus|vs|between|"
    r"in depth|deeper|more detail|more details|deep dive|step by step|walk me through|tell me more|explain more|"
    r"expand on|my rating|my mistake|my blunder|this blunder|this mistake|"
    r"what did i|what have i|where did i|how did i|(?:what|which) (?:mistake|blunder|error) (?:did i|have i)|"
    r"i (?:made|played|missed|chose)|coach me|analy[sz]e my)\b",
    re.IGNORECASE,
)
_PERSONALIZED = re.compile(
    r"\b(?:why do i keep|i keep|i always|i often|based on my (?:games?|history|mistakes)|"
    r"my (?:games?|weakness(?:es)?|blunders?)|what should i practice|coach me|personalize|"
    r"i understand .{0,55}\bbut\b.{0,40}\b(?:miss|misses|missing|blunder|blundering|forget)\b)\b",
    re.IGNORECASE,
)
_POSITION_CONTEXT = re.compile(
    r"\b(?:this|that)\s+(?:position|game|move|pin|fork|skewer|knight|bishop|rook|queen|king|pawn|piece)\b|"
    r"\b(?:here|in my position|in this position|in that position|in my game|in this game|"
    r"on this board|against this|against that|what should i play|which move should i play|best move)\b",
    re.IGNORECASE,
)
_EXAMPLE_ACTION = re.compile(r"\b(?:show|give|find|provide|send|see)\b", re.IGNORECASE)
_EXAMPLE_OBJECT = re.compile(r"\b(?:example|position|puzzle|tactic|pattern|diagram)\b", re.IGNORECASE)
_EXAMPLE_OF = re.compile(r"\b(?:example|position|puzzle|tactic|pattern|diagram)\s+(?:of|where|to)\b", re.I)
_OPENING_SIGNAL = re.compile(
    r"\b(?:variation|gambit|defen[cs]e|opening line|main line|repertoire|opening theory|"
    r"opening system|eco)\b", re.IGNORECASE)
_OPENING_STYLE = re.compile(
    r"\b(?:how do i play|how can i play|how do i respond to|how should i respond to|"
    r"what do i play against|what is the main line)\b", re.IGNORECASE)
_HOW_TO = re.compile(r"\b(?:how do i|how can i|how to|show me how|teach me how)\b", re.IGNORECASE)
_DETAIL_MISCONCEPTION = re.compile(
    r"\b(misconception|common myth|what (?:do|does) (?:people|beginners|players) (?:often )?"
    r"(?:get wrong|misunderstand))\b", re.IGNORECASE)
_DETAIL_KEY_IDEA = re.compile(r"\b(key|main) ideas?\b|\b(key takeaways?|core ideas?)\b", re.IGNORECASE)
_LEVEL_WORDS = {
    "beginner": re.compile(r"\b(beginner|beginners|simple|basic|plain english|new to chess)\b", re.I),
    "intermediate": re.compile(r"\b(intermediate|club player)\b", re.I),
    "advanced": re.compile(r"\b(advanced|expert)\b", re.I),
}

# Words which do not identify a second subject when checking whether a definition names one or
# several Basic Explanation entries. Piece names are qualifiers ("queen fork"), not extra ideas.
_SUBJECT_FILLER = set("""a an the some any me my i we us you your please can could would will do does
what which who is are was were be been to of about on in for with and or again once more remind
forgot forget remember explain define definition meaning tell review go over simple basic
beginner intermediate advanced chess concept idea how works mean means move moves king queen rook
bishop knight pawn piece players people often do""".split())


@dataclass(frozen=True)
class QuestionIntent:
    """A local, confidence-scored routing decision made before answer retrieval."""

    kind: str
    confidence: float
    source: str
    concept: str | None = None
    explanation_id: str | None = None
    opening_id: str | None = None
    rule_piece: str | None = None


def is_definition_question(message: str) -> bool:
    """Whether the sentence has the shape of a short, direct definition request."""
    text = " ".join((message or "").split())
    return bool(text) and len(text) <= MAX_QUESTION and bool(_BASIC_REQUEST.search(text)) \
        and not _NOT_DEFINITION.search(text)


def _requested_level(message: str) -> str:
    # The library supports these explicit levels; absent a request, give a concise beginner answer.
    for level in ("beginner", "intermediate", "advanced"):
        if _LEVEL_WORDS[level].search(message):
            return level
    return "beginner"


def _requested_detail(message: str) -> str:
    if _DETAIL_MISCONCEPTION.search(message):
        return "common_misconception"
    if _DETAIL_KEY_IDEA.search(message):
        return "key_idea"
    return "definition"


def _basic_candidates(message: str, basic, library, glossary) -> list[tuple[object, int]]:
    """Basic entries actually named in the question, longest phrase first.

    Keeping every top match lets the classifier decline a genuinely multi-concept request instead
    of resolving a tie by JSON order. A specific alias ("absolute pin") still beats its parent
    ("pin").
    """
    tokens = _normalize(message)
    candidates: list[tuple[object, int]] = []
    for entry in basic.all():
        sizes = []
        for alias in basic._aliases(entry, library, glossary):
            phrase = _normalize(alias)
            if phrase and _phrase_span(phrase, tokens) is not None:
                sizes.append(len(phrase))
        if sizes:
            candidates.append((entry, max(sizes)))
    if not candidates:
        return []
    longest = max(size for _, size in candidates)
    return [(entry, size) for entry, size in candidates if size == longest]


def _knowledge_matches(message: str, library) -> list[str]:
    return [cid for cid in library.match_concepts(message)
            if cid in library.concepts and library.concepts[cid].summary.strip()]


def _has_phrase(tokens: list[str], phrase: list[str]) -> bool:
    return bool(phrase) and any(tokens[i:i + len(phrase)] == phrase for i in range(len(tokens) - len(phrase) + 1))


def _opening_context(message: str, basic_matches: list[tuple[object, int]], library):
    """Return a verified opening-tree match only when the sentence supplies opening context.

    OpeningTrees.find("explain a pin") can correctly find the *Sicilian Pin Variation* by the
    word "pin" alone. That is a useful search match but not evidence that the learner asked about
    an opening. Require an opening name, a variation/line cue, or an opening-style request before
    treating that result as intent.
    """
    basic_present = bool(basic_matches)
    explicit_signal = bool(_OPENING_SIGNAL.search(message))
    direct_or_play = is_definition_question(message) or bool(_OPENING_STYLE.search(message))

    # Avoid loading/scanning opening data for an ordinary Basic concept question such as
    # "Explain a pin to me". A known non-concept modifier ("Sicilian") can still trigger it.
    needs_lookup = explicit_signal or direct_or_play and not basic_present
    if basic_present and not explicit_signal:
        tokens = _normalize(message)
        covered: set[int] = set()
        for entry, _size in basic_matches:
            for alias in [entry.name, entry.id.replace("_", " "), *entry.aliases]:
                span = _phrase_span(_normalize(alias), tokens)
                if span:
                    covered.update(span)
            if entry.knowledge_concept and entry.knowledge_concept in library.concepts:
                concept = library.concepts[entry.knowledge_concept]
                for alias in [concept.name, entry.knowledge_concept.replace("_", " "), *concept.aliases]:
                    span = _phrase_span(_normalize(alias), tokens)
                    if span:
                        covered.update(span)
        extra = [token for i, token in enumerate(tokens)
                 if i not in covered and token not in _SUBJECT_FILLER and not token.isdigit()]
        # A phrase such as "pin in the Sicilian" has an unexplained, potentially named opening;
        # "Explain a pin to me" leaves no such content word after ordinary request language.
        needs_lookup = bool(extra) and direct_or_play

    if not needs_lookup:
        return None
    try:
        from .opening_trees import Match, get_opening_trees
        trees = get_opening_trees()
        match = trees.find(message)
    except Exception:  # opening data must never make the instant-answer route unavailable
        return None

    tokens = _normalize(message)
    named_tree = next((tree for tree in trees.trees.values()
                       if any(_has_phrase(tokens, _normalize(alias))
                              for alias in [tree.title, *tree.aliases, *tree.names] if alias)), None)
    # OpeningTrees.find intentionally rejects unknown residual words. For an unambiguous family
    # name plus a general request such as "respond to this Sicilian line", route to that verified
    # tree even if those generic words do not name a recorded variation.
    if named_tree is not None and (match is None or match.tree is not named_tree):
        match = Match(named_tree, None, named_tree.title, [])
    if match is None:
        return None

    if named_tree is not None or explicit_signal or (not basic_present and direct_or_play and match.node is not None):
        return match
    return None


def _concept_for_opening(match) -> str:
    return match.tree.catalog_topic or match.tree.library_concept or match.tree.id


def _concept_for_basic(entry, glossary) -> str | None:
    if entry.knowledge_concept:
        return entry.knowledge_concept
    term = glossary.terms.get(entry.glossary_term) if entry.glossary_term else None
    return term.broader if term else None


def classify_question(message: str, library, glossary, basic=None) -> QuestionIntent:
    """Determine the requested task and source before retrieving any answer.

    Only a high-confidence, direct concept-definition intent is eligible for an instant Basic
    Explanation. Examples, position-specific reasoning, openings and coaching deliberately return
    a different route even when their sentence contains a Basic concept alias.
    """
    text = " ".join((message or "").split())
    if not text or len(text) > MAX_QUESTION:
        return QuestionIntent("ambiguous", 0.0, "tutor")

    basic = basic or get_basic_explanations()
    basic_matches = _basic_candidates(text, basic, library, glossary)
    knowledge_matches = _knowledge_matches(text, library)
    concept = knowledge_matches[0] if knowledge_matches else None

    if _PERSONALIZED.search(text):
        return QuestionIntent("personalized_coaching", 0.98, "personalized_tutor", concept)

    opening = _opening_context(text, basic_matches, library)
    # Actual references to the board currently under discussion outrank generic examples.
    if _POSITION_CONTEXT.search(text):
        return QuestionIntent("position_question", 0.97, "position_tutor", concept)

    example = bool(
        (_EXAMPLE_OBJECT.search(text) and (_EXAMPLE_ACTION.search(text) or _EXAMPLE_OF.search(text)))
        or (_EXAMPLE_ACTION.search(text) and basic_matches and not _HOW_TO.search(text)
            and not is_definition_question(text))
    )
    if example:
        # Opening examples use their verified opening data; tactic examples use the Knowledge Library.
        source = "opening_knowledge" if opening else "knowledge_library"
        return QuestionIntent("example_request", 0.95, source, concept,
                              opening_id=opening.tree.id if opening else None)

    # A named line/variation or opening-style question is opening knowledge, not a definition of
    # a tactic keyword that happens to occur in its name.
    if opening:
        return QuestionIntent("opening_question", 0.97, "opening_knowledge",
                              _concept_for_opening(opening), opening_id=opening.tree.id)

    if is_definition_question(text):
        piece = _PIECE_MOVE.search(text)
        if piece and not set(library.match_concepts(text)) - _GENERIC_RULES:
            return QuestionIntent("basic_explanation", 0.98, "rules", rule_piece=piece.group(1).lower())
        if len(basic_matches) > 1:
            return QuestionIntent("ambiguous", 0.45, "tutor", concept)
        if basic_matches:
            entry, size = basic_matches[0]
            # Preserve the existing specific-concept fallback: "smothered mate" is more specific
            # than a broad checkmate explanation. But an unrelated library hit cannot displace a
            # directly requested Basic entry just because one token overlaps.
            if knowledge_matches:
                specific = knowledge_matches[0]
                knowledge_size = _knowledge_match_size(specific, text, library)
                if specific != entry.knowledge_concept and knowledge_size > size:
                    return QuestionIntent("concept_explanation", 0.90, "knowledge_library", specific)
            return QuestionIntent("basic_explanation", 0.99, "basic_explanations",
                                  _concept_for_basic(entry, glossary), explanation_id=entry.id)
        if knowledge_matches:
            return QuestionIntent("concept_explanation", 0.88, "knowledge_library", knowledge_matches[0])
        found = glossary.match(text)
        if found is not None:
            return QuestionIntent("concept_explanation", 0.82, "glossary", found.broader)
        return QuestionIntent("ambiguous", 0.35, "tutor")

    if _HOW_TO.search(text):
        return QuestionIntent("lesson_request", 0.86, "lesson_system", concept)
    return QuestionIntent("tutor_question", 0.55, "tutor", concept)


def _knowledge_match_size(concept_id: str, message: str, library) -> int:
    concept = library.concepts[concept_id]
    tokens = _normalize(message)
    aliases = [concept.name, concept_id.replace("_", " "), *concept.aliases]
    return max((len(_normalize(alias)) for alias in aliases
                if _phrase_span(_normalize(alias), tokens) is not None), default=0)


def _basic_answer(message: str, entry, library, glossary) -> dict:
    level = _requested_level(message)
    detail = _requested_detail(message)
    answer = entry.as_dict(library, glossary, level=level)
    if detail == "common_misconception":
        answer["text"] = entry.common_misconception
    elif detail == "key_idea":
        answer["text"] = entry.key_idea

    glossary_term = glossary.terms.get(entry.glossary_term) if entry.glossary_term else None
    answer.update({
        "source": "basic_explanations",
        "concept": entry.knowledge_concept or (glossary_term.broader if glossary_term else None),
        "verified": bool(entry.knowledge_concept in library.concepts),
        "examples": library.count_for(entry.knowledge_concept) if entry.knowledge_concept in library.concepts else 0,
        "lesson_goal": f"I want to learn {entry.name.lower()}",
        "detail": detail,
    })
    return answer


def quick_answer(message: str, library, glossary, basic=None, *, with_intent: bool = False):
    """Return a direct local fact only when intent confidence and source both agree.

    Other intents return ``None`` so examples continue through Knowledge Library retrieval, and
    position/opening/coaching questions continue through their existing tutor/lesson flows. The
    coach route can request the already-computed local intent so it does not classify the same
    active-session message again before semantic routing.
    """
    basic = basic or get_basic_explanations()
    classifier_started = time.perf_counter()
    intent = classify_question(message, library, glossary, basic)
    if with_intent:
        perf_log.info("latency stage=position_classifier duration_ms=%.1f confidence=%.2f kind=%s source=quick_answer",
                      (time.perf_counter() - classifier_started) * 1000, intent.confidence, intent.kind)
    answer = _answer_for_intent(message, intent, library, glossary, basic)
    return (answer, intent) if with_intent else answer


def _answer_for_intent(message: str, intent: QuestionIntent, library, glossary, basic) -> dict | None:
    """Resolve one already-classified local answer; the intent can also feed coach routing."""
    if intent.confidence < 0.80:
        return None

    if intent.kind == "basic_explanation" and intent.explanation_id:
        entry = basic.get(intent.explanation_id)
        return _basic_answer(message, entry, library, glossary) if entry is not None else None

    if intent.kind == "basic_explanation" and intent.rule_piece:
        from ..learner.personalize import PIECES  # the same hand-written rule text lessons use
        text = PIECES[intent.rule_piece]
        return {"term": f"How the {intent.rule_piece} moves", "text": text[0].upper() + text[1:] + ".",
                "source": "rules", "concept": None, "verified": True, "examples": 0,
                "lesson_goal": None}

    if intent.kind == "concept_explanation" and intent.source == "knowledge_library" and intent.concept:
        concept = library.concepts[intent.concept]
        summary = concept.summary.strip()
        if summary:
            return {"term": concept.name, "text": summary, "source": "library", "concept": intent.concept,
                    "verified": True, "examples": library.count_for(intent.concept),
                    "lesson_goal": f"I want to learn {concept.name.lower()}"}

    if intent.kind == "concept_explanation" and intent.source == "glossary":
        found = glossary.match(message)
        if found is not None:
            return {"term": found.term, "text": found.definition, "source": "glossary", "concept": found.broader,
                    "verified": False, "examples": 0, "lesson_goal": f"I want to learn {found.term.lower()}"}
    return None
