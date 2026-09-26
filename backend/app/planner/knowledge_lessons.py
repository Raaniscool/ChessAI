"""Verified Knowledge Library examples -> lessons in the normal lesson schema.

The library decides WHAT is true (every example passed python-chess + Stockfish
verification); this module only arranges retrieved examples into the existing
step types, so they play through the same session engine as every other lesson:

    demonstration  demonstrate the whole line (with the verified move notes) -> explanation
    guided         demonstrate up to the key moment -> exercise: find the key move -> rest -> explanation
    practice       show the setup -> exercise for every learner move (replies demonstrated) -> explanation

Each step carries ``example: <id>`` so the session can hand the verified facts of
that entry to the AI teacher. Nothing here asks an LLM for chess content.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import chess

from ..lessons.schema import parse_lesson
from .generator import move_hints, topic_lessons

KNOWLEDGE_VERIFIED_BY = "Knowledge Library (python-chess + Stockfish verified)"
MAX_TOPIC_UNITS = 2
PRACTICE_EXAMPLES = 3

# Library category -> planner category (plan icons, reasons).
CATEGORY = {"openings": "opening", "tactics": "tactic", "checkmates": "tactic",
            "endgames": "endgame", "basics": "strategy", "mistakes": "strategy"}
ROLE_TEXT = {
    "demonstration": "Watch this one.",
    "guided": "Watch how it starts, then you find the key move.",
    "practice": "Your turn: solve this one on your own.",
}


def _side(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def _credit(example) -> str:
    src = example.source or {}
    kind = src.get("source_type")
    if kind == "lichess_puzzle" and src.get("source_url"):
        return f"From a real game: Lichess puzzle {src['source_url']} (public domain)."
    if kind == "lichess_openings":
        return "Opening line from the Lichess opening database (public domain)."
    return ""


def _header(example, number: int, total: int, role: str) -> str:
    text = f"Example {number} of {total}: {example.title}."
    if example.description:
        text += f" {example.description}"
    return f"{text}\n\n{ROLE_TEXT[role]}"


def _explanation(example) -> str:
    parts = [example.explanation or example.description]
    credit = _credit(example)
    if credit:
        parts.append(credit)
    parts.append("Every move here was checked with python-chess and Stockfish.")
    return "\n\n".join(p for p in parts if p)


def _board(fen: str, example) -> dict:
    return {"fen": fen, "highlights": list(example.highlights)}


def _alternatives(example, ply: int) -> list[str]:
    """Other moves Stockfish verified as good at this learner move."""
    eng = example.verification.get("engine") or {}
    if ply == example.key_ply:
        key = eng.get("key_move") or {}
        return list(key.get("alternatives") or [])
    label = example.labels[ply]
    for later in eng.get("learner_moves") or []:
        if later.get("label") == label:
            return list(later.get("alternatives") or [])
    return []


def _accepted(example, board: chess.Board, ply: int, final: bool) -> list[str]:
    """The line's move; the curator's accepted moves at the key moment; and, on the
    learner's LAST move (where no continuation depends on it), Stockfish's alternatives."""
    moves = [example.moves[ply]]
    if ply == example.key_ply:
        moves += example.accepted
    if final:
        moves += _alternatives(example, ply)
    out = []
    for san in moves:
        try:
            out.append(board.san(board.parse_san(san)))
        except ValueError:
            continue
    return list(dict.fromkeys(out))


def _exercise(example, rep, ply: int, prompt: str, hints: list[str], continue_text: str,
              concepts: list[str], final: bool) -> dict:
    board = rep.boards[ply]
    move = board.parse_san(example.moves[ply])
    concrete = move_hints(board, move)[1:2]  # "The rook on d1." — never the answer itself
    hints = [h for h in hints if h] + [h for h in concrete if h not in hints]
    return {
        "type": "exercise",
        "fen": board.fen(),
        "side": _side(board),
        "prompt": prompt,
        "hints": hints or move_hints(board, move),
        "advance_on": "accepted_move",
        "accepted": _accepted(example, board, ply, final),
        "continue_text": continue_text,
        "concepts": concepts,
        "example": example.id,
    }


def _demo(example, rep, start: int, end: int, text: str, with_highlights: bool) -> dict:
    comments = example.comments()[start:end]
    while comments and not comments[-1]:
        comments.pop()
    step = {"type": "demonstrate", "text": text, "fen": rep.boards[start].fen(),
            "moves": example.uci[start:end], "comments": comments, "example": example.id}
    if with_highlights and example.highlights:
        step["board"] = {"highlights": list(example.highlights)}
    return step


def example_steps(example, role: str, number: int, total: int, concept_names: list[str]) -> list[dict]:
    """Lesson steps presenting one verified example in the given role."""
    rep = example.replay()
    n = len(example.moves)
    header = _header(example, number, total, role)
    closing = {"type": "teach", "text": _explanation(example),
               "board": _board(rep.final.fen(), example), "example": example.id}
    key = example.key_ply
    if role == "demonstration" or key is None:
        header = header.replace(ROLE_TEXT[role], ROLE_TEXT["demonstration"])
        return [_demo(example, rep, 0, n, header, True), closing]

    steps: list[dict] = []
    if key > 0:
        steps.append(_demo(example, rep, 0, key, header, False))
    else:
        steps.append({"type": "teach", "text": header, "board": {"fen": rep.boards[0].fen()},
                      "example": example.id})
    prompt = example.prompt or "Find the key move."
    notes = example.comments()
    learner = [p for p in range(key, n) if (p - key) % 2 == 0]
    last_learner = learner[-1]

    if role == "guided":
        final = key == last_learner
        done = notes[key] or f"Correct: {example.labels[key]}!"
        steps.append(_exercise(example, rep, key, prompt, example.hints, done, concept_names, final))
        if key + 1 < n:
            steps.append(_demo(example, rep, key + 1, n, "See how it continues.", True))
        steps.append(closing)
        return steps

    # practice: every learner move is an exercise; the opponent's replies are demonstrated.
    for ply in range(key, n):
        board = rep.boards[ply]
        if ply in learner:
            final = ply == last_learner
            if ply == key:
                text, hints = prompt, example.hints
            else:
                text, hints = "Keep going: find the next move.", []
            done = "Correct!" if not final else ("Checkmate!" if rep.final.is_checkmate() else "Solved!")
            steps.append(_exercise(example, rep, ply, text, hints, done, concept_names, final))
        else:
            label = example.labels[ply]
            steps.append({"type": "demonstrate", "text": f"Your opponent replies {label}.",
                          "fen": board.fen(), "moves": [example.uci[ply]], "example": example.id})
    steps.append(closing)
    return steps


def _level(examples) -> str:
    return max(examples, key=lambda e: e.difficulty).level if examples else "beginner"


def knowledge_lesson(lesson_id: str, title: str, intro: str, sequence: list[tuple], completion: str,
                     concept_names: list[str]) -> dict:
    examples = [e for e, _ in sequence]
    steps: list[dict] = [{"type": "teach", "text": intro, "board": {"fen": examples[0].start_fen}}]
    for number, (example, role) in enumerate(sequence, start=1):
        steps += example_steps(example, role, number, len(sequence), concept_names)
    lesson = {
        "id": lesson_id,
        "title": title,
        "description": intro.split("\n")[0],
        "difficulty": _level(examples),
        "concepts": concept_names,
        "steps": steps,
        "completion": {"text": completion},
        "examples": [e.id for e in examples],
    }
    parse_lesson(lesson, course_id="_generated")  # same validation as hand-written lessons
    return lesson


def _names(library, concepts: list[str]) -> list[str]:
    return [library.concepts[c].name for c in concepts]


def _intro(library, retrieval, names: list[str]) -> str:
    main = library.concepts[retrieval.concepts[0]]
    roles = [role for _, role in retrieval.sequence]
    plan = []
    if "demonstration" in roles:
        plan.append("first I'll show you an example")
    if "guided" in roles:
        plan.append("then you'll find the key move in a slightly harder one")
    if "practice" in roles:
        plan.append("and finally you'll solve one on your own")
    text = f"{main.name}: {main.summary}" if len(names) == 1 else f"{', '.join(names)}."
    count = len(retrieval.sequence)
    text += f"\n\nI picked {count} verified example{'s' if count != 1 else ''} for you"
    text += (": " + ", ".join(plan) + ".") if plan else "."
    if retrieval.prerequisites:
        pre = ", ".join(library.concepts[p].name.lower() for p in retrieval.prerequisites)
        text += f" We'll start with a quick look at {pre}, which you need for this."
    return text


def create_knowledge_plan(goal: str, library=None, usage=None, level: str | None = None,
                          catalog=None, record_usage: bool = True) -> dict | None:
    """A plan record built from verified library examples, or None when the library
    has nothing suitable (the caller then falls back to the catalog/Qwen planner)."""
    from ..knowledge.library import get_knowledge
    from ..knowledge.retrieval import RetrievalRequest, retrieve
    from ..knowledge.usage import get_usage
    from .catalog import get_catalog

    library = library or get_knowledge()
    usage = usage if usage is not None else get_usage()
    retrieval = retrieve(library, RetrievalRequest(text=goal, level=level,
                                                   practice_count=PRACTICE_EXAMPLES), usage)
    if not retrieval.found:
        return None

    names = _names(library, retrieval.concepts)
    subject = names[0] if len(names) == 1 else " & ".join(names[:2])
    plan_id = uuid.uuid4().hex[:8]
    prefix = f"plan_{plan_id}_01"
    lessons = [knowledge_lesson(
        f"{prefix}a", f"{subject}: learn from examples", _intro(library, retrieval, names),
        retrieval.sequence, f"Lesson complete — you've seen {subject.lower()} in action.", names)]
    if retrieval.practice:
        lessons.append(knowledge_lesson(
            f"{prefix}b", f"{subject}: practice",
            f"Time to practise {subject.lower()} on your own. These are a little harder. "
            "Use hints if you get stuck.",
            [(e, "practice") for e in retrieval.practice],
            f"Practice complete — {subject} added to your toolkit.", names))

    main_category = library.concepts[retrieval.concepts[0]].category
    units = [{
        "topic_id": None,
        "title": f"{subject}: verified examples",
        "category": CATEGORY.get(main_category, "tactic"),
        "reason": "Hand-picked from the verified example library: watch, try, then solve.",
        "verified_by": KNOWLEDGE_VERIFIED_BY,
        "lesson_ids": [lesson["id"] for lesson in lessons],
        "concepts": retrieval.concepts,
        "example_ids": [e.id for e in retrieval.examples],
    }]

    # More practice from the verified catalog, linked through the concept graph.
    catalog = catalog or get_catalog()
    topic_ids: list[str] = []
    for cid in retrieval.concepts:
        for tid in library.concepts[cid].topics:
            if tid not in topic_ids and catalog.get(tid):
                topic_ids.append(tid)
    for n, tid in enumerate(topic_ids[:MAX_TOPIC_UNITS], start=2):
        topic = catalog.topics[tid]
        new = topic_lessons(f"plan_{plan_id}_{n:02d}", topic)
        units.append({"topic_id": tid, "title": topic.title, "category": topic.category,
                      "reason": f"More practice: {topic.summary}", "verified_by": "catalog + Stockfish",
                      "lesson_ids": [lesson["id"] for lesson in new]})
        lessons += new

    count = len(retrieval.examples)
    title = f"Learn: {subject}"
    summary = (f"{count} verified examples of {subject.lower()}: watch one, find the key move in the next, "
               "then solve on your own. Every move was checked with python-chess and Stockfish.")
    plan = {
        "id": plan_id,
        "goal": goal,
        "title": title,
        "summary": summary,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner": "knowledge",
        "units": units,
        "skipped": [],
        "related": [library.concepts[c].name for c in retrieval.related][:4],
        "knowledge": {
            "concepts": retrieval.concepts,
            "level": retrieval.level,
            "level_source": retrieval.level_source,
            "prerequisites": retrieval.prerequisites,
            "example_ids": [e.id for e in retrieval.examples],
            "roles": [role for _, role in retrieval.sequence],
        },
    }
    course = {"id": f"plan_{plan_id}", "title": title, "description": summary, "kind": "plan",
              "lessons": [{"id": lesson["id"], "title": lesson["title"]} for lesson in lessons]}
    if record_usage:
        usage.record_used(retrieval.examples)  # "seen": the next request gets fresh examples
    return {"plan": plan, "course": course, "lessons": lessons}
