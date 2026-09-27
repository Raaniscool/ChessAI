"""Load, index and search the Knowledge Library.

    knowledge/data/concepts.json                 the concept graph
    knowledge/data/examples/<category>/*.json    the global (seed) library
    DATA_DIR/knowledge/{generated,personal}/     runtime tiers (see store.py)

On load every entry is re-checked with the deterministic stages of the
pipeline (rules + concept validator). Only entries with status `verified` are
indexed for trusted retrieval; rejected / needs_review / candidate / deprecated
entries are kept for review but can never reach a lesson. Personal examples are
only returned when a caller asks for them.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..planner.catalog import _normalize, _phrase_span
from .schema import CATEGORIES, Concept, Example, KnowledgeError, parse_concept, parse_example
from .store import RuntimeStore

DATA_DIR = Path(__file__).resolve().parent / "data"
MAX_COUNT = 5
DEFAULT_COUNT = 3
RECENT_DAYS = 7


@dataclass
class Query:
    """What a learner (or the training system) asks the library for."""

    text: str = ""
    concepts: list[str] = field(default_factory=list)
    count: int = DEFAULT_COUNT
    level: str | None = None  # beginner | intermediate | advanced
    simplest: bool = False  # "really simple": easiest first, whatever else
    mode: str | None = None  # demonstration | interactive | practice | ... (required support)
    tags: list[str] = field(default_factory=list)
    category: str | None = None
    exclude: set[str] = field(default_factory=set)
    not_seen_recently: bool = True
    include_personal: bool = False
    target_rating: int | None = None  # learner-matched difficulty (knowledge.difficulty scale)

    def as_dict(self) -> dict:
        return {"concepts": self.concepts, "count": self.count, "level": self.level,
                "simplest": self.simplest, "mode": self.mode, "tags": self.tags, "category": self.category}


class KnowledgeLibrary:
    def __init__(self, data_dir: Path | None = None, store: RuntimeStore | None = None,
                 load_runtime: bool = True):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.store = store if store is not None else (RuntimeStore() if load_runtime else None)
        self.concepts: dict[str, Concept] = {}
        self.children: dict[str, list[str]] = {}
        self.entries: dict[str, Example] = {}  # every entry, any status, any tier
        self.load_errors: list[str] = []
        self._by_concept: dict[str, list[str]] = {}  # verified global + generated
        self._personal_by_concept: dict[str, list[str]] = {}
        self._by_tag: dict[str, list[str]] = {}
        self._opening_index = None
        self._lock = threading.RLock()
        self._load_concepts()
        self._load_global()
        if self.store is not None:
            for tier in ("generated", "personal"):
                for raw in self.store.all(tier):
                    self._load_runtime(raw, tier)

    # ------------------------------------------------------------ loading
    def _load_concepts(self) -> None:
        with open(self.data_dir / "concepts.json", encoding="utf-8") as fh:
            raw = json.load(fh)
        for entry in raw.get("concepts", []):
            concept = parse_concept(entry)
            if concept.id in self.concepts:
                raise KnowledgeError(f"duplicate concept {concept.id}")
            self.concepts[concept.id] = concept
        for concept in self.concepts.values():
            for key in ("parents", "related", "prerequisites"):
                for other in getattr(concept, key):
                    if other not in self.concepts:
                        raise KnowledgeError(f"concept {concept.id}: unknown {key[:-1]} {other!r}")
            for parent in concept.parents:
                self.children.setdefault(parent, []).append(concept.id)
        self._check_acyclic()

    def _check_acyclic(self) -> None:
        state: dict[str, int] = {}

        def visit(cid: str, trail: list[str]) -> None:
            if state.get(cid) == 1:
                raise KnowledgeError(f"concept hierarchy has a cycle: {' -> '.join(trail + [cid])}")
            if state.get(cid) == 2:
                return
            state[cid] = 1
            for child in self.children.get(cid, []):
                visit(child, trail + [cid])
            state[cid] = 2

        for cid in self.concepts:
            visit(cid, [])

    def _load_global(self) -> None:
        root = self.data_dir / "examples"
        for path in sorted(root.rglob("*.json")) if root.exists() else []:
            rel = path.relative_to(self.data_dir).as_posix()
            with open(path, encoding="utf-8") as fh:
                try:
                    entries = json.load(fh)
                except json.JSONDecodeError as exc:
                    raise KnowledgeError(f"{rel}: invalid JSON ({exc})") from exc
            if isinstance(entries, dict):
                entries = entries.get("examples", [])
            folder = path.parent.name
            for raw in entries:
                example = parse_example(raw, self.concepts, rel, tier="global")
                if folder in CATEGORIES and example.category != folder:
                    raise KnowledgeError(f"{example.id}: category {example.category} but stored in {folder}/")
                if example.status not in ("verified", "deprecated"):
                    raise KnowledgeError(f"{example.id}: the global library only holds verified entries "
                                         f"(status {example.status}); run the seed builder")
                problem = self.recheck(example)
                if problem:
                    raise KnowledgeError(f"{example.id} ({rel}): {problem}")
                self._add(example)

    def _load_runtime(self, raw: dict, tier: str) -> None:
        try:
            example = parse_example(raw, self.concepts, f"{tier}/{raw.get('id')}.json", tier=tier)
        except KnowledgeError as exc:
            self.load_errors.append(f"{tier}: {exc}")
            return
        if example.id in self.entries:
            self.load_errors.append(f"{tier}: duplicate id {example.id} (ignored)")
            return
        if example.status == "verified":
            problem = self.recheck(example)
            if problem:  # rules/validators changed since it was verified: take it out of rotation
                example.status = "needs_review"
                example.verification["recheck"] = problem
        self._add(example)

    def recheck(self, example: Example) -> str | None:
        """Deterministic re-verification: rules (by parsing) + every claimed concept's validator."""
        from .pipeline import concept_stage
        stage = concept_stage(example, self)
        return None if stage.outcome == "pass" else f"concept check: {stage.detail}"

    def _add(self, example: Example) -> None:
        with self._lock:
            self.entries[example.id] = example
            if example.status != "verified":
                return
            index = self._personal_by_concept if example.tier == "personal" else self._by_concept
            for cid in example.concepts:
                index.setdefault(cid, []).append(example.id)
            if example.tier != "personal":
                for tag in example.tags:
                    self._by_tag.setdefault(tag.lower(), []).append(example.id)

    def _remove_from_index(self, example_id: str) -> None:
        with self._lock:
            for index in (self._by_concept, self._personal_by_concept, self._by_tag):
                for ids in index.values():
                    if example_id in ids:
                        ids.remove(example_id)

    def add(self, example: Example) -> None:
        """Index an in-memory entry without persisting it (the seed builder uses this so the
        duplicate check sees what has been accepted so far)."""
        if example.id in self.entries:
            raise KnowledgeError(f"duplicate id {example.id}")
        self._add(example)

    # ------------------------------------------------------ runtime changes
    def save_entry(self, example: Example) -> None:
        """Persist a runtime-tier entry (any status) and (re)index it."""
        if example.tier == "global":
            raise KnowledgeError("the global library is edited through its data files, not at runtime")
        if self.store is None:
            raise KnowledgeError("this library has no runtime store")
        self.store.save(example.tier, example.to_record())
        self._remove_from_index(example.id)
        self.entries.pop(example.id, None)
        self._add(example)

    def set_status(self, example_id: str, status: str, note: str = "", actor: str = "reviewer") -> Example:
        example = self.entries.get(example_id)
        if example is None:
            raise KeyError(example_id)
        if example.tier == "global":
            raise KnowledgeError("global entries change through their data files")
        if status == "verified":  # a reviewer may approve, but never approve broken chess
            problem = self.recheck(example)
            if problem:
                raise KnowledgeError(f"cannot verify {example_id}: {problem}")
        record = self.store.set_status(example.tier, example_id, status, note, actor)
        updated = parse_example(record, self.concepts, example.path, tier=example.tier)
        self._remove_from_index(example_id)
        self.entries.pop(example_id, None)
        if status == "verified":
            self.recheck(updated)
        self._add(updated)
        return updated

    # --------------------------------------------------------------- graph
    def validator_for(self, concept_id: str) -> dict:
        return self._inherit(concept_id, lambda c: c.validator or None) or {}

    def engine_profile_for(self, concept_id: str) -> str:
        return self._inherit(concept_id, lambda c: c.engine) or "none"

    def _inherit(self, concept_id: str, get):
        seen, queue = set(), [concept_id]
        while queue:
            cid = queue.pop(0)
            if cid in seen or cid not in self.concepts:
                continue
            seen.add(cid)
            value = get(self.concepts[cid])
            if value:
                return value
            queue.extend(self.concepts[cid].parents)
        return None

    def descendants(self, concept_id: str) -> list[str]:
        out, stack = [], [concept_id]
        while stack:
            cid = stack.pop()
            if cid in out:
                continue
            out.append(cid)
            stack.extend(reversed(self.children.get(cid, [])))
        return out

    @property
    def opening_index(self):
        if self._opening_index is None:
            from .sources.lichess_openings import get_opening_index
            self._opening_index = get_opening_index()
        return self._opening_index

    # ------------------------------------------------------------ queries
    def verified(self, include_personal: bool = False) -> list[Example]:
        return [e for e in self.entries.values() if e.status == "verified"
                and (include_personal or e.tier != "personal")]

    def all_examples(self, include_unverified: bool = False) -> list[Example]:
        """Global + generated entries (for duplicate checks); personal ones are separate."""
        ok = ("verified", "needs_review", "candidate", "verifying") if include_unverified else ("verified",)
        return [e for e in self.entries.values() if e.tier != "personal" and e.status in ok]

    def get(self, example_id: str, trusted_only: bool = True) -> Example | None:
        example = self.entries.get(example_id)
        if example is None or (trusted_only and example.status != "verified"):
            return None
        return example

    def examples_for(self, concept_id: str, include_personal: bool = False) -> list[Example]:
        ids: list[str] = []
        indexes = [self._by_concept] + ([self._personal_by_concept] if include_personal else [])
        for cid in self.descendants(concept_id):
            for index in indexes:
                for eid in index.get(cid, []):
                    if eid not in ids:
                        ids.append(eid)
        return [self.entries[i] for i in ids if self.entries[i].status == "verified"]

    def count_for(self, concept_id: str) -> int:
        return len(self.examples_for(concept_id))

    def concept_for_topic(self, topic_id: str) -> Concept | None:
        """The most specific concept with examples that practises catalog topic `topic_id`."""
        matches = [c for c in self.concepts.values() if topic_id in c.topics and self.count_for(c.id)]
        matches.sort(key=lambda c: len(self.descendants(c.id)))
        return matches[0] if matches else None

    def match_concepts(self, text: str) -> list[str]:
        """Concepts named in free text, most specific first ("smothered mate" beats "mate")."""
        tokens = _normalize(text)
        if not tokens:
            return []
        scored: list[tuple[int, int, str, frozenset[int]]] = []
        for order, concept in enumerate(self.concepts.values()):
            best, best_span = 0, frozenset()
            for alias in [concept.name, concept.id.replace("_", " ")] + concept.aliases:
                phrase = _normalize(alias)
                span = _phrase_span(phrase, tokens)
                if span is not None and len(" ".join(phrase)) > best:
                    best, best_span = len(" ".join(phrase)), span
            if best:
                scored.append((best, order, concept.id, best_span))
        spans = [s[3] for s in scored]
        scored = [s for s in scored if not any(s[3] < other for other in spans)]
        scored.sort(key=lambda s: (min(s[3]), -s[0], s[1]))
        out: list[str] = []
        for s in scored:
            if any(s[3] == t[3] and (t[0] > s[0] or (t[0] == s[0] and t[1] < s[1])) for t in scored):
                continue
            out.append(s[2])
        return out

    def parse_query(self, text: str, context_concepts: list[str] | None = None) -> Query:
        q = parse_request(text)
        q.concepts = [c for c in self.match_concepts(text) if self.count_for(c)]
        if not q.concepts and context_concepts:
            for hint in context_concepts:
                for cid in ([hint] if hint in self.concepts else self.match_concepts(hint)):
                    if self.count_for(cid) and cid not in q.concepts:
                        q.concepts.append(cid)
        return q

    def search(self, concept: str | None = None, level: str | None = None, tag: str | None = None,
               category: str | None = None, text: str | None = None, mode: str | None = None,
               include_personal: bool = False) -> list[Example]:
        if concept:
            pool = self.examples_for(concept, include_personal)
        elif text:
            pool = []
            for cid in self.match_concepts(text):
                pool += [e for e in self.examples_for(cid, include_personal) if e not in pool]
        else:
            pool = self.verified(include_personal)
        if tag:
            pool = [e for e in pool if tag.lower() in (t.lower() for t in e.tags)]
        if category:
            pool = [e for e in pool if e.category == category]
        if level:
            pool = [e for e in pool if e.level == level]
        if mode:
            pool = [e for e in pool if mode in e.presentation_modes]
        return sorted(pool, key=lambda e: (e.difficulty, e.id))

    def select(self, query: Query, seen: dict[str, int] | None = None,
               last_used: dict[str, str] | None = None) -> list[Example]:
        """Pick `query.count` verified examples: varied, at the right level, fresh.

        * variety: grouped by primary concept and picked round-robin, so
          "checkmate" gives a back-rank mate, a smothered mate and a queen mate;
        * level: the requested difficulty band first ("really simple" = easiest);
        * freshness: least-seen first; examples used in the last RECENT_DAYS days
          only when nothing else fits;
        * presentation: only examples that support the requested mode.
        The result is ordered easiest first.
        """
        seen = seen or {}
        recent = _recent_ids(last_used or {}) if query.not_seen_recently else set()
        pool: list[Example] = []
        for cid in query.concepts:
            for e in self.examples_for(cid, query.include_personal):
                if e in pool or e.id in query.exclude:
                    continue
                if query.mode and query.mode not in e.presentation_modes:
                    continue
                if query.category and e.category != query.category:
                    continue
                if query.tags and not set(t.lower() for t in query.tags) & set(t.lower() for t in e.tags):
                    continue
                pool.append(e)
        if not pool:
            return []

        def score(e: Example) -> tuple:
            fresh = (e.id in recent, seen.get(e.id, 0))
            if query.simplest:
                return (e.difficulty,) + fresh + (e.id,)
            if query.target_rating is not None:
                # nearest to the learner's level first, in 100-point buckets so freshness still counts
                from .difficulty import puzzle_rating
                return fresh[:1] + (abs(puzzle_rating(e) - query.target_rating) // 100,) + fresh[1:] + (e.id,)
            return fresh + (_level_distance(e.difficulty, query.level), e.difficulty, e.id)

        groups: dict[str, list[Example]] = {}
        for e in sorted(pool, key=score):
            groups.setdefault(e.concept, []).append(e)
        order = sorted(groups.values(), key=lambda g: score(g[0]))
        picked: list[Example] = []
        while len(picked) < query.count and any(order):
            for group in order:
                if group and len(picked) < query.count:
                    picked.append(group.pop(0))
        if query.target_rating is not None:
            from .difficulty import puzzle_rating
            return sorted(picked, key=lambda e: (puzzle_rating(e), e.id))
        return sorted(picked, key=lambda e: (e.difficulty, e.id))

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for e in self.entries.values():
            by_status[e.status] = by_status.get(e.status, 0) + 1
            by_tier[e.tier] = by_tier.get(e.tier, 0) + 1
            if e.status == "verified":
                by_category[e.category] = by_category.get(e.category, 0) + 1
        return {"concepts": len(self.concepts), "verified": by_status.get("verified", 0),
                "by_status": by_status, "by_category": by_category, "by_tier": by_tier,
                "load_errors": len(self.load_errors)}


def _recent_ids(last_used: dict[str, str]) -> set[str]:
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=RECENT_DAYS)
    out = set()
    for eid, when in last_used.items():
        try:
            if _dt.datetime.fromisoformat(when) >= cutoff:
                out.add(eid)
        except (TypeError, ValueError):
            continue
    return out


def _level_distance(difficulty: int, level: str | None) -> int:
    band = {"beginner": (1, 2), "intermediate": (3, 3), "advanced": (4, 5)}.get(level or "")
    if not band:
        return 0
    lo, hi = band
    return 0 if lo <= difficulty <= hi else min(abs(difficulty - lo), abs(difficulty - hi))


_NUMBERS = {"one": 1, "an": 1, "a": 1, "single": 1, "two": 2, "couple": 2, "pair": 2,
            "three": 3, "few": 3, "some": 3, "several": 4, "four": 4, "five": 5}
_SIMPLEST = re.compile(r"\b(really|very|super|extra|most)\s+(simple|easy|basic)|\b(simplest|easiest)\b")
_LEVEL_WORDS = {
    "beginner": ("beginner", "beginners", "simple", "easy", "basic", "novice", "new to"),
    "intermediate": ("intermediate",),
    "advanced": ("advanced", "hard", "harder", "difficult", "tricky", "challenging", "complex", "expert"),
}


def parse_request(text: str) -> Query:
    """Count, level and presentation mode from a request (concepts are matched separately)."""
    low = " " + text.lower() + " "
    q = Query(text=text)
    m = re.search(r"((?:\S+\s+){0,3})(?:examples?|positions?|ways?|games?|patterns?|kinds?|types?)\b", low)
    if m:
        for word in reversed(m.group(1).split()):
            if word[-1] in "?.!,;:":  # a new sentence: "what is a fork? show examples"
                break
            if word.isdigit():
                q.count = int(word)
                break
            if word in _NUMBERS:
                q.count = _NUMBERS[word]
                break
    q.count = max(1, min(MAX_COUNT, q.count))
    if _SIMPLEST.search(low):
        q.simplest = True
        q.level = "beginner"
    else:
        for level, words in _LEVEL_WORDS.items():
            if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
                q.level = level
                break
    if re.search(r"\b(quiz|test me|let me (try|find|play)|interactive|my turn|practi[cs]e)\b", low):
        q.mode = "interactive"
    return q


_library: KnowledgeLibrary | None = None
_lock = threading.Lock()


def get_knowledge() -> KnowledgeLibrary:
    global _library
    with _lock:
        if _library is None:
            _library = KnowledgeLibrary()
        return _library


def set_knowledge(library: KnowledgeLibrary | None) -> None:
    global _library
    with _lock:
        _library = library
