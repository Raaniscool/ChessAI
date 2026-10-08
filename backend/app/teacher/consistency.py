"""Catch move explanations that contradict the engine verdict or the lesson result.

A small local model can say "That's the right move!" about a move the engine
graded Blunder, or call a lesson's solution a mistake. The prompt already states
the verdict and the lesson result; this is the deterministic backstop. A reply
that contradicts them is replaced by the fallback teacher's text, which is built
from the same facts and so can't disagree.

Only claims about the *student's* move count: "the best move was Nxg5" or "you
punished Black's blunder" are normal teaching sentences, not contradictions. A
false alarm costs little (the learner reads the deterministic explanation); a
missed contradiction is the bug users see, so the patterns lean towards catching.
"""
from __future__ import annotations

import re

from ..engine import MoveFeedback
from ..engine.classification import Classification

_GOOD = {Classification.EXCELLENT, Classification.GOOD}
_BAD = {Classification.MISTAKE, Classification.BLUNDER}

# Sentence openers that congratulate the student ("Great move!", "Well done.").
_CONGRATS = re.compile(
    r"(?:^|[.!?]\s+)(?:great|excellent|good|nice|perfect|brilliant|fantastic|superb|awesome|"
    r"wonderful|strong|well)\s+(?:move|job|work|find|choice|play|played|done|thinking)\b"
    r"|(?:^|[.!?]\s+)(?:well done|bravo|congratulations|correct|exactly|spot on)\b",
    re.IGNORECASE)
# "You found it", "that's right" — solving claims.
_SOLVED = re.compile(
    r"\byou (?:found|got|solved|nailed)\b|\bthat(?:'s| is) (?:right|correct|it)\b"
    r"|\b(?:solves|solved) (?:the|this) (?:exercise|puzzle|problem)\b",
    re.IGNORECASE)

_PRAISE_WORDS = r"right|correct|best|good|great|excellent|strong|perfect|brilliant|clever|smart|solid|winning|accurate"
_BLAME_WORDS = r"blunder|mistake|error|inaccura\w*|wrong|bad|weak|poor|dubious"
# Words between the subject and the judgement that flip or redirect it.
_REDIRECT = re.compile(
    r"\b(?:not|n't|never|no|instead|rather|but|however|although|while|whereas|should|would|could|"
    r"better|missed|misses|miss|avoid\w*|allows?|punish\w*|exploit\w*|refut\w*|advantage|opponent|"
    r"black's|white's|their|than)\b|n't\b", re.IGNORECASE)


def _judged(text: str, subjects: list[str], words: str) -> str | None:
    """A sentence where the student's move is the subject of the judgement, e.g.
    "Your move is the best", "Bc4 was a mistake", "You made a blunder"."""
    subject = "|".join(subjects)
    pattern = re.compile(rf"(?:{subject})(?P<between>[^.!?;,:]{{0,40}}?)\b(?:{words})\b", re.IGNORECASE)
    for m in pattern.finditer(text):
        if not _REDIRECT.search(m.group("between")):
            return m.group(0)
    return None


def _subjects(feedback: MoveFeedback) -> list[str]:
    san = re.escape(feedback.user_move_san.rstrip("+#"))
    return [r"\byour move\b", r"\bthis move\b", r"\bthat move\b", r"\byou\b",
            r"\bthat(?:'s| is| was)", r"\bthis is\b", rf"(?<![\w-]){san}(?:[+#])?(?!\w)"]


def verdict_conflicts(text: str, feedback: MoveFeedback, move_accepted: bool | None) -> list[str]:
    """Problems where `text` contradicts the engine verdict / lesson result (empty if none)."""
    problems: list[str] = []
    subjects = _subjects(feedback)
    rejected = move_accepted is False
    bad = feedback.category in _BAD
    good = feedback.category in _GOOD and move_accepted is not False

    if bad or rejected:
        # The move is not the answer: no "correct!", "you found it", "that's right".
        solved = _SOLVED.search(text)
        if solved:
            problems.append(f"claims the move solves the exercise: {solved.group(0)!r}")
        if feedback.category not in _GOOD:
            cheer = _CONGRATS.search(text)
            praise = _judged(text, subjects, _PRAISE_WORDS)
            for hit in (cheer and cheer.group(0).strip(" .!?"), praise):
                if hit:
                    problems.append(f"praises a {feedback.category.value} move: {hit!r}")
        else:  # a sound move that isn't the lesson's: it may be good, but not "right/correct"
            claim = _judged(text, subjects, r"right|correct")
            if claim:
                problems.append(f"calls a move the lesson rejects correct: {claim!r}")
    if good:
        blame = _judged(text, subjects, _BLAME_WORDS)
        if blame:
            problems.append(f"calls a {feedback.category.value} move bad: {blame!r}")
    return problems
