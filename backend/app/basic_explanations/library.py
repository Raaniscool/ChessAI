"""The separate Basic Explanation Library: short, curated chess explanations.

This text library complements, but does not replace, the verified Knowledge Library or the
legacy Glossary. Entries may reuse a Knowledge Library summary or Glossary definition as their
beginner wording instead of copying it. Intermediate and advanced wording, key ideas and common
misconceptions live here. Example IDs are resolved at read time from verified Knowledge Library
positions; explanation prose itself is curated text, not Stockfish output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..planner.catalog import _normalize, _phrase_span

DATA_FILE = Path(__file__).resolve().parent / "data" / "explanations.json"
LEVELS = ("beginner", "intermediate", "advanced")
CATEGORIES = frozenset({"rules", "tactics", "strategy", "opening", "pawn_structure", "endgame", "mistakes"})


class BasicExplanationError(ValueError):
    """Invalid Basic Explanation Library data or reference."""


@dataclass(frozen=True)
class BasicExplanation:
    id: str
    name: str
    category: str
    aliases: tuple[str, ...]
    levels: dict[str, str | None]
    key_idea: str
    common_misconception: str
    related: tuple[str, ...]
    knowledge_concept: str | None = None
    glossary_term: str | None = None
    example_concepts: tuple[str, ...] = ()
    exclude_source_aliases: tuple[str, ...] = ()

    def level_text(self, level: str, knowledge, glossary) -> str:
        """Return the requested text, reusing an existing beginner definition by reference."""
        if level not in LEVELS:
            raise ValueError(f"unknown explanation level {level!r}")
        text = self.levels.get(level)
        if text:
            return text
        if level == "beginner" and self.knowledge_concept:
            concept = knowledge.concepts.get(self.knowledge_concept)
            if concept is not None and concept.summary.strip():
                return concept.summary.strip()
        if level == "beginner" and self.glossary_term:
            term = glossary.terms.get(self.glossary_term)
            if term is not None and term.definition.strip():
                return term.definition.strip()
        raise BasicExplanationError(f"{self.id}: no {level} explanation is available")

    def as_dict(self, knowledge, glossary, *, level: str = "beginner",
                example_limit: int = 5) -> dict:
        """A complete, ready-to-use record with inherited text and verified example IDs."""
        examples = related_verified_example_ids(self, knowledge, limit=example_limit)
        return {
            "id": self.id,
            "term": self.name,
            "category": self.category,
            "level": level,
            "text": self.level_text(level, knowledge, glossary),
            "levels": {name: self.level_text(name, knowledge, glossary) for name in LEVELS},
            "key_idea": self.key_idea,
            "common_misconception": self.common_misconception,
            "related": list(self.related),
            "related_verified_examples": examples,
            "knowledge_concept": self.knowledge_concept,
            "glossary_term": self.glossary_term,
            "curated": True,
        }


class BasicExplanationLibrary:
    """Load, validate and retrieve the independently curated explanation entries."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DATA_FILE
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BasicExplanationError(f"cannot load {self.path}: {exc}") from exc
        if data.get("version") != 1 or not isinstance(data.get("explanations"), list):
            raise BasicExplanationError(f"{self.path}: unsupported Basic Explanation Library schema")
        self.entries: dict[str, BasicExplanation] = {}
        for raw in data["explanations"]:
            entry = self._parse(raw)
            if entry.id in self.entries:
                raise BasicExplanationError(f"duplicate explanation id {entry.id!r}")
            self.entries[entry.id] = entry
        self._validate_internal()

    @staticmethod
    def _parse(raw: dict) -> BasicExplanation:
        ident = raw.get("id")
        name = raw.get("name")
        category = raw.get("category")
        levels = raw.get("levels", {})
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9_]+", ident):
            raise BasicExplanationError(f"invalid explanation id {ident!r}")
        if not isinstance(name, str) or not name.strip():
            raise BasicExplanationError(f"{ident}: missing name")
        if category not in CATEGORIES:
            raise BasicExplanationError(f"{ident}: invalid category {category!r}")
        if not isinstance(levels, dict) or set(levels) - set(LEVELS):
            raise BasicExplanationError(f"{ident}: invalid levels")
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in levels.values()):
            raise BasicExplanationError(f"{ident}: level text must be a non-empty string or null")
        if not levels.get("intermediate") or not levels.get("advanced"):
            raise BasicExplanationError(f"{ident}: intermediate and advanced explanations are required")
        knowledge_concept = raw.get("knowledge_concept")
        glossary_term = raw.get("glossary_term")
        if knowledge_concept and glossary_term:
            raise BasicExplanationError(f"{ident}: choose one beginner-text reference, not both")
        if not levels.get("beginner") and not (knowledge_concept or glossary_term):
            raise BasicExplanationError(f"{ident}: needs beginner text or a Knowledge/Glossary reference")
        for key in ("aliases", "related", "example_concepts", "exclude_source_aliases"):
            value = raw.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise BasicExplanationError(f"{ident}: {key} must be a list of non-empty strings")
        for key in ("key_idea", "common_misconception"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise BasicExplanationError(f"{ident}: missing {key}")
        if knowledge_concept is not None and not isinstance(knowledge_concept, str):
            raise BasicExplanationError(f"{ident}: knowledge_concept must be a string or null")
        if glossary_term is not None and not isinstance(glossary_term, str):
            raise BasicExplanationError(f"{ident}: glossary_term must be a string or null")
        return BasicExplanation(
            id=ident,
            name=name.strip(),
            category=category,
            aliases=tuple(raw.get("aliases", [])),
            levels={level: levels.get(level) for level in LEVELS},
            key_idea=raw["key_idea"].strip(),
            common_misconception=raw["common_misconception"].strip(),
            related=tuple(raw.get("related", [])),
            knowledge_concept=knowledge_concept,
            glossary_term=glossary_term,
            example_concepts=tuple(raw.get("example_concepts", [])),
            exclude_source_aliases=tuple(raw.get("exclude_source_aliases", [])),
        )

    def _validate_internal(self) -> None:
        for entry in self.entries.values():
            for related in entry.related:
                if related not in self.entries:
                    raise BasicExplanationError(f"{entry.id}: unknown related explanation {related!r}")

    def validate_references(self, knowledge, glossary) -> None:
        """Check every cross-library pointer and ensure every entry has all three levels."""
        for entry in self.entries.values():
            if entry.knowledge_concept and entry.knowledge_concept not in knowledge.concepts:
                raise BasicExplanationError(
                    f"{entry.id}: unknown Knowledge Library concept {entry.knowledge_concept!r}")
            if entry.glossary_term and entry.glossary_term not in glossary.terms:
                raise BasicExplanationError(f"{entry.id}: unknown Glossary term {entry.glossary_term!r}")
            for concept_id in entry.example_concepts:
                if concept_id not in knowledge.concepts:
                    raise BasicExplanationError(
                        f"{entry.id}: unknown example concept {concept_id!r}")
            for level in LEVELS:
                entry.level_text(level, knowledge, glossary)

    def get(self, explanation_id: str) -> BasicExplanation | None:
        return self.entries.get(explanation_id)

    def all(self) -> list[BasicExplanation]:
        return list(self.entries.values())

    def _aliases(self, entry: BasicExplanation, knowledge, glossary) -> list[str]:
        aliases = [entry.name, entry.id.replace("_", " "), *entry.aliases]
        if entry.knowledge_concept and knowledge is not None:
            concept = knowledge.concepts.get(entry.knowledge_concept)
            if concept:
                aliases.extend([concept.name, *concept.aliases])
        if entry.glossary_term and glossary is not None:
            term = glossary.terms.get(entry.glossary_term)
            if term:
                aliases.extend([term.term, *term.aliases])
        excluded = {tuple(_normalize(alias)) for alias in entry.exclude_source_aliases}
        seen: set[tuple[str, ...]] = set()
        out: list[str] = []
        for alias in aliases:
            normalized = tuple(_normalize(alias))
            if normalized and normalized not in excluded and normalized not in seen:
                seen.add(normalized)
                out.append(alias)
        return out

    def match_size(self, text: str, knowledge=None, glossary=None) -> tuple[BasicExplanation, int] | None:
        """Return the longest matching entry and its phrase length, if any."""
        tokens = _normalize(text)
        if not tokens:
            return None
        best: tuple[int, int, BasicExplanation] | None = None
        for order, entry in enumerate(self.entries.values()):
            for alias in self._aliases(entry, knowledge, glossary):
                phrase = _normalize(alias)
                if _phrase_span(phrase, tokens) is None:
                    continue
                size = len(" ".join(phrase))
                candidate = (size, -order, entry)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        return (best[2], best[0]) if best else None

    def match(self, text: str, knowledge=None, glossary=None) -> BasicExplanation | None:
        """The most specific explanation named in free text, or None."""
        found = self.match_size(text, knowledge, glossary)
        return found[0] if found else None


def related_verified_example_ids(entry: BasicExplanation, knowledge, *, limit: int = 5) -> list[str]:
    """IDs for checked examples related to the explanation; personal entries are excluded."""
    concept_ids = list(dict.fromkeys(
        ([entry.knowledge_concept] if entry.knowledge_concept else []) + list(entry.example_concepts)))
    found: list[str] = []
    for concept_id in concept_ids:
        if concept_id not in knowledge.concepts:
            continue
        for example in knowledge.examples_for(concept_id):
            if example.status == "verified" and example.id not in found:
                found.append(example.id)
                if len(found) >= limit:
                    return found
    return found


@lru_cache(maxsize=1)
def get_basic_explanations() -> BasicExplanationLibrary:
    return BasicExplanationLibrary()
