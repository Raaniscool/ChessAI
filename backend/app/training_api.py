"""Training (Puzzles tab -> Training): short games against a bot that assess the learner.

    GET  /api/puzzles/training                 -> modes (with position counts) and the profile summary
    POST /api/puzzles/training/start           {mode} -> a new segment: the board only (no idea, no source)
    POST /api/puzzles/training/{id}/move       {uci} -> the learner's move (python-chess checks it), the
                                                  bot's reply, and whether the segment is over
    POST /api/puzzles/training/{id}/finish     -> Stockfish analysis of the segment, the feedback, and the
                                                  updated profile (also ends a segment early)
    GET  /api/puzzles/training/profile         -> the explainable skill profile (assessment.needs.summary)

Modes: opening ("Beginning Game"), middlegame, endgame, mixed. See backend/app/assessment.
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import chess
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/puzzles/training", tags=["training"])
ENGINE_NEEDED = "Training needs the Stockfish engine to play and analyse — it isn't available right now."


class StartRequest(BaseModel):
    mode: str = "mixed"
    seed: int | None = Field(default=None, description="tests: a reproducible segment")


class MoveRequest(BaseModel):
    uci: str


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _knowledge():
    from .knowledge.library import get_knowledge
    return get_knowledge()


def _engine():
    from .engine.service import EngineUnavailable, get_engine
    try:
        return get_engine()
    except EngineUnavailable:
        return None


def _summary(profile, knowledge) -> dict:
    from .assessment.needs import summary
    return summary(profile, knowledge)


@router.get("")
def training_home():
    from .assessment.positions import MODES, get_pool
    from .learner import get_profile
    knowledge = _knowledge()
    counts = get_pool(knowledge).counts()
    modes = [{"id": m, "label": label, "count": sum(counts.values()) if m == "mixed" else counts.get(m, 0)}
             for m, label in MODES.items()]
    return {"modes": modes, "profile": _summary(get_profile(), knowledge)}


@router.get("/profile")
def training_profile():
    from .learner import get_profile
    return _summary(get_profile(), _knowledge())


@router.post("/start")
def training_start(body: StartRequest):
    from .assessment import record
    from .assessment.bot import strength_for
    from .assessment.needs import concept_needs
    from .assessment.positions import MODES, RECENT_CONCEPTS, choose, get_pool, next_phase
    from .assessment.segment import get_segments
    from .knowledge.usage import get_usage
    from .learner import get_profile, get_store
    from .learner.difficulty import ZONE_OFFSET, for_learner

    if body.mode not in MODES:
        return _error(404, f"unknown Training mode: {body.mode}")
    knowledge = _knowledge()
    profile, usage = get_profile(), get_usage()
    pool = get_pool(knowledge)
    training = record.ensure(profile.training)
    seed = body.seed if body.seed is not None else random.SystemRandom().randrange(2 ** 31)
    rng = random.Random(seed)
    try:
        phase = next_phase(body.mode, training["history"], pool.phases(), rng)
    except LookupError:
        return _error(422, "No Training positions are available for this mode yet.")
    difficulty = for_learner(knowledge, profile, usage)
    bot = strength_for(difficulty, phase)
    used = {pid for pid, st in usage.all_puzzle_stats().items() if st.get("seen")}
    recent = []
    for h in training["history"][-RECENT_CONCEPTS:]:
        if h.get("concept"):
            recent.append(h["concept"])
        if str(h.get("position", "")).startswith("opening:"):
            recent.append("opening:" + h["position"].split(":")[1])
    try:
        position = choose(pool, phase, seen=training["seen"], used_puzzles=used, recent_concepts=recent,
                          target=bot["estimate"] - ZONE_OFFSET, needs=concept_needs(profile, knowledge), rng=rng)
    except LookupError:
        return _error(422, "No Training positions are available for this mode yet.")
    seg = get_segments().new(body.mode, position, bot, seed)
    get_store().update("local", lambda prof: setattr(prof, "training", _seen(prof.training, position.id)))
    if position.hidden:
        # the Puzzles tab won't serve this puzzle right away (it was just met here)
        usage.record_used([SimpleNamespace(id=position.hidden["puzzle_id"], concept=position.hidden["concept"])])
    return {"segment": seg.public(), "mode_label": MODES[body.mode]}


def _seen(training: dict, position_id: str) -> dict:
    from .assessment import record
    training = record.ensure(training)
    record.mark_seen(training, position_id)
    return training


@router.post("/{segment_id}/move")
def training_move(segment_id: str, body: MoveRequest):
    from .assessment.bot import choose_move
    from .assessment.segment import end_reason, get_segments
    seg = get_segments().get(segment_id)
    if seg is None:
        return _error(404, "This Training segment has expired — start a new one.")
    if seg.finished or seg.end_reason:
        return _error(409, "This segment is over.")
    board = seg.board()
    if board.turn != seg.side:
        return _error(409, "It's not your move.")
    try:
        move = chess.Move.from_uci(body.uci)
    except ValueError:
        return _error(400, "illegal move")
    if move not in board.legal_moves:
        return _error(400, "illegal move")
    seg.moves_san.append(board.san(move))
    seg.moves_uci.append(move.uci())
    board.push(move)
    reason = end_reason(seg, board)
    bot_move = None
    if reason is None and not board.is_game_over():
        engine = _engine()
        if engine is None:
            seg.moves_san.pop()
            seg.moves_uci.pop()
            return _error(503, ENGINE_NEEDED)
        bot_move = choose_move(engine, board, seg.bot["rating"], random.Random(f"{seg.seed}:{len(seg.moves_uci)}"))
        seg.moves_san.append(bot_move["san"])
        seg.moves_uci.append(bot_move["uci"])
        board.push_uci(bot_move["uci"])
        reason = end_reason(seg, board)
    seg.end_reason = reason
    return {"segment": seg.public(), "bot_move": bot_move and {"uci": bot_move["uci"], "san": bot_move["san"]},
            "ended": reason is not None}


@router.post("/{segment_id}/finish")
def training_finish(segment_id: str):
    from .assessment import evaluate, feedback, record
    from .assessment.needs import concept_needs, phase_needs
    from .assessment.segment import get_segments
    from .learner import get_store
    seg = get_segments().get(segment_id)
    if seg is None:
        return _error(404, "This Training segment has expired — start a new one.")
    if seg.result is not None:
        return seg.result
    if seg.learner_moves() == 0:
        return _error(422, "Play at least one move first.")
    engine = _engine()
    if engine is None:
        return _error(503, ENGINE_NEEDED)
    knowledge = _knowledge()
    seg.end_reason = seg.end_reason or "stopped"
    facts = evaluate.analyze(seg, engine, knowledge)

    def apply(prof):
        prof.training = record.apply(prof.training, seg, facts)
        return prof
    profile = get_store().update("local", apply)
    needs = concept_needs(profile, knowledge)
    phases = phase_needs(profile)
    seg.finished = True
    seg.result = {"segment": seg.public(), "analysis": _public_facts(facts),
                  "feedback": feedback.build(seg, facts, needs, phases, knowledge),
                  "profile": _summary(profile, knowledge)}
    return seg.result


def _public_facts(facts: dict) -> dict:
    keep = ("moves", "learner_moves", "accuracy", "avg_loss_cp", "best_moves", "best_move_rate", "blunders",
            "mistakes", "inaccuracies", "phases", "important_mistakes", "missed_opportunities", "concepts")
    return {k: facts[k] for k in keep}
