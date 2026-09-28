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

import re
import uuid
from datetime import datetime, timezone

import chess

from ..lessons.schema import parse_lesson
from .generator import move_hints, topic_lessons

KNOWLEDGE_VERIFIED_BY = "Knowledge Library (python-chess + Stockfish verified)"
MAX_TOPIC_UNITS = 2
PRACTICE_EXAMPLES = 3
REVIEW_TOO_EASY = 350  # personalized: no review lesson whose hardest position is this far below the target

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
    if kind in ("procedural", "qwen_generated"):
        who = "proposed by Qwen" if kind == "qwen_generated" else "built by ChessAI"
        return (f"Generated position ({who}, not from a real game) — checked by python-chess and Stockfish "
                "before it was used.")
    return ""


class Said:
    """What the lesson has already told the learner, so later examples don't repeat it: the
    concept summary, template sentences ("White can only save one of them..."), the same
    provenance note under every generated position. Color words are ignored when comparing
    ("Black can only save one" repeats "White can only save one"); squares are not, so every
    concrete sentence ("Nd2+ attacks the king on f3 and the queen on b3") survives."""

    _SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
    _COLOR = re.compile(r"\b(white|black)('s)?\b")

    def __init__(self, *texts: str):
        self.sentences: set[str] = set()
        self.credits: set[str] = set()
        self.role: str | None = None  # the previous example's role ("Your turn..." only when it changes)
        for text in texts:
            self.fresh(text or "")

    def _keys(self, sentence: str) -> set[str]:
        """The sentence, and — for "Knight fork: knights are the best..." — the part after a short
        label, so the concept summary counts as said however it was introduced."""
        key = self._COLOR.sub("side", re.sub(r"\s+", " ", sentence.strip().lower()))
        head, sep, rest = key.partition(": ")
        if sep and len(head.split()) <= 4 and len(rest.split()) >= 4:
            return {key, rest}
        return {key}

    def fresh(self, text: str, keep_one: bool = True) -> str:
        """`text` without the sentences already said (paragraphs kept; at least one sentence
        survives when keep_one). "Black to move." is never dropped."""
        out_paras, first = [], None
        for para in text.split("\n\n"):
            kept = []
            for sentence in self._SENTENCE.split(para.strip()):
                if not sentence:
                    continue
                first = first or sentence
                keys = self._keys(sentence)
                if keys & self.sentences and "to move" not in sentence.lower():
                    continue
                self.sentences |= keys
                kept.append(sentence)
            if kept:
                out_paras.append(" ".join(kept))
        if not out_paras and keep_one and first:
            return first
        return "\n\n".join(out_paras)

    def credit(self, example) -> str:
        """Provenance once per kind per lesson (a Lichess puzzle's own link is always shown)."""
        text = _credit(example)
        kind = (example.source or {}).get("source_type")
        if not text or (kind != "lichess_puzzle" and kind in self.credits):
            return ""
        self.credits.add(kind)
        return text


def _title(example, concept_names: list[str]) -> str:
    """ "Knight fork: win the queen" -> "win the queen" inside a knight-fork lesson."""
    head, sep, rest = example.title.partition(":")
    if sep and rest.strip() and head.strip().lower() in {n.lower() for n in concept_names}:
        return rest.strip()
    return example.title


def _header(example, number: int, total: int, role: str, concept_names: list[str] | None = None,
            said: Said | None = None) -> str:
    text = f"Example {number} of {total} — {_title(example, concept_names or [])}."
    if example.description:
        description = said.fresh(example.description, keep_one=False) if said else example.description
        if description:
            text += f" {description}"
    if said is not None:
        repeated = said.role == role
        said.role = role
        if repeated:
            return text
    return f"{text}\n\n{ROLE_TEXT[role]}"


def _explanation(example, said: Said | None = None) -> str:
    text = example.explanation or example.description
    if said:
        parts = [said.fresh(text or ""), said.credit(example)]
    else:
        parts = [text, _credit(example)]
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


def example_steps(example, role: str, number: int, total: int, concept_names: list[str],
                  said: Said | None = None) -> list[dict]:
    """Lesson steps presenting one verified example in the given role. `said` (one per lesson)
    keeps later examples from repeating what earlier steps already told the learner."""
    rep = example.replay()
    n = len(example.moves)
    key = example.key_ply
    shown = "demonstration" if role == "demonstration" or key is None else role
    header = _header(example, number, total, shown, concept_names, said)
    closing = {"type": "teach", "text": _explanation(example, said),
               "board": _board(rep.final.fen(), example), "example": example.id}
    if shown == "demonstration":
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


def _distinct_titles(new: list[dict], existing: list[dict], topic_title: str) -> None:
    """Catalog lessons added to a library plan are the *extra* practice: rename them when
    they'd read like the library lessons ("Back-rank mate: practice" twice in one plan)."""
    taken = {lesson["title"].lower() for lesson in existing}
    for lesson in new:
        title = lesson["title"]
        if title.lower() == topic_title.lower():
            title = f"{topic_title}: more examples"
        if title.lower() in taken and title.lower().endswith(": practice"):
            title = title[: -len("practice")] + "more practice"
        n = 2
        base = title
        while title.lower() in taken:
            title, n = f"{base} ({n})", n + 1
        lesson["title"] = title
        taken.add(title.lower())


def knowledge_lesson(lesson_id: str, title: str, intro: str, sequence: list[tuple], completion: str,
                     concept_names: list[str], reminder: str | None = None) -> dict:
    examples = [e for e, _ in sequence]
    if reminder:
        intro = f"{intro}\n\n{reminder}"
    steps: list[dict] = [{"type": "teach", "text": intro, "board": {"fen": examples[0].start_fen}}]
    said = Said(intro)
    for number, (example, role) in enumerate(sequence, start=1):
        steps += example_steps(example, role, number, len(sequence), concept_names, said)
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


def completion_text(subject: str) -> str:
    return f"That's the {subject.lower()} lesson done. Want another round, or shall we move on?"


def _names(library, concepts: list[str]) -> list[str]:
    return [library.concepts[c].name for c in concepts]


_WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}


def _outline(roles: list[str]) -> str:
    """What the examples are for, counted: "two to watch, one where you find the key move, then one
    on your own"."""
    demos, guided, practice = roles.count("demonstration"), roles.count("guided"), roles.count("practice")
    parts = []
    if demos:
        parts.append(f"{_WORDS.get(demos, demos)} to watch")
    if guided:
        parts.append(f"{_WORDS.get(guided, guided)} where you find the key move with a little help")
    if practice:
        parts.append(f"{_WORDS.get(practice, practice)} to solve on your own")
    if len(parts) > 1:
        parts[-1] = "then " + parts[-1]
    return ", ".join(parts)


def _intro(library, retrieval, names: list[str]) -> str:
    main = library.concepts[retrieval.concepts[0]]
    roles = [role for _, role in retrieval.sequence]
    if len(names) == 1:
        # "A back-rank mate: ..." already names the idea — don't prefix "Back-rank mate:" again.
        names_it = re.match(rf"^(an?\s+|the\s+)?{re.escape(main.name)}\b", main.summary, re.IGNORECASE)
        text = main.summary if names_it else f"{main.name}: {main.summary}"
    else:
        text = f"{', '.join(names)}."
    count = len(retrieval.sequence)
    outline = _outline(roles)
    text += f"\n\nI picked {count} verified example{'s' if count != 1 else ''} for you"
    text += f": {outline}." if outline else "."
    if retrieval.prerequisites:
        pre = ", ".join(library.concepts[p].name.lower() for p in retrieval.prerequisites)
        text += f" We'll start with a quick look at {pre}, which you need for this."
    return text


def _personal_intro(intro: str, why: str) -> str:
    """The concept summary, why the lesson is shaped this way for this learner, then what's in it."""
    head, _, rest = intro.partition("\n\nI picked ")
    return f"{head}\n\n{why} I picked {rest}" if rest else f"{head}\n\n{why}"


def _personal_request(goal: str, library, level: str | None, profile) -> tuple[dict, dict | None]:
    """RetrievalRequest settings for this learner (learner.personalize), or the defaults."""
    from ..knowledge.retrieval import resolve_concepts
    from ..learner.personalize import personalized, retrieval_settings

    defaults = {"level": level, "practice_count": PRACTICE_EXAMPLES}
    if not personalized(profile):
        return defaults, None
    concepts, confident = resolve_concepts(library, goal)
    if not concepts or not confident:
        return defaults, None
    out = retrieval_settings(profile, concepts[0], concepts, library)
    settings = out["settings"]
    if level:  # an explicit level for this request (the words, or the API) wins
        settings["level"] = level
    return settings, out["shape"]


def _review_intro(retrieval, shape: dict | None, subject: str) -> str | None:
    """The review lesson's opening line — honest about how hard it is — or None to leave it out.

    The review only says "a little harder" when its positions really are (by puzzle rating). A
    strong learner can use up the library's hardest positions in the first lesson; then a review of
    much easier ones would be a sudden easy stretch, so it is left out rather than padded."""
    if not retrieval.practice:
        return None
    from statistics import mean

    from ..knowledge.difficulty import puzzle_rating
    practice = [puzzle_rating(e) for e in retrieval.practice]
    if shape is not None and max(practice) < shape["target_rating"] - REVIEW_TOO_EASY:
        return None
    solved = [puzzle_rating(e) for e, role in retrieval.sequence if role != "demonstration"] or \
             [puzzle_rating(e) for e, _ in retrieval.sequence]
    text = f"Review time: solve these {subject.lower()} positions on your own, with no demonstration first."
    if solved and mean(practice) > mean(solved) + 50:
        text += " They are a little harder."
    return text + " Use hints if you get stuck."


def create_knowledge_plan(goal: str, library=None, usage=None, level: str | None = None,
                          catalog=None, record_usage: bool = True, profile=None) -> dict | None:
    """A plan record built from verified library examples, or None when the library
    has nothing suitable (the caller then falls back to the catalog/Qwen planner).

    With a learner `profile` the examples, their roles and the wording follow the
    learner model (learner.personalize); a blank profile changes nothing."""
    from ..knowledge.library import get_knowledge
    from ..knowledge.retrieval import RetrievalRequest, retrieve
    from ..knowledge.usage import get_usage
    from .catalog import get_catalog

    library = library or get_knowledge()
    usage = usage if usage is not None else get_usage()
    settings, shape = _personal_request(goal, library, level, profile)
    retrieval = retrieve(library, RetrievalRequest(text=goal, **settings), usage)
    if not retrieval.found and shape is not None and settings.get("exclude"):
        settings["exclude"] = set()  # the calculation filter left too little: plain examples then
        retrieval = retrieve(library, RetrievalRequest(text=goal, **settings), usage)
    if not retrieval.found:
        return None

    names = _names(library, retrieval.concepts)
    subject = names[0] if len(names) == 1 else " & ".join(names[:2])
    main_category = CATEGORY.get(library.concepts[retrieval.concepts[0]].category, "tactic")
    opening = main_category == "opening"
    plan_id = uuid.uuid4().hex[:8]
    prefix = f"plan_{plan_id}_01"
    intro = _intro(library, retrieval, names)
    personalization = None
    reminder = None
    if shape is not None:
        from ..learner.personalize import piece_reminder, plan_note, reason
        main = retrieval.concepts[0]
        why = reason(shape, profile, main, library.concepts[main].name)
        intro = _personal_intro(intro, why)
        personalization = plan_note(profile, shape, [r for _, r in retrieval.sequence], why)
        if shape.get("reminders"):
            reminder = piece_reminder(library, main)
    examples_lesson = knowledge_lesson(
        f"{prefix}a", f"{subject}: see it in real lines" if opening else f"{subject}: understand the idea",
        intro, retrieval.sequence,
        completion_text(subject), names, reminder=reminder)
    review_lesson = None
    review_text = _review_intro(retrieval, shape, subject)
    if review_text:
        review_lesson = knowledge_lesson(
            f"{prefix}b", f"{subject}: review", review_text,
            [(e, "practice") for e in retrieval.practice],
            f"Review complete — {subject} added to your toolkit.", names)

    # Catalog lessons on the same subject (verified puzzles / opening drills), linked through the
    # concept graph. Only what was asked for: a narrow request ("scholar's mate") doesn't pull in
    # a neighbouring topic ("attacking f7") unless the catalog itself matches the goal; a broad
    # one ("checkmates") covers its sub-patterns.
    catalog = catalog or get_catalog()
    searched = {t.id for t in catalog.search(goal)}
    topic_ids: list[str] = []
    for cid in retrieval.concepts:
        broad = len(library.descendants(cid)) > 1
        for tid in library.concepts[cid].topics:
            if tid not in topic_ids and catalog.get(tid) and (broad or tid in searched):
                topic_ids.append(tid)
    blocks = []
    taken = [examples_lesson] + ([review_lesson] if review_lesson else [])
    for n, tid in enumerate(topic_ids[:MAX_TOPIC_UNITS], start=2):
        topic = catalog.topics[tid]
        new = topic_lessons(f"plan_{plan_id}_{n:02d}", topic)
        _distinct_titles(new, taken, topic.title)
        taken += new
        blocks.append((topic, new))

    knowledge_unit = {"topic_id": None, "verified_by": KNOWLEDGE_VERIFIED_BY, "concepts": retrieval.concepts,
                      "example_ids": [e.id for e in retrieval.examples]}
    units: list[dict] = []

    def unit(title, category, reason, new_lessons, base=None, **extra):
        units.append({**(base or {}), "title": title, "category": category,
                      "reason": f"Step {len(units) + 1}: {reason}",
                      "lesson_ids": [lesson["id"] for lesson in new_lessons], **extra})

    first_opening = next((b for b in blocks if b[0].category == "opening" and len(b[1]) == 2), None)
    if opening and first_opening:
        # Openings: the moves and ideas -> the verified lines and traps -> the whole line from memory.
        topic, (ideas, whole_line) = first_opening
        unit(f"{topic.title}: moves and ideas", "opening", f"learn how the {topic.title} starts and why.",
             [ideas], topic_id=topic.id, verified_by="catalog + Stockfish")
        unit(f"{subject}: verified lines", main_category,
             "see the ideas in verified lines from the opening database, then find the key moves yourself.",
             [examples_lesson], knowledge_unit)
        unit(f"{topic.title}: the whole line", "opening", "play every move of the line from memory.",
             [whole_line], topic_id=topic.id, verified_by="catalog + Stockfish")
        blocks = [b for b in blocks if b is not first_opening]
    else:
        unit(f"{subject}: the idea", main_category,
             "what the idea is: one verified example shown move by move, then you find the key move in the next.",
             [examples_lesson], knowledge_unit)
    for topic, new in blocks:
        unit(topic.title, topic.category,
             "spot it in easier positions first, then harder ones that take several moves."
             if topic.category != "opening" else f"learn the moves and ideas of the {topic.title}.",
             new, topic_id=topic.id, verified_by="catalog + Stockfish")
    if review_lesson:
        unit(f"{subject}: review", main_category,
             "a mixed review: solve new positions on your own, with no demonstration first.",
             [review_lesson], knowledge_unit)
    by_id = {lesson["id"]: lesson for lesson in taken}
    lessons = [by_id[lid] for u in units for lid in u["lesson_ids"]]

    count = len(retrieval.examples)
    title = f"Learn: {subject}"
    summary = (f"A step-by-step plan for {subject.lower()}: " + " → ".join(u["title"] for u in units) +
               f". It uses {count} verified example{'s' if count != 1 else ''}; every move is checked by the "
               "Stockfish chess engine.")
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
        **({"personalization": personalization} if personalization else {}),
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
