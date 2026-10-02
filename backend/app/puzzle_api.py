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
    POST /api/puzzles/{id}/result {solved, first_try, critical_first_try, mistakes, hints, seconds,
                                   revealed}   -> records the outcome (puzzle stats + learner model)
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
    return select(candidates, knowledge, concept, count=count, profile=get_profile(), usage=get_usage()).as_dict()


MIN_LIBRARY_BEFORE_GENERATING = 3  # fewer library matches than this -> build verified puzzles
MAX_GENERATED = 3                   # per set (each one costs Stockfish time)


class SetRequest(BaseModel):
    mode: Literal["personalized", "practice"] = "personalized"
    concept: str | None = None
    weakness: str | None = None
    count: int = Field(5, ge=1, le=10)
    generate: bool = True
    username: str | None = None


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


@router.get("/dashboard")
def dashboard() -> dict:
    from .learner import get_profile
    from .puzzles import get_puzzles
    from .puzzles.dashboard import candidates, personalized, themes

    knowledge = _knowledge()
    pool = candidates(get_puzzles(knowledge).all())
    return {"personalized": personalized(get_profile(), knowledge, pool), "practice": themes(pool, knowledge),
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


@router.post("/set")
def puzzle_set(body: SetRequest):
    from .knowledge.generation.log import get_log
    from .knowledge.usage import get_usage
    from .learner import get_profile
    from .puzzles import get_puzzles, select
    from .puzzles.dashboard import candidates, payload, personalized, theme_pool
    from .puzzles.personal import _SeenUsage, debug_block, library_selection

    knowledge = _knowledge()
    profile, usage, log = get_profile(), get_usage(), get_log()
    index = get_puzzles(knowledge)
    shown = log.shown()
    weakness, total_games, generation = None, 0, {"needed": 0, "generated": 0, "rejected": 0, "attempts": 0,
                                                  "status": "not needed"}
    if body.mode == "practice":
        concept = body.concept
        if not concept or concept not in knowledge.concepts:
            return _error(404, f"unknown concept: {concept}")
        pool = theme_pool(candidates(index.all()), knowledge, concept, body.count)
        selection = select(pool, knowledge, concept, count=body.count, profile=profile,
                           usage=_SeenUsage(usage, shown))
        title = f"Practice: {knowledge.concepts[concept].name}"
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
        selection = library_selection({**weakness, "evidence": weakness.get("evidence", [])}, knowledge,
                                      body.count, profile=profile, usage=usage, shown=shown)
        title = weakness["title"]
    chosen = [c for c in (selection.chosen if selection else []) if c.puzzle.clear_start]
    items = [(c.puzzle, knowledge.get(c.puzzle.id), c.reasons, "personal" if c.puzzle.tier == "personal"
              else "library") for c in chosen]
    items = [it for it in items if it[1] is not None]
    if weakness is not None and not key.startswith("puzzles:") and body.generate \
            and len(items) < min(body.count, MIN_LIBRARY_BEFORE_GENERATING):
        items, generation = _generate(weakness, knowledge, items, body, index)
    if not items:
        return _error(422, f"No verified puzzles for {title.lower()} right now — you've seen them all recently. "
                           "Try another theme, or come back tomorrow.")
    items.sort(key=lambda it: (it[0].rating, it[0].id))
    examples = [ex for _p, ex, _r, _o in items]
    try:
        usage.record_used(examples)
    except OSError:
        pass
    log.mark_shown([ex.id for _p, ex, _r, o in items if o != "library"])
    out = {"mode": body.mode, "concept": concept, "title": title, "weakness": weakness and weakness["key"],
           "puzzles": [payload(p, ex, knowledge, reasons, origin) for p, ex, reasons, origin in items]}
    if weakness is not None:
        out["debug"] = debug_block(weakness, total_games, selection, generation, examples,
                                   {ex.id: o for _p, ex, _r, o in items}, knowledge=knowledge)
    return out


def _generate(weakness: dict, knowledge, items: list, body: SetRequest, index):
    """Top up a short personalized set with new puzzles — only ones that passed the full pipeline."""
    from .analysis import personal_puzzles
    from .engine.service import EngineUnavailable, get_engine

    need = min(MAX_GENERATED, body.count - len(items))
    generation = {"needed": need, "generated": 0, "rejected": 0, "attempts": 0, "status": "ran"}
    if not personal_puzzles.supported_weakness(weakness):
        generation["status"] = "unsupported weakness"
        return items, generation
    try:
        engine = get_engine()
    except EngineUnavailable:
        generation["status"] = "engine unavailable"
        return items, generation
    try:
        result = personal_puzzles.generate_for(weakness, knowledge, engine, count=need, username=body.username)
    except Exception as exc:  # generation failing must not lose the library puzzles
        generation["status"] = f"failed: {exc}"
        return items, generation
    generation.update(generated=len(result.accepted), rejected=len(result.rejected), attempts=result.attempts)
    for ex in result.accepted:
        if ex.status != "verified":   # never serve anything that didn't pass every check
            continue
        p = index.get(ex.id)
        if p is not None and p.clear_start:
            items.append((p, ex, [f"Built for your {weakness['title'].lower()} weakness", "New to you"], "generated"))
    return items, generation


@router.post("/{puzzle_id}/result")
def puzzle_result(puzzle_id: str, body: ResultRequest):
    from .knowledge.usage import get_usage
    from .learner import get_store
    from .puzzles import get_puzzles

    p = get_puzzles(_knowledge()).get(puzzle_id)
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
        learner_moves=max(1, p.meaningful_moves)))
    return {"recorded": True, "concept": p.concept, "solved": solved, "first_try": first_try,
            "rating_before": (changed or {}).get("rating_before"), "rating_after": (changed or {}).get("rating_after"),
            "stats": usage.puzzle_stats(p.id)}


@router.get("/{puzzle_id}")
def puzzle(puzzle_id: str):
    from .knowledge.usage import get_usage
    from .puzzles import get_puzzles

    p = get_puzzles(_knowledge()).get(puzzle_id)
    if p is None:
        return JSONResponse(status_code=404, content={"error": "no verified puzzle with that id"})
    return {**p.as_dict(), "stats": get_usage().puzzle_stats(p.id)}
