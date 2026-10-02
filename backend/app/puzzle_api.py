"""Puzzle Library API (read-only; puzzles are solved inside lessons/plans).

    GET /api/puzzles                              -> library summary (counts by type, tier, uniqueness)
    GET /api/puzzles/select?concept=&count=5      -> the most useful puzzles for a concept right now,
                                                     easy -> hard, each with its reasons and stats
    GET /api/puzzles/{id}                         -> one puzzle (metadata + the learner's stats)
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

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


@router.get("/{puzzle_id}")
def puzzle(puzzle_id: str):
    from .knowledge.usage import get_usage
    from .puzzles import get_puzzles

    p = get_puzzles(_knowledge()).get(puzzle_id)
    if p is None:
        return JSONResponse(status_code=404, content={"error": "no verified puzzle with that id"})
    return {**p.as_dict(), "stats": get_usage().puzzle_stats(p.id)}
