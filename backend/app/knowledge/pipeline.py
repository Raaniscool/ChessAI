"""The verification pipeline: how a candidate becomes (or doesn't become) library knowledge.

    candidate
      → fen            the start position parses
      → position       it is a legal chess position
      → moves          every move is legal, in order
      → structure      fields, labels, notes, hints, modes, source are consistent
      → concept        the concept validator finds the idea on the board (facts)
      → engine         Stockfish profile for the concept (tactic / opening / ...)
      → solution       the key move and every accepted alternative are good moves
      → explanation    the text doesn't contradict the board or the facts
      → duplicate      not a copy / near-copy of an existing entry
    PASS → verified · FAIL → rejected · UNCERTAIN → needs_review

AI-generated knowledge never skips a stage: Qwen can propose an example, only
this pipeline can declare it verified. Everything except the engine stage is
deterministic; the engine stage runs at a fixed depth.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

import chess

from . import dedupe, engine_check, validators
from .facts import check_explanation
from .positions import ReplayError, replay
from .schema import Example, KnowledgeError, parse_example

PIPELINE_VERSION = 1


@dataclass
class Stage:
    name: str
    outcome: str  # pass | fail | uncertain | skipped
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "outcome": self.outcome, "detail": self.detail}


@dataclass
class VerificationReport:
    status: str  # verified | rejected | needs_review
    stages: list[Stage] = field(default_factory=list)
    example: Example | None = None

    @property
    def reasons(self) -> list[str]:
        return [f"{s.name}: {s.detail}" for s in self.stages if s.outcome in ("fail", "uncertain")]

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": self.reasons, "stages": [s.as_dict() for s in self.stages],
                "id": self.example.id if self.example else None}


def _verdict(stages: list[Stage]) -> str:
    if any(s.outcome == "fail" for s in stages):
        return "rejected"
    if any(s.outcome == "uncertain" for s in stages):
        return "needs_review"
    return "verified"


def rules_stages(raw: dict, library, tier: str = "generated") -> tuple[list[Stage], Example | None]:
    """fen → position → moves → structure. Stops at the first failure."""
    stages: list[Stage] = []
    fen = raw.get("start_fen") or chess.STARTING_FEN
    fen = chess.STARTING_FEN if fen == "startpos" else fen
    try:
        board = chess.Board(fen)
        stages.append(Stage("fen", "pass"))
    except (ValueError, TypeError) as exc:
        return stages + [Stage("fen", "fail", f"invalid FEN {fen!r}: {exc}")], None
    status = board.status()
    if status != chess.STATUS_VALID:
        flags = [n for n in dir(chess) if n.startswith("STATUS_") and getattr(chess, n) & status and n != "STATUS_VALID"]
        return stages + [Stage("position", "fail", "illegal position: " + ", ".join(f.lower()[7:] for f in flags))], None
    stages.append(Stage("position", "pass"))
    moves = raw.get("moves") or []
    moves = moves.split() if isinstance(moves, str) else list(moves)
    try:
        replay(fen, moves)
        stages.append(Stage("moves", "pass", f"{len(moves)} legal moves"))
    except ReplayError as exc:
        return stages + [Stage("moves", "fail", str(exc))], None
    try:
        example = parse_example(raw, library.concepts, tier=tier)
        stages.append(Stage("structure", "pass"))
    except KnowledgeError as exc:
        return stages + [Stage("structure", "fail", str(exc))], None
    return stages, example


def concept_stage(example: Example, library) -> Stage:
    """Every concept the entry claims (primary and extra) must pass its validator."""
    facts, checked = {}, []
    for cid in example.concepts:
        spec = library.validator_for(cid)
        if not spec:
            if cid == example.concept:
                return Stage("concept", "uncertain", f"concept {cid} has no validator yet")
            continue
        params = {k: v for k, v in spec.items() if k != "type"}
        if cid == example.concept:
            params.update(example.concept_params)
        ctx = validators.Ctx(example.replay(), key_ply=example.key_ply, mistake_ply=example.mistake_ply,
                             opening_index=library.opening_index)
        try:
            facts.setdefault(spec["type"], validators.run(spec["type"], ctx, params))
        except validators.Fail as exc:
            return Stage("concept", "fail", f"{library.concepts[cid].name}: {exc}")
        checked.append(spec["type"])
    example.facts = facts
    return Stage("concept", "pass", ", ".join(dict.fromkeys(checked)))


def verify_candidate(raw: dict, library, engine=None, depth: int = 14, tier: str = "generated",
                     check_duplicates: bool = True, method: str = "automated") -> VerificationReport:
    """Run the full pipeline on a raw example dict. Never raises for bad chess content."""
    stages, example = rules_stages(raw, library, tier)
    if example is None:
        return VerificationReport("rejected", stages)
    example.status = "verifying"

    stages.append(concept_stage(example, library))
    if stages[-1].outcome == "fail":
        return _finish(example, stages, None, None, depth, method)

    profile = library.engine_profile_for(example.concept)
    report = engine_check.verify(example, engine, profile, depth=depth)
    stages.append(Stage("engine", report.outcome if report.outcome != "skipped" else "skipped",
                        "; ".join(report.reasons) or profile))

    stages.append(_solution_stage(example, report, engine, depth, profile))

    texts = [example.explanation, example.description, example.prompt, *example.hints, *example.notes.values(),
             *(v.note for v in example.variations)]
    extra = [m for v in example.variations for m in v.moves]
    problems = check_explanation("\n".join(t for t in texts if t), example, extra_moves=extra)
    stages.append(Stage("explanation", "fail" if problems else "pass", "; ".join(problems[:5])))

    dup = None
    if check_duplicates:
        dup = dedupe.check(example, library.all_examples(include_unverified=True))
        outcome = "pass" if dup.allowed else "fail"
        stages.append(Stage("duplicate", outcome, dup.reason if dup.kind != "none" else ""))
    return _finish(example, stages, report, dup, depth, method)


def _solution_stage(example: Example, report, engine, depth: int, profile: str) -> Stage:
    if example.key_ply is None:
        return Stage("solution", "skipped", "no key move")
    rules_only = profile == "none"
    if engine is None:
        if rules_only:
            return Stage("solution", "pass", "rules concept: the concept validator identified the key move")
        return Stage("solution", "uncertain", "engine unavailable: the solution is unchecked")
    rep = example.replay()
    board = rep.boards[example.key_ply]
    key = (report.details or {}).get("key_move")
    if key is None:  # profile didn't judge the key move (e.g. opening / endgame): judge it now
        try:
            key = engine_check.judge_move(engine, board, rep.moves[example.key_ply], depth).as_dict()
        except Exception as exc:
            return Stage("solution", "uncertain", f"engine error: {exc}")
        report.details["key_move"] = key
    if rules_only and key["category"] not in ("mistake", "blunder"):
        return Stage("solution", "pass", f"rules concept; Stockfish: {key['category']}")
    if rules_only:
        return Stage("solution", "uncertain", f"the rules example's key move {key['move']} is a "
                                              f"{key['category']} (best: {key['best_move']})")
    if key["category"] not in engine_check.GOOD_CATEGORIES:
        return Stage("solution", "fail", f"the solution {key['move']} is a {key['category']} "
                                         f"(best: {key['best_move']})")
    for san in example.accepted:
        move = board.parse_san(san)
        try:
            j = engine_check.judge_move(engine, board, move, depth)
        except Exception as exc:
            return Stage("solution", "uncertain", f"engine error: {exc}")
        if j.category not in engine_check.GOOD_CATEGORIES:
            return Stage("solution", "fail", f"accepted move {san} is a {j.category} (best: {j.best_move})")
    alts = [a for a in key.get("alternatives", []) if a not in example.accepted]
    return Stage("solution", "pass", ("also good: " + ", ".join(alts)) if alts else key["category"])


def _finish(example: Example, stages: list[Stage], report, dup, depth: int, method: str) -> VerificationReport:
    status = _verdict(stages)
    example.status = status
    engine_details = report.as_dict() if report is not None else None
    example.verification = {
        "status": status,
        "pipeline_version": PIPELINE_VERSION,
        "method": method,
        "checked": [s.name for s in stages if s.outcome == "pass"],
        "stages": [s.as_dict() for s in stages],
        "verified_at": _dt.date.today().isoformat(),
        "engine": engine_details,
        "alternatives": (engine_details or {}).get("key_move", {}).get("alternatives", []) if engine_details else [],
        "duplicate": dup.as_dict() if dup and dup.kind != "none" else None,
    }
    example.verification = {k: v for k, v in example.verification.items() if v not in (None, [], {})}
    return VerificationReport(status, stages, example)
