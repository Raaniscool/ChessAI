"""Weaknesses found in the learner's games -> a personal training plan (ordinary lessons).

For each weakness (most important first):

  1. short explanation   what was found, in which games, and what the concept is
  2. library examples    verified Knowledge Library examples of the concept
                         (demonstration -> guided -> practice, same as any library lesson)
  3. your own positions  the positions from the learner's games, before the mistake:
                         "find a better move" — accepted moves are Stockfish's best move
                         and the alternatives it rated as good during the analysis
  4. follow-up practice  more verified library practice + catalog lessons for the topic

The plan is saved like every other plan (DATA_DIR/plans, private). Positions from
the learner's games live only inside this personal plan: they are never written
to the Knowledge Library and never presented as library examples.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import chess

from ..knowledge.retrieval import RetrievalRequest, retrieve
from ..lessons.schema import parse_lesson
from ..planner.catalog import get_catalog
from ..planner.generator import move_hints, topic_lessons
from ..planner.knowledge_lessons import CATEGORY, _distinct_titles, example_steps
from . import review

MAX_WEAKNESSES = 3
MAX_OWN_POSITIONS = 3
LIBRARY_EXAMPLES = 3
FOLLOW_UP_EXAMPLES = 2
OWN_LINE_PLIES = 4
PLANNER = "games"


class TrainingError(ValueError):
    pass


def _game_ref(e: dict) -> str:
    who = f" against {e['opponent']}" if e.get("opponent") else ""
    return f"move {e['move_number']}{who}"


def _intro(weakness: dict, library, total_games: int) -> str:
    refs = [_game_ref(e) for e in weakness["evidence"][:3]]
    games = weakness["game_count"]
    where = (f"in {games} of the {total_games} games I analyzed" if games > 1 else "in one of your games")
    text = f"{weakness['title']} — {where}"
    if refs:
        text += f" ({', '.join(refs)})"
    text += "."
    concept = library.concepts.get(weakness.get("concept") or "")
    if concept:
        text += f"\n\n{concept.name}: {concept.summary}"
    plan = []
    if weakness.get("library_examples"):
        plan.append("first some verified examples from the lesson library")
    plan.append("then positions from your own games")
    text += "\n\nThe plan: " + ", ".join(plan) + "."
    return text


def _highlights(moment: dict) -> list[dict]:
    out = []
    try:
        move = chess.Move.from_uci(moment["uci"])
        out += [{"square": chess.square_name(move.from_square), "color": "red"},
                {"square": chess.square_name(move.to_square), "color": "red"}]
    except (KeyError, ValueError):
        pass
    return out


def _moment_for(evidence: dict, moments: dict[str, dict]) -> dict | None:
    m = moments.get(evidence.get("moment_id") or "")
    if m is None or m.get("category") == "habit" or not m.get("best_move"):
        return None
    return m


def own_position_steps(moment: dict, number: int, total: int, concept_names: list[str]) -> list[dict]:
    """'You played X here — find a better move' from the learner's own game."""
    board = chess.Board(moment["fen_before"])
    accepted = []
    for san in [moment["best_move"]] + list(moment.get("alternatives") or []):
        try:
            accepted.append(board.san(board.parse_san(san)))
        except ValueError:
            continue
    accepted = list(dict.fromkeys(accepted))
    best = board.parse_san(accepted[0])
    src = moment.get("source") or {}
    who = f" against {src['opponent']}" if src.get("opponent") else ""
    side = "white" if board.turn else "black"
    prompt = (f"Your game{who}, move {moment['move_number']} ({number} of {total}). You played "
              f"{moment['san']} here — {review.headline(moment)[0].lower() + review.headline(moment)[1:]} "
              f"Find a better move.")
    hints = [review.tip(moment)] + move_hints(board, best)[:2]
    steps = [
        {"type": "teach", "text": f"A position from your own game{who}. It's {side.capitalize()} to move — "
                                  f"you played {moment['san']}.",
         "board": {"fen": moment["fen_before"], "highlights": _highlights(moment)}},
        {"type": "exercise", "fen": moment["fen_before"], "side": side, "prompt": prompt, "hints": hints,
         "advance_on": "accepted_move", "accepted": accepted, "concepts": concept_names,
         "continue_text": f"Yes — Stockfish's choice here is {accepted[0]}."
                          + (f" {', '.join(accepted[1:])} is just as good." if len(accepted) > 1 else "")},
    ]
    probe, line = board.copy(), []
    for san in (moment.get("best_line") or [])[:OWN_LINE_PLIES]:
        try:
            move = probe.parse_san(san)
        except ValueError:
            break
        line.append(move.uci())
        probe.push(move)
    if len(line) > 1:
        steps.append({"type": "demonstrate", "text": "How the engine's line continues from there.",
                      "fen": moment["fen_before"], "moves": line})
    steps.append({"type": "teach", "text": review.fallback_why(moment), "board": {"fen": probe.fen()}})
    return steps


def _lesson(lesson_id: str, title: str, steps: list[dict], completion: str, concepts: list[str],
            difficulty: str = "beginner", **extra) -> dict:
    lesson = {"id": lesson_id, "title": title, "description": steps[0].get("text", "").split("\n")[0][:200],
              "difficulty": difficulty, "concepts": concepts, "steps": steps, "completion": {"text": completion}}
    lesson.update(extra)
    parse_lesson(lesson, course_id="_generated")  # same validation as every other lesson
    return lesson


def create_training_plan(weaknesses: list[dict], moments: dict[str, dict], total_games: int, library,
                         usage=None, catalog=None, level: str | None = None, record_usage: bool = True) -> dict:
    """A plan record ({plan, course, lessons}) — registered and saved like any other plan."""
    if not weaknesses:
        raise TrainingError("choose at least one weakness to train")
    catalog = catalog or get_catalog()
    plan_id = uuid.uuid4().hex[:8]
    lessons: list[dict] = []
    units: list[dict] = []
    used_examples = []
    used_topics: set[str] = set()
    for n, weakness in enumerate(weaknesses[:MAX_WEAKNESSES], start=1):
        concept_id = weakness.get("concept")
        concept = library.concepts.get(concept_id or "")
        names = [concept.name] if concept else [weakness["title"]]
        subject = concept.name if concept else weakness["title"]
        prefix = f"plan_{plan_id}_{n:02d}"
        unit_lessons: list[dict] = []
        intro = {"type": "teach", "text": _intro(weakness, library, total_games)}
        own = [m for m in (_moment_for(e, moments) for e in weakness["evidence"]) if m]
        # distinct games first, most severe first (evidence is already sorted by severity)
        seen, picked = set(), []
        for m in own:
            if m["game_id"] not in seen:
                picked.append(m)
                seen.add(m["game_id"])
        picked += [m for m in own if m not in picked]
        picked = picked[:MAX_OWN_POSITIONS]
        if picked:
            intro["board"] = {"fen": picked[0]["fen_before"], "highlights": _highlights(picked[0])}

        retrieval = None
        if concept and library.count_for(concept_id):
            retrieval = retrieve(library, RetrievalRequest(concepts=[concept_id], level=level,
                                                           count=LIBRARY_EXAMPLES,
                                                           practice_count=FOLLOW_UP_EXAMPLES), usage)
        steps = [intro]
        if retrieval and retrieval.found:
            for k, (example, role) in enumerate(retrieval.sequence, start=1):
                steps += example_steps(example, role, k, len(retrieval.sequence), names)
            used_examples += retrieval.examples
        if steps == [intro]:
            intro["text"] += ("\n\nThe lesson library has no verified examples of this yet, so we'll work "
                              "with positions from your own games.")
        if len(steps) > 1 or not picked:
            unit_lessons.append(_lesson(f"{prefix}a", f"{subject}: learn the pattern", steps,
                                        f"You've seen {subject.lower()} in action.", names,
                                        origin={"type": PLANNER, "weakness": weakness["key"]}))
        if picked:
            own_steps = [] if unit_lessons else [intro]
            for k, m in enumerate(picked, start=1):
                own_steps += own_position_steps(m, k, len(picked), names)
            unit_lessons.append(_lesson(
                f"{prefix}b", f"{subject}: your own games", own_steps,
                "Those were your own positions — next time you'll spot it over the board.", names,
                personal=True, origin={"type": PLANNER, "weakness": weakness["key"],
                                       "moments": [m["id"] for m in picked]}))
        if retrieval and retrieval.practice:
            practice = []
            for k, example in enumerate(retrieval.practice, start=1):
                practice += example_steps(example, "practice", k, len(retrieval.practice), names)
            practice.insert(0, {"type": "teach", "text": f"Follow-up: {len(retrieval.practice)} more "
                                                          f"{subject.lower()} puzzles from the lesson library.",
                                "board": {"fen": retrieval.practice[0].start_fen}})
            unit_lessons.append(_lesson(f"{prefix}c", f"{subject}: follow-up practice", practice,
                                        f"Follow-up complete — keep an eye out for {subject.lower()}.", names,
                                        origin={"type": PLANNER, "weakness": weakness["key"]}))
        # One catalog lesson per weakness at most, and never the same topic twice in a plan.
        topic_id = next((t for t in weakness.get("topics", []) if catalog.get(t) and t not in used_topics), None)
        topic = catalog.topics[topic_id] if topic_id else None
        extra = []
        if topic is not None:
            used_topics.add(topic_id)
            extra = topic_lessons(f"plan_{plan_id}_{n:02d}t", topic)[:1]
            _distinct_titles(extra, lessons + unit_lessons, topic.title)
        units.append({
            "topic_id": topic.id if topic is not None and extra else None,
            "title": weakness["title"],
            "category": CATEGORY.get(concept.category if concept else "", "tactic"),
            "reason": weakness["description"],
            "verified_by": "Stockfish (your games) + Knowledge Library",
            "lesson_ids": [les["id"] for les in unit_lessons + extra],
            "concepts": [concept_id] if concept_id else [],
            "weakness": weakness["key"],
        })
        lessons += unit_lessons + extra
    if not lessons:
        raise TrainingError("nothing to train for these weaknesses yet")

    titles = [w["title"] for w in weaknesses[:MAX_WEAKNESSES]]
    game_ids = sorted({g for w in weaknesses[:MAX_WEAKNESSES] for g in w["games"]})
    title = "From your games: " + (titles[0] if len(titles) == 1 else " & ".join(titles[:2]))
    summary = (f"Built from {len(game_ids)} of your analyzed game{'s' if len(game_ids) != 1 else ''}: "
               + "; ".join(w["description"].rstrip(".") for w in weaknesses[:MAX_WEAKNESSES])
               + ". Every move is checked by the Stockfish chess engine.")
    plan = {
        "id": plan_id, "goal": "Train the weaknesses from my games", "title": title, "summary": summary,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "planner": PLANNER,
        "units": units, "skipped": [], "related": [],
        "personal": {"games": game_ids, "weaknesses": [w["key"] for w in weaknesses[:MAX_WEAKNESSES]]},
    }
    course = {"id": f"plan_{plan_id}", "title": title, "description": summary, "kind": "plan",
              "lessons": [{"id": les["id"], "title": les["title"]} for les in lessons]}
    if record_usage and usage is not None and used_examples:
        usage.record_used(used_examples)
    return {"plan": plan, "course": course, "lessons": lessons}
