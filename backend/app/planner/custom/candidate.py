"""One format for every candidate plan, whoever proposed it.

The deterministic composer, Qwen and the plan library all produce a CandidatePlan. The
validator only ever sees this format, so every candidate gets exactly the same checks.

Items say where their content came from (`provenance`). Only these are trusted as
already verified: library entries with status "verified", catalog puzzles/lines (checked
by Stockfish when the catalog was built), and items of a plan that was verified before
being stored in the plan library. Everything else — Qwen-proposed positions, opening
lines from the database, newly generated positions — is checked here before use.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..intent.model import Component, MaterialSpec

TRUSTED = {"library-verified", "catalog-verified", "plan-library"}
ITEM_KINDS = ("library", "catalog", "proposed", "generated", "topic", "opening_line", "definition", "unknown")
ROLES = ("demonstration", "guided", "practice", "lesson")
ROLE_ORDER = {"demonstration": 0, "guided": 1, "practice": 2, "lesson": 1}


@dataclass
class Item:
    kind: str
    ref: str
    role: str = "practice"
    provenance: str = "unverified"
    fen: str | None = None
    moves: list[str] = field(default_factory=list)   # SAN from `fen`
    key_index: int | None = None                     # index of the learner's key move in `moves`
    claims: dict = field(default_factory=dict)       # {"result": win|draw, "mate_in": n, "best": bool}
    concept: str | None = None                       # the idea the position is claimed to show
    title: str = ""
    text: str = ""                                   # explanation written for this item
    difficulty: int = 3
    line: list[str] = field(default_factory=list)    # opening lines (SAN from the start)
    side: str | None = None                          # opening lines: the learner's side
    verification: dict = field(default_factory=dict)  # stored results (plan library)
    example: object = None                           # runtime: the Example used to render it

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in ("kind", "ref", "role", "provenance", "fen", "moves", "key_index",
                                             "claims", "concept", "title", "text", "difficulty", "line", "side",
                                             "verification")}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})} | {"kind": self.kind, "ref": self.ref}

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        keys = cls.__dataclass_fields__.keys() - {"example"}
        return cls(**{k: d[k] for k in keys if k in d})

    @property
    def trusted(self) -> bool:
        return self.provenance in TRUSTED

    def content_key(self) -> str:
        return f"{self.kind}:{self.ref}:{self.fen or ''}:{' '.join(self.moves or self.line)}"


@dataclass
class Unit:
    title: str
    objective: str
    component: dict                  # Component.as_dict() — what part of the request this unit serves
    role: str = "learn"              # learn | practice | topic | opening | definition
    items: list[Item] = field(default_factory=list)
    text: str = ""                   # the unit's explanation (may come from Qwen: checked against facts)

    @property
    def component_obj(self) -> Component:
        return Component.from_dict(self.component)

    @property
    def material(self) -> MaterialSpec | None:
        return self.component_obj.material

    def difficulty(self) -> float:
        ds = sorted(i.difficulty for i in self.items) or [3]
        return ds[len(ds) // 2]

    def as_dict(self) -> dict:
        return {"title": self.title, "objective": self.objective, "component": self.component, "role": self.role,
                "text": self.text, "items": [i.as_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, d: dict) -> "Unit":
        return cls(d["title"], d.get("objective", ""), d["component"], d.get("role", "learn"),
                   [Item.from_dict(i) for i in d.get("items", [])], d.get("text", ""))


@dataclass
class CandidatePlan:
    goal: str
    intent: dict
    title: str
    summary: str
    units: list[Unit]
    proposer: str = "composer"       # composer | qwen | plan-library
    level: str | None = None
    personal: dict | None = None     # {"learner", "weakness", "concept", "evidence", "evidence_fens", "level"}
    learner: str | None = None
    skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Hash of the teaching content (not the wording of the title): what the validator
        checked. A report only vouches for the candidate with this exact fingerprint."""
        data = [[u.component, u.role, u.text, [i.content_key() + "|" + i.role + "|" + i.text for i in u.items]]
                for u in self.units]
        return hashlib.sha1(json.dumps([data, self.personal and self.personal.get("weakness")],
                                       sort_keys=True, default=str).encode()).hexdigest()[:20]

    def items(self):
        for u in self.units:
            yield from u.items

    def content_refs(self) -> set[str]:
        return {i.content_key() for i in self.items()}

    def personal_content(self) -> bool:
        """Does the plan contain anything that belongs to one learner?"""
        if self.personal:
            return True
        return any(i.ref.startswith("mygen_") or i.provenance == "personal" or "personal" in i.verification
                   for i in self.items())

    def as_dict(self) -> dict:
        return {"goal": self.goal, "intent": self.intent, "title": self.title, "summary": self.summary,
                "units": [u.as_dict() for u in self.units], "proposer": self.proposer, "level": self.level,
                "personal": self.personal, "learner": self.learner, "skipped": self.skipped, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> "CandidatePlan":
        return cls(d.get("goal", ""), d.get("intent", {}), d.get("title", ""), d.get("summary", ""),
                   [Unit.from_dict(u) for u in d.get("units", [])], d.get("proposer", "composer"), d.get("level"),
                   d.get("personal"), d.get("learner"), d.get("skipped", []), d.get("notes", []))
