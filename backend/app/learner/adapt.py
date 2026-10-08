"""In-lesson adaptation: what to change after each example, from how it went.

Decided after every finished example (all of its exercises resolved), from this
lesson's outcomes and the learner model. At most MAX_CHANGES per lesson, so a lesson
never balloons, and nothing is ever changed behind the learner's back: every change
comes with a short note saying why.

    harder         the last two examples were solved cleanly (first try, no hints, and no
                   explanation requested) and the next one isn't harder than those: swap it
                   for a harder position
    teach          two examples in this lesson were solved but the learner asked for an
                   explanation each time ("I can solve it, but why?"): insert one worked
                   example of the same idea (never harder; once per lesson). Asking is not
                   a failure: nothing is made easier because of it
    easier         the learner had to look at the solution, or needed 3+ tries: insert a
                   clearer position of the same idea before going on
    prerequisite   ... and for a beginner who hasn't met a building block of the idea
                   (e.g. "check" before "fork"): show that first instead
    calculation    they found the key move but lost the thread later in the line:
                   insert a shorter multi-move position to practise the follow-up

Candidates are verified library examples only, chosen by rating (knowledge.difficulty)
and never one this lesson has already used.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

MAX_CHANGES = 2
HARDER_STEP = 200         # aim this far above the hardest position solved cleanly
HARDER_MIN = 100          # ... and accept nothing less than this much harder
EASIER_STEP = 250         # aim this far below the position that went wrong
EASIER_MIN = 80
TEACH_AFTER = 2           # explained solves in a lesson before a worked example is added
TEACH_BELOW = 100         # the worked example is aimed this far below the last position

NOTES = {
    "harder": ["You're finding these easy, so here's a harder one.",
               "Two clean solves in a row. Let's step it up.",
               "That was quick. Try this tougher position."],
    "easier": ["That one was tough. Let's try a clearer position of the same idea first.",
               "Let's take a step back with a simpler position, then come back to harder ones.",
               "No problem: here's an easier one to lock in the idea."],
    "calculation": ["You found the key move; the hard part was seeing it through. "
                    "Here's a shorter line to practise the follow-up.",
                    "The idea was right. Now let's practise finishing the job, one move at a time."],
    "teach": ["You solved these, and you asked why. Here's a worked example that shows the idea step by step.",
              "Good solving. Since you wanted the why, here's one worked example of the same idea before we go on."],
    "prerequisite": ["This idea builds on {name}. Here's a quick look at that first.",
                     "First a quick refresher on {name}, which this idea depends on."],
}


@dataclass
class Outcome:
    example_id: str
    concept: str
    rating: int
    score: float
    learner_moves: int = 1
    wrong: int = 0
    hints: int = 0
    revealed: bool = False
    key_found: bool = False     # the first move of the example was found without help
    explained: bool = False     # an explanation was requested for it (learner.help; not a failure)


@dataclass
class Decision:
    kind: str                   # harder | easier | calculation | prerequisite | teach
    example_id: str
    role: str                   # role for the new example's steps
    replace: str | None = None  # example id to swap out (harder), else insert
    note: str = ""
    extra: dict = field(default_factory=dict)


def note_for(kind: str, seed: str, **fmt) -> str:
    options = NOTES[kind]
    return random.Random(seed).choice(options).format(**fmt)


def decide(outcomes: list[Outcome], upcoming: list[tuple[str, int, str]], used: set[str], changes: int,
           library, level: str = "beginner", known: set[str] | None = None, seen=None,
           last_used=None, rating_of=None, done_kinds=None) -> Decision | None:
    """`upcoming`: (example_id, rating, role) of the examples still ahead in the lesson.
    `rating_of`: example -> rating on the same scale as the outcomes' ratings (default
    knowledge.difficulty; the session passes learner.training_level.rating_of).
    `done_kinds`: kinds of the changes already made in this lesson."""
    from ..knowledge.difficulty import puzzle_rating as _library_rating
    from ..knowledge.library import Query
    puzzle_rating = rating_of or _library_rating

    if not outcomes or changes >= MAX_CHANGES:
        return None
    last = outcomes[-1]
    seed = f"{last.example_id}:{changes}"

    def find(target: int, ok, mode: str = "interactive", concept: str | None = None, extra=None):
        # nearest to the target among a few verified candidates, never reusing one
        picks = library.select(Query(concepts=[concept or last.concept], count=6, mode=mode,
                                     target_rating=target, rating_of=rating_of, exclude=set(used),
                                     not_seen_recently=True), seen or {}, last_used or {})
        picks = [e for e in picks if e.status == "verified" and e.tier != "personal" and ok(e)
                 and (extra is None or extra(e))]
        picks.sort(key=lambda e: (abs(puzzle_rating(e) - target), e.id))
        return picks[0] if picks else None

    struggled = last.revealed or last.score == 0 or last.wrong >= 3
    if struggled:
        if last.key_found and last.learner_moves >= 2:
            ex = find(last.rating - EASIER_STEP // 2, lambda e: puzzle_rating(e) <= last.rating,
                      extra=lambda e: e.key_ply is not None and len(e.moves) - e.key_ply >= 3)
            if ex is not None:
                return Decision("calculation", ex.id, "practice", note=note_for("calculation", seed))
        if level == "beginner" and last.concept in library.concepts:
            for pre in prerequisites(library, last.concept):
                if known and any(d in known for d in library.descendants(pre)):
                    continue
                ex = find(0, lambda e: True, mode=None, concept=pre)
                if ex is not None:
                    return Decision("prerequisite", ex.id, "demonstration",
                                    note=note_for("prerequisite", seed, name=library.concepts[pre].name.lower()))
        # the same idea first; else its family (a clearer fork of any piece for a knight fork)
        family = [last.concept] + (list(library.concepts[last.concept].parents)
                                   if last.concept in library.concepts else [])
        for cid in family:
            ex = find(last.rating - EASIER_STEP, lambda e: puzzle_rating(e) <= last.rating - EASIER_MIN,
                      concept=cid)
            if ex is not None:
                return Decision("easier", ex.id, "guided", note=note_for("easier", seed))
        return None

    explained_solves = [o for o in outcomes if o.explained and o.score > 0 and not o.revealed]
    if len(explained_solves) >= TEACH_AFTER and "teach" not in (done_kinds or ()) and last.score > 0:
        ex = find(last.rating - TEACH_BELOW, lambda e: puzzle_rating(e) <= last.rating, mode=None)
        if ex is not None:
            return Decision("teach", ex.id, "demonstration", note=note_for("teach", seed))

    clean = [o for o in outcomes[-2:] if o.score >= 1.0 and not o.explained]
    if len(outcomes) >= 2 and len(clean) == 2:
        best = max(o.rating for o in clean)
        nxt = next(((eid, r, role) for eid, r, role in upcoming if role in ("guided", "practice")), None)
        if nxt is None or nxt[1] > best + HARDER_MIN // 2:
            return None  # nothing to swap, or it's already a step up
        ex = find(best + HARDER_STEP, lambda e: puzzle_rating(e) >= best + HARDER_MIN)
        if ex is not None:
            return Decision("harder", ex.id, nxt[2], replace=nxt[0], note=note_for("harder", seed))
    return None


def prerequisites(library, cid: str) -> list[str]:
    """A concept's prerequisites, including those it inherits (smothered mate needs check,
    because every checkmate does)."""
    out: list[str] = []
    todo, done = [cid], set()
    while todo:
        c = todo.pop(0)
        if c in done or c not in library.concepts:
            continue
        done.add(c)
        out += [p for p in library.concepts[c].prerequisites if p not in out]
        todo += list(library.concepts[c].parents)
    return out


def help_offer(wrong: int, hints_used: int, hints_total: int) -> dict | None:
    """After repeated wrong tries, offer the right kind of help instead of just "try again"."""
    if wrong >= 2 and hints_used < hints_total:
        return {"offer": "hint", "text": "Want a hint? It points you to the right piece without giving the move away."}
    if wrong >= 3:
        return {"offer": "solution", "text": "Still stuck? You can see the answer, and I'll find you a clearer "
                                             "position of the same idea next."}
    return None
