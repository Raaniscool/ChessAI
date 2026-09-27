"""Hand-written definitions of chess terms that have no verified examples (data/glossary.json).

Used only as a last resort when a lesson is requested for something the library can't
show with checked positions. The text is labelled as unchecked wherever it is shown.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..planner.catalog import _normalize, _phrase_span

GLOSSARY_FILE = Path(__file__).parent / "data" / "glossary.json"


@dataclass(frozen=True)
class Term:
    id: str
    term: str
    definition: str
    aliases: tuple[str, ...] = ()
    broader: str | None = None
    related: tuple[str, ...] = field(default_factory=tuple)


class Glossary:
    def __init__(self, terms: list[Term]):
        self.terms = {t.id: t for t in terms}

    @classmethod
    def load(cls, path: Path = GLOSSARY_FILE) -> "Glossary":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([Term(id=t["id"], term=t["term"], definition=t["definition"], aliases=tuple(t.get("aliases", [])),
                         broader=t.get("broader"), related=tuple(t.get("related", []))) for t in data["terms"]])

    def match(self, text: str) -> Term | None:
        """The term named in `text` (the longest alias wins), or None."""
        found = self.match_size(text)
        return found[0] if found else None

    def match_size(self, text: str) -> tuple[Term, int] | None:
        """(term, length of the matched alias) — to compare with catalog alias matches."""
        tokens = _normalize(text)
        best: tuple[int, Term] | None = None
        for term in self.terms.values():
            for alias in (term.term, *term.aliases):
                phrase = _normalize(alias)
                if _phrase_span(phrase, tokens) is not None:
                    size = len(" ".join(phrase))
                    if best is None or size > best[0]:
                        best = (size, term)
        return (best[1], best[0]) if best else None


@lru_cache(maxsize=1)
def get_glossary() -> Glossary:
    return Glossary.load()
