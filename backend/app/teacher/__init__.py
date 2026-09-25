from .base import LessonContext, Teacher
from .fallback import FallbackTeacher
from .qwen import QwenTeacher, TeacherUnavailable

__all__ = ["FallbackTeacher", "LessonContext", "QwenTeacher", "Teacher", "TeacherUnavailable", "get_teacher"]


def get_teacher():
    """Pick the configured teacher; fall back deterministically when Qwen is
    not configured or unreachable. The app must remain usable either way."""
    from ..config import get_settings

    settings = get_settings()
    if settings.qwen_configured():
        try:
            teacher = QwenTeacher(settings)
            # Probe cheaply at startup? We probe lazily: first failing call
            # raises TeacherUnavailable which callers convert to fallback.
            return teacher
        except TeacherUnavailable:
            pass
    return FallbackTeacher()


def explain_or_fallback(teacher, feedback, context: LessonContext) -> tuple[str, str]:
    """Explain a move; if Qwen fails, degrade to the fallback teacher.

    Returns (explanation, teacher_name_used).
    """
    try:
        return teacher.explain_move(feedback, context), getattr(teacher, "name", "unknown")
    except TeacherUnavailable:
        fallback = FallbackTeacher()
        return fallback.explain_move(feedback, context), fallback.name


def chat_or_fallback(teacher, message: str, context: LessonContext, transcript: list[dict]) -> tuple[str, str]:
    try:
        return teacher.chat(message, context, transcript), getattr(teacher, "name", "unknown")
    except TeacherUnavailable:
        fallback = FallbackTeacher()
        return fallback.chat(message, context, transcript), fallback.name
