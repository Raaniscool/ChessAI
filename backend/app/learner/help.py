"""When the learner asked for more explanation: a soft, per-concept learner signal.

Performance tells us what the learner did. Explanation requests tell us how much additional
understanding they sought. Both are stored side by side and never mixed: a request is not a
wrong answer, does not change a puzzle or lesson score, and does not move any rating.

What counts (an *explicit* request, recorded by the lesson session):

    explain    "Explain this example": asking for an explanation of a verified example
    deeper     "Explain deeper": asking for more after the instant verified feedback
    question   a question typed in the lesson chat ("why is Nf7 better?", "what was wrong
               with my move?", "I don't understand"): see is_help_request

What never counts: the instant feedback after a move, the stored explanation shown after a
puzzle, the automatic AI explanation the importance rule streams for some moves, hints (their
own counter) and revealing the solution (its own counter).

How it is used (deterministic, documented, per concept; never decided by the language model):

    window      the concept's last MASTERY_WINDOW results (learner.profile.recent)
    explained   results in that window where an explanation was requested
    signal      none          fewer than MIN_EXPLAINED explained results, or under
                              EXPLAINED_SHARE of the window: one request has no effect at all
                understanding mostly solved (success >= SOLVED_WELL) but repeatedly asked why:
                              "I can solve this, but I don't fully understand it yet"
                difficulty    mostly not solved, and repeatedly asked for explanations

    effect      understanding -> not "mastered" yet; the next lesson starts with one more worked
                                 example; the concept's difficulty target eases by TARGET_EASE
                                 (smaller than a game weakness, and only for this concept)
                difficulty    -> the next lesson starts with one more worked example; the
                                 target eases by TARGET_EASE (failures already lower the rating)
                none          -> nothing (incorrect answers without a request stay ordinary
                                 performance failures; nobody is assumed to need teaching)
"""
from __future__ import annotations

import re

KINDS = ("explain", "deeper", "question")
MIN_EXPLAINED = 3        # explained results (in the window) before the signal means anything
EXPLAINED_SHARE = 0.5    # ... and at least this share of the window
SOLVED_WELL = 0.7        # recent success at or above this: "solves it"
TARGET_EASE = 40         # difficulty-target easing for a concept with a signal (WEAKNESS_SHIFT is 50)

# A chat message in a lesson counts as a request for understanding when it asks something.
_QUESTION = re.compile(
    r"\?|\b(why|how|what|which|explain\w*|understand\w*|unclear|confus\w*|clarif\w*|mean\w*|wrong|"
    r"instead|reason\w*|purpose|point of|idea)\b", re.IGNORECASE)


def is_help_request(message: str) -> bool:
    """Is this lesson-chat message a request for explanation? ("thanks", "ok", "nice": no)."""
    return bool(message and _QUESTION.search(message))


def aligned_flags(recent: list, flags: list) -> list[int]:
    """Explanation flags lined up with `recent` (older profiles have none: no request)."""
    flags = [int(bool(f)) for f in (flags or [])][-len(recent):] if recent else []
    return [0] * (len(recent) - len(flags)) + flags


def signal(recent: list[float], flags: list, window: int) -> dict:
    """The explanation signal over the last `window` results (see the module docstring)."""
    scores = list(recent)[-window:]
    marks = aligned_flags(list(recent), flags)[-window:] if scores else []
    explained = sum(marks)
    n = len(scores)
    share = explained / n if n else 0.0
    success = sum(scores) / n if n else None
    explained_success = [s for s, m in zip(scores, marks) if m]
    if explained < MIN_EXPLAINED or share < EXPLAINED_SHARE:
        kind = "none"
    elif success is not None and success >= SOLVED_WELL:
        kind = "understanding"
    else:
        kind = "difficulty"
    return {"signal": kind, "explained": explained, "results": n, "share": round(share, 2),
            "explained_success": round(sum(explained_success) / len(explained_success), 2)
            if explained_success else None}
