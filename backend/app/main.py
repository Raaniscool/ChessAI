"""FastAPI application: lesson-session API + web UI.

All board state lives server-side (session.py). The frontend sends moves and
receives validated step payloads — the AI never bypasses validation.
"""
from __future__ import annotations

from pathlib import Path

import json
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chess_system
from .chess_system import ChessError
from .config import get_settings
from .engine import EngineUnavailable
from .lessons import LessonNotFound, get_library
from .planner import PlanError, get_catalog, plan_for_goal
from .planner.store import delete_record, register_record, save_record
from .session import SessionError, get_manager
from .teacher import get_teacher

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

def _warm_up_qwen() -> None:
    """Load the model in the background so the learner's first reply isn't the slowest."""
    settings = get_settings()
    if not (settings.qwen_configured() and settings.qwen_warmup):
        return

    def run():
        from .teacher import QwenTeacher
        ok = QwenTeacher(settings).warm_up()
        logging.getLogger("chessai").info("Qwen warm-up %s", "done" if ok else "failed")

    threading.Thread(target=run, name="qwen-warmup", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _warm_up_qwen()
    yield


app = FastAPI(title="AI Chess Tutor", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def revalidate_frontend(request: Request, call_next):
    """Make the browser re-check the page files on every load.

    Without this a browser may keep a cached index.html from an older version while
    fetching a newer app.js (or the other way round), and the page breaks with errors
    like "Cannot read properties of null". Static files carry an ETag, so the re-check
    is a cheap 304 when nothing changed."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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


@app.exception_handler(PlanError)
async def plan_error_handler(request: Request, exc: PlanError):
    return JSONResponse(status_code=422, content={"error": str(exc), "suggestions": exc.suggestions})


@app.exception_handler(EngineUnavailable)
async def engine_unavailable_handler(request: Request, exc: EngineUnavailable):
    return JSONResponse(status_code=503, content={"error": str(exc)})


# --- request models -------------------------------------------------------

class MoveRequest(BaseModel):
    uci: str


class ChatRequest(BaseModel):
    message: str


class PlanRequest(BaseModel):
    goal: str
    # Try the verified Knowledge Library first (the UI always does). Off = catalog planner only.
    library: bool = False
    level: str | None = None  # beginner | intermediate | advanced (learner skill, optional)


class IntentRequest(BaseModel):
    message: str


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
    completed = get_manager().completed_lessons
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
    record = plan_for_goal(body.goal, library_first=body.library, level=body.level)
    register_record(get_library(), record)
    save_record(record)
    return {
        "plan": record["plan"],
        "course_id": record["course"]["id"],
        "first_lesson_id": record["lessons"][0]["id"],
        "source": "knowledge" if record["plan"].get("planner") == "knowledge" else "planner",
    }


@app.post("/api/knowledge/intent")
def knowledge_intent(body: IntentRequest) -> dict:
    """Is a chat message ("Show me checkmates") a request for a lesson the library can give?"""
    from .knowledge.library import get_knowledge
    from .knowledge.retrieval import lesson_request

    concepts = lesson_request(get_knowledge(), body.message[:300])
    return {"lesson_request": bool(concepts), "concepts": concepts}


@app.get("/api/plans")
def list_plans() -> dict:
    plans = [c.meta.get("plan", {}) for c in get_library().courses() if c.kind == "plan"][::-1]
    plans.sort(key=lambda p: p.get("created", ""), reverse=True)
    return {"plans": plans}


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
    return {"completed_lessons": sorted(get_manager().completed_lessons)}


@app.post("/api/lessons/{lesson_id}/start")
def start_lesson(lesson_id: str) -> dict:
    session, step = get_manager().start(lesson_id)
    return {"session_id": session.id, "lesson_id": lesson_id, "step": step}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    manager = get_manager()
    session = manager.get(session_id)
    return {
        "session_id": session.id,
        "lesson_id": session.lesson_id,
        "status": session.status,
        "step": manager.step_payload(session),
    }


@app.post("/api/sessions/{session_id}/advance")
def advance_session(session_id: str) -> dict:
    manager = get_manager()
    session = manager.get(session_id)
    step = manager.advance(session)
    if step is None:
        lesson = manager.lesson(session)
        return {"completed": True, "completion_text": lesson.completion_text}
    return {"completed": False, "step": step}


@app.post("/api/sessions/{session_id}/move")
def submit_move(session_id: str, body: MoveRequest) -> dict:
    return get_manager().apply_move(get_manager().get(session_id), body.uci)


@app.post("/api/sessions/{session_id}/hint")
def request_hint(session_id: str) -> dict:
    return get_manager().hint(get_manager().get(session_id))


@app.post("/api/sessions/{session_id}/reveal")
def reveal_solution(session_id: str) -> dict:
    return get_manager().reveal(get_manager().get(session_id))


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
    return _ndjson(manager.chat_stream(manager.get(session_id), body.message))


@app.post("/api/sessions/{session_id}/chat")
def session_chat(session_id: str, body: ChatRequest) -> dict:
    return get_manager().chat(get_manager().get(session_id), body.message)


# --- frontend -------------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
