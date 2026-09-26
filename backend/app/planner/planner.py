"""'I want to learn ___' → a personal, verified learning plan.

Flow:
  1. Match the goal against the curated catalog (deterministic, always works).
  2. If Qwen is configured, ask it to *organize* a plan: pick catalog topics,
     title/summary, and — only for openings missing from the catalog — propose
     a SAN move sequence.
  3. Qwen-proposed lines are replayed with python-chess (legality) and screened
     move by move with Stockfish; the line is cut before the first mistake. Lines
     that can't be verified are not turned into lessons.
  4. Topics become lessons via generator.py and are validated by the lesson schema.

Qwen organizes; python-chess and Stockfish decide what's true.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import chess

from ..chess_system import ChessError
from ..engine.classification import Classification
from .catalog import Catalog, Topic, get_catalog, validate_line
from .generator import opening_lessons, topic_lessons

log = logging.getLogger(__name__)

MAX_UNITS = 8
MIN_CUSTOM_PLIES = 6
MAX_CUSTOM_PLIES = 16
SCREEN_DEPTH = 10
MAX_GOAL_LENGTH = 300


class PlanError(ValueError):
    """The goal can't be turned into a plan; carries suggestions for the user."""

    def __init__(self, message: str, suggestions: list[str] | None = None):
        super().__init__(message)
        self.suggestions = suggestions or []


@dataclass
class QwenPlan:
    title: str = ""
    summary: str = ""
    topic_ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    new_openings: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- Qwen ----

PLANNER_SYSTEM = """You are the curriculum planner of a chess tutoring app. \
You organize learning plans; you do not teach. Reply with ONE JSON object and nothing else."""


def build_planner_messages(goal: str, catalog: Catalog) -> list[dict]:
    listing = "\n".join(
        f"- {t.id} | {t.title} | {t.category}{' (' + t.side + ')' if t.side else ''} | {t.level}"
        for t in catalog.topics.values()
    )
    user = f"""Student's request: "{goal}"

Verified topics available (id | title | category | level):
{listing}

Return JSON with exactly these keys:
{{"title": "short plan title",
  "summary": "1-2 sentences on what the plan covers",
  "topics": [{{"id": "<topic id from the list>", "reason": "why it is in the plan, one sentence"}}],
  "new_openings": [{{"name": "opening name", "side": "white or black",
                    "moves": "SAN moves from the starting position, e.g. e4 e5 Nf3 Nc6",
                    "reason": "one sentence"}}]}}

Rules:
- Use topic ids from the list only. Order them from foundational to advanced. At most 6.
- Only use "new_openings" when the student asks for a specific opening that is NOT in the list;
  give its main line (8-16 half-moves). Otherwise return an empty list.
- If nothing fits, return empty lists. Do not invent anything else."""
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": user},
    ]


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences / chatter)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON object in reply")


def _clean_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def parse_qwen_plan(data: dict, catalog: Catalog) -> QwenPlan:
    plan = QwenPlan(title=_clean_text(data.get("title"), 80),
                    summary=_clean_text(data.get("summary"), 400))
    for item in data.get("topics") or []:
        tid = item.get("id") if isinstance(item, dict) else item
        if isinstance(tid, str) and catalog.get(tid) and tid not in plan.topic_ids:
            plan.topic_ids.append(tid)
            if isinstance(item, dict):
                plan.reasons[tid] = _clean_text(item.get("reason"), 200)
    for item in (data.get("new_openings") or [])[:2]:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 60)
        side = item.get("side") if item.get("side") in ("white", "black") else None
        moves = item.get("moves")
        if isinstance(moves, list):
            moves = " ".join(str(m) for m in moves)
        if name and side and isinstance(moves, str) and moves.strip():
            plan.new_openings.append({
                "name": name, "side": side, "moves": moves,
                "reason": _clean_text(item.get("reason"), 200),
            })
    return plan


def ask_qwen(goal: str, catalog: Catalog) -> QwenPlan | None:
    """Returns None when Qwen isn't configured or its answer is unusable."""
    from ..config import get_settings
    from ..teacher.qwen import QwenTeacher, TeacherUnavailable

    settings = get_settings()
    if not settings.qwen_configured():
        return None
    try:
        reply = QwenTeacher(settings).complete(build_planner_messages(goal, catalog))
        return parse_qwen_plan(extract_json(reply), catalog)
    except (TeacherUnavailable, ValueError, TypeError, AttributeError) as exc:
        log.warning("Qwen planning failed, using catalog only: %s", exc)
        return None


# ------------------------------------------------------ line screening ----

SAN_TOKEN = re.compile(r"^(?:\d+\.(?:\.\.)?)?(.+?)[!?]*$")


def tokenize_moves(moves: str) -> list[str]:
    """'1. e4 e5 2.Nf3 Nc6' -> ['e4','e5','Nf3','Nc6'] (move numbers stripped)."""
    out = []
    for raw in moves.replace("\n", " ").split():
        if re.fullmatch(r"\d+\.(\.\.)?", raw) or raw in ("*", "1-0", "0-1", "1/2-1/2"):
            continue
        m = SAN_TOKEN.match(raw)
        if m and m.group(1):
            out.append(m.group(1).replace("0-0-0", "O-O-O").replace("0-0", "O-O"))
    return out


def screen_line(sans: list[str], engine) -> tuple[list[str], str]:
    """Legal prefix of `sans` whose every move Stockfish rates better than a mistake.

    Returns (verified_moves, note). An empty list means nothing could be verified.
    """
    board = chess.Board()
    verified: list[str] = []
    for i, san in enumerate(sans[:MAX_CUSTOM_PLIES]):
        try:
            move = board.parse_san(san)
        except ValueError:
            return verified, f"stopped at move {i + 1} ({san}): illegal"
        feedback = engine.evaluate_move(board, move, depth=SCREEN_DEPTH)
        if feedback.category in (Classification.MISTAKE, Classification.BLUNDER):
            return verified, f"stopped at move {i + 1} ({san}): Stockfish rates it a {feedback.category.value}"
        verified.append(board.san(move))
        board.push(move)
    return verified, "every move checked by Stockfish"


# --------------------------------------------------------------- plans ----

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30] or "plan"


def _suggestions(catalog: Catalog) -> list[str]:
    picks = ["Italian Game", "Sicilian Defense", "Forks", "Checkmate with king and queen",
             "Opening principles", "London System"]
    titles = {t.title for t in catalog.topics.values()}
    return [f"I want to learn {p}" for p in picks if p in titles]


def create_plan(goal: str, catalog: Catalog | None = None, use_qwen: bool = True,
                engine=None) -> dict:
    """Build a plan record: {"plan": {...}, "course": {...}, "lessons": [lesson dicts]}."""
    catalog = catalog or get_catalog()
    goal = " ".join((goal or "").split())[:MAX_GOAL_LENGTH]
    if len(goal) < 2:
        raise PlanError("Tell me what you'd like to learn.", _suggestions(catalog))

    matched = catalog.search(goal)
    qwen = ask_qwen(goal, catalog) if use_qwen else None

    # Topic selection: Qwen's ordering first, then any direct match it missed.
    chosen_ids: list[str] = list(qwen.topic_ids) if qwen else []
    for t in matched:
        if t.id not in chosen_ids:
            chosen_ids.append(t.id)
    chosen = [catalog.topics[tid] for tid in chosen_ids]
    ordered = catalog.with_prerequisites(chosen, limit=MAX_UNITS)

    plan_id = uuid.uuid4().hex[:8]
    units: list[dict] = []
    lessons: list[dict] = []
    skipped: list[dict] = []

    def add_unit(title, category, reason, verified_by, new_lessons, topic_id=None):
        units.append({
            "topic_id": topic_id,
            "title": title,
            "category": category,
            "reason": reason,
            "verified_by": verified_by,
            "lesson_ids": [lesson["id"] for lesson in new_lessons],
        })
        lessons.extend(new_lessons)

    for n, (topic, required_by) in enumerate(ordered, start=1):
        reason = (qwen.reasons.get(topic.id) if qwen else "") or (
            f"Foundation for {required_by}." if required_by else _default_reason(topic)
        )
        add_unit(topic.title, topic.category, reason, "catalog + Stockfish",
                 topic_lessons(f"plan_{plan_id}_{n:02d}", topic), topic.id)

    # Qwen-proposed openings that aren't in the catalog: verify before teaching.
    known_titles = {t.title.lower() for t in catalog.topics.values()}
    for extra in (qwen.new_openings if qwen else []):
        if len(units) >= MAX_UNITS:
            break
        if extra["name"].lower() in known_titles:
            continue
        unit = _verified_custom_opening(plan_id, len(units) + 1, extra, engine)
        if isinstance(unit, str):
            skipped.append({"title": extra["name"], "reason": unit})
            continue
        add_unit(extra["name"], "opening", extra["reason"] or "Requested opening.",
                 "Stockfish (checked move by move)", unit)

    if not units:
        hint = "" if use_qwen and qwen is not None else (
            " With Qwen connected I can also build lessons for openings that aren't in my library."
        )
        raise PlanError(
            f"I don't have verified lessons for “{goal}” yet.{hint}", _suggestions(catalog)
        )

    title = (qwen.title if qwen and qwen.title else "") or _default_title(goal, ordered)
    summary = (qwen.summary if qwen and qwen.summary else "") or _default_summary(units)
    plan = {
        "id": plan_id,
        "goal": goal,
        "title": title,
        "summary": summary,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner": "qwen" if qwen else "catalog",
        "units": units,
        "skipped": skipped,
    }
    course = {
        "id": f"plan_{plan_id}",
        "title": title,
        "description": summary,
        "kind": "plan",
        "lessons": [{"id": lesson["id"], "title": lesson["title"]} for lesson in lessons],
    }
    return {"plan": plan, "course": course, "lessons": lessons}


def _verified_custom_opening(plan_id: str, n: int, extra: dict, engine) -> list[dict] | str:
    sans = tokenize_moves(extra["moves"])
    try:
        validate_line(sans[:1])
    except ChessError:
        return "the suggested moves aren't legal from the starting position"
    if engine is None:
        try:
            from ..engine import get_engine
            engine = get_engine()
        except Exception as exc:  # EngineUnavailable or spawn failure
            return f"Stockfish isn't available to verify the line ({exc})"
    verified, note = screen_line(sans, engine)
    if len(verified) < MIN_CUSTOM_PLIES:
        return f"couldn't verify enough of the line ({note})"
    return opening_lessons(
        f"plan_{plan_id}_{n:02d}", extra["name"], extra["side"], verified,
        summary=f"The {extra['name']}, playing as {extra['side']}.",
        ideas=[],
        verified_note=f"This line was suggested by the AI planner and checked by Stockfish ({note}).",
    )


def _default_reason(topic: Topic) -> str:
    return {
        "opening": f"Learn the moves and ideas of the {topic.title}.",
        "tactic": f"Practice spotting {topic.title.lower()} in real positions.",
        "endgame": "Convert winning positions with the right technique.",
        "strategy": "Build the habits every strong player relies on.",
    }.get(topic.category, topic.summary)


def _default_title(goal: str, ordered: list[tuple[Topic, str | None]]) -> str:
    main = [t for t, required_by in ordered if required_by is None]
    if len(main) == 1:
        return f"Learn: {main[0].title}"
    cleaned = re.sub(r"^(i\s+(want|would like|'d like|wanna)\s+(to\s+)?(learn|study|improve)\s*(about)?\s*|teach me\s*(about)?\s*|learn\s+)",
                     "", goal, flags=re.I).strip(" .!?")
    return f"Plan: {cleaned[:60] or goal[:60]}"


def _default_summary(units: list[dict]) -> str:
    names = ", ".join(u["title"] for u in units)
    return f"{len(units)} unit{'s' if len(units) != 1 else ''}: {names}. Every move you're asked to find has been checked by Stockfish."
