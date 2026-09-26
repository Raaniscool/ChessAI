"""Lesson sessions — the server owns the authoritative board state.

The browser renders what it is told; every learner move is validated here
against the session's `chess.Board` before any engine or teacher is involved.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import chess

from . import chess_system
from .chess_system import ChessError
from .engine.classification import Classification
from .lessons import Lesson, LessonLibrary, get_library
from .lessons.schema import DemonstrateStep, ExerciseStep, TeachStep

_CATEGORY_RANK = [
    Classification.EXCELLENT,
    Classification.GOOD,
    Classification.INACCURATE,
    Classification.MISTAKE,
    Classification.BLUNDER,
]


class SessionError(Exception):
    """Base for session problems; carries an HTTP-ish status."""
    status = 400


class SessionNotFound(SessionError):
    status = 404


class ExerciseConflict(SessionError):
    """Action not allowed in the session's current state."""
    status = 409


@dataclass
class Session:
    id: str
    lesson_id: str
    step_index: int = 0
    board: chess.Board = field(default_factory=chess.Board)
    history: list[str] = field(default_factory=list)
    hint_index: int = 0
    exercise_accepted: bool = False
    status: str = "active"  # active | completed
    transcript: list[dict] = field(default_factory=list)


class SessionManager:
    def __init__(self, library: LessonLibrary | None = None):
        self.library = library or get_library()
        self._sessions: dict[str, Session] = {}
        self.completed_lessons: set[str] = set()

    # --- lifecycle ---

    def start(self, lesson_id: str) -> tuple[Session, dict]:
        lesson = self.library.lesson(lesson_id)
        session = Session(
            id=uuid.uuid4().hex[:12],
            lesson_id=lesson_id,
        )
        self._enter_step(session, lesson, 0)
        self._sessions[session.id] = session
        return session, self.step_payload(session)

    def get(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise SessionNotFound(f"Unknown session: {session_id}")
        return self._sessions[session_id]

    def lesson(self, session: Session) -> Lesson:
        return self.library.lesson(session.lesson_id)

    # --- board/step mechanics ---

    def _enter_step(self, session: Session, lesson: Lesson, index: int) -> None:
        session.step_index = index
        session.hint_index = 0
        session.exercise_accepted = False
        step = lesson.steps[index]
        if isinstance(step, TeachStep):
            if step.board and "fen" in step.board:
                session.board = chess_system.parse_fen(step.board["fen"])
        elif isinstance(step, DemonstrateStep):
            session.board = chess_system.parse_fen(step.fen)
        elif isinstance(step, ExerciseStep):
            session.board = chess_system.parse_fen(step.fen)

    def current_step(self, session: Session) -> object:
        return self.lesson(session).steps[session.step_index]

    def step_payload(self, session: Session) -> dict:
        lesson = self.lesson(session)
        step = lesson.steps[session.step_index]
        base = {
            "index": session.step_index,
            "type": step.type,
            "total_steps": len(lesson.steps),
            "lesson_title": lesson.title,
            "accepted": session.exercise_accepted,
        }
        if isinstance(step, TeachStep):
            board = chess_system.validate_board_spec(
                {**(step.board or {}), "fen": session.board.fen()}
            )
            return {**base, "text": step.text, "board": board}
        if isinstance(step, DemonstrateStep):
            after = chess_system.validate_board_spec(
                step.board or {}, board=chess_system.parse_fen(step.fen)
            )
            final = chess_system.parse_fen(step.fen)
            for uci in step.moves:
                final.push(chess_system.parse_move(final, uci))
            return {
                **base,
                "text": step.text,
                "start_fen": step.fen,
                "moves": step.moves,
                "comments": step.comments,
                "final_fen": final.fen(),
                "board_after": {**after, "fen": final.fen()},
            }
        assert isinstance(step, ExerciseStep)
        return {
            **base,
            "prompt": step.prompt,
            "side": step.side,
            "concepts": step.concepts or lesson.concepts,
            "hints_total": len(step.hints),
            "hints_used": session.hint_index,
            "board": {"fen": step.fen, "lock": False, "highlights": []},
        }

    def advance(self, session: Session) -> dict | None:
        """Move to the next step. Returns payload, or None when completed."""
        if session.status == "completed":
            raise ExerciseConflict("Lesson already completed")
        lesson = self.lesson(session)
        step = lesson.steps[session.step_index]
        if isinstance(step, ExerciseStep) and not session.exercise_accepted:
            raise ExerciseConflict("Resolve the exercise before moving on")
        next_index = session.step_index + 1
        if next_index >= len(lesson.steps):
            session.status = "completed"
            self.completed_lessons.add(lesson.id)
            return None
        self._enter_step(session, lesson, next_index)
        return self.step_payload(session)

    # --- exercise interaction ---

    def apply_move(self, session: Session, uci: str) -> dict:
        from .engine import get_engine
        from .teacher import LessonContext, explain_or_fallback, get_teacher

        if session.status == "completed":
            raise ExerciseConflict("Lesson already completed")
        step = self.current_step(session)
        if not isinstance(step, ExerciseStep):
            raise ExerciseConflict("This step does not accept moves")

        before = session.board.copy()
        move = chess_system.parse_move(before, uci)  # raises ChessError if illegal
        san = before.san(move)

        engine = get_engine()  # may raise EngineUnavailable -> 503
        feedback = engine.evaluate_move(before, move)

        accepted = self._is_accepted(step, san, feedback)
        push = accepted or step.advance_on == "any_legal"
        if push:
            session.board.push(move)
            session.history.append(move.uci())
        if accepted:
            session.exercise_accepted = True

        lesson = self.lesson(session)
        context = LessonContext(
            course_title=self.library.course(lesson.course_id).title,
            lesson_title=lesson.title,
            concepts=step.concepts or lesson.concepts,
            exercise_prompt=step.prompt,
            accepted_moves=step.accepted_san,
        )
        teacher = get_teacher()
        explanation, teacher_used = explain_or_fallback(teacher, feedback, context)

        result = {
            "accepted": accepted,
            "feedback": feedback.as_dict(),
            "explanation": explanation,
            "teacher": teacher_used,
            "san": san,
        }
        if accepted:
            result["continue_text"] = step.continue_text
        else:
            # Not good enough: position stays put so the learner can try again.
            result["reset_fen"] = step.fen
        return result

    @staticmethod
    def _is_accepted(step: ExerciseStep, san: str, feedback) -> bool:
        if step.advance_on == "any_legal":
            return True
        if step.advance_on == "accepted_move":
            return san in (step.accepted_san or [])
        # min_classification
        try:
            played = Classification(feedback.category.value)
            required = Classification(step.min_category)
        except ValueError:
            return False
        return _CATEGORY_RANK.index(played) <= _CATEGORY_RANK.index(required)

    def hint(self, session: Session) -> dict:
        step = self.current_step(session)
        if not isinstance(step, ExerciseStep):
            raise ExerciseConflict("Hints are only available during exercises")
        if session.exercise_accepted:
            raise ExerciseConflict("Exercise already solved")
        if session.hint_index >= len(step.hints):
            return {"exhausted": True, "index": session.hint_index, "total": len(step.hints)}
        hint = step.hints[session.hint_index]
        session.hint_index += 1
        return {
            "hint": hint,
            "index": session.hint_index,
            "total": len(step.hints),
            "remaining": len(step.hints) - session.hint_index,
            "exhausted": session.hint_index >= len(step.hints),
        }

    def reveal(self, session: Session) -> dict:
        """Learner explicitly asks for the solution (never automatic)."""
        step = self.current_step(session)
        if not isinstance(step, ExerciseStep):
            raise ExerciseConflict("Nothing to reveal in this step")
        session.exercise_accepted = True
        return {"accepted_moves": step.accepted_san or [], "accepted": True}

    # --- chat ---

    def chat(self, session: Session, message: str) -> dict:
        from .teacher import LessonContext, chat_or_fallback, get_teacher

        if not message or not message.strip():
            raise ChessError("Empty message")
        lesson = self.lesson(session)
        context = LessonContext(
            course_title=self.library.course(lesson.course_id).title,
            lesson_title=lesson.title,
            concepts=lesson.concepts,
        )
        session.transcript.append({"role": "user", "content": message.strip()})
        teacher = get_teacher()
        reply, teacher_used = chat_or_fallback(teacher, message.strip(), context, session.transcript)
        session.transcript.append({"role": "assistant", "content": reply})
        return {"reply": reply, "teacher": teacher_used}


_manager: SessionManager | None = None


def get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


def set_manager(manager: SessionManager | None) -> None:
    global _manager
    _manager = manager
