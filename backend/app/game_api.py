"""HTTP API for game analysis: import Chess.com PGNs, analyze, review, recurring weaknesses, training.

    POST   /api/games/import                     {pgn, username?, source?}  -> imported games / per-game errors
    GET    /api/games                            -> the learner's games (+ analysis summary)
    GET    /api/games/{id}                       -> game + full analysis with review cards
    DELETE /api/games/{id}
    POST   /api/games/analyze                    {game_ids?, reanalyze?} -> NDJSON progress, then weaknesses
    GET    /api/games/weaknesses?ids=a,b         -> recurring weaknesses (>= 2 games) + seen-once patterns
    POST   /api/games/{id}/moments/{ply}/explain {question?} -> NDJSON: Qwen explains verified facts
    POST   /api/games/training                   {keys, game_ids?, level?} -> personal plan (like /api/plans)
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .analysis import GameAnalyzer, recurring_weaknesses
from .analysis import review
from .analysis.training import TrainingError, create_training_plan
from .engine import EngineUnavailable, get_engine
from .games import store
from .games.importers import PlayerNeeded, get_importer
from .games.model import GameRecord
from .games.pgn import PgnError

router = APIRouter(prefix="/api/games")


class ImportRequest(BaseModel):
    pgn: str
    username: str | None = None
    source: str = "chesscom"


class AnalyzeRequest(BaseModel):
    game_ids: list[str] | None = None
    reanalyze: bool = False


class ExplainRequest(BaseModel):
    question: str | None = None
    level: str | None = None


class TrainingRequest(BaseModel):
    keys: list[str]
    game_ids: list[str] | None = None
    level: str | None = None


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


def _analysis_summary(analysis: dict | None) -> dict | None:
    if not analysis:
        return None
    by = analysis["stats"]["by_category"]
    return {"analyzed_at": analysis["analyzed_at"], "blunders": by.get("blunder", 0),
            "mistakes": by.get("mistake", 0), "inaccuracies": by.get("inaccurate", 0),
            "moments": len(analysis["moments"]), "habits": len(analysis.get("habits", [])),
            "learner_moves": analysis["stats"]["learner_moves"]}


def _with_cards(analysis: dict) -> dict:
    out = dict(analysis)
    out["moments"] = [{**m, "review": review.review_card(m)} for m in analysis["moments"]]
    out["habits"] = [{**h, "review": review.review_card(h)} for h in analysis.get("habits", [])]
    return out


def _knowledge():
    from .knowledge.library import get_knowledge
    return get_knowledge()


def _analyzed(game_ids: list[str] | None = None) -> list[dict]:
    docs = store.list_docs()
    if game_ids:
        wanted = set(game_ids)
        docs = [d for d in docs if d["game"]["id"] in wanted]
    return [d for d in docs if d.get("analysis")]


def _weaknesses(game_ids: list[str] | None = None) -> dict:
    return recurring_weaknesses([d["analysis"] for d in _analyzed(game_ids)], _knowledge())


@router.post("/import")
def import_games(body: ImportRequest):
    try:
        importer = get_importer(body.source)
        result = importer.parse(body.pgn, body.username)
    except PlayerNeeded as exc:
        return _error(422, str(exc), needs_player=True, players=exc.players)
    except (PgnError, ValueError) as exc:
        return _error(422, str(exc))
    new = 0
    for game in result.games:
        new += store.save_game(game)
    return {
        "source": importer.source,
        "imported": [g.summary() for g in result.games],
        "new": new,
        "errors": [{"index": e.index, "error": e.error, "white": e.white, "black": e.black}
                   for e in result.errors],
    }


@router.get("")
def list_games() -> dict:
    return {"games": [{**_summary(d), "analysis": _analysis_summary(d.get("analysis"))}
                      for d in store.list_docs()]}


def _summary(doc: dict) -> dict:
    return GameRecord.from_dict(doc["game"]).summary()


@router.get("/weaknesses")
def weaknesses(ids: str | None = None) -> dict:
    return _weaknesses([i for i in ids.split(",") if i] if ids else None)


@router.get("/{game_id}")
def get_game(game_id: str):
    try:
        doc = store.load_doc(game_id)
    except store.GameNotFound:
        return _error(404, f"no game {game_id!r}")
    game = doc["game"]
    return {"game": {**_summary(doc), "start_fen": game["start_fen"], "moves_san": game["moves_san"],
                     "moves_uci": game["moves_uci"]},
            "analysis": _with_cards(doc["analysis"]) if doc.get("analysis") else None}


@router.delete("/{game_id}")
def delete_game(game_id: str):
    try:
        store.delete_game(game_id)
    except store.GameNotFound:
        return _error(404, f"no game {game_id!r}")
    return {"deleted": game_id}


def _ndjson(events) -> StreamingResponse:
    def lines():
        for event in events:
            yield json.dumps(event) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/analyze")
def analyze(body: AnalyzeRequest):
    docs = store.list_docs()
    if body.game_ids:
        wanted = set(body.game_ids)
        docs = [d for d in docs if d["game"]["id"] in wanted]
    todo = [d for d in docs if body.reanalyze or not d.get("analysis")]
    try:
        engine = get_engine()
    except EngineUnavailable as exc:
        return _error(503, str(exc))

    def events():
        analyzer = GameAnalyzer(engine)
        yield {"type": "start", "count": len(todo), "games": [d["game"]["id"] for d in todo]}
        for index, doc in enumerate(todo, start=1):
            game = GameRecord.from_dict(doc["game"])
            try:
                for event in analyzer.iter_analysis(game):
                    if event["type"] == "progress":
                        yield {**event, "index": index, "count": len(todo)}
                    else:
                        store.save_analysis(game.id, event["analysis"])
                        yield {"type": "game_done", "index": index, "count": len(todo), "game_id": game.id,
                               "summary": _analysis_summary(event["analysis"])}
            except EngineUnavailable as exc:
                yield {"type": "error", "game_id": game.id, "error": str(exc)}
                return
            except Exception as exc:  # one broken game must not stop the batch
                yield {"type": "error", "game_id": game.id, "error": f"{type(exc).__name__}: {exc}"}
        ids = body.game_ids or None
        yield {"type": "done", "weaknesses": _weaknesses(ids)}

    return _ndjson(events())


@router.post("/{game_id}/moments/{ply}/explain")
def explain_moment(game_id: str, ply: int, body: ExplainRequest | None = None):
    body = body or ExplainRequest()
    try:
        analysis = store.load_analysis(game_id)
    except store.GameNotFound:
        return _error(404, f"no game {game_id!r}")
    moment = next((m for m in (analysis or {}).get("moments", []) if m["ply"] == ply), None)
    if moment is None:
        return _error(404, f"move {ply} of this game is not one of its analysed moments")
    from .teacher import stream_events
    from .teacher.prompts import build_game_moment_messages
    level = body.level if body.level in ("beginner", "intermediate", "advanced") else "beginner"
    facts = review.moment_facts(moment, _knowledge(), level)
    question = (body.question or "").strip()[:300] or None
    return _ndjson(stream_events(
        lambda: build_game_moment_messages(facts, level, question),
        lambda: review.fallback_why(moment),
        validate=lambda text: review.conflicts(text, moment),
    ))


@router.post("/training")
def training(body: TrainingRequest):
    from .knowledge.usage import get_usage
    from .lessons import get_library
    from .planner.store import register_record, save_record

    docs = _analyzed(body.game_ids)
    if not docs:
        return _error(422, "analyze some games first")
    found = recurring_weaknesses([d["analysis"] for d in docs], _knowledge())
    by_key = {w["key"]: w for w in found["weaknesses"] + found["seen_once"]}
    chosen = [by_key[k] for k in body.keys if k in by_key]
    if not chosen:
        return _error(422, "none of those weaknesses were found in the analyzed games")
    moments = {m["id"]: m for d in docs for m in d["analysis"]["moments"]}
    try:
        record = create_training_plan(chosen, moments, len(docs), _knowledge(), usage=get_usage(),
                                      level=body.level)
    except TrainingError as exc:
        return _error(422, str(exc))
    register_record(get_library(), record)
    save_record(record)
    return {"plan": record["plan"], "course_id": record["course"]["id"],
            "first_lesson_id": record["lessons"][0]["id"], "source": "games"}
