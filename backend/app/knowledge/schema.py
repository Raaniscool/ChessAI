"""Knowledge Library data model: concepts and teaching examples.

A *teaching example* is a short, curated move sequence that shows one idea on the
board: a start position, up to 10 moves per side, notes, hints, an explanation,
where it came from (``source``) and how it was verified (``verification``).
Examples are data; see docs/KNOWLEDGE_LIBRARY.md for the field reference.

This module does structure + rules checks (valid FEN, legal moves, consistent
labels). Concept checks live in validators.py, engine checks in engine_check.py,
and pipeline.py runs all of them to decide an entry's status.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

from .positions import Replay, ReplayError, replay

CATEGORIES = ("basics", "tactics", "checkmates", "openings", "endgames", "mistakes")
STATUSES = ("candidate", "verifying", "verified", "rejected", "needs_review", "deprecated")
PRESENTATION_MODES = ("demonstration", "interactive", "hint", "practice", "deep_dive")
ENGINE_PROFILES = ("none", "tactic", "mate", "opening", "mistake", "principle", "endgame_win", "endgame_draw")
SOURCE_TYPES = ("curated", "lichess_puzzle", "lichess_openings", "historical_game", "rules_reference",
                "endgame_theory", "qwen_generated", "user_submitted", "user_game")
MAX_PLIES = 20  # "up to 10 moves" for each side
LEVELS = {1: "beginner", 2: "beginner", 3: "intermediate", 4: "advanced", 5: "advanced"}
LEVEL_TO_DIFFICULTY = {"beginner": 1, "intermediate": 3, "advanced": 4}
ID_RE = re.compile(r"^[a-z0-9_]+$")


class KnowledgeError(ValueError):
    pass


@dataclass
class Concept:
    id: str
    name: str
    category: str
    summary: str
    aliases: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    validator: dict = field(default_factory=dict)  # {"type": <validator>, ...params}
    engine: str | None = None  # engine verification profile (None = inherit from parents)
    topics: list[str] = field(default_factory=list)  # planner catalog topics for practice

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category, "summary": self.summary,
                "aliases": self.aliases, "parents": self.parents, "related": self.related,
                "prerequisites": self.prerequisites, "topics": self.topics}


@dataclass
class Variation:
    after: str  # move label the side line branches from ("start" = before move 1)
    moves: list[str]
    note: str


@dataclass
class Example:
    id: str
    status: str
    title: str
    concept: str
    concepts: list[str]
    category: str
    subcategory: str
    difficulty: int
    description: str
    start_fen: str
    moves: list[str]  # normalized SAN
    uci: list[str]
    labels: list[str]  # "1.e4", "1...e5", ...
    final_fen: str
    notes: dict[str, str]  # move label -> comment
    explanation: str
    key_move: str | None  # label of the move the learner finds (interactive / practice)
    mistake_move: str | None  # label of the mistake (mistake examples)
    accepted: list[str]  # other SAN moves accepted at the key move
    prompt: str
    hints: list[str]
    highlights: list[dict]
    tags: list[str]
    related: list[str]
    prerequisites: list[str]
    presentation_modes: list[str]
    variations: list[Variation]
    concept_params: dict  # extra constraints for the concept validator (e.g. fork targets)
    facts: dict  # verified facts from the concept validator (filled by the pipeline)
    source: dict
    verification: dict
    teaching_purpose: str = ""
    tier: str = "global"  # global | generated | personal
    path: str = ""

    @property
    def level(self) -> str:
        return LEVELS[self.difficulty]

    @property
    def key_ply(self) -> int | None:
        return self.labels.index(self.key_move) if self.key_move else None

    @property
    def mistake_ply(self) -> int | None:
        return self.labels.index(self.mistake_move) if self.mistake_move else None

    @property
    def solution(self) -> str | None:
        return self.moves[self.key_ply] if self.key_move else None

    def comments(self) -> list[str]:
        return [self.notes.get(label, "") for label in self.labels]

    def replay(self) -> Replay:
        return replay(self.start_fen, self.moves)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "concept": self.concept, "concepts": self.concepts,
                "category": self.category, "subcategory": self.subcategory,
                "difficulty": self.difficulty, "level": self.level, "description": self.description,
                "presentation_modes": self.presentation_modes, "tags": self.tags,
                "plies": len(self.moves), "status": self.status, "tier": self.tier,
                "source_type": self.source.get("source_type")}

    def to_record(self) -> dict:
        """The stored form (what goes back into a JSON file)."""
        out = {
            "id": self.id, "status": self.status, "title": self.title, "concept": self.concept,
            "concepts": self.concepts[1:], "category": self.category, "subcategory": self.subcategory,
            "difficulty": self.difficulty, "description": self.description,
            "start_fen": self.start_fen, "moves": self.moves, "final_fen": self.final_fen,
            "key_move": self.key_move, "mistake_move": self.mistake_move, "accepted": self.accepted,
            "prompt": self.prompt, "hints": self.hints, "notes": self.notes,
            "explanation": self.explanation, "highlights": self.highlights, "tags": self.tags,
            "related": self.related, "prerequisites": self.prerequisites,
            "presentation_modes": self.presentation_modes,
            "variations": [{"after": v.after, "moves": v.moves, "note": v.note} for v in self.variations],
            "concept_params": self.concept_params, "teaching_purpose": self.teaching_purpose,
            "facts": self.facts, "source": self.source, "verification": self.verification,
        }
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}

    def as_dict(self) -> dict:
        out = self.to_record()
        out.update({"concepts": self.concepts, "uci": self.uci, "labels": self.labels,
                    "level": self.level, "solution": self.solution,
                    "move_explanations": self.comments(), "tier": self.tier})
        return out


def parse_concept(raw: dict) -> Concept:
    for key in ("id", "name", "category", "summary"):
        if not raw.get(key):
            raise KnowledgeError(f"concept missing {key}: {raw.get('id')}")
    if not ID_RE.match(raw["id"]):
        raise KnowledgeError(f"bad concept id {raw['id']!r}")
    if raw["category"] not in CATEGORIES:
        raise KnowledgeError(f"concept {raw['id']}: bad category {raw['category']!r}")
    engine = raw.get("engine")
    if engine is not None and engine not in ENGINE_PROFILES:
        raise KnowledgeError(f"concept {raw['id']}: unknown engine profile {engine!r}")
    validator = dict(raw.get("validator", {}))
    if validator:
        from .validators import REGISTRY
        if validator.get("type") not in REGISTRY:
            raise KnowledgeError(f"concept {raw['id']}: unknown validator {validator.get('type')!r}")
    return Concept(id=raw["id"], name=raw["name"], category=raw["category"], summary=raw["summary"],
                   aliases=list(raw.get("aliases", [])), parents=list(raw.get("parents", [])),
                   related=list(raw.get("related", [])), prerequisites=list(raw.get("prerequisites", [])),
                   validator=validator, engine=engine, topics=list(raw.get("topics", [])))


def _str_list(raw: dict, key: str, eid: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise KnowledgeError(f"{eid}: {key} must be a list of non-empty strings")
    return [v.strip() for v in value]


def _difficulty(raw, eid: str) -> int:
    value = raw.get("difficulty")
    if isinstance(value, str) and value in LEVEL_TO_DIFFICULTY:
        return LEVEL_TO_DIFFICULTY[value]
    if isinstance(value, int) and not isinstance(value, bool) and value in LEVELS:
        return value
    raise KnowledgeError(f"{eid}: difficulty must be 1-5 or beginner/intermediate/advanced")


def parse_example(raw: dict, concepts: dict[str, Concept], path: str = "", tier: str = "global") -> Example:
    """Structure + rules validation. Raises KnowledgeError with a precise reason."""
    if not isinstance(raw, dict):
        raise KnowledgeError("an example must be a JSON object")
    eid = raw.get("id") or "?"
    try:
        return _parse(raw, concepts, path, tier)
    except ReplayError as exc:
        raise KnowledgeError(f"{eid}: {exc}") from exc


def _parse(raw: dict, concepts: dict[str, Concept], path: str, tier: str) -> Example:
    eid = raw.get("id") or "?"
    for key in ("id", "title", "concept", "category", "description", "moves", "explanation"):
        if raw.get(key) in (None, "", []):
            raise KnowledgeError(f"{eid}: missing {key}")
    if not ID_RE.match(eid):
        raise KnowledgeError(f"bad example id {eid!r} (lowercase letters, digits, _)")
    status = raw.get("status", "candidate")
    if status not in STATUSES:
        raise KnowledgeError(f"{eid}: unknown status {status!r}")
    if raw["category"] not in CATEGORIES:
        raise KnowledgeError(f"{eid}: bad category {raw['category']!r}")
    difficulty = _difficulty(raw, eid)

    concept_ids = [raw["concept"]] + [c for c in raw.get("concepts", []) if c != raw["concept"]]
    for key, ids in (("concept", concept_ids), ("related", raw.get("related", [])),
                     ("prerequisites", raw.get("prerequisites", []))):
        for cid in ids:
            if cid not in concepts:
                raise KnowledgeError(f"{eid}: unknown {key} {cid!r}")

    # --- rules: valid position, legal moves, consistent final position
    start_fen = raw.get("start_fen") or chess.STARTING_FEN
    if start_fen == "startpos":
        start_fen = chess.STARTING_FEN
    moves = raw["moves"].split() if isinstance(raw["moves"], str) else list(raw["moves"])
    if not 1 <= len(moves) <= MAX_PLIES:
        raise KnowledgeError(f"{eid}: needs 1-{MAX_PLIES} moves, has {len(moves)}")
    rep = replay(start_fen, moves)
    start_fen = rep.boards[0].fen()
    final_fen = rep.final.fen()
    if raw.get("final_fen") and raw["final_fen"] != final_fen:
        raise KnowledgeError(f"{eid}: final_fen doesn't match the moves (they end in {final_fen})")

    notes = raw.get("notes", {})
    if not isinstance(notes, dict):
        raise KnowledgeError(f"{eid}: notes must be an object keyed by move label")
    for label, text in notes.items():
        if label not in rep.labels:
            raise KnowledgeError(f"{eid}: note for {label!r}, but the moves are {' '.join(rep.labels)}")
        if not isinstance(text, str) or not text.strip():
            raise KnowledgeError(f"{eid}: empty note for {label}")

    # --- key move / mistake move / accepted alternatives
    key_move, mistake_move = raw.get("key_move"), raw.get("mistake_move")
    for name, label in (("key_move", key_move), ("mistake_move", mistake_move)):
        if label is not None and label not in rep.labels:
            raise KnowledgeError(f"{eid}: {name} {label!r} isn't one of {' '.join(rep.labels)}")
    accepted = _str_list(raw, "accepted", eid)
    hints = _str_list(raw, "hints", eid)
    if key_move:
        before = rep.boards[rep.ply(key_move)]
        normalized = []
        for san in accepted:
            try:
                normalized.append(before.san(before.parse_san(san)))
            except ValueError:
                raise KnowledgeError(f"{eid}: accepted move {san!r} is illegal at {key_move}") from None
        accepted = [a for a in dict.fromkeys(normalized) if a != rep.sans[rep.ply(key_move)]]
    elif accepted:
        raise KnowledgeError(f"{eid}: accepted moves without a key_move")

    modes = raw.get("presentation_modes") or (
        ["demonstration", "interactive", "hint", "practice", "deep_dive"] if key_move
        else ["demonstration", "deep_dive"])
    for mode in modes:
        if mode not in PRESENTATION_MODES:
            raise KnowledgeError(f"{eid}: unknown presentation mode {mode!r}")
        if mode in ("interactive", "hint", "practice") and not key_move:
            raise KnowledgeError(f"{eid}: presentation mode {mode} needs a key_move")
    if key_move and not hints and "interactive" in modes:
        raise KnowledgeError(f"{eid}: an interactive example needs at least one hint")

    highlights = raw.get("highlights", [])
    from ..chess_system import ChessError, validate_highlights
    try:
        validate_highlights(highlights)
    except ChessError as exc:
        raise KnowledgeError(f"{eid}: {exc}") from exc

    variations = []
    for v in raw.get("variations", []):
        after = v.get("after", "start")
        vmoves = v["moves"].split() if isinstance(v.get("moves"), str) else list(v.get("moves", []))
        if not vmoves or not v.get("note"):
            raise KnowledgeError(f"{eid}: a variation needs moves and a note")
        replay(rep.position(after).fen(), vmoves)  # legal side line
        variations.append(Variation(after=after, moves=vmoves, note=v["note"]))

    source = raw.get("source") or {}
    if not isinstance(source, dict) or source.get("source_type") not in SOURCE_TYPES:
        raise KnowledgeError(f"{eid}: source.source_type must be one of {', '.join(SOURCE_TYPES)}")

    return Example(
        id=eid, status=status, title=raw["title"].strip(), concept=raw["concept"], concepts=concept_ids,
        category=raw["category"], subcategory=raw.get("subcategory", ""), difficulty=difficulty,
        description=raw["description"].strip(), start_fen=start_fen, moves=rep.sans,
        uci=[m.uci() for m in rep.moves], labels=rep.labels, final_fen=final_fen,
        notes={k: v.strip() for k, v in notes.items()}, explanation=raw["explanation"].strip(),
        key_move=key_move, mistake_move=mistake_move, accepted=accepted,
        prompt=(raw.get("prompt") or "").strip(), hints=hints, highlights=highlights,
        tags=_str_list(raw, "tags", eid), related=list(raw.get("related", [])),
        prerequisites=list(raw.get("prerequisites", [])), presentation_modes=list(modes),
        variations=variations, concept_params=dict(raw.get("concept_params", {})),
        facts=dict(raw.get("facts", {})), source=dict(source),
        verification=dict(raw.get("verification", {})),
        teaching_purpose=(raw.get("teaching_purpose") or "").strip(), tier=tier, path=path,
    )
