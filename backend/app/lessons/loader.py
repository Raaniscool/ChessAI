"""Lesson/course loading and caching."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import get_settings
from .schema import Course, Lesson, LessonError, parse_course, parse_lesson


class LessonNotFound(LessonError):
    pass


class LessonLibrary:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else get_settings().lessons_dir
        self._courses: dict[str, Course] = {}
        self._lessons: dict[str, Lesson] = {}
        self._load()

    def _load(self) -> None:
        if not self.root.exists():
            raise LessonError(f"Lessons directory not found: {self.root}")
        for course_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            course_file = course_dir / "course.json"
            if not course_file.exists():
                continue
            with open(course_file, encoding="utf-8") as fh:
                course = parse_course(json.load(fh))
            for plan in course.lessons:
                if plan.status != "available" or not plan.file:
                    continue
                lesson_file = course_dir / plan.file
                if not lesson_file.exists():
                    raise LessonError(f"Lesson file missing: {lesson_file}")
                with open(lesson_file, encoding="utf-8") as fh:
                    lesson = parse_lesson(json.load(fh), course_id=course.id)
                if lesson.id != plan.id:
                    raise LessonError(
                        f"Lesson id mismatch: course lists {plan.id}, file says {lesson.id}"
                    )
                self._lessons[lesson.id] = lesson
            self._courses[course.id] = course

    def courses(self) -> list[Course]:
        return list(self._courses.values())

    def course(self, course_id: str) -> Course:
        if course_id not in self._courses:
            raise LessonNotFound(f"Unknown course: {course_id}")
        return self._courses[course_id]

    def lesson(self, lesson_id: str) -> Lesson:
        if lesson_id not in self._lessons:
            raise LessonNotFound(f"Unknown lesson: {lesson_id}")
        return self._lessons[lesson_id]

    def lesson_ids(self) -> list[str]:
        return list(self._lessons.keys())


_library: LessonLibrary | None = None


def get_library() -> LessonLibrary:
    global _library
    if _library is None:
        _library = LessonLibrary()
    return _library


def set_library(library: LessonLibrary | None) -> None:
    global _library
    _library = library
