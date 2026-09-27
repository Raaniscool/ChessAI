"""Validate a candidate plan before anyone sees it.

Every candidate — from the composer, from Qwen, or reloaded from the plan library — goes
through the same registry of named checks, grouped in families:

  legality         python-chess: FENs parse, positions are legal, moves are legal,
                   the key move is the learner's, opening lines replay from the start
  correctness      Stockfish: untrusted positions go through the Knowledge Library's own
                   verification pipeline (concept validator, engine profile, explanation
                   facts, duplicates); every claim ("wins", "draws", "mate in n", "best")
                   is compared with the engine; the solution line is sound; the exercise
                   actually discriminates; untrusted opening lines are screened move by move
  education        units have objectives, there is practice, prerequisites come first,
                   difficulty progresses, the level fits, demonstrations precede practice,
                   no position appears twice, no two units do the same job, size limits
  personalization  a weakness-triggered plan targets that weakness, is backed by game
                   evidence, fits the learner's level, and doesn't copy the learner's games
  consistency      every unit belongs to the request, every requested subject is covered,
                   every item demonstrates its unit's subject (concept family or material
                   predicate), nothing excluded slipped in, texts match verified facts
  duplication      new positions aren't already in the library; the plan isn't a near copy
                   of a stored plan
  provenance       items that claim to be verified really are (library status, catalog)

Severity: "error" (rejected), "uncertain" (needs review — e.g. Stockfish unavailable for
an untrusted position), "warning" (shown in the report, doesn't block). A report vouches
only for the candidate whose fingerprint it carries.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import chess

from ..intent.model import LearningIntent, MaterialSpec
from .candidate import ROLE_ORDER, CandidatePlan, Item, Unit

log = logging.getLogger(__name__)

MAX_UNITS = 8
MAX_ITEMS = 8
KEY_TOLERANCE = 60        # cp: a key move this close to the engine's best is sound
WIN_CP = 200              # "wins" claims need at least this much for the learner
DRAW_CP = 80              # "draws" claims need |eval| at most this
DISCRIMINATION_CP = 30    # all top moves within this of each other: nothing to find
LEVEL_WINDOW = {"beginner": (1, 3), "intermediate": (2, 4), "advanced": (3, 5)}
_SAN_IN_TEXT = re.compile(r"(?<![\w.])(?:\d+\.(?:\.\.)?\s*)?((?:[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8]|O-O(?:-O)?)"
                          r"(?:=[QRBN])?[+#]?)")


@dataclass
class Issue:
    check: str
    family: str
    severity: str        # error | uncertain | warning
    message: str
    unit: int | None = None
    item: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ValidationReport:
    status: str                      # verified | needs_review | rejected
    issues: list[Issue]
    checks: list[str]
    fingerprint: str
    validated_at: str
    engine: str | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def uncertain(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "uncertain"]

    def as_dict(self) -> dict:
        return {"status": self.status, "issues": [i.as_dict() for i in self.issues], "checks": self.checks,
                "fingerprint": self.fingerprint, "validated_at": self.validated_at, "engine": self.engine}


@dataclass
class Context:
    library: object
    catalog: object
    content: object
    intent: LearningIntent
    level: str | None = None
    engine: object = None
    engine_factory: object = None
    depth: int = 12
    personal: dict | None = None
    plan_library: object = None
    learner: str | None = None
    _engine_failed: bool = field(default=False, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)

    def get_engine(self):
        if self.engine is None and self.engine_factory is not None and not self._engine_failed:
            try:
                self.engine = self.engine_factory()
            except Exception as exc:  # EngineUnavailable and friends
                log.info("Stockfish unavailable for plan validation: %s", exc)
                self._engine_failed = True
        return self.engine


# ---------------------------------------------------------------------- helpers
def _replay(item: Item) -> tuple[list[chess.Board], list[chess.Move]] | str:
    """Boards before each move (plus the final one), or a reason it can't be replayed."""
    try:
        board = chess.Board(item.fen) if item.fen else chess.Board()
    except ValueError as exc:
        return f"invalid FEN: {exc}"
    boards, moves = [board.copy(stack=False)], []
    for san in (item.moves if item.fen or item.kind != "opening_line" else item.line):
        try:
            move = board.parse_san(san)
        except ValueError:
            return f"illegal move {san!r} in {board.fen()}"
        moves.append(move)
        board.push(move)
        boards.append(board.copy(stack=False))
    return boards, moves


def _learner_board(item: Item) -> chess.Board | None:
    got = _replay(item)
    if isinstance(got, str):
        return None
    boards, _ = got
    k = item.key_index if item.key_index is not None else 0
    return boards[min(k, len(boards) - 1)]


def _positions(cand: CandidatePlan):
    for n, u in enumerate(cand.units):
        for it in u.items:
            if it.fen is not None and it.kind != "opening_line":
                yield n, u, it


def _mover_cp(score, turn: chess.Color) -> int:
    """Engine score (White POV) as centipawns for the side to move; mates as ±(10000 - n)."""
    kind, value = score.kind, score.value
    cp = (10000 - abs(value)) * (1 if value > 0 else -1) if kind == "mate" else value
    return cp if turn == chess.WHITE else -cp


def _family(library, cid: str) -> set[str]:
    return {cid, *library.descendants(cid)} if cid in library.concepts else {cid}


def _clean_san(s: str) -> str:
    return re.sub(r"^\d+\.(\.\.)?\s*", "", s).rstrip("+#!?")


# ---------------------------------------------------------------------- registry
CHECKS: list[tuple[str, str, object]] = []


def check(name: str, family: str):
    def wrap(fn):
        CHECKS.append((name, family, fn))
        return fn
    return wrap


# ------------------------------------------------------------------ legality
@check("fen_valid", "legality")
def _fen_valid(cand, ctx):
    for n, u, it in _positions(cand):
        try:
            chess.Board(it.fen)
        except ValueError as exc:
            yield Issue("fen_valid", "legality", "error", f"“{it.title or it.ref}” has an invalid FEN: {exc}", n, it.ref)


@check("position_legal", "legality")
def _position_legal(cand, ctx):
    for n, u, it in _positions(cand):
        try:
            board = chess.Board(it.fen)
        except ValueError:
            continue
        if not board.is_valid():
            yield Issue("position_legal", "legality", "error",
                        f"“{it.title or it.ref}” is an impossible position ({board.status()!r})", n, it.ref)
        elif board.is_game_over():
            yield Issue("position_legal", "legality", "error", f"“{it.title or it.ref}” starts with the game already over",
                        n, it.ref)


@check("moves_legal", "legality")
def _moves_legal(cand, ctx):
    for n, u in enumerate(cand.units):
        for it in u.items:
            if it.kind in ("topic", "definition", "unknown"):
                continue
            if it.fen is not None:
                try:
                    if not chess.Board(it.fen).is_valid():
                        continue  # reported by position_legal
                except ValueError:
                    continue
            got = _replay(it)
            if isinstance(got, str):
                yield Issue("moves_legal", "legality", "error", f"“{it.title or it.ref}”: {got}", n, it.ref)
            elif it.kind != "opening_line" and not it.moves:
                yield Issue("moves_legal", "legality", "error", f"“{it.title or it.ref}” has no moves", n, it.ref)


@check("key_move_is_learners", "legality")
def _key_move(cand, ctx):
    for n, u, it in _positions(cand):
        if it.key_index is None:
            continue
        if not 0 <= it.key_index < len(it.moves):
            yield Issue("key_move_is_learners", "legality", "error",
                        f"“{it.title or it.ref}”: the key move index {it.key_index} is outside the solution", n, it.ref)


# ------------------------------------------------------------------ provenance
@check("provenance_verified", "provenance")
def _provenance(cand, ctx):
    lib, cat = ctx.library, ctx.catalog
    for n, u in enumerate(cand.units):
        for it in u.items:
            if it.provenance == "library-verified":
                ex = lib.get(it.ref, trusted_only=True)
                if ex is None or ex.status != "verified":
                    yield Issue("provenance_verified", "provenance", "error",
                                f"{it.ref} is presented as verified library content but isn't", n, it.ref)
                elif ex.tier == "personal" and not (cand.personal or cand.learner):
                    yield Issue("provenance_verified", "provenance", "error",
                                f"{it.ref} is a learner's personal position in a shared plan", n, it.ref)
            elif it.provenance == "catalog-verified":
                if it.kind == "catalog":
                    tid, _, pid = it.ref.partition(":")[2].partition("/")
                    topic = cat.topics.get(tid)
                    if topic is None or pid not in {p["id"] for p in topic.puzzles or []}:
                        yield Issue("provenance_verified", "provenance", "error",
                                    f"{it.ref} isn't a catalog puzzle", n, it.ref)
                elif it.kind in ("topic", "opening_line"):
                    tid = it.ref.split("@")[0]
                    topic = cat.topics.get(tid)
                    if topic is None:
                        yield Issue("provenance_verified", "provenance", "error", f"no catalog topic {tid}", n, it.ref)
                    elif it.kind == "opening_line" and list(it.line) != list(topic.line):
                        yield Issue("provenance_verified", "provenance", "error",
                                    f"the line for {topic.title} differs from the verified catalog line", n, it.ref)
            elif it.provenance == "plan-library":
                if (it.verification or {}).get("status") != "verified":
                    yield Issue("provenance_verified", "provenance", "error",
                                f"{it.ref} came from a stored plan without a verification record", n, it.ref)
            elif it.kind == "unknown":
                yield Issue("provenance_verified", "provenance", "error",
                            f"“{it.ref}” doesn't refer to anything in the library or catalog", n, it.ref)


# ------------------------------------------------------------------ correctness (Stockfish)
def _raw_for(item: Item, unit: Unit, library) -> dict:
    board = chess.Board(item.fen)
    from ...knowledge.positions import replay
    labels = replay(item.fen, item.moves).labels
    concept = item.concept
    cat = library.concepts[concept].category
    side = "White" if board.turn == chess.WHITE else "Black"
    return {
        "id": "plan_" + re.sub(r"[^a-z0-9]", "", (item.ref or "x").lower())[:24], "title": item.title or unit.title,
        "concept": concept, "category": cat, "description": f"{side} to move.",
        "start_fen": item.fen, "moves": item.moves, "key_move": labels[item.key_index or 0],
        "explanation": item.text or f"{labels[item.key_index or 0]} is the key move.",
        "prompt": "Find the best move.", "hints": ["Look at checks, captures and threats first."],
        "presentation_modes": ["interactive", "practice"], "difficulty": max(1, min(5, item.difficulty)),
        "source": {"source_type": "plan_candidate", "source_license": "generated by ChessAI",
                   "reference": f"proposed for a custom plan by {item.provenance}"},
    }


def _engine_lines(ctx, board: chess.Board, multipv: int):
    key = (board.fen(), multipv)
    if key not in ctx._cache:
        ctx._cache[key] = ctx.get_engine().analyse_lines(board, depth=ctx.depth, multipv=multipv, fresh=True)
    return ctx._cache[key]


def _move_cp(ctx, board: chess.Board, move: chess.Move, lines) -> int:
    for ln in lines:
        if ln.move == move:
            return _mover_cp(ln.score, board.turn)
    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        return 9999
    if after.is_game_over():
        return 0
    reply = _engine_lines(ctx, after, 1)
    return -_mover_cp(reply[0].score, after.turn) if reply else 0


@check("engine_verified", "correctness")
def _engine_verified(cand, ctx):
    """Untrusted positions: Knowledge Library pipeline (concept items) + claims vs Stockfish."""
    from ...knowledge.pipeline import verify_candidate

    for n, u, it in _positions(cand):
        if it.trusted:
            continue
        got = _replay(it)
        if isinstance(got, str):
            continue  # legality already failed
        boards, moves = got
        try:
            if not boards[0].is_valid():
                continue
        except Exception:
            continue
        engine = ctx.get_engine()
        if engine is None:
            yield Issue("engine_verified", "correctness", "uncertain",
                        f"“{it.title or it.ref}” is a new position and Stockfish isn't available to check it", n, it.ref)
            continue
        k = it.key_index or 0
        board, key = boards[k], moves[k]
        lines = _engine_lines(ctx, board, 3)
        if not lines:
            yield Issue("engine_verified", "correctness", "error", f"Stockfish found no move in “{it.title}”", n, it.ref)
            continue
        best_cp = _mover_cp(lines[0].score, board.turn)
        key_cp = _move_cp(ctx, board, key, lines)
        san = board.san(key)
        decisive_best = best_cp > 9000
        if (decisive_best and key_cp < 9000) or key_cp < best_cp - KEY_TOLERANCE:
            yield Issue("key_move_sound", "correctness", "error",
                        f"“{it.title or it.ref}”: {san} isn't sound — Stockfish prefers {lines[0].san}", n, it.ref)
            continue
        claims = it.claims or {}
        if claims.get("result") == "win" and key_cp < WIN_CP:
            yield Issue("claim_matches_engine", "correctness", "error",
                        f"“{it.title or it.ref}” claims {san} wins, but Stockfish rates it {key_cp / 100:+.1f}", n, it.ref)
        if claims.get("result") == "draw" and abs(key_cp) > DRAW_CP and key_cp < 9000:
            yield Issue("claim_matches_engine", "correctness", "error",
                        f"“{it.title or it.ref}” claims a draw, but Stockfish rates it {key_cp / 100:+.1f}", n, it.ref)
        if claims.get("mate_in"):
            want = int(claims["mate_in"])
            got_mate = 10000 - key_cp if key_cp > 9000 else None
            if got_mate != want:
                yield Issue("claim_matches_engine", "correctness", "error",
                            f"“{it.title or it.ref}” claims mate in {want}, but Stockfish "
                            + (f"finds mate in {got_mate}" if got_mate else "finds no forced mate"), n, it.ref)
        if claims.get("best") and lines[0].move != key and key_cp < best_cp - 15:
            yield Issue("claim_matches_engine", "correctness", "error",
                        f"“{it.title or it.ref}” calls {san} the best move; Stockfish prefers {lines[0].san}", n, it.ref)
        if it.role in ("guided", "practice") and len(lines) >= 3 and not decisive_best:
            spread = best_cp - min(_mover_cp(ln.score, board.turn) for ln in lines[:3])
            if spread <= DISCRIMINATION_CP:
                yield Issue("discriminates", "correctness", "error",
                            f"“{it.title or it.ref}”: several moves are equally good, so the exercise tests nothing",
                            n, it.ref)
        # the rest of the learner's solution must be sound too (first two more moves)
        for ply in [p for p in range(k + 2, len(moves), 2)][:2]:
            b = boards[ply]
            ls = _engine_lines(ctx, b, 1)
            if ls and _move_cp(ctx, b, moves[ply], ls) < _mover_cp(ls[0].score, b.turn) - KEY_TOLERANCE * 2:
                yield Issue("solution_line_sound", "correctness", "error",
                            f"“{it.title or it.ref}”: later move {b.san(moves[ply])} is a mistake per Stockfish", n, it.ref)
                break
        # concept items: the library's own verification pipeline (validator, profile, facts, duplicates)
        if it.concept and it.concept in ctx.library.concepts and ctx.library.concepts[it.concept].validator:
            try:
                report = verify_candidate(_raw_for(it, u, ctx.library), ctx.library, engine, depth=ctx.depth,
                                          tier="generated", method="custom-plan")
            except Exception as exc:
                yield Issue("concept_pipeline", "correctness", "uncertain", f"verification failed to run: {exc}", n, it.ref)
                continue
            if report.status != "verified":
                bad = next((s for s in report.stages if s.outcome in ("fail", "uncertain")), None)
                fam = {"concept": "consistency", "explanation": "consistency", "duplicate": "duplication"}.get(
                    bad.name if bad else "", "correctness")
                yield Issue(f"pipeline_{bad.name if bad else 'status'}", fam,
                            "error" if report.status == "rejected" else "uncertain",
                            f"“{it.title or it.ref}”: {bad.detail if bad else report.status}", n, it.ref)
            else:
                it.example = report.example


@check("opening_line_sound", "correctness")
def _opening_line(cand, ctx):
    from ..planner import screen_line

    for n, u in enumerate(cand.units):
        for it in u.items:
            if it.kind != "opening_line" or it.trusted:
                continue
            if it.side not in ("white", "black"):
                yield Issue("opening_line_sound", "correctness", "error", f"{it.title}: unknown side {it.side!r}", n, it.ref)
            if isinstance(_replay(it), str) or not it.line:
                continue
            engine = ctx.get_engine()
            if engine is None:
                yield Issue("opening_line_sound", "correctness", "uncertain",
                            f"{it.title}: the line needs Stockfish to be checked move by move", n, it.ref)
                continue
            kept, reason = screen_line(list(it.line), engine)
            if len(kept) < len(it.line):
                yield Issue("opening_line_sound", "correctness", "error", f"{it.title}: {reason}", n, it.ref)


# ------------------------------------------------------------------ education
@check("size_bounds", "education")
def _size(cand, ctx):
    if not cand.units:
        yield Issue("size_bounds", "education", "error", "the plan has no units")
    if len(cand.units) > MAX_UNITS:
        yield Issue("size_bounds", "education", "error", f"{len(cand.units)} units is too many (max {MAX_UNITS})")
    for n, u in enumerate(cand.units):
        if not u.items:
            yield Issue("size_bounds", "education", "error", f"unit “{u.title}” is empty", n)
        elif len(u.items) > MAX_ITEMS:
            yield Issue("size_bounds", "education", "error", f"unit “{u.title}” has {len(u.items)} items (max {MAX_ITEMS})", n)


@check("objectives", "education")
def _objectives(cand, ctx):
    for n, u in enumerate(cand.units):
        if not (u.title or "").strip() or not (u.objective or "").strip():
            yield Issue("objectives", "education", "error", f"unit {n + 1} has no title or learning objective", n)


@check("has_practice", "education")
def _practice(cand, ctx):
    interactive = any(it.role in ("guided", "practice") or it.kind in ("topic", "opening_line")
                      for u in cand.units for it in u.items)
    if cand.units and not interactive and not all(u.role == "definition" for u in cand.units):
        yield Issue("has_practice", "education", "error", "the plan only shows positions — the learner never practises")


@check("role_order", "education")
def _role_order(cand, ctx):
    for n, u in enumerate(cand.units):
        order = [ROLE_ORDER.get(it.role, 1) for it in u.items]
        if any(b < a for a, b in zip(order, order[1:])):
            yield Issue("role_order", "education", "error",
                        f"unit “{u.title}” asks the learner to practise before showing the idea", n)
    firsts = {}
    for n, u in enumerate(cand.units):
        key = u.component.get("id") or str(u.component.get("material"))
        firsts.setdefault(key, []).append((n, u.role))
    for key, seq in firsts.items():
        roles = [r for _, r in seq]
        if "practice" in roles and "learn" in roles and roles.index("practice") < roles.index("learn"):
            yield Issue("role_order", "education", "error",
                        f"“{cand.units[seq[roles.index('practice')][0]].title}” comes before the unit that teaches it",
                        seq[roles.index('practice')][0])


def _requires(ctx, a: Unit, b: Unit) -> bool:
    """Does unit a need unit b first? (concept prerequisites + catalog topic prerequisites)"""
    lib, cat = ctx.library, ctx.catalog
    ca, cb = a.component, b.component
    concepts_a, concepts_b = set(), set()
    for comp, out in ((ca, concepts_a), (cb, concepts_b)):
        if comp.get("kind") == "concept":
            out.add(comp["id"])
        elif comp.get("kind") in ("topic", "opening_as"):
            c = lib.concept_for_topic(comp["id"]) if hasattr(lib, "concept_for_topic") else None
            if c:
                out.add(c.id)
    for x in concepts_a:
        seen, queue = set(), list(lib.concepts[x].prerequisites) if x in lib.concepts else []
        while queue:
            p = queue.pop()
            if p in seen:
                continue
            seen.add(p)
            if p in concepts_b:
                return True
            if p in lib.concepts:
                queue.extend(lib.concepts[p].prerequisites)
    if ca.get("kind") in ("topic", "opening_as") and cb.get("kind") in ("topic", "opening_as"):
        ta = cat.topics.get(ca["id"])
        if ta and cb["id"] in (ta.prerequisites or []):
            return True
    return False


@check("prerequisites_first", "education")
def _prereqs(cand, ctx):
    for i, a in enumerate(cand.units):
        for j in range(i + 1, len(cand.units)):
            b = cand.units[j]
            if a.component != b.component and _requires(ctx, a, b):
                yield Issue("prerequisites_first", "education", "error",
                            f"“{a.title}” comes before its prerequisite “{b.title}”", i)


@check("difficulty_progression", "education")
def _difficulty(cand, ctx):
    for n, u in enumerate(cand.units):
        top = 0
        for it in u.items:
            if it.role == "demonstration":
                top = max(top, it.difficulty)
                continue
            if it.difficulty <= top - 2:
                yield Issue("difficulty_progression", "education", "error",
                            f"unit “{u.title}” jumps from difficulty {top} back down to {it.difficulty}", n, it.ref)
                break
            top = max(top, it.difficulty)
    by_comp: dict[str, list[tuple[int, Unit]]] = {}
    for n, u in enumerate(cand.units):
        by_comp.setdefault(repr(sorted(u.component.items(), key=str)), []).append((n, u))
    for seq in by_comp.values():
        for (i, a), (j, b) in zip(seq, seq[1:]):
            if b.difficulty() <= a.difficulty() - 2:
                yield Issue("difficulty_progression", "education", "error",
                            f"“{b.title}” is much easier than “{a.title}”, which comes first", j)


@check("level_fit", "education")
def _level_fit(cand, ctx):
    level = (cand.personal or {}).get("level") or cand.level or ctx.level
    if level not in LEVEL_WINDOW:
        return
    lo, hi = LEVEL_WINDOW[level]
    positions = [it for u in cand.units for it in u.items if it.fen]
    if not positions:
        return
    first = positions[0]
    if first.difficulty > hi + 1:
        yield Issue("level_fit", "education", "error",
                    f"the plan opens with a difficulty-{first.difficulty} position — too hard for a {level}", 0, first.ref)
    if all(it.difficulty < lo - 1 for it in positions):
        yield Issue("level_fit", "education", "warning", f"every position is well below {level} level")


@check("no_repeated_positions", "education")
def _repeats(cand, ctx):
    seen: dict[str, str] = {}
    for n, u, it in _positions(cand):
        b = _learner_board(it)
        if b is None:
            continue
        key = f"{b.board_fen()} {b.turn}"
        if key in seen:
            yield Issue("no_repeated_positions", "education", "error",
                        f"“{it.title or it.ref}” repeats a position already used ({seen[key]})", n, it.ref)
        else:
            seen[key] = it.title or it.ref


@check("no_redundant_units", "education")
def _redundant(cand, ctx):
    titles = {}
    for n, u in enumerate(cand.units):
        t = u.title.strip().lower()
        if t in titles:
            yield Issue("no_redundant_units", "education", "error", f"two units are called “{u.title}”", n)
        titles[t] = n
        for m in range(n):
            v = cand.units[m]
            a = {i.content_key() for i in u.items}
            b = {i.content_key() for i in v.items}
            if a and b and len(a & b) / min(len(a), len(b)) > 0.5:
                yield Issue("no_redundant_units", "education", "error", f"“{u.title}” mostly repeats “{v.title}”", n)


# ------------------------------------------------------------------ consistency
@check("units_in_scope", "consistency")
def _scope(cand, ctx):
    wanted = {c.key() for c in ctx.intent.components}
    if not wanted:
        return
    from ..intent.model import Component
    for n, u in enumerate(cand.units):
        if Component.from_dict(u.component).key() not in wanted:
            yield Issue("units_in_scope", "consistency", "error",
                        f"“{u.title}” teaches something that wasn't asked for", n)


@check("subjects_covered", "consistency")
def _covered(cand, ctx):
    from ..intent.model import Component
    have = {Component.from_dict(u.component).key() for u in cand.units}
    missing = [c for c in ctx.intent.components if c.key() not in have]
    if missing and len(missing) == len(ctx.intent.components):
        yield Issue("subjects_covered", "consistency", "error", "none of the requested subjects is covered")
    elif missing:
        skipped = {s.get("key") for s in cand.skipped}
        for c in missing:
            sev = "warning" if c.key() in skipped else "error"
            yield Issue("subjects_covered", "consistency", sev,
                        f"“{c.label}” was requested but has no unit" + (" (explained in the plan)" if sev == "warning" else ""))


def _item_concepts(it: Item) -> set[str]:
    out = set()
    if it.concept:
        out.add(it.concept)
    ex = it.example
    if ex is not None:
        out |= set(getattr(ex, "concepts", None) or [getattr(ex, "concept", None)])
    out.discard(None)
    return out


@check("items_match_unit", "consistency")
def _items_match(cand, ctx):
    lib = ctx.library
    for n, u in enumerate(cand.units):
        comp = u.component
        kind = comp.get("kind")
        spec = MaterialSpec.from_dict(comp["material"]) if comp.get("material") else None
        for it in u.items:
            name = it.title or it.ref
            if kind == "material" and it.fen:
                b = _learner_board(it)
                if b is None:
                    continue
                if not spec.matches(b):
                    yield Issue("items_match_unit", "consistency", "error",
                                f"“{name}” doesn't have the material of “{u.title}” ({spec.label().lower()})", n, it.ref)
                    continue
                if spec.head == "mate":
                    got = _replay(it)
                    final = got[0][-1] if not isinstance(got, str) else None
                    if final is None or not final.is_checkmate() or spec.winner(b) != b.turn:
                        yield Issue("items_match_unit", "consistency", "error",
                                    f"“{name}” isn't a checkmate by the side with {spec.label().lower()}", n, it.ref)
            elif kind == "concept" and it.fen:
                fam = _family(lib, comp["id"])
                if not _item_concepts(it) & fam:
                    yield Issue("items_match_unit", "consistency", "error",
                                f"“{name}” doesn't show {comp.get('label') or comp['id']}", n, it.ref)
            elif kind == "topic" and it.kind != "topic" and not (it.kind == "catalog" and it.ref.startswith(f"catalog:{comp['id']}/")):
                yield Issue("items_match_unit", "consistency", "error", f"“{name}” isn't part of {comp.get('label')}", n, it.ref)
            elif kind == "topic" and it.kind == "topic" and it.ref != comp["id"]:
                yield Issue("items_match_unit", "consistency", "error", f"“{name}” isn't {comp.get('label')}", n, it.ref)
            elif kind in ("opening_as", "opening_db"):
                if it.kind != "opening_line":
                    yield Issue("items_match_unit", "consistency", "error", f"“{name}” isn't an opening line", n, it.ref)
                elif kind == "opening_as" and (it.side != comp.get("side") or not it.ref.startswith(comp["id"])):
                    yield Issue("items_match_unit", "consistency", "error",
                                f"“{name}” isn't {comp.get('label')} from {comp.get('side')}'s side", n, it.ref)
            elif kind == "glossary" and it.kind != "definition":
                yield Issue("items_match_unit", "consistency", "error", f"“{name}” isn't a definition", n, it.ref)


@check("respects_exclusions", "consistency")
def _exclusions(cand, ctx):
    lib = ctx.library
    excluded: set[str] = set()
    excluded_topics: set[str] = set()
    for key in ctx.intent.exclude:
        kind, _, ident = key.partition(":")
        if kind == "concept":
            excluded |= _family(lib, ident)
        elif kind == "topic":
            excluded_topics.add(ident)
            c = lib.concept_for_topic(ident)
            if c:
                excluded |= _family(lib, c.id)
    if not excluded and not excluded_topics:
        return
    for n, u in enumerate(cand.units):
        if u.component.get("kind") == "topic" and u.component.get("id") in excluded_topics:
            yield Issue("respects_exclusions", "consistency", "error", f"“{u.title}” was excluded by the request", n)
        for it in u.items:
            if _item_concepts(it) & excluded or (it.kind == "catalog" and it.ref.split(":")[1].split("/")[0] in excluded_topics):
                yield Issue("respects_exclusions", "consistency", "error",
                            f"“{it.title or it.ref}” is about something the request excluded", n, it.ref)


@check("texts_match_facts", "consistency")
def _texts(cand, ctx):
    from ...knowledge.facts import check_explanation

    for n, u in enumerate(cand.units):
        verified: set[str] = set()
        for it in u.items:
            got = _replay(it)
            if isinstance(got, str):
                continue
            boards, moves = got
            for b, m in zip(boards, moves):
                verified.add(_clean_san(b.san(m)))
            ex = it.example
            for alt in (getattr(ex, "accepted", None) or {}).values() if isinstance(getattr(ex, "accepted", None), dict) else []:
                verified |= {_clean_san(a) for a in alt}
        texts = [("unit", u.text)] + [(it.ref, it.text) for it in u.items if it.text and it.trusted]
        for where, text in texts:
            if not text:
                continue
            for m in _SAN_IN_TEXT.finditer(text):
                san = _clean_san(m.group(1))
                if san not in verified:
                    yield Issue("texts_match_facts", "consistency", "error",
                                f"“{u.title}”: the text mentions {m.group(1)}, which isn't a verified move here", n)
                    break
            low = text.lower()
            claims = [it.claims.get("result") for it in u.items if it.claims.get("result")]
            if claims and all(c == "win" for c in claims) and re.search(r"\b(draws?|drawn|drawing)\b", low) \
                    and not re.search(r"\b(not|avoid|instead of|rather than)\b[^.]*\bdraw", low):
                yield Issue("texts_match_facts", "consistency", "error",
                            f"“{u.title}”: the text talks about a draw, but these positions are verified wins", n)
            if claims and all(c == "draw" for c in claims) and re.search(r"\b(wins|winning)\b", low) \
                    and not re.search(r"\b(not|no|can't|cannot)\b[^.]*\bwin", low):
                yield Issue("texts_match_facts", "consistency", "error",
                            f"“{u.title}”: the text promises a win, but these positions are verified draws", n)
        for it in u.items:
            if it.text and it.example is not None and not it.trusted:
                for problem in check_explanation(it.text, it.example):
                    yield Issue("texts_match_facts", "consistency", "error", f"“{it.title or it.ref}”: {problem}", n, it.ref)
                    break


# ------------------------------------------------------------------ duplication
@check("positions_not_duplicated", "duplication")
def _dup_positions(cand, ctx):
    known = {i.board_key(): i.ref for i in ctx.content.items}
    for n, u, it in _positions(cand):
        if it.trusted:
            continue
        b = _learner_board(it)
        if b is None:
            continue
        key = f"{b.board_fen()} {'w' if b.turn else 'b'}"
        if key in known:
            yield Issue("positions_not_duplicated", "duplication", "error",
                        f"“{it.title or it.ref}” is already in the library as {known[key]} — use the verified entry",
                        n, it.ref)


@check("plan_not_duplicated", "duplication")
def _dup_plan(cand, ctx):
    if ctx.plan_library is None:
        return
    twin = ctx.plan_library.similar(cand, learner=cand.learner)
    if twin is not None:
        yield Issue("plan_not_duplicated", "duplication", "warning",
                    f"an equivalent verified plan already exists ({twin['id']})")


# ------------------------------------------------------------------ personalization
@check("personalization", "personalization")
def _personal(cand, ctx):
    p = cand.personal
    if not p:
        return
    lib = ctx.library
    target = p.get("concept")
    if not target:
        yield Issue("targets_weakness", "personalization", "error", "the weakness has no concept to train")
        return
    fam = _family(lib, target) | set(lib.concepts[target].parents if target in lib.concepts else [])
    positions = [it for u in cand.units for it in u.items if it.fen]
    on_target = [it for it in positions if _item_concepts(it) & fam]
    units_on = [u for u in cand.units if u.component.get("id") in fam]
    if not units_on:
        yield Issue("targets_weakness", "personalization", "error",
                    f"no unit trains the weakness it was made for ({p.get('title') or target})")
    if positions and len(on_target) * 2 < len(positions):
        yield Issue("targets_weakness", "personalization", "error",
                    f"only {len(on_target)} of {len(positions)} positions are about the weakness")
    if not p.get("evidence"):
        yield Issue("uses_evidence", "personalization", "error",
                    "the plan claims to target a weakness but has no evidence from the learner's games")
    elif len({e.get("game_id") for e in p["evidence"]}) < 2:
        yield Issue("uses_evidence", "personalization", "warning", "the weakness was seen in only one game")
    own = {f.split(" ")[0] for f in p.get("evidence_fens", [])}
    for n, u, it in _positions(cand):
        b = _learner_board(it)
        if b is not None and b.board_fen() in own and u.role != "own_games":
            yield Issue("not_copying_games", "personalization", "error",
                        f"“{it.title or it.ref}” is a position from the learner's own games, presented as a new puzzle",
                        n, it.ref)


# ---------------------------------------------------------------------- run
def validate(cand: CandidatePlan, ctx: Context, only: set[str] | None = None) -> ValidationReport:
    issues: list[Issue] = []
    ran: list[str] = []
    for name, family, fn in CHECKS:
        if only and family not in only and name not in only:
            continue
        ran.append(name)
        try:
            issues.extend(fn(cand, ctx) or [])
        except Exception as exc:  # a crashing check never lets a plan through
            log.exception("plan check %s crashed", name)
            issues.append(Issue(name, family, "uncertain", f"check failed to run: {exc}"))
    status = "rejected" if any(i.severity == "error" for i in issues) else (
        "needs_review" if any(i.severity == "uncertain" for i in issues) else "verified")
    engine = "Stockfish" if ctx.engine is not None else None
    return ValidationReport(status, issues, ran, cand.fingerprint(),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"), engine)
