"""A developer view of how one request became a plan (shown in the UI with ?debug=1).

USER REQUEST → INTERPRETED INTENT (parser / Qwen / agreement) → LIBRARY COVERAGE →
GENERATION → POSITION CONSTRAINTS → VALIDATION (python-chess, Stockfish, material
constraints, educational validation, request satisfaction: PASS / FAIL).
Built from what the pipeline actually recorded; nothing here is re-derived or guessed.
"""
from __future__ import annotations

STAGES = {
    "python-chess": {"fen_valid", "position_legal", "moves_legal", "key_move_is_learners"},
    "stockfish": {"engine_verified", "opening_line_sound"},
    "material constraints": {"items_match_unit"},
    "educational validation": {"size_bounds", "objectives", "has_practice", "role_order", "prerequisites_first",
                               "difficulty_progression", "level_fit", "no_repeated_positions", "no_redundant_units",
                               "texts_match_facts"},
    "request satisfaction": {"request_satisfied", "units_in_scope", "subjects_covered", "respects_exclusions"},
}


def constraints(spec, objective: str | None) -> list[str]:
    """The position specification in words (what every position must satisfy)."""
    from ..intent.material import objective_label
    from ..intent.model import LETTER_NAME, PLURAL
    if spec.relation != "versus":
        return [spec.label()]

    def side(group):
        counts = [(LETTER_NAME[p], PLURAL[p], group.count(p)) for p in dict.fromkeys(group)]
        return ", ".join(f"exactly {n} {plural if n > 1 else name}" for name, plural, n in counts)
    who = ("learner", "opponent") if spec.owner == "learner" else ("one side", "the other side")
    out = [f"{who[0]}: {side(spec.pieces)}", f"{who[1]}: {side(spec.against)}",
           "no other pieces (pawns allowed)" if "P" not in spec.pieces + spec.against else "no other pieces",
           "kings on the board, legal position, the learner to move and not in check",
           "an endgame position, the same material at the start and at the learner's move"]
    if objective and objective != "general":
        out.append(f"focus: {objective_label(objective, spec).lower()}")
    return out


def validation(report) -> dict:
    if report is None:
        return {name: "NOT RUN" for name in STAGES}
    ran = set(report.checks or [])
    out = {}
    for name, checks in STAGES.items():
        hit = ran & checks
        if not hit:
            out[name] = "NOT RUN"
            continue
        errors = [i.message for i in report.issues if i.check in checks and i.severity == "error"]
        unsure = [i.message for i in report.issues if i.check in checks and i.severity == "uncertain"]
        out[name] = "FAIL" if errors else ("UNCERTAIN" if unsure else "PASS")
        if errors or unsure:
            out[name + " (why)"] = (errors + unsure)[:3]
    return out


def view(goal: str, intent, result=None, candidate=None) -> dict:
    """The debug block for plan["debug"] (or the error response when nothing verified)."""
    specs = intent.material_specs() if intent is not None else []
    cand = candidate or (result.candidate if result is not None else None)
    report = result.report if result is not None else None
    coverage = list(getattr(cand, "coverage", None) or [])
    out = {
        "user_request": goal,
        "interpreted_intent": {
            "material": [s.describe_dict() for s in specs],
            "objective": getattr(intent, "objective", None) or ("general" if specs else None),
            "components": [c.key() for c in intent.components] if intent is not None else [],
            "reading": getattr(intent, "reading", None) or {},
            "source": getattr(intent, "source", None),
        },
        "library_coverage": [{"material": c["material"]["id"], "coverage": c["library_coverage"].upper(),
                              "verified_matches": c["library"], "needed": c["needed"],
                              "refs": c.get("library_refs", [])} for c in coverage],
        "generation": [{"material": c["material"]["id"], "generated": c["generated"],
                        "details": c.get("generation")} for c in coverage],
        "position_constraints": [constraints(s, getattr(intent, "objective", None)) for s in specs],
        "validation": validation(report),
        "status": result.status if result is not None else None,
        "attempts": result.attempts if result is not None else [],
    }
    return out
