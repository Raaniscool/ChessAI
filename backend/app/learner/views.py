"""Derived views of a learner profile: statuses, gaps, targets, summaries.

Nothing here is stored; it's recomputed from the facts in the profile each time, so the
rules can change without migrating anyone's data.

Concept status (in priority order):
    mastered     ≥4 attempts and the recent results average ≥ 0.8
    weak         ≥3 attempts averaging < 0.45, or a recurring mistake in their games
    practicing   some attempts, neither of the above
    learned      a lesson was completed but no exercises solved yet
    new          nothing yet

A concept with the "understanding" explanation signal (learner.help: mostly solved, but an
explanation was requested on at least 3 of the last results and half of them) is not called
mastered yet: it stays "practicing". Explanation requests never make a concept "weak".

Gaps (what kind of help works):
    calculation    one-move positions go well (≥0.7) but multi-move ones don't (<0.45)
    transfer       solved in puzzles (≥0.7) but still a recurring mistake in real games
    understanding  solved, but the learner keeps asking why (learner.help signal)
Needs review: time since last seen exceeds REVIEW_AFTER[status] (spaced repetition).
"""
from __future__ import annotations

from .profile import (MASTERED_SCORE, MASTERY_WINDOW, MIN_FOR_MASTERY, MIN_FOR_WEAK, REVIEW_AFTER, WEAK_SCORE,
                      ConceptState, LearnerProfile, _mean, days_since)
from . import help as H
from . import rating as R

# How far below/above the learner's rating material is aimed, by purpose.
# 190 below ≈ 75% expected success: new ideas should mostly succeed.
TARGET_OFFSET = {"learn": -190, "practice": -80, "review": -120, "challenge": 60, "simplify": -330}
WINDOW = (-350, 150)  # the difficulty range shown in the profile


def combined(profile: LearnerProfile, cid: str, library=None) -> ConceptState | None:
    """The state of a concept, including its sub-concepts (forks = knight forks + pawn forks ...)."""
    ids = [cid]
    if library is not None:
        try:
            ids += [c for c in library.descendants(cid) if c != cid]
        except Exception:
            pass
    states = [profile.concepts[i] for i in ids if i in profile.concepts]
    if not states:
        return None
    if len(states) == 1:
        return states[0]
    out = ConceptState()
    for st in states:
        for name in ("attempts", "solved", "first_try", "hints", "reveals", "lessons_started",
                     "lessons_completed", "game_misses", "game_evidence_games"):
            setattr(out, name, getattr(out, name) + getattr(st, name))
        out.recent_explained += H.aligned_flags(st.recent, st.recent_explained)
        out.recent += st.recent
        out.explain_requests += st.explain_requests
        out.explained += st.explained
        for kind, n in st.explain_kinds.items():
            out.explain_kinds[kind] = out.explain_kinds.get(kind, 0) + n
        out.recent_short += st.recent_short
        out.recent_long += st.recent_long
        if st.last_seen and (out.last_seen or "") < st.last_seen:
            out.last_seen, out.last_result = st.last_seen, st.last_result
    rated = [(st.rating, st.attempts) for st in states if st.rating is not None]
    if rated:
        weight = sum(max(1, a) for _, a in rated)
        out.rating = int(sum(r * max(1, a) for r, a in rated) / weight)
    return out


def help_of(st: ConceptState | None) -> dict:
    """The explanation signal for a concept state (learner.help), over the mastery window."""
    if st is None:
        return H.signal([], [], MASTERY_WINDOW) | {"requests": 0}
    return H.signal(st.recent, st.recent_explained, MASTERY_WINDOW) | {"requests": st.explain_requests}


def status_of(st: ConceptState | None) -> str:
    if st is None:
        return "new"
    window = st.recent[-MASTERY_WINDOW:]
    score = _mean(window)
    if st.attempts >= MIN_FOR_MASTERY and score is not None and score >= MASTERED_SCORE:
        # solving it while repeatedly asking why isn't mastery yet (not a weakness either)
        return "practicing" if help_of(st)["signal"] == "understanding" else "mastered"
    if (st.attempts >= MIN_FOR_WEAK and score is not None and score < WEAK_SCORE) or st.game_misses >= 2:
        return "weak"
    if st.attempts:
        return "practicing"
    if st.lessons_completed:
        return "learned"
    return "new"


def gaps_of(st: ConceptState | None) -> list[str]:
    if st is None:
        return []
    out = []
    short, long_ = _mean(st.recent_short[-MASTERY_WINDOW:]), _mean(st.recent_long[-MASTERY_WINDOW:])
    if short is not None and long_ is not None and len(st.recent_long) >= 2 and short >= 0.7 and long_ < WEAK_SCORE:
        out.append("calculation")
    score = _mean(st.recent[-MASTERY_WINDOW:])
    if st.game_misses >= 2 and score is not None and st.attempts >= 3 and score >= 0.7:
        out.append("transfer")
    if help_of(st)["signal"] == "understanding":
        out.append("understanding")
    return out


def concept_view(profile: LearnerProfile, cid: str, library=None) -> dict:
    st = combined(profile, cid, library)
    status = status_of(st)
    since = days_since(st.last_seen) if st else None
    due = bool(st and since is not None and status in REVIEW_AFTER and since >= REVIEW_AFTER[status])
    return {
        "concept": cid, "status": status, "gaps": gaps_of(st), "needs_review": due,
        "rating": st.rating if st and st.rating is not None else profile.rating,
        "attempts": st.attempts if st else 0,
        "success": round(_mean(st.recent[-MASTERY_WINDOW:]), 2) if st and st.recent else None,
        "last_seen": st.last_seen if st else None,
        "explanations": help_of(st),
    }


def target_rating(profile: LearnerProfile, concept: str | None = None, purpose: str = "learn",
                  library=None) -> int:
    base = profile.rating
    if concept:
        st = combined(profile, concept, library)
        if st is not None and st.rating is not None:
            base = st.rating
    return R.clamp(base + TARGET_OFFSET.get(purpose, 0))


def lesson_shape(profile: LearnerProfile, concept: str | None, library=None) -> dict:
    """How a lesson on `concept` should be built for this learner (used by the planner).

    demos       worked examples before the learner moves (fewer as skill grows)
    practice    positions to solve alone
    purpose     which TARGET_OFFSET the examples are chosen with
    reminders   explain how the pieces involved move (true beginners only)
    prerequisites  teach the building blocks first
    realistic   prefer positions from real games (the "solves puzzles, misses it in games" gap)
    """
    view = concept_view(profile, concept, library) if concept else {"status": "new", "gaps": []}
    status, gaps, level = view["status"], view["gaps"], profile.level
    explain = (view.get("explanations") or {}).get("signal", "none")
    if status == "mastered":
        shape = {"demos": 0, "practice": 4, "purpose": "challenge"}
    elif status == "weak":
        shape = {"demos": 1, "practice": 3, "purpose": "simplify"}
    elif status in ("practicing", "learned"):
        shape = {"demos": 1 if level != "advanced" else 0, "practice": 3, "purpose": "practice"}
    else:  # new to this learner
        shape = {"demos": {"beginner": 2, "intermediate": 1, "advanced": 1}[level],
                 "practice": {"beginner": 2, "intermediate": 3, "advanced": 3}[level], "purpose": "learn"}
    # Repeatedly asked for explanations on this idea: teach it once more before stepping up
    # (one more worked example; the purpose, and so the difficulty, is not lowered here).
    teaching = explain in ("understanding", "difficulty")
    if teaching:
        shape["demos"] = min(3, shape["demos"] + 1)
    shape.update({
        "status": status, "gaps": gaps, "level": level, "teaching": teaching, "explanations": explain,
        "reminders": profile.rating < 700,
        "prerequisites": level == "beginner" and status in ("new", "weak"),
        "realistic": "transfer" in gaps,
        "calculation": "calculation" in gaps,
        "target_rating": target_rating(profile, concept, shape["purpose"], library),
    })
    return shape


def summary(profile: LearnerProfile, library=None) -> dict:
    """Everything the UI's "what your coach knows" panel shows."""
    views = {cid: concept_view(profile, cid) for cid in profile.concepts}
    names = {}
    if library is not None:
        for cid in views:
            c = library.concepts.get(cid) if hasattr(library, "concepts") else None
            names[cid] = getattr(c, "name", None) or cid.replace("_", " ")

    def pick(status):
        return sorted((c for c, v in views.items() if v["status"] == status),
                      key=lambda c: (-(views[c]["attempts"]), c))

    lo, hi = WINDOW
    puzzles = profile.stats["puzzles"]
    strengths = [{"concept": c, "name": names.get(c, c.replace("_", " "))} for c in pick("mastered")]
    return {
        "id": profile.id,
        "new": profile.is_new,
        "onboarding": profile.onboarding,
        "rating": profile.rating, "rating_source": profile.skill.get("source"),
        "evidence": profile.skill.get("evidence", 0),
        "level": profile.level,
        "difficulty_range": [R.clamp(profile.rating + lo), R.clamp(profile.rating + hi)],
        "concepts": {c: {**v, "name": names.get(c, c.replace("_", " "))} for c, v in views.items()},
        "mastered": pick("mastered"), "practicing": pick("practicing"), "learned": pick("learned"),
        "weak": pick("weak"),
        "needs_review": sorted(c for c, v in views.items() if v["needs_review"]),
        "gaps": {c: v["gaps"] for c, v in views.items() if v["gaps"]},
        "strengths": strengths,
        "weaknesses": profile.weaknesses,
        "game_observations": profile.game_observations,
        "recent_topics": [t["concept"] for t in reversed(profile.recent_topics)][:8],
        "stats": {**profile.stats,
                  "first_try_rate": round(puzzles["first_try"] / puzzles["attempts"], 2)
                  if puzzles["attempts"] else None},
        "preferences": profile.preferences,
        "completed_lessons": profile.completed_lessons,
    }


def prompt_context(profile: LearnerProfile, concepts: list[str] | None = None, library=None) -> str:
    """A few short lines about the learner for Qwen (small on purpose: context costs latency)."""
    parts = [f"Learner: about {profile.rating} rating ({profile.level})"]
    goals = profile.onboarding.get("goals") or []
    if goals:
        parts.append("goals: " + ", ".join(g.replace("_", " ") for g in goals[:3]))
    for cid in (concepts or [])[:2]:
        v = concept_view(profile, cid, library)
        bits = [v["status"]] + [f"gap: {g}" for g in v["gaps"]]
        parts.append(f"{cid.replace('_', ' ')}: {', '.join(bits)}")
    weak = [w.get("title") for w in profile.weaknesses if w.get("tier") == "recurring"][:2]
    if weak:
        parts.append("often in their games: " + "; ".join(t for t in weak if t))
    return ". ".join(parts) + "."
