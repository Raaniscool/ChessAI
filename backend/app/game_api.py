"""HTTP API for game analysis: import Chess.com PGNs, analyze, review, recurring weaknesses, training.

    POST   /api/games/import                     {pgn, username?, source?}  -> imported games / per-game errors
    GET    /api/games                            -> the learner's games (+ analysis summary)
    GET    /api/games/{id}                       -> game + full analysis with review cards
    DELETE /api/games/{id}
    POST   /api/games/analyze                    {game_ids?, reanalyze?} -> NDJSON progress, then weaknesses
    GET    /api/games/weaknesses?ids=a,b         -> patterns in >= 2 games + seen-once patterns (low level)
    POST   /api/games/fetch                      {username, count} -> fetch recent games from Chess.com's public API
    GET    /api/games/history?count=10&username= -> history report for the last N games (no engine work)
    POST   /api/games/history/analyze            {count, reanalyze?} -> NDJSON: analyze what's missing, then report
    POST   /api/games/history/explain            {count, level?} -> NDJSON: Qwen explains the report's findings
    POST   /api/games/{id}/moments/{ply}/explain {question?} -> NDJSON: Qwen explains verified facts
    POST   /api/games/training                   {keys, game_ids?, level?} -> personal plan (like /api/plans)
    POST   /api/games/puzzles                    {key, count?} -> NDJSON: new engine-verified puzzles for a
                                                 weakness (personal tier, never copies of the learner's games)
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .analysis import GameAnalyzer, recurring_weaknesses
from .analysis import history, review
from .analysis.analyzer import SCHEMA_VERSION as ANALYSIS_VERSION
from .analysis import personal_puzzles
from .analysis.training import TrainingError, create_puzzle_plan, create_training_plan
from .engine import EngineUnavailable, get_engine
from .games import store
from .games.importers import PlayerNeeded, chesscom_api, get_importer
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


class HistoryRequest(BaseModel):
    count: int = history.DEFAULT_COUNT
    reanalyze: bool = False
    level: str | None = None
    username: str | None = None


class FetchRequest(BaseModel):
    username: str
    count: int = history.DEFAULT_COUNT


class TrainingRequest(BaseModel):
    keys: list[str]
    game_ids: list[str] | None = None
    username: str | None = None
    level: str | None = None


class PuzzleRequest(BaseModel):
    key: str
    count: int = Field(3, ge=1, le=5)
    game_ids: list[str] | None = None
    username: str | None = None


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


def _analyzed(game_ids: list[str] | None = None, username: str | None = None) -> list[dict]:
    docs = store.list_docs()
    if game_ids:
        wanted = set(game_ids)
        docs = [d for d in docs if d["game"]["id"] in wanted]
        if username:
            docs = history.games_of(docs, username)
    else:
        # patterns belong to one learner: games of different players are never mixed
        docs, _ = history.one_player(docs, username)
    # an analysis made by an older analyzer version doesn't count until it is redone
    # (the history report already ignored them; weaknesses and training used them anyway)
    return [d for d in docs if _current(d.get("analysis"))]


def _current(analysis: dict | None) -> bool:
    """A cached analysis is reused unless it was made by an older version of the analyzer."""
    return bool(analysis) and analysis.get("schema_version") == ANALYSIS_VERSION


def _weaknesses(game_ids: list[str] | None = None, username: str | None = None) -> dict:
    return recurring_weaknesses([d["analysis"] for d in _analyzed(game_ids, username)], _knowledge())


@router.post("/import")
def import_games(body: ImportRequest):
    try:
        return _import(body.pgn, body.username, body.source)
    except PlayerNeeded as exc:
        return _error(422, str(exc), needs_player=True, players=exc.players)
    except (PgnError, ValueError) as exc:
        return _error(422, str(exc))


def _import(pgn: str, username: str | None, source: str = "chesscom") -> dict:
    importer = get_importer(source)
    result = importer.parse(pgn, username)
    new = 0
    legacy = getattr(importer, "legacy_ids", lambda record: [])
    for game in result.games:
        new += store.save_game(game, legacy_ids=legacy(game))
    return {
        "source": importer.source,
        "imported": [g.summary() for g in result.games],
        "new": new,
        "errors": [{"index": e.index, "error": e.error, "white": e.white, "black": e.black}
                   for e in result.errors],
    }


@router.post("/fetch")
def fetch_games(body: FetchRequest):
    """Download the player's most recent games from Chess.com (official public API) and import
    them like pasted PGNs. Games already imported are recognized and not duplicated."""
    try:
        count = history.validate_fetch_count(body.count)
        fetched = chesscom_api.fetch_recent_games(body.username, count)
    except history.HistoryError as exc:
        return _error(422, str(exc))
    except chesscom_api.ChessComFetchError as exc:
        return _error(exc.status, str(exc))
    if not fetched.pgns:
        return _error(404, f"No finished standard chess games found for “{fetched.username}” in the last "
                           f"{fetched.months_checked or chesscom_api.MAX_MONTHS} months on Chess.com.")
    try:
        result = _import("\n\n".join(fetched.pgns), fetched.username)
    except (PlayerNeeded, PgnError, ValueError) as exc:
        return _error(422, str(exc))
    return {**result, "username": fetched.username, "fetched": len(fetched.pgns),
            "skipped_variants": fetched.skipped_variants}


@router.get("")
def list_games() -> dict:
    return {"games": [{**_summary(d), "analysis": _analysis_summary(d.get("analysis"))}
                      for d in store.list_docs()]}


def _summary(doc: dict) -> dict:
    return GameRecord.from_dict(doc["game"]).summary()


# --- game history: the last N games, analyzed together ----------------------------------------
def _history_selection(count, username: str | None = None) -> tuple[int, list[dict], int]:
    n = history.validate_count(count)
    docs, _ = history.one_player(store.list_docs(), username)
    selected = history.select_recent(docs, n)
    return n, selected, sum(1 for d in docs if d["game"].get("player_color"))


def _history_report(count, failed: list[dict] | None = None, username: str | None = None) -> dict:
    n, selected, available = _history_selection(count, username)
    for d in selected:  # a stale analysis doesn't count until it's redone
        if not _current(d.get("analysis")):
            d["analysis"] = None
    report = history.build_report(selected, _knowledge(), requested=n, available=available, failed=failed)
    report["player"] = next((d["game"].get("player") for d in selected if d["game"].get("player")), None)
    return report


@router.get("/history")
def history_report(count: int = history.DEFAULT_COUNT, username: str | None = None):
    try:
        return _history_report(count, username=username)
    except history.HistoryError as exc:
        return _error(422, str(exc))


@router.post("/history/analyze")
def history_analyze(body: HistoryRequest):
    """Analyze the last N games: cached analyses are reused, so only new games cost engine time."""
    try:
        n, selected, available = _history_selection(body.count, body.username)
    except history.HistoryError as exc:
        return _error(422, str(exc))
    todo = [d for d in selected if body.reanalyze or not _current(d.get("analysis"))]
    engine = None
    if todo:
        try:
            engine = get_engine()
        except EngineUnavailable as exc:
            return _error(503, str(exc))

    def events():
        yield {"type": "select", "requested": n, "available": available,
               "selected": [d["game"]["id"] for d in selected], "cached": len(selected) - len(todo),
               "to_analyze": len(todo)}
        failed = []
        for event in _analysis_events(todo, engine):
            if event["type"] == "error":
                failed.append({"game_id": event["game_id"], "error": event["error"]})
            yield event
        yield {"type": "done", "report": _history_report(n, failed, body.username)}

    return _ndjson(events())


@router.post("/history/explain")
def history_explain(body: HistoryRequest):
    try:
        report = _history_report(body.count, username=body.username)
    except history.HistoryError as exc:
        return _error(422, str(exc))
    if not report["games_analyzed"]:
        return _error(422, "analyze some games first")
    from .teacher import stream_events
    from .teacher.prompts import build_history_messages
    level = body.level if body.level in ("beginner", "intermediate", "advanced") else "beginner"
    library = _knowledge()
    facts = history.history_facts(report, library, level)
    return _ndjson(stream_events(
        lambda: build_history_messages(facts, level),
        lambda: history.fallback_explanation(report),
        validate=lambda text: history.conflicts(text, report, library),
    ))


@router.get("/weaknesses")
def weaknesses(ids: str | None = None, username: str | None = None) -> dict:
    return _weaknesses([i for i in ids.split(",") if i] if ids else None, username)


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
    todo = [d for d in docs if body.reanalyze or not _current(d.get("analysis"))]
    engine = None
    if todo:  # nothing to analyze = no engine needed (cached results work offline)
        try:
            engine = get_engine()
        except EngineUnavailable as exc:
            return _error(503, str(exc))

    def events():
        yield from _analysis_events(todo, engine)
        # Patterns are judged across all of the learner's analyzed games, not just this batch:
        # a mistake in today's game may repeat one from last week.
        yield {"type": "done", "weaknesses": _weaknesses()}

    return _ndjson(events())


def _analysis_events(todo: list[dict], engine):
    """Analyze games one by one (each saved as soon as it's done, so an interrupted batch
    keeps its finished games). One broken game never stops the others."""
    analyzer = GameAnalyzer(engine) if todo else None
    yield {"type": "start", "count": len(todo), "games": [d["game"]["id"] for d in todo]}
    for index, doc in enumerate(todo, start=1):
        game = GameRecord.from_dict(doc["game"])
        deleted = {"type": "skipped", "index": index, "count": len(todo), "game_id": game.id,
                   "reason": "this game was deleted, so it wasn't analyzed"}
        if not store.exists(game.id):  # deleted after the batch started: don't spend engine time on it
            yield deleted
            continue
        try:
            for event in analyzer.iter_analysis(game):
                if event["type"] == "progress":
                    yield {**event, "index": index, "count": len(todo)}
                else:
                    store.save_analysis(game.id, event["analysis"])
                    yield {"type": "game_done", "index": index, "count": len(todo), "game_id": game.id,
                           "summary": _analysis_summary(event["analysis"])}
        except store.GameNotFound:  # deleted while Stockfish was working on it: nothing to save
            yield deleted
        except EngineUnavailable as exc:  # the engine itself is gone: the rest would fail too
            for rest in todo[index - 1:]:
                yield {"type": "error", "game_id": rest["game"]["id"], "error": str(exc)}
            return
        except Exception as exc:  # one broken game must not stop the batch
            yield {"type": "error", "game_id": game.id, "error": f"{type(exc).__name__}: {exc}"}


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


def _find_weaknesses(keys: list[str], game_ids: list[str] | None, username: str | None = None):
    """(docs, chosen weaknesses) or an error response."""
    docs = _analyzed(game_ids, username)
    if not docs:
        return None, _error(422, "analyze some games first")
    analyses = [d["analysis"] for d in docs]
    found = recurring_weaknesses(analyses, _knowledge())
    by_key = {w["key"]: w for w in found["weaknesses"] + found["seen_once"]}
    # the history report may group concepts differently (a parent that only recurs when its
    # children are added up): accept its keys too
    grouped = recurring_weaknesses(analyses, _knowledge(), rank=lambda games: history.tier_for(games, len(docs)))
    for w in grouped["weaknesses"] + grouped["seen_once"]:
        by_key.setdefault(w["key"], w)
    chosen = [by_key[k] for k in keys if k in by_key]
    if not chosen:
        return None, _error(422, "none of those weaknesses were found in the analyzed games")
    return (docs, chosen), None


@router.post("/training")
def training(body: TrainingRequest):
    from .knowledge.generation.log import get_log
    from .knowledge.usage import get_usage
    from .lessons import get_library
    from .planner.store import register_record, save_record

    found, err = _find_weaknesses(body.keys, body.game_ids, body.username)
    if err is not None:
        return err
    docs, chosen = found
    moments = {m["id"]: m for d in docs for m in d["analysis"]["moments"]}
    shown = get_log().shown()
    generated = {w["key"]: personal_puzzles.unseen_for(w, _knowledge(), shown) for w in chosen}
    try:
        record = create_training_plan(chosen, moments, len(docs), _knowledge(), usage=get_usage(),
                                      level=body.level, generated=generated)
    except TrainingError as exc:
        return _error(422, str(exc))
    used = [eid for u in record["lessons"] for eid in (u.get("origin") or {}).get("generated", [])]
    if used:
        get_log().mark_shown(used)
    register_record(get_library(), record)
    save_record(record)
    return {"plan": record["plan"], "course_id": record["course"]["id"],
            "first_lesson_id": record["lessons"][0]["id"], "source": "games"}


@router.post("/puzzles")
def puzzles(body: PuzzleRequest):
    """New puzzles for one weakness, streamed (NDJSON) while they are generated and checked:

    {"type": "status", "text"} · {"type": "puzzle", "id", "title", "concept"} ·
    {"type": "done", "plan", "course_id", "first_lesson_id", "new", "reused", "rejected", "attempts"} ·
    {"type": "error", "error"} (nothing passed verification, or the engine failed)
    """
    from .knowledge.generation.log import get_log
    from .lessons import get_library
    from .planner.store import register_record, save_record

    found, err = _find_weaknesses([body.key], body.game_ids, body.username)
    if err is not None:
        return err
    docs, (weakness,) = found
    if not personal_puzzles.supported_weakness(weakness):
        return _error(422, f"I can't build new puzzles for “{weakness['title']}” yet — try the training "
                           "plan, which uses verified examples and your own positions.")
    library = _knowledge()
    reused = personal_puzzles.unseen_for(weakness, library)[: body.count]
    engine = None
    if len(reused) < body.count:
        try:
            engine = get_engine()
        except EngineUnavailable as exc:
            if not reused:
                return _error(503, f"New puzzles have to be checked by Stockfish, which isn't available: {exc}")

    def events():
        examples = list(reused)
        result = None
        names = ", ".join(personal_puzzles.puzzle_names(library, weakness)).lower() or "puzzles"
        if reused:
            yield {"type": "status", "text": f"{len(reused)} puzzle{'s' if len(reused) != 1 else ''} already made "
                                             "for you and not shown yet."}
        if engine is not None:
            yield {"type": "status", "text": f"Building new {names} positions and checking each with Stockfish…"}
            try:
                result = personal_puzzles.generate_for(weakness, library, engine, count=body.count - len(reused),
                                                       username=body.username)
            except Exception as exc:  # generation failing must not lose the reused ones
                yield {"type": "status", "text": f"Building new positions failed: {exc}"}
            else:
                for ex in result.accepted:
                    yield {"type": "puzzle", "id": ex.id, "title": ex.title, "concept": ex.concept}
                examples += result.accepted
        if not examples:
            yield {"type": "error", "error": "None of the positions I built passed every check in time, so I'm "
                                             "not showing any. Try again — each attempt uses new positions."}
            return
        record = create_puzzle_plan(weakness, examples, len(docs), library)
        get_log().mark_shown([e.id for e in examples])
        register_record(get_library(), record)
        save_record(record)
        yield {"type": "done", "plan": record["plan"], "course_id": record["course"]["id"],
               "first_lesson_id": record["lessons"][0]["id"], "new": len(examples) - len(reused),
               "reused": len(reused), "rejected": len(result.rejected) if result else 0,
               "attempts": result.attempts if result else 0}

    return _ndjson(events())
