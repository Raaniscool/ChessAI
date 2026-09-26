from .base import LessonContext, Teacher
from .fallback import FallbackTeacher
from .qwen import RESET, QwenTeacher, TeacherUnavailable

__all__ = [
    "FallbackTeacher", "LessonContext", "QwenTeacher", "Teacher", "TeacherUnavailable",
    "get_teacher", "stream_events",
]


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


def stream_events(make_messages, fallback_text, validate=None):
    """Stream a teacher reply as events for the browser (one JSON object per line).

    Events: {"type": "start", "teacher": ...}, {"type": "delta", "text": ...},
            {"type": "done", "teacher": ..., "text": <full reply>}.
    `make_messages()` builds the Qwen prompt; `fallback_text()` is the
    deterministic answer used when Qwen is off, fails, or says nothing.
    If Qwen dies mid-answer, the partial answer is kept and marked.
    `validate(text)` (optional) returns problems where the finished reply contradicts
    verified facts; a reply with problems is replaced by the fallback text.
    """
    teacher = get_teacher()
    if not isinstance(teacher, QwenTeacher):
        text = fallback_text()
        yield {"type": "start", "teacher": "fallback"}
        yield {"type": "delta", "text": text}
        yield {"type": "done", "teacher": "fallback", "text": text}
        return

    yield {"type": "start", "teacher": "qwen"}
    parts: list[str] = []
    try:
        for chunk in teacher.stream(make_messages()):
            if chunk == RESET:  # what was shown was reasoning after all: retract it
                parts.clear()
                yield {"type": "replace", "text": ""}
                continue
            parts.append(chunk)
            yield {"type": "delta", "text": chunk}
    except TeacherUnavailable:
        if parts:
            note = " …(the AI stopped responding)"
            parts.append(note)
            yield {"type": "delta", "text": note}
    text = "".join(parts).strip()
    if not text:  # Qwen unreachable or returned only reasoning: never leave the student empty-handed
        text = fallback_text()
        yield {"type": "replace", "text": text}
        yield {"type": "done", "teacher": "fallback", "text": text}
        return
    if validate is not None and validate(text):
        text = fallback_text()
        yield {"type": "replace", "text": text}
        yield {"type": "done", "teacher": "fallback", "text": text, "corrected": True}
        return
    yield {"type": "done", "teacher": "qwen", "text": text}
