"""Persist plans as JSON (data/plans/<id>.json) and register them as courses."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..lessons.schema import Course, LessonError, LessonPlan, parse_lesson

log = logging.getLogger(__name__)


def plans_dir() -> Path:
    from ..config import get_settings
    return get_settings().data_dir / "plans"


def register_record(library, record: dict) -> None:
    """Validate a plan record's lessons and add the course to the library."""
    course_raw = record["course"]
    lessons = [parse_lesson(raw, course_id=course_raw["id"]) for raw in record["lessons"]]
    course = Course(
        id=course_raw["id"],
        title=course_raw["title"],
        description=course_raw.get("description", ""),
        lessons=[LessonPlan(id=les.id, title=les.title, status="available") for les in lessons],
        kind="plan",
        meta={"plan": record["plan"]},
    )
    library.register_course(course, lessons)


def save_record(record: dict, directory: Path | None = None) -> Path:
    directory = directory or plans_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['plan']['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def delete_record(plan_id: str, directory: Path | None = None) -> None:
    path = (directory or plans_dir()) / f"{plan_id}.json"
    if path.exists():
        path.unlink()


def load_saved_plans(library, directory: Path | None = None) -> int:
    """Load every saved plan into the library. Broken files are skipped, not fatal."""
    directory = directory or plans_dir()
    if not directory.is_dir():
        return 0
    loaded = 0
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            register_record(library, json.loads(path.read_text(encoding="utf-8")))
            loaded += 1
        except (OSError, ValueError, KeyError, LessonError) as exc:
            log.warning("Skipping unreadable plan %s: %s", path.name, exc)
    return loaded
