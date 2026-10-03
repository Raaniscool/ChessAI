"""Puzzle Library API and the Puzzles tab.

    GET  /api/puzzles                          -> library summary (counts by type, tier, uniqueness)
    GET  /api/puzzles/dashboard                -> Puzzles tab: personalized weakness cards (with their
                                                  evidence), practice themes, profile box
    POST /api/puzzles/set {mode, concept?, weakness?, count}
                                               -> a set of solver-ready puzzles, easy -> hard
                                                  mode "practice": by concept (no analysis needed)
                                                  mode "personalized": for a weakness (default: the
                                                  main one); library first, then strictly verified
                                                  generation when the library runs short
    POST /api/puzzles/next {mode, concept?, weakness?, exclude, done, count}
                                               -> the next puzzles of a continuous session: same
                                                  selection as /set (weakness, concept skill,
                                                  calibration + this session's results, library
                                                  first, then verified generation), never one
                                                  already in the session; when everything fitting
                                                  has been played, earlier ones come back for
                                                  review rather than the session ending
    POST /api/puzzles/{id}/result {solved, first_try, critical_first_try, mistakes, hints, seconds,
                                   revealed}   -> records the outcome (puzzle stats + learner model)
    POST /api/puzzles/adapt {mode, concept?, weakness?, done, remaining, set_ids}
                                               -> swaps the remaining puzzles of a running set that
                                                  fell out of the learner's zone (session adaptation)
    GET  /api/puzzles/difficulty               -> the learner's difficulty profile with its evidence
    GET  /api/puzzles/select?concept=&count=5  -> the most useful puzzles for a concept right now
    GET  /api/puzzles/{id}                     -> one puzzle (metadata + the learner's stats)
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/puzzles", tags=["puzzles"])


def _knowledge():
    from .knowledge.library import get_knowledge
    return get_knowledge()


@router.get("")
def summary() -> dict:
    from .puzzles import get_puzzles
    return get_puzzles(_knowledge()).summary()


@router.get("/select")
def select_puzzles(concept: str, count: int = Query(5, ge=1, le=10)):
    from .knowledge.usage import get_usage
    from .learner import get_profile
    from .puzzles import get_puzzles, select

    knowledge = _knowledge()
    if concept not in knowledge.concepts:
        return JSONResponse(status_code=404, content={"error": f"unknown concept: {concept}"})
    candidates = [p for p in get_puzzles(knowledge).all() if (p.source or {}).get("type") != "user_game"]
    from .learner.difficulty import for_learner
    profile, usage = get_profile(), get_usage()
    cal = for_learner(knowledge, profile, usage).target(concept, knowledge)
    return select(candidates, knowledge, concept, count=count, profile=profile, usage=usage,
                  calibration=cal).as_dict()


MIN_LIBRARY_BEFORE_GENERATING = 3  # fewer library matches than this -> build verified puzzles
MAX_GENERATED = 3                   # per set (each one costs Stockfish time)


class SetRequest(BaseModel):
    mode: Literal["personalized", "practice"] = "personalized"
    concept: str | None = None
    weakness: str | None = None
    count: int = Field(5, ge=1, le=10)
    generate: bool = True
    username: str | None = None
    # continuous sessions (POST /next): what the session already holds and how it went
    exclude: list[str] = Field(default_factory=list)
    done: list[dict] = Field(default_factory=list)
    continuation: bool = False


class NextRequest(BaseModel):
    mode: Literal["personalized", "practice"] = "personalized"
    concept: str | None = None
    weakness: str | None = None
    username: str | None = None
    count: int = Field(3, ge=1, le=10)
    exclude: list[str] = Field(default_factory=list)   # every puzzle already in this session
    done: list[dict] = Field(default_factory=list)     # finished puzzles of this session, in order
    generate: bool = True


REVIEW_WINDOW = 12  # a review puzzle is never one of the last this many of the session


class ResultRequest(BaseModel):
    solved: bool
    first_try: bool = False
    critical_first_try: bool | None = None
    mistakes: int = Field(0, ge=0)
    hints: int = Field(0, ge=0)
    seconds: float | None = Field(None, ge=0)
    revealed: bool = False


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


@router.get("/difficulty")
def difficulty(concept: str | None = None) -> dict:
    """The learner's difficulty profile (per skill, with evidence) and, for `concept`, the target."""
    from .learner.difficulty import for_learner
    knowledge = _knowledge()
    dp = for_learner(knowledge)
    out = dp.as_dict()
    if concept:
        if concept not in knowledge.concepts:
            return _error(404, f"unknown concept: {concept}")
        out["target"] = dp.target(concept, knowledge)
    return out


@router.get("/dashboard")
def dashboard() -> dict:
    from .learner import get_profile
    from .puzzles import get_puzzles
    from .puzzles.dashboard import candidates, personalized, themes

    knowledge = _knowledge()
    pool = candidates(get_puzzles(knowledge).all())
    from .knowledge.usage import get_usage
    return {"personalized": personalized(get_profile(), knowledge, pool, usage=get_usage()),
            "practice": themes(pool, knowledge),
            "library": {"puzzles": len(pool)}}


def _weakness(key: str, username: str | None) -> tuple[dict | None, int]:
    """The full weakness (with evidence from the analyzed games) or the learner-profile entry."""
    from .game_api import _find_weaknesses
    from .learner import get_profile
    try:
        found, err = _find_weaknesses([key], None, username)
    except Exception:  # no analyzed games / store unavailable: the profile entry still works
        found, err = None, True
    if err is None:
        docs, (w,) = found
        return w, len(docs)
    for w in get_profile().weaknesses:
        if w.get("key") == key:
            return dict(w), int(w.get("total_games") or 0)
    return None, 0


class _LazyEngine:
    """Stockfish, started only if something actually needs a fresh analysis."""

    def __init__(self):
        self._engine = None
        self.unavailable = False

    def get(self):
        from .engine.service import EngineUnavailable, get_engine
        if self._engine is None and not self.unavailable:
            try:
                self._engine = get_engine()
            except EngineUnavailable:
                self.unavailable = True
        return self._engine

    def analyse_lines(self, *args, **kwargs):
        engine = self.get()
        if engine is None:
            from .engine.service import EngineUnavailable
            raise EngineUnavailable("Stockfish is not available")
        return engine.analyse_lines(*args, **kwargs)


@router.post("/set")
def puzzle_set(body: SetRequest):
    from .knowledge.generation.log import get_log
    from .knowledge.usage import get_usage
    from .learner import get_profile
    from .puzzles import get_puzzles, select
    from .puzzles import sets
    from .puzzles.dashboard import candidates, payload, personalized, theme_pool
    from .puzzles.from_game import pick as pick_game
    from .puzzles.personal import _SeenUsage, debug_block, library_selection, own_boards
    from .puzzles.progression import ladder
    from .learner.difficulty import for_learner, session_shift

    knowledge = _knowledge()
    profile, usage, log = get_profile(), get_usage(), get_log()
    index = get_puzzles(knowledge)
    shown = log.shown()
    exclude = set(body.exclude)
    shift, shift_reason = session_shift(body.done) if body.continuation else (0, None)
    weakness, total_games, generation = None, 0, {"needed": 0, "generated": 0, "rejected": 0, "attempts": 0,
                                                  "status": "not needed"}
    items: list[dict] = []
    extra_debug: dict = {}
    if body.mode == "practice":
        concept = body.concept
        if not concept or concept not in knowledge.concepts:
            return _error(404, f"unknown concept: {concept}")
        pool = theme_pool(candidates(index.all()), knowledge, concept, body.count)
        seen = _SeenUsage(usage, shown)
        lad = ladder(pool, seen.puzzle_stats)
        calibration = for_learner(knowledge, profile, usage).target(concept, knowledge, shift)
        selection = select(pool, knowledge, concept, count=body.count, profile=profile, usage=seen, bonus=lad.bonus,
                           calibration=calibration, exclude=exclude)
        selection.ladder = lad.as_dict()
        title = f"Practice: {knowledge.concepts[concept].name}"
        items = [{"puzzle": c.puzzle, "example": knowledge.get(c.puzzle.id), "reasons": c.reasons,
                  "origin": "personal" if c.puzzle.tier == "personal" else "library", "role": "practice",
                  "why": None} for c in selection.chosen if c.puzzle.clear_start]
        items = [it for it in items if it["example"] is not None]
        if not items and body.continuation and body.generate:
            # the theme's library puzzles are used up for now: strictly verified new ones
            theme = {"key": f"practice:{concept}", "concept": concept, "title": knowledge.concepts[concept].name,
                     "evidence": []}
            generated, generation = _generate(theme, knowledge, body.count, body, index)
            items = [{**it, "role": "practice", "why": None, "reasons": ["New puzzle, verified by Stockfish"]}
                     for it in generated if it["puzzle"].id not in exclude]
        items.sort(key=lambda it: (it["puzzle"].rating, it["puzzle"].id))
    else:
        key = body.weakness
        if not key:
            main = personalized(profile, knowledge, candidates(index.all()))["main"]
            if main is None:
                return _error(422, "No weaknesses found yet. Analyze some games, or pick a theme in Practice.")
            key = main["key"]
        if key.startswith("puzzles:"):
            weakness = {"key": key, "concept": key.split(":", 1)[1], "title": None}
        else:
            weakness, total_games = _weakness(key, body.username)
        if weakness is None or not weakness.get("concept") or weakness["concept"] not in knowledge.concepts:
            return _error(404, "That weakness isn't in your profile any more — open the dashboard again.")
        concept = weakness["concept"]
        weakness.setdefault("title", None)
        weakness["title"] = weakness["title"] or knowledge.concepts[concept].name
        weakness.setdefault("evidence", [])
        title = weakness["title"]
        engine = _LazyEngine()
        # A. the learner's own mistake, re-verified (only for weaknesses found in their games)
        game_puzzle, trail = (pick_game(weakness, engine if body.generate else None, usage)
                              if weakness["evidence"] and not body.continuation else (None, []))
        if game_puzzle is not None and game_puzzle.id in exclude:
            game_puzzle = None
        extra_debug["your_game"] = {"used": game_puzzle.id if game_puzzle else None, "trail": trail}
        if game_puzzle is not None:
            items.append({"puzzle": game_puzzle, "example": None, "role": "your_game", "origin": "your_game",
                          "reasons": ["The position from your game, re-checked by Stockfish"]})
        wants_defend = sets.walked_into(weakness) and body.count >= 4 and not key.startswith("puzzles:")
        skill_count = max(1, body.count - len(items) - (1 if wants_defend else 0))
        # B. verified library puzzles of exactly this skill (easy -> hard, progression-aware)
        selection = library_selection(weakness, knowledge, skill_count, profile=profile, usage=usage, shown=shown,
                                      exclude=exclude, session_shift=shift)
        target = selection.target_rating if selection else 1200
        for c in (selection.chosen if selection else []):
            ex = knowledge.get(c.puzzle.id)
            if ex is not None and c.puzzle.clear_start:
                items.append({"puzzle": c.puzzle, "example": ex, "reasons": c.reasons, "role": sets.role_for(c.puzzle, target),
                              "origin": "personal" if c.puzzle.tier == "personal" else "library"})
        skill_items = [it for it in items if it["role"] in ("same", "easier", "harder")]
        # C. strictly verified new puzzles when the library runs short
        if not key.startswith("puzzles:") and body.generate \
                and len(skill_items) < min(skill_count, MIN_LIBRARY_BEFORE_GENERATING):
            generated, generation = _generate(weakness, knowledge, skill_count - len(skill_items), body, index)
            items += [{**it, "role": sets.role_for(it["puzzle"], target)} for it in generated
                      if it["puzzle"].id not in exclude]
        # the defensive version, for weaknesses the learner walked into
        if wants_defend:
            avoid = own_boards(weakness) | {it["puzzle"].fen.split(" ")[0] for it in items}
            defend, extra_debug["defend"] = sets.defend_item(
                weakness, knowledge, index, usage, shown, target, avoid,
                engine=engine if body.generate else None, username=body.username)
            if defend is not None and defend["puzzle"].id not in exclude:
                items.append({**defend, "reasons": ["The defensive side of the same weakness"]})
        for it in items:
            it["why"] = sets.why(weakness, it["role"], it["puzzle"])
            it["why_after"] = sets.why_after(weakness, it["role"], it["puzzle"])
        items = sets.order(items)
    if not items and body.continuation:
        # nothing new fits right now (library and generation): bring back earlier puzzles for
        # review rather than ending the session — never one of the session's last few
        items = _review_items(body, knowledge, index, concept, weakness, selection)
    if not items:
        return _error(422, f"No verified puzzles for {title.lower()} right now — you've seen them all recently. "
                           "Try another theme, or come back tomorrow.")
    try:
        usage.record_used([it["puzzle"] for it in items])
    except OSError:
        pass
    log.mark_shown([it["puzzle"].id for it in items if it["origin"] not in ("library", "your_game")])
    from .puzzles.dashboard import MIXED_THEMES
    out = {"mode": body.mode, "concept": concept, "title": title, "weakness": weakness and weakness["key"],
           "mixed": body.mode == "practice" and concept in MIXED_THEMES,
           "session": {"shift": shift, "reason": shift_reason} if body.continuation else None,
           "ladder": selection.ladder if selection else None,
           "difficulty": _difficulty_brief(selection.calibration if selection else None),
           "puzzles": [payload(it["puzzle"], it["example"], knowledge, it["reasons"], it["origin"],
                               role=it["role"], why=it.get("why"), why_after=it.get("why_after"))
                       for it in items]}
    if weakness is not None:
        examples = [it["example"] or it["puzzle"] for it in items]
        out["debug"] = debug_block(weakness, total_games, selection, generation, examples,
                                   {it["puzzle"].id: it["origin"] for it in items}, knowledge=knowledge,
                                   extra={**extra_debug, "roles": {it["puzzle"].id: it["role"] for it in items}})
    return out


@router.post("/next")
def puzzle_next(body: NextRequest):
    """The next puzzles of a continuous session (see the module docstring). Same rules as /set."""
    return puzzle_set(SetRequest(mode=body.mode, concept=body.concept, weakness=body.weakness, count=body.count,
                                 generate=body.generate, username=body.username, exclude=body.exclude,
                                 done=body.done, continuation=True))


def _review_items(body, knowledge, index, concept: str, weakness: dict | None, selection) -> list[dict]:
    """Earlier puzzles of this skill, nearest the learner's level, least recently played first."""
    from .puzzles import sets
    from .puzzles.dashboard import candidates, matching
    recent = set(body.exclude[-REVIEW_WINDOW:])
    if weakness is not None:
        from .puzzles.personal import own_boards
        from .puzzles.skill import split
        mine = own_boards(weakness)
        pool, _o, _p = split([p for p in candidates(index.all()) if p.fen.split(" ")[0] not in mine], concept,
                             knowledge)
    else:
        pool = matching(candidates(index.all()), knowledge, concept)
    pool = [p for p in pool if knowledge.get(p.id) is not None]
    fresh = [p for p in pool if p.id not in recent]
    if not fresh:  # a tiny theme: anything but the puzzle just played
        last = body.exclude[-1:] or []
        fresh = [p for p in pool if p.id not in last]
    order = {pid: i for i, pid in enumerate(body.exclude)}
    target = selection.target_rating if selection else 1200
    fresh.sort(key=lambda p: (order.get(p.id, -1), abs(p.rating - target), p.id))
    out = []
    for p in fresh[:body.count]:
        role = sets.role_for(p, target) if weakness is not None else "practice"
        out.append({"puzzle": p, "example": knowledge.get(p.id), "origin": "library", "role": role,
                    "reasons": ["Review: a puzzle from earlier — new ones for this theme are used up for now"],
                    "why": sets.why(weakness, role, p) if weakness is not None else None,
                    "why_after": sets.why_after(weakness, role, p) if weakness is not None else None})
    return out


def _difficulty_brief(cal: dict | None) -> dict | None:
    """What the solver may show about difficulty: one line, the target and zone (no evidence dump)."""
    if not cal:
        return None
    return {"target": cal["target"], "zone": cal["zone"], "summary": cal.get("summary"), "skill": cal.get("skill"),
            "level": cal.get("skill_level")}


class AdaptRequest(BaseModel):
    mode: Literal["personalized", "practice"] = "personalized"
    concept: str | None = None
    weakness: str | None = None
    username: str | None = None
    done: list[dict] = Field(default_factory=list)       # finished puzzles of this set, in order
    remaining: list[str] = Field(default_factory=list)   # ids not opened yet, in order
    set_ids: list[str] = Field(default_factory=list)     # everything in the set (never re-served)


@router.post("/adapt")
def adapt(body: AdaptRequest):
    """Keep a running set in the productive zone: after two instant solves the remaining puzzles
    that are now too easy are swapped for harder ones (and vice versa after two tough ones).
    Deterministic (learner.difficulty.session_shift); puzzles already in range stay."""
    from .knowledge.generation.log import get_log
    from .knowledge.usage import get_usage
    from .learner import get_profile
    from .learner.difficulty import for_learner, session_shift
    from .puzzles import get_puzzles, select
    from .puzzles import sets
    from .puzzles.dashboard import candidates, payload, theme_pool
    from .puzzles.personal import _SeenUsage, library_selection
    from .puzzles.progression import ladder

    shift, reason = session_shift(body.done)
    out = {"shift": shift, "reason": reason, "replace": {}}
    if not shift or not body.remaining:
        return out
    knowledge = _knowledge()
    profile, usage, shown = get_profile(), get_usage(), get_log().shown()
    index = get_puzzles(knowledge)
    weakness = None
    if body.mode == "practice":
        concept = body.concept
    else:
        if not body.weakness:
            return out
        if body.weakness.startswith("puzzles:"):
            weakness = {"key": body.weakness, "concept": body.weakness.split(":", 1)[1], "title": None, "evidence": []}
        else:
            weakness, _n = _weakness(body.weakness, body.username)
        concept = weakness and weakness.get("concept")
    if not concept or concept not in knowledge.concepts:
        return out
    cal = for_learner(knowledge, profile, usage).target(concept, knowledge, shift)
    out["difficulty"] = _difficulty_brief(cal)
    low, high = cal["zone"]
    swap = []
    for pid in body.remaining:
        p = index.get(pid)
        if p is None or pid.startswith("game:") or (weakness is not None and p.type == "defense"
                                                    and weakness.get("concept") not in sets.DEFENSIVE):
            continue   # your game / the defensive item keep their place
        if (shift > 0 and p.rating < low) or (shift < 0 and p.rating > high):
            swap.append(p)
    if not swap:
        return out
    exclude = set(body.set_ids) | set(body.remaining) | {d.get("id") for d in body.done if d.get("id")}
    if weakness is not None:
        weakness.setdefault("title", None)
        weakness["title"] = weakness["title"] or knowledge.concepts[concept].name
        weakness.setdefault("evidence", [])
        selection = library_selection(weakness, knowledge, len(swap), profile=profile, usage=usage, shown=shown,
                                      exclude=exclude, calibration=cal)
    else:
        pool = theme_pool(candidates(index.all()), knowledge, concept, len(swap))
        seen = _SeenUsage(usage, shown)
        selection = select(pool, knowledge, concept, count=len(swap), profile=profile, usage=seen,
                           exclude=exclude, bonus=ladder(pool, seen.puzzle_stats).bonus, calibration=cal)
    fresh = [c for c in (selection.chosen if selection else [])
             if knowledge.get(c.puzzle.id) is not None and low <= c.puzzle.rating <= high]
    replaced = []
    for old, c in zip(sorted(swap, key=lambda p: p.rating), sorted(fresh, key=lambda c: c.puzzle.rating)):
        role = sets.role_for(c.puzzle, cal["target"]) if weakness is not None else "practice"
        why = sets.why(weakness, role, c.puzzle) if weakness is not None else None
        after = sets.why_after(weakness, role, c.puzzle) if weakness is not None else None
        out["replace"][old.id] = payload(c.puzzle, knowledge.get(c.puzzle.id), knowledge, c.reasons,
                                         "personal" if c.puzzle.tier == "personal" else "library", role=role, why=why,
                                         why_after=after)
        replaced.append(c.puzzle)
    try:
        usage.record_used(replaced)
    except OSError:
        pass
    return out


def _generate(weakness: dict, knowledge, need: int, body: SetRequest, index) -> tuple[list[dict], dict]:
    """New puzzles for a short personalized set — only ones that passed the full pipeline."""
    from .analysis import personal_puzzles
    from .engine.service import EngineUnavailable, get_engine

    need = min(MAX_GENERATED, need)
    generation = {"needed": need, "generated": 0, "rejected": 0, "attempts": 0, "status": "ran"}
    out: list[dict] = []
    if need <= 0:
        generation["status"] = "not needed"
        return out, generation
    if not personal_puzzles.supported_weakness(weakness):
        generation["status"] = "unsupported weakness"
        return out, generation
    try:
        engine = get_engine()
    except EngineUnavailable:
        generation["status"] = "engine unavailable"
        return out, generation
    try:
        result = personal_puzzles.generate_for(weakness, knowledge, engine, count=need, username=body.username)
    except Exception as exc:  # generation failing must not lose the library puzzles
        generation["status"] = f"failed: {exc}"
        return out, generation
    generation.update(generated=len(result.accepted), rejected=len(result.rejected), attempts=result.attempts)
    for ex in result.accepted:
        if ex.status != "verified":   # never serve anything that didn't pass every check
            continue
        p = index.get(ex.id)
        if p is not None and p.clear_start:
            out.append({"puzzle": p, "example": ex, "origin": "generated",
                        "reasons": [f"Built for your {weakness['title'].lower()} weakness", "New to you"]})
    return out, generation

def _find(puzzle_id: str):
    """A library puzzle, or one of the learner's own game puzzles (puzzles.from_game)."""
    from .puzzles import get_puzzles
    from .puzzles.from_game import get_game_puzzles
    return get_puzzles(_knowledge()).get(puzzle_id) or get_game_puzzles().get(puzzle_id)


@router.post("/{puzzle_id}/result")
def puzzle_result(puzzle_id: str, body: ResultRequest):
    from .knowledge.usage import get_usage
    from .learner import get_store
    from .puzzles import get_puzzles

    p = _find(puzzle_id)
    if p is None:
        return _error(404, "no verified puzzle with that id")
    solved = body.solved and not body.revealed
    first_try = bool(solved and (body.critical_first_try if body.critical_first_try is not None
                                 else body.first_try) and body.mistakes == 0)
    usage = get_usage()
    try:
        usage.record_resolved(p.id, solved, first_try, hints=body.hints, seconds=body.seconds,
                              revealed=body.revealed, concept=p.concept)
    except OSError:
        pass
    changed = get_store().update("local", lambda prof: prof.record_attempt(
        p.concept, p.rating, solved=solved, first_try=first_try, hints=body.hints, revealed=body.revealed,
        learner_moves=max(1, p.meaningful_moves), context={"source": "puzzle", "exercise_id": p.id}))
    return {"recorded": True, "concept": p.concept, "solved": solved, "first_try": first_try,
            "rating_before": (changed or {}).get("rating_before"), "rating_after": (changed or {}).get("rating_after"),
            "stats": usage.puzzle_stats(p.id)}


@router.get("/{puzzle_id}")
def puzzle(puzzle_id: str):
    from .knowledge.usage import get_usage
    from .puzzles import get_puzzles

    p = _find(puzzle_id)
    if p is None:
        return JSONResponse(status_code=404, content={"error": "no verified puzzle with that id"})
    return {**p.as_dict(), "stats": get_usage().puzzle_stats(p.id)}
