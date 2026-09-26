"""FastAPI application: lesson-session API + web UI.

All board state lives server-side (session.py). The frontend sends moves and
receives validated step payloads — the AI never bypasses validation.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chess_system
from .chess_system import ChessError
from .config import get_settings
from .engine import EngineUnavailable
from .lessons import LessonNotFound, get_library
from .planner import PlanError, create_plan, get_catalog
from .planner.store import delete_record, register_record, save_record
from .session import SessionError, get_manager
from .teacher import get_teacher

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="AI Chess Tutor", version="0.1.0")


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
        "lessons": len(library.lesson_ids()),
    }


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
    plans = [c for c in out if c["kind"] == "plan"]
    plans.sort(key=lambda c: c["plan"].get("created", ""), reverse=True)
    return {"courses": plans + [c for c in out if c["kind"] != "plan"]}


# --- learning plans -------------------------------------------------------

@app.get("/api/topics")
def topics() -> dict:
    """The verified topic catalog that plans are built from."""
    return {"topics": [t.as_summary() for t in get_catalog().topics.values()]}


@app.post("/api/plans")
def make_plan(body: PlanRequest) -> dict:
    """'I want to learn ___' → a personal course of verified lessons."""
    record = create_plan(body.goal)
    register_record(get_library(), record)
    save_record(record)
    return {
        "plan": record["plan"],
        "course_id": record["course"]["id"],
        "first_lesson_id": record["lessons"][0]["id"],
    }


@app.get("/api/plans")
def list_plans() -> dict:
    plans = [c.meta.get("plan", {}) for c in get_library().courses() if c.kind == "plan"]
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


@app.post("/api/sessions/{session_id}/chat")
def session_chat(session_id: str, body: ChatRequest) -> dict:
    return get_manager().chat(get_manager().get(session_id), body.message)


# --- frontend -------------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
