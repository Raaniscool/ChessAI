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
PLANNER_MAX_TOKENS = 800  # a plan is a small JSON object; explanations use QWEN_MAX_TOKENS
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
    # Compact (id | title [side]): every token here is prompt-reading time on a CPU.
    listing = "\n".join(
        f"- {t.id} | {t.title}{' (' + t.side + ')' if t.side else ''}"
        for t in catalog.topics.values()
    )
    user = f"""Student's request: "{goal}"

Verified topics available (id | title):
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
        reply = QwenTeacher(settings).complete(build_planner_messages(goal, catalog),
                                              max_tokens=PLANNER_MAX_TOKENS)
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
    repertoire = catalog.repertoire(goal)
    if not (repertoire and repertoire.openings and matched == repertoire.openings):
        repertoire = None  # a named topic ("the Sicilian against e4") wins over the move
    qwen = ask_qwen(goal, catalog) if use_qwen and _planner_wants_qwen(matched) else None

    # A plan only contains what the student asked for. Qwen may order the matched
    # topics, but topics it adds on its own are "related" suggestions, never units:
    # a small model asked about an unknown subject (e.g. "smothered mate") happily
    # picks forks and pins, which is a plan for something else.
    matched_ids = {t.id for t in matched}
    chosen_ids = [tid for tid in (qwen.topic_ids if qwen else []) if tid in matched_ids]
    chosen_ids += [t.id for t in matched if t.id not in chosen_ids]
    related = [catalog.topics[tid] for tid in (qwen.topic_ids if qwen else []) if tid not in matched_ids]
    chosen = [catalog.topics[tid] for tid in chosen_ids]
    if repertoire:
        # "What do I play as Black against e4?": one main opening to learn properly, the other
        # verified answers are offered, not piled into the plan.
        chosen, others = chosen[:1], chosen[1:]
        related = others + [t for t in related if t not in others]
    # Only what was asked for becomes a unit. Prerequisites ("Opening principles" before the
    # Sicilian) are suggested next to the plan, never added to it.
    ordered = [(t, None) for t in chosen][:MAX_UNITS]
    chosen_set = {t.id for t in chosen}
    prerequisites = [{"topic_id": t.id, "title": t.title, "for": required_by}
                     for t, required_by in catalog.with_prerequisites(chosen, limit=50)
                     if required_by and t.id not in chosen_set]

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

    for n, (topic, _) in enumerate(ordered, start=1):
        reason = (qwen.reasons.get(topic.id) if qwen else "") or (
            f"Your answer {repertoire.description}: {topic.summary}" if repertoire else _default_reason(topic))
        add_unit(topic.title, topic.category, reason, "catalog + Stockfish",
                 topic_lessons(f"plan_{plan_id}_{n:02d}", topic), topic.id)

    # Qwen-proposed openings that aren't in the catalog: verify before teaching.
    known_titles = {t.title.lower() for t in catalog.topics.values()}
    # New openings only for opening requests: asked about a checkmate pattern, a small model
    # likes to add an opening or two, which is a plan for something else.
    wants_openings = _asks_for_openings(goal, matched, qwen)
    for extra in (qwen.new_openings if qwen and wants_openings else []):
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
        from ..config import get_settings
        subject = _subject(goal)
        if related:
            message = (f"I don't have verified lessons on “{subject}” yet, and I won't give you a plan "
                       "for something else. Related topics I can teach:")
        else:
            message = f"I don't have verified lessons on “{subject}” yet. Here's what I can teach:"
        if not get_settings().qwen_configured():
            message += " (With Qwen connected I can also build lessons for openings that aren't in my library.)"
        suggestions = [f"I want to learn {t.title}" for t in related[:4]]
        suggestions += [s for s in _suggestions(catalog) if s not in suggestions]
        raise PlanError(message, suggestions[:6])

    if repertoire:
        title = f"Plan: play {repertoire.description}"
        summary = _repertoire_summary(repertoire, chosen[0], units)
    else:
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
        "related": [t.title for t in related if t.id not in {u["topic_id"] for u in units}][:4],
        "prerequisites": prerequisites[:4],
    }
    course = {
        "id": f"plan_{plan_id}",
        "title": title,
        "description": summary,
        "kind": "plan",
        "lessons": [{"id": lesson["id"], "title": lesson["title"]} for lesson in lessons],
    }
    return {"plan": plan, "course": course, "lessons": lessons}


def plan_for_goal(goal: str, library_first: bool = True, level: str | None = None,
                  catalog: Catalog | None = None, use_qwen: bool = True, engine=None, *,
                  clarify: bool = False, answers: dict | None = None, reclarify: bool = False,
                  memory=None, personal: dict | None = None, learner: str | None = None,
                  teacher=None, profile=None) -> dict:
    """Understand the request, then library first, then the existing planner.

    0. With `clarify` (the API): the request is structured into an intent
       (planner.intent). A request with several materially different readings raises
       ClarificationNeeded — the learner chooses; remembered answers are reused. A request
       with structure the other planners can't express (material, a flipped side,
       exclusions, a clarified reading) or a weakness to train goes to the custom-plan
       pipeline: propose → validate (python-chess, Stockfish, plan checks) → present.
    1. Verified Knowledge Library examples for the requested concept (demonstration ->
       guided example -> practice), when the library has something suitable;
    2. otherwise exactly the old behaviour: catalog topics, then Qwen-organized plans
       whose new lines are screened by Stockfish;
    3. never a dead end for something recognisable: a custom plan for a name only the
       opening database knows, then the missing-library fallback.

    `profile` (the learner model) personalizes library lessons and sets the level the
    other planners use when the request doesn't say; a blank profile changes nothing.
    """
    from ..learner.personalize import personalized
    cleaned = " ".join((goal or "").split())[:MAX_GOAL_LENGTH]
    intent = None
    if clarify and library_first and len(cleaned) >= 2:
        from .intent import IntentMemory, understand
        intent = understand(cleaned, answers=answers, memory=memory if memory is not None else IntentMemory(),
                            reclarify=reclarify)
        level = intent.level or level
        cleaned = " ".join(intent.goal.split())[:MAX_GOAL_LENGTH] or cleaned
        if personal and not intent.components and personal.get("concept"):
            from .intent import Component
            intent.components = [Component("concept", personal["concept"], personal.get("title") or personal["concept"])]
        single = intent.components[0] if len(intent.components) == 1 else None
        if single is not None and single.kind in ("topic", "concept", "glossary") and not intent.exclude \
                and not personal:
            # a clarified plain subject ("Philidor" → the Philidor position): the normal planners
            # teach one named subject best (library examples, catalog lessons, definition fallback)
            cleaned = single.label
        elif intent.structured:
            record = _custom(intent, level or _profile_level(profile), catalog, use_qwen, engine, personal,
                             learner, teacher)
            if record is not None:
                return record
    record = _plan_without_intent(cleaned, library_first, level, catalog, use_qwen, engine, intent, teacher,
                                  profile=profile if personalized(profile) else None)
    if intent is not None and (intent.clarified or intent.structured):
        record["plan"]["intent"] = intent.as_dict()
    return record


def _custom(intent, level, catalog, use_qwen, engine, personal, learner, teacher) -> dict | None:
    from ..engine import get_engine
    from .custom import build_custom_plan

    try:
        result = build_custom_plan(intent, level=level, catalog=catalog, engine=engine, engine_factory=get_engine,
                                   use_qwen=use_qwen, teacher=teacher, personal=personal, learner=learner)
    except Exception as exc:  # the custom pipeline must never break planning
        log.warning("custom plan pipeline failed: %s", exc)
        return None
    if result.record is None:
        log.info("no verified custom plan for %r: %s", intent.goal, result.attempts)
    return result.record


def _profile_level(profile) -> str | None:
    from ..learner.personalize import personalized
    return profile.level if personalized(profile) else None


def _plan_without_intent(cleaned: str, library_first: bool, level, catalog, use_qwen: bool, engine, intent,
                         teacher=None, profile=None) -> dict:
    if library_first and len(cleaned) >= 2:
        from .knowledge_lessons import create_knowledge_plan
        try:
            record = create_knowledge_plan(cleaned, level=level, catalog=catalog, profile=profile)
        except Exception as exc:  # the library must never break planning
            log.warning("Knowledge Library retrieval failed, using the catalog planner: %s", exc)
            record = None
        if record:
            return record
    # A named idea from the glossary that the catalog only partly matches ("stalemate tricks"
    # vs the king-and-queen topic's "stalemate" alias) is not a request for that topic.
    if library_first and _glossary_outranks_catalog(cleaned, catalog or get_catalog()):
        record = _fallback(cleaned, level or _profile_level(profile), catalog, use_qwen, engine,
                           clarify=intent is not None, teacher=teacher)
        if record is not None:
            return record
    # the same cleaned, length-capped goal (a pasted essay used to become the plan title)
    try:
        return create_plan(cleaned, catalog=catalog, use_qwen=use_qwen, engine=engine)
    except PlanError as err:
        if not library_first or len(cleaned) < 2:
            raise
        # A name only the opening database knows ("the Dutch"): a custom plan from its line,
        # screened by Stockfish before it is shown.
        suggested = [c for c in (getattr(intent, "suggested", None) or []) if c.kind == "opening_db"]
        if intent is not None and suggested:
            from dataclasses import replace
            record = _custom(replace(intent, components=suggested[:1], interpretation=suggested[0].label), level,
                             catalog, use_qwen, engine, None, None, teacher)
            if record is not None:
                return record
        # Nothing verified in the library or the catalog: never a dead end for something we
        # recognise (generated + engine-checked positions, a broader idea, a labelled definition).
        record = _fallback(cleaned, level or _profile_level(profile), catalog, use_qwen, engine,
                           clarify=intent is not None, teacher=teacher)
        if record is None:
            raise  # nothing recognisable in the request: keep the answer + suggestions
        return record


def _glossary_outranks_catalog(goal: str, catalog: Catalog) -> bool:
    from ..knowledge.glossary import get_glossary

    found = get_glossary().match_size(goal)
    return bool(found) and found[1] > catalog.best_alias_size(goal)


def _fallback(goal: str, level, catalog, use_qwen: bool, engine, clarify: bool = False, teacher=None) -> dict | None:
    from .intent import ClarificationNeeded
    from .missing import fallback_plan

    from ..engine import get_engine

    try:  # the engine is started only if positions are actually generated
        return fallback_plan(goal, catalog=catalog, engine=engine, engine_factory=get_engine, level=level,
                             use_qwen=use_qwen, clarify=clarify, teacher=teacher)
    except ClarificationNeeded:
        raise  # Qwen saw several readings: the learner chooses
    except Exception as exc:  # the fallback must never turn a clean "not available" into a crash
        log.warning("missing-library fallback failed: %s", exc)
        return None


def _planner_wants_qwen(matched: list[Topic]) -> bool:
    """QWEN_PLANNER: auto (only when the catalog has no answer — instant plans for
    known topics), always, or never."""
    from ..config import get_settings
    mode = get_settings().qwen_planner
    if mode == "never":
        return False
    if mode == "always":
        return True
    return not matched


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


_OPENING_WORDS = {"opening", "openings", "defense", "defence", "defenses", "defences", "gambit", "gambits",
                  "system", "variation", "repertoire", "attack", "game", "line", "lines", "against", "vs",
                  "versus", "white", "black"}


def _asks_for_openings(goal: str, matched: list[Topic], qwen) -> bool:
    """Is this a request for an opening (so new, Stockfish-screened opening lines may be added)?"""
    if any(t.category == "opening" for t in matched):
        return True
    words = set(re.findall(r"[a-z0-9]+", goal.lower().replace("'", "")))
    if words & _OPENING_WORDS:
        return True
    names = [re.findall(r"[a-z0-9]+", o["name"].lower()) for o in (qwen.new_openings if qwen else [])]
    return any(len(w) > 3 and w in words for name in names for w in name)


def _repertoire_summary(repertoire, main: Topic, units: list[dict]) -> str:
    level = {"beginner": "beginner-friendly", "intermediate": "a step up", "advanced": "advanced"}.get(main.level, "")
    text = (f"Your main answer {repertoire.description}: the {main.title}"
            + (f" ({level})" if level else "") + ". Learn its ideas first, then the moves, then play the whole "
            "line from memory. Every move has been checked by Stockfish.")
    others = [t.title for t in repertoire.openings if t.id != main.id]
    if others:
        text += f" Other good answers you can add later: {', '.join(others[:3])}."
    return text


def _default_reason(topic: Topic) -> str:
    return {
        "opening": f"Learn the moves and ideas of the {topic.title}.",
        "tactic": f"Practice spotting {topic.title.lower()} in real positions.",
        "endgame": "Convert winning positions with the right technique.",
        "strategy": "Build the habits every strong player relies on.",
    }.get(topic.category, topic.summary)


_REQUEST_PREFIX = re.compile(
    r"^(i\s+(really\s+)?(want|would like|'d like|wanna|need)\s+(to\s+)?(learn|study|practice|improve|master|get better at)"
    r"\s*(about|how to)?\s*|(can you\s+)?teach me\s*(about|how to)?\s*|learn\s+)", re.I)


def _subject(goal: str) -> str:
    """'I want to learn the smothered mate' -> 'the smothered mate' (for messages/titles)."""
    cleaned = _REQUEST_PREFIX.sub("", goal).strip(" .!?")
    return cleaned[:60] or goal[:60]


_CATEGORY_TITLE = {"opening": "Openings", "tactic": "Tactics", "endgame": "Endgames", "strategy": "Strategy"}


def _default_title(goal: str, ordered: list[tuple[Topic, str | None]]) -> str:
    main = [t for t, required_by in ordered if required_by is None]
    if len(main) == 1:
        return f"Learn: {main[0].title}"
    subject = _subject(goal)
    cats = {t.category for t in main}
    # "I keep losing in the endgame" / "help me get better at tactics": name the subject, don't echo
    if re.match(r"^(i|i'm|im|help|how|my|what|why)\b", subject, re.I) and len(cats) == 1 and \
            next(iter(cats)) in _CATEGORY_TITLE:
        subject = _CATEGORY_TITLE[next(iter(cats))]
    subject = re.sub(r"\b(white|black)\b", lambda m: m.group(1).capitalize(), subject)
    return f"Plan: {subject[:1].upper()}{subject[1:]}"


def _default_summary(units: list[dict]) -> str:
    names = ", ".join(u["title"] for u in units)
    return f"{len(units)} unit{'s' if len(units) != 1 else ''}: {names}. Every move you're asked to find has been checked by Stockfish."
