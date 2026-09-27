"""What to study next, from the learner model (never a generic list).

Sources, in priority order (each suggestion says why, in the learner's terms):

    game weakness   a recurring mistake from their own analysed games, not yet solid
                    in puzzles  → a lesson that trains it (with a real-game emphasis
                    when they already solve it in puzzles: the "transfer" gap)
    calculation     one-movers go fine, longer lines don't → multi-move practice
    weak            a concept whose recent results are poor → an easier revisit
    review          spaced repetition: not seen for a while
    next idea       the lesson just finished went well → a related idea not yet met
    goals           a new learner's onboarding goals → where to start

Each suggestion carries `goal` (text for POST /api/plans) and, for game weaknesses,
the weakness `key` so the planner builds a personal plan from the evidence.
"""
from __future__ import annotations

from .profile import LearnerProfile
from .views import concept_view

MAX_SUGGESTIONS = 4
GOAL_START = {
    "tactics": ("fork", "Forks are the most common tactic, and a great place to start."),
    "checkmates": ("back_rank_mate", "Back-rank mates are the checkmate pattern you'll meet most."),
    "endgames": ("opposition", "The opposition is the key to king-and-pawn endgames."),
    "openings": ("openings", "Learn the ideas behind the first moves, not just the moves."),
    "stop_blundering": ("hanging_piece", "Most games at every level are decided by pieces left undefended."),
    "calculation": ("discovered_attack", "Discovered attacks train you to look a few moves ahead."),
    "strategy": ("king_safety", "Keeping your king safe is the first strategic habit."),
    "rating": ("tactics", "Tactics win the most rating points, fastest."),
}
DEFAULT_START = ("check", "Start with checks: they come first in every tactic.")


def _name(library, cid: str | None) -> str:
    if cid and library is not None and cid in library.concepts:
        return library.concepts[cid].name
    return (cid or "").replace("_", " ")


def suggestions(profile: LearnerProfile, library=None, just_studied: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, concept: str | None, title: str, reason: str, goal: str, **extra) -> None:
        key = concept or extra.get("weakness") or title
        if key in seen or len(out) >= MAX_SUGGESTIONS:
            return
        seen.add(key)
        out.append({"kind": kind, "concept": concept, "title": title, "reason": reason, "goal": goal, **extra})

    views = {c: concept_view(profile, c, library) for c in profile.concepts}

    # just finished a lesson: what now?
    for cid in just_studied or []:
        v = concept_view(profile, cid, library)
        name = _name(library, cid)
        if v["status"] == "weak":
            add("revisit", cid, f"{name} again, a little easier",
                "That lesson was hard going; a second pass with clearer positions usually clicks.",
                f"easy {name.lower()}")
        elif v["status"] == "mastered" and library is not None and cid in library.concepts:
            for rel in list(library.concepts[cid].related) + [
                    c.id for c in library.concepts.values() if cid in c.prerequisites]:
                if rel in library.concepts and library.count_for(rel) and \
                        concept_view(profile, rel, library)["status"] in ("new", "learned"):
                    add("next", rel, f"Next: {_name(library, rel)}",
                        f"You've got {name.lower()} down; {_name(library, rel).lower()} builds on it.",
                        _name(library, rel))
                    break

    for w in profile.weaknesses:
        if w.get("tier") != "recurring":
            continue
        cid = w.get("concept")
        v = views.get(cid) or (concept_view(profile, cid, library) if cid else {"status": "new", "gaps": []})
        if v["status"] == "mastered" and "transfer" not in v["gaps"]:
            continue
        found = f"{w.get('game_count')} of your last {w.get('total_games')} games"
        if "transfer" in v["gaps"]:
            reason = (f"You solve these in puzzles but it happened in {found}; "
                      "practise it in positions from real games.")
        else:
            reason = f"This happened in {found}."
        add("weakness", cid, f"Train: {w.get('title') or _name(library, cid)}", reason,
            w.get("title") or _name(library, cid), weakness=w.get("key"))

    for cid, v in views.items():
        if "calculation" in v["gaps"]:
            add("calculation", cid, f"Calculation: {_name(library, cid).lower()} in longer lines",
                "You find the first move well; these positions need you to see a few moves further.",
                f"hard {_name(library, cid).lower()}")
    for cid, v in sorted(views.items(), key=lambda kv: (kv[1]["success"] or 0, kv[0])):
        if v["status"] == "weak":
            add("revisit", cid, f"Revisit {_name(library, cid).lower()}",
                "Your recent attempts here were mixed; clearer positions will help it stick.",
                f"easy {_name(library, cid).lower()}")
    for cid, v in views.items():
        if v["needs_review"]:
            add("review", cid, f"Quick review: {_name(library, cid).lower()}",
                "It's been a while; a short review keeps it fresh.", _name(library, cid))

    if not out:
        goals = profile.onboarding.get("goals") or []
        for g in goals:
            cid, why = GOAL_START.get(g, DEFAULT_START)
            add("start", cid, f"Start with {_name(library, cid).lower()}", why, _name(library, cid))
        if not goals and not profile.concepts:
            cid, why = (("check", DEFAULT_START[1]) if profile.level == "beginner" else
                        ("fork", GOAL_START["tactics"][1]) if profile.level == "intermediate" else
                        ("deflection", "Deflections are a strong player's everyday weapon."))
            add("start", cid, f"Start with {_name(library, cid).lower()}", why, _name(library, cid))
    return out
