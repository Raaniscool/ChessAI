"""FastAPI application: lesson-session API + web UI.

All board state lives server-side (session.py). The frontend sends moves and
receives validated step payloads — the AI never bypasses validation.
"""
from __future__ import annotations

from pathlib import Path

import asyncio
import json
import logging
import time
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import chess_system
from .chess_system import ChessError
from .config import get_settings
from .engine import EngineUnavailable
from .lessons import LessonNotFound, get_library
from .planner import PlanError, get_catalog, plan_for_goal
from .planner.store import delete_record, register_record, save_record
from .session import ExerciseConflict, SessionError, get_manager
from .teacher import get_teacher

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
perf_log = logging.getLogger("chessai.performance")
perf_log.setLevel(logging.INFO)
if not perf_log.handlers:
    _perf_handler = logging.StreamHandler()
    _perf_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    perf_log.addHandler(_perf_handler)
    perf_log.propagate = False


def _warm_up_knowledge() -> None:
    """Load the verified retrieval index before the first learner request arrives."""
    started = time.perf_counter()
    try:
        from .knowledge.library import get_knowledge
        library = get_knowledge()
        perf_log.info("latency stage=knowledge_warmup duration_ms=%.1f verified_examples=%d status=done",
                      (time.perf_counter() - started) * 1000, len(library.verified()))
    except Exception:
        # Keep startup usable in degraded mode; the endpoint reports the retrieval failure if used.
        logging.getLogger("chessai").exception("Knowledge Library warm-up failed")
        perf_log.info("latency stage=knowledge_warmup duration_ms=%.1f status=failed",
                      (time.perf_counter() - started) * 1000)


def _warm_up_qwen() -> None:
    """Load the model in the background so the learner's first reply isn't the slowest."""
    settings = get_settings()
    if not (settings.qwen_configured() and settings.qwen_warmup):
        return

    def run():
        from .teacher import QwenTeacher
        try:
            ok = QwenTeacher(settings).warm_up()
        except Exception:  # noqa: BLE001 - settings changed or Ollama gone; warm-up is optional
            ok = False
        logging.getLogger("chessai").info("Qwen warm-up %s", "done" if ok else "failed")

    threading.Thread(target=run, name="qwen-warmup", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The verified index is used by local answers, routing and lesson context. Do its one-time
    # validation before accepting traffic so it does not consume the learner's first TTFO.
    await asyncio.to_thread(_warm_up_knowledge)
    _warm_up_qwen()
    yield
    from .engine.service import shutdown_engine
    shutdown_engine()  # otherwise Ctrl+C hangs once Stockfish has been started


app = FastAPI(title="AI Chess Tutor", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def revalidate_frontend(request: Request, call_next):
    """Make the browser re-check the page files on every load.

    Without this a browser may keep a cached index.html from an older version while
    fetching a newer app.js (or the other way round), and the page breaks with errors
    like "Cannot read properties of null". Static files carry an ETag, so the re-check
    is a cheap 304 when nothing changed."""
    started = time.perf_counter()
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    else:
        # Latency is visible (browser dev tools → Timing) and slow requests are logged.
        # For streamed replies this is the time to the first byte.
        ms = (time.perf_counter() - started) * 1000
        response.headers["Server-Timing"] = f"app;dur={ms:.0f}"
        if ms > SLOW_REQUEST_MS:
            logging.getLogger("chessai").info("slow request: %s %s took %.1fs", request.method,
                                              request.url.path, ms / 1000)
    return response


SLOW_REQUEST_MS = 3000


# --- error handling -------------------------------------------------------

@app.exception_handler(SessionError)
async def session_error_handler(request: Request, exc: SessionError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(ChessError)
async def chess_error_handler(request: Request, exc: ChessError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(LessonNotFound)
async def lesson_not_found_handler(request: Request, exc: LessonNotFound):
    return JSONResponse(status_code=404, content={"error": str(exc)})


log = logging.getLogger("chessai")


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    # Never leave the learner with a bare "500": log the traceback for the
    # console and send a readable message to the UI.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": f"Server error ({type(exc).__name__}): {exc}. See the server console for details."},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # The UI reads {"error": ...}; FastAPI's default {"detail": [...]} showed up as a bare
    # "Request failed (422)".
    problems = []
    for err in exc.errors()[:3]:
        where = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        problems.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return JSONResponse(status_code=422, content={"error": "Invalid request — " + "; ".join(problems)})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    if exc.status_code == 404 and message == "Not Found":
        message = f"Not found: {request.url.path}"
    return JSONResponse(status_code=exc.status_code, content={"error": message}, headers=exc.headers)


@app.exception_handler(PlanError)
async def plan_error_handler(request: Request, exc: PlanError):
    content = {"error": str(exc), "suggestions": exc.suggestions}
    if getattr(exc, "debug", None):
        content["debug"] = exc.debug
    return JSONResponse(status_code=422, content=content)


@app.exception_handler(EngineUnavailable)
async def engine_unavailable_handler(request: Request, exc: EngineUnavailable):
    return JSONResponse(status_code=503, content={"error": str(exc)})


# --- request models -------------------------------------------------------

class MoveRequest(BaseModel):
    uci: str
    expected_fen: str | None = None


class RevealConfirmRequest(BaseModel):
    expected_fen: str


class AdvanceConfirmRequest(BaseModel):
    source_index: int
    source_fen: str


class ChatRequest(BaseModel):
    message: str
    intent: dict | None = None


class StartLessonRequest(BaseModel):
    history: list[dict] = Field(default_factory=list)


class CoachRouteRequest(BaseModel):
    message: str
    session_id: str | None = None
    # Used only before a lesson exists. Once session_id is provided, the server ignores this
    # object and reads the authoritative learning state and transcript from SessionManager.
    learning_state: dict | None = None
    history: list[dict] = Field(default_factory=list)


class Clarification(BaseModel):
    key: str                  # the question's key (from a previous {"clarify": ...} answer)
    choice: str               # an option id, or "other"
    text: str | None = None   # the learner's own words for "other"


class PlanRequest(BaseModel):
    goal: str
    # Try the verified Knowledge Library first (the UI always does). Off = catalog planner only.
    # It also turns on intent understanding: ambiguous requests get a question back.
    library: bool = False
    level: str | None = None  # beginner | intermediate | advanced (learner skill, optional)
    clarification: Clarification | None = None  # the answer to a question this endpoint asked
    reclarify: bool = False   # ignore remembered answers for this request and ask again
    weakness: str | None = None  # a recurring weakness key (Game History) this plan should train
    username: str | None = None  # whose games / whose personal plans


class IntentRequest(BaseModel):
    message: str
    session_id: str | None = None


# --- API ------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    from .engine import engine_available
    library = get_library()
    try:
        engine_ok = engine_available()
    except Exception:
        engine_ok = False
    return {
        "status": "ok" if engine_ok else "degraded",
        "engine": engine_ok,
        "teacher": get_teacher().name,
        "teacher_model": get_settings().qwen_model or None,
        "courses": len(library.courses()),
        "catalog_topics": _catalog_size(),
        "lessons": len(library.lesson_ids()),
        "knowledge_examples": _knowledge_size(),
    }


def _knowledge_size() -> int | str:
    try:
        from .knowledge.library import get_knowledge
        return len(get_knowledge().verified())
    except Exception as exc:
        return f"error: {exc}"


def _catalog_size() -> int | str:
    try:
        return len(get_catalog().topics)
    except Exception as exc:  # surfaced in /api/health instead of crashing it
        return f"error: {exc}"


@app.get("/api/courses")
def courses() -> dict:
    library = get_library()
    completed = _completed()
    out = []
    for course in library.courses():
        lessons = []
        for plan in course.lessons:
            entry = {
                "id": plan.id,
                "title": plan.title,
                "status": plan.status,
                "completed": plan.id in completed,
            }
            if plan.status == "available":
                lesson = library.lesson(plan.id)
                entry["description"] = lesson.description
                entry["difficulty"] = lesson.difficulty
            lessons.append(entry)
        entry = {
            "id": course.id,
            "title": course.title,
            "description": course.description,
            "kind": course.kind,
            "lessons": lessons,
        }
        if course.kind == "plan":
            entry["plan"] = course.meta.get("plan", {})
        out.append(entry)
    # Learner's own plans first (newest first), then the curated courses.
    # Reversed first so plans created within the same second stay newest-first (stable sort).
    plans = [c for c in out if c["kind"] == "plan"][::-1]
    plans.sort(key=lambda c: c["plan"].get("created", ""), reverse=True)
    return {"courses": plans + [c for c in out if c["kind"] != "plan"]}


# --- learning plans -------------------------------------------------------

@app.get("/api/topics")
def topics() -> dict:
    """The verified topic catalog that plans are built from."""
    return {"topics": [t.as_summary() for t in get_catalog().topics.values()]}


@app.post("/api/plans")
def make_plan(body: PlanRequest) -> dict:
    """'I want to learn ___' → a personal course of verified lessons.

    With ``library: true`` verified Knowledge Library examples are tried first;
    without a suitable example this is exactly the catalog → Qwen planner.
    """
    from .planner.intent import ClarificationError, ClarificationNeeded

    answers = None
    if body.clarification is not None:
        c = body.clarification
        answers = {c.key: {"choice": c.choice, "text": c.text}}
    personal = None
    if body.weakness:
        from .game_api import weakness_context
        personal = weakness_context(body.weakness, body.username, body.level)
        if personal is None:
            return JSONResponse(status_code=422, content={"error": "that weakness wasn't found in your analyzed games"})
    plan_started = time.perf_counter()
    try:
        from .learner import get_profile
        record = plan_for_goal(body.goal, library_first=body.library, level=body.level, clarify=body.library,
                               answers=answers, reclarify=body.reclarify, personal=personal,
                               learner=body.username or (personal or {}).get("learner"),
                               profile=get_profile())
    except ClarificationNeeded as need:
        # Several materially different readings: the learner chooses before anything is built.
        perf_log.info("latency stage=plan_build duration_ms=%.1f status=clarify",
                      (time.perf_counter() - plan_started) * 1000)
        return {"clarify": need.question.as_dict(), "goal": body.goal}
    except ClarificationError as exc:
        perf_log.info("latency stage=plan_build duration_ms=%.1f status=invalid",
                      (time.perf_counter() - plan_started) * 1000)
        return JSONResponse(status_code=422, content={"error": str(exc)})
    register_record(get_library(), record)
    save_record(record)
    perf_log.info("latency stage=plan_build duration_ms=%.1f planner=%s lessons=%d status=done",
                  (time.perf_counter() - plan_started) * 1000, record["plan"].get("planner", "unknown"),
                  len(record.get("lessons", [])))
    return {
        "plan": record["plan"],
        "course_id": record["course"]["id"],
        "first_lesson_id": record["lessons"][0]["id"],
        "source": {"knowledge": "knowledge", "fallback": "fallback", "custom": "custom"}.get(
            record["plan"].get("planner"), "planner"),
    }


@app.post("/api/knowledge/intent")
def knowledge_intent(body: IntentRequest) -> dict:
    """Is a chat message ("Show me checkmates") a request for a lesson the library can give?"""
    from .knowledge.library import get_knowledge
    from .knowledge.retrieval import lesson_request

    concepts = lesson_request(get_knowledge(), body.message[:300])
    return {"lesson_request": bool(concepts), "concepts": concepts}


@app.post("/api/knowledge/answer")
def knowledge_answer(body: IntentRequest) -> dict:
    """An instant structured fact from local libraries (no AI call), or null for contextual chat."""
    from .basic_explanations.library import get_basic_explanations
    from .knowledge.answers import quick_answer
    from .knowledge.glossary import get_glossary
    from .knowledge.library import get_knowledge

    started = time.perf_counter()
    answer = quick_answer(body.message[:300], get_knowledge(), get_glossary(), get_basic_explanations())
    perf_log.info("latency stage=quick_answer duration_ms=%.1f knowledge_lookup_calls=1 matched=%s source=%s",
                  (time.perf_counter() - started) * 1000, bool(answer),
                  (answer or {}).get("source", "none"))
    if answer and body.session_id:
        manager = get_manager()
        manager.record_turn(manager.get(body.session_id), body.message,
                            f"{answer.get('term', 'Chess concept')}: {answer.get('text', '')}")
    return {"answer": answer}


def _open_coach_reply(message: str, action: str, state: dict | None, history: list[dict]) -> str:
    """Answer a no-session conversational turn without manufacturing a lesson or board."""
    from .planner.intent.conversation import offline_reply

    # These are closed, deterministic intents with useful local copy; a second model call cannot
    # improve routing or correctness and would only delay the first visible reply.
    if action in {"greeting", "discovery"}:
        return offline_reply(action, message, state)

    teacher = get_teacher()
    if getattr(teacher, "name", "fallback") != "qwen":
        return offline_reply(action, message, state)
    from .teacher import LessonContext, chat_or_fallback
    context = LessonContext(course_title="Chess coach", lesson_title="Open conversation", concepts=[],
                            learning_state=state or {})
    reply, used = chat_or_fallback(teacher, message[:2000], context, history[-8:])
    return reply if used == "qwen" else offline_reply(action, message, state)


@app.post("/api/coach/route")
def coach_route(body: CoachRouteRequest) -> dict:
    """Interpret this turn against the current lesson, without receiving a board from the browser."""
    from .planner.intent.conversation import LESSON_STATE_QUESTION, route_message

    started = time.perf_counter()
    manager = get_manager()
    if body.session_id:
        session = manager.get(body.session_id)
        learning_state = manager.learning_state(session)
        history = list(session.transcript)
    else:
        session = None
        learning_state = body.learning_state or {}
        history = body.history

    # Handle closed greetings/discovery before initializing or searching the Knowledge Library.
    # A narrow lesson-status question can be routed from structured state here; other messages
    # share one local intent classification between quick answers and the existing semantic router.
    fast_state = learning_state if session is not None and LESSON_STATE_QUESTION.search(body.message) else None
    decision = route_message(body.message, state=fast_state, history=history, fast_only=True)
    quick_checked = False
    if decision is None:
        from .basic_explanations.library import get_basic_explanations
        from .knowledge.answers import quick_answer
        from .knowledge.glossary import get_glossary
        from .knowledge.library import get_knowledge

        quick_checked = True
        lookup_started = time.perf_counter()
        answer, question_intent = quick_answer(body.message[:300], get_knowledge(), get_glossary(),
                                              get_basic_explanations(), with_intent=True)
        lookup_ms = (time.perf_counter() - lookup_started) * 1000
        perf_log.info("latency stage=quick_answer duration_ms=%.1f knowledge_lookup_calls=1 matched=%s source=%s",
                      lookup_ms, bool(answer), (answer or {}).get("source", "none"))
        if answer:
            answer_text = f"{answer.get('term', 'Chess concept')}: {answer.get('text', '')}"
            if session is not None:
                manager.record_turn(session, body.message, answer_text)
            perf_log.info("latency endpoint=coach_route duration_ms=%.1f action=explanation source=quick_answer "
                          "model_calls=0 knowledge_lookup_calls=1", (time.perf_counter() - started) * 1000)
            return {
                "action": "explanation", "confidence": 1.0, "source": "quick_answer",
                "topic": answer.get("term"), "topic_id": answer.get("concept"), "subtopic": None,
                "mode": "explanation", "objective": "Explain the requested chess concept",
                "preserve_context": True, "requires_engine": False, "clarification": None,
                "quick_answer": answer,
            }
        decision = route_message(body.message, state=learning_state, history=history,
                                 question_intent=question_intent)

    result = decision.as_dict()
    if session is None and decision.action not in {"start_lesson", "topic_change", "puzzle", "practice",
                                                    "resume_lesson", "clarify", "mode_change", "continue_lesson"}:
        result["reply"] = _open_coach_reply(body.message, decision.action, learning_state, history)
    perf_log.info("latency endpoint=coach_route duration_ms=%.1f action=%s source=%s knowledge_lookup_calls=%d",
                  (time.perf_counter() - started) * 1000, decision.action, decision.source, int(quick_checked))
    return result


@app.get("/api/plans")
def list_plans() -> dict:
    plans = [c.meta.get("plan", {}) for c in get_library().courses() if c.kind == "plan"][::-1]
    plans.sort(key=lambda p: p.get("created", ""), reverse=True)
    return {"plans": plans}


@app.get("/api/plans/{plan_id}/progression")
def plan_progression(plan_id: str, current_lesson_id: str | None = None, restart: bool = False) -> dict:
    """Return the next available lesson from this stored plan, using saved progress.

    Lesson order comes from the registered course; completion comes from the same
    source used by the Your Lessons sidebar. No model is asked to infer progression.
    """
    library = get_library()
    course = library.course(f"plan_{plan_id}")
    plan = course.meta.get("plan", {})
    if course.kind != "plan" or plan.get("id") != plan_id:
        raise LessonNotFound(f"Unknown lesson plan: {plan_id}")

    lessons = [(n, item) for n, item in enumerate(course.lessons, start=1) if item.status == "available"]
    completed = _completed()
    current = next((entry for entry in lessons if entry[1].id == current_lesson_id), None)
    if restart:
        target_position = 0 if lessons else None
    else:
        target_position = next((i for i, entry in enumerate(lessons) if entry[1].id not in completed), None)
    target = lessons[target_position] if target_position is not None else None

    last_completed = None
    if current and current[1].id in completed:
        last_completed = current
    elif target_position is not None:
        last_completed = next((entry for entry in reversed(lessons[:target_position])
                               if entry[1].id in completed), None)
    elif lessons:
        last_completed = next((entry for entry in reversed(lessons) if entry[1].id in completed), None)

    def item_payload(entry):
        if not entry:
            return None
        number, item = entry
        return {"id": item.id, "title": item.title, "number": number}

    return {
        "plan_id": plan_id,
        "plan_title": course.title,
        "summary": course.description,
        "goal": plan.get("goal", ""),
        "related": plan.get("related", []),
        "lesson_count": len(lessons),
        "lesson_titles": [item.title for _, item in lessons],
        "completed_lesson": item_payload(last_completed),
        "next_lesson": item_payload(target),
        "complete": target is None,
    }


@app.delete("/api/plans/{plan_id}")
def remove_plan(plan_id: str) -> dict:
    library = get_library()
    course_id = f"plan_{plan_id}"
    library.course(course_id)  # 404 if unknown
    library.remove_course(course_id)
    delete_record(plan_id)
    return {"deleted": plan_id}


@app.get("/api/progress")
def progress() -> dict:
    return {"completed_lessons": sorted(_completed())}


def _completed() -> set[str]:
    """Finished lessons: this run's, plus those remembered in the learner profile."""
    from .learner import get_profile
    try:
        remembered = set(get_profile().completed_lessons)
    except Exception:
        remembered = set()
    return set(get_manager().completed_lessons) | remembered


@app.post("/api/lessons/{lesson_id}/start")
def start_lesson(lesson_id: str, body: StartLessonRequest | None = None) -> dict:
    manager = get_manager()
    session, step = manager.start(lesson_id, body.history if body else None)
    lesson = manager.lesson(session)
    course = manager.library.course(lesson.course_id)
    plan_id = course.meta.get("plan", {}).get("id") if course.kind == "plan" else None
    return {"session_id": session.id, "lesson_id": lesson_id, "course_id": course.id,
            "plan_id": plan_id, "step": step, "learning_state": manager.learning_state(session)}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    manager = get_manager()
    session = manager.get(session_id)
    return {
        "session_id": session.id,
        "lesson_id": session.lesson_id,
        "status": session.status,
        "board_fen": session.board.fen(en_passant="fen"),
        "step": manager.step_payload(session),
        "learning_state": manager.learning_state(session),
    }


def _advance_response(manager, session, step) -> dict:
    if step is None:
        lesson = manager.lesson(session)
        from .lessons.requirements import INVALID, problems as requirement_problems
        wrong = requirement_problems(lesson)
        if wrong:  # never report success for a lesson that doesn't match what was asked
            req = lesson.requirements
            return {"completed": True, "status": INVALID, "completion_text": None, "problems": wrong[:3],
                    "requested": req.get("label"),
                    "message": f"This lesson didn't match your request ({req.get('label', 'the requested material')}), "
                               "so it isn't counted as completed. Ask for it again and I'll build verified positions."}
        return {"completed": True, "completion_text": lesson.completion_text,
                **manager.completion_summary(session)}
    return {"completed": False, "step": step}


@app.post("/api/sessions/{session_id}/advance/prepare")
def prepare_advance_session(session_id: str) -> dict:
    manager = get_manager()
    return manager.prepare_advance(manager.get(session_id))


@app.post("/api/sessions/{session_id}/advance/confirm")
def confirm_advance_session(session_id: str, body: AdvanceConfirmRequest) -> dict:
    manager = get_manager()
    session = manager.get(session_id)
    step = manager.confirm_advance(session, body.source_index, body.source_fen)
    return _advance_response(manager, session, step)


@app.post("/api/sessions/{session_id}/advance")
def advance_session(session_id: str) -> dict:
    raise ExerciseConflict("Lesson steps now require /advance/prepare and /advance/confirm after board verification.")


@app.post("/api/sessions/{session_id}/move")
def submit_move(session_id: str, body: MoveRequest) -> dict:
    return get_manager().apply_move(get_manager().get(session_id), body.uci, expected_fen=body.expected_fen)


@app.post("/api/sessions/{session_id}/hint")
def request_hint(session_id: str) -> dict:
    return get_manager().hint(get_manager().get(session_id))


@app.post("/api/sessions/{session_id}/reveal/prepare")
def prepare_reveal_solution(session_id: str) -> dict:
    return get_manager().reveal_preview(get_manager().get(session_id))


@app.post("/api/sessions/{session_id}/reveal/confirm")
def confirm_reveal_solution(session_id: str, body: RevealConfirmRequest) -> dict:
    manager = get_manager()
    return manager.confirm_reveal(manager.get(session_id), expected_fen=body.expected_fen)


@app.post("/api/sessions/{session_id}/reveal")
def reveal_solution(session_id: str) -> dict:
    # Backwards-compatible preview alias. Unlocking is intentionally only possible through the
    # position-checked /reveal/confirm endpoint after the client has verified its board setup.
    manager = get_manager()
    return manager.reveal_preview(manager.get(session_id))


def _ndjson(events) -> StreamingResponse:
    def lines():
        for event in events:
            yield json.dumps(event) + "\n"
    # X-Accel-Buffering: stop proxies from holding the stream back.
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/sessions/{session_id}/explain")
def explain_stream(session_id: str) -> StreamingResponse:
    """Stream the AI explanation of the latest graded move (one JSON event per line)."""
    manager = get_manager()
    return _ndjson(manager.explain_stream(manager.get(session_id)))


@app.post("/api/sessions/{session_id}/example/explain")
def explain_example(session_id: str) -> StreamingResponse:
    """Stream an explanation of the current verified library example (facts → Qwen)."""
    manager = get_manager()
    return _ndjson(manager.explain_example_stream(manager.get(session_id)))


@app.post("/api/sessions/{session_id}/chat/stream")
def chat_stream(session_id: str, body: ChatRequest) -> StreamingResponse:
    manager = get_manager()
    return _ndjson(manager.chat_stream(manager.get(session_id), body.message, body.intent))


@app.post("/api/sessions/{session_id}/chat")
def session_chat(session_id: str, body: ChatRequest) -> dict:
    manager = get_manager()
    return manager.chat(manager.get(session_id), body.message, body.intent)


# --- game analysis (Chess.com games) ----------------------------------------

from .game_api import router as games_router  # noqa: E402

app.include_router(games_router)

from .learner_api import router as profile_router  # noqa: E402

app.include_router(profile_router)

from .tts_api import router as tts_router  # noqa: E402

app.include_router(tts_router)

from .training_api import router as training_router  # noqa: E402

app.include_router(training_router)  # before the puzzle router: /api/puzzles/training/...

from .puzzle_api import router as puzzle_router  # noqa: E402

app.include_router(puzzle_router)


# --- frontend -------------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
