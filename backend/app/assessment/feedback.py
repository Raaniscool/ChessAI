"""The short report after a Training segment — facts from the analysis, in plain words, no AI.

    headline        accuracy and moves
    went_well       the hidden idea found, a clean segment, moves matching Stockfish, high accuracy
    work_on         the costliest confirmed mistakes (what was better), then the profile's top need
                    (assessment.needs, only once the evidence supports it) and a struggling phase
    concepts        validator-backed concepts seen in this segment (found / missed / allowed)
    reveal          now that it's over: the position's idea (if it had one) and where it comes from

Wording describes moves, never the player ("this move lost a piece", not "you are bad at ...").
"""
from __future__ import annotations

CATEGORY_WORD = {"blunder": "a blunder", "mistake": "a mistake", "inaccurate": "inaccurate"}
KIND_WORD = {"found": "found", "missed": "missed", "allowed": "allowed against you"}
ENDED = {"checkmate": "The game ended in checkmate.", "stalemate": "The game ended in stalemate.",
         "draw": "The game ended in a draw.", "length": None, "quiet": None, "stopped": "You ended the segment."}


def _name(knowledge, concept: str | None) -> str | None:
    if not concept:
        return None
    c = knowledge.concepts.get(concept)
    return c.name if c else concept.replace("_", " ")


def build(seg, facts: dict, needs: dict, phases: dict, knowledge) -> dict:
    n = facts["learner_moves"]
    acc = facts.get("accuracy")
    went_well: list[str] = []
    work_on: list[str] = []
    tested = facts.get("tested")
    if tested and tested["result"] == "found":
        went_well.append(f"You found the key idea straight away ({_name(knowledge, tested['concept'])}).")
    errors = facts["mistakes"] + facts["blunders"]
    if n and errors == 0:
        went_well.append(f"No mistakes or blunders in {n} {'move' if n == 1 else 'moves'}.")
    if n >= 3 and facts["best_moves"] >= max(2, (n + 1) // 2):
        went_well.append(f"{facts['best_moves']} of your {n} moves matched Stockfish's top choice.")
    if acc is not None and acc >= 85 and errors == 0:
        went_well.append(f"Accuracy {acc:.0f}% — very clean play.")

    for m in facts["important_mistakes"][:2]:
        label = m.get("label") or m.get("san")
        line = f"{label} was {CATEGORY_WORD.get(m.get('category'), 'costly')}"
        line += f": {m['best_move']} was better." if m.get("best_move") else "."
        if m.get("lines"):
            line += f" ({m['lines'][0]})"
        work_on.append(line)
    if tested and tested["result"] == "missed" and not any(m.get("ply") == 0 for m in facts["important_mistakes"]):
        work_on.append(f"Your first move missed the key idea: {tested.get('key_move')} "
                       f"({_name(knowledge, tested['concept'])}).")
    seen_here = {c["concept"] for c in facts["concepts"]}
    flagged = sorted((x for x in needs.values() if x["status"] == "needs_work"),
                     key=lambda x: (x["concept"] not in seen_here, -x["priority"], x["concept"]))
    if flagged:
        top = flagged[0]
        work_on.append(f"Across your Training so far: {top['why']}")
    weak_phase = next((p for p in phases.values() if p["status"] == "needs_work" and p["phase"] == seg.position.phase),
                      None)
    if weak_phase:
        work_on.append(f"Your {weak_phase['name']}: {'; '.join(weak_phase['reasons'])}.")
    if not went_well:
        went_well.append("Segment finished — every one adds evidence, so the picture gets clearer.")
    if not work_on:
        work_on.append("Nothing stood out this time. Keep going!")

    concepts = []
    for c in facts["concepts"]:
        concepts.append({"concept": c["concept"], "name": _name(knowledge, c["concept"]), "result": c["result"],
                         "kind": c["kind"], "text": c.get("text"),
                         "line": f"{_name(knowledge, c['concept'])}: {KIND_WORD.get(c['result'], c['result'])}"})
    hidden = seg.position.hidden or {}
    reveal = {"source": seg.position.source.get("label"), "url": seg.position.source.get("url"),
              "line": seg.position.source.get("line"),
              "idea": _name(knowledge, hidden.get("concept")), "key_move": hidden.get("key_move")}
    headline = (f"Accuracy {acc:.0f}% over {n} {'move' if n == 1 else 'moves'}" if acc is not None
                else f"{n} {'move' if n == 1 else 'moves'} played")
    return {"headline": headline, "ended": ENDED.get(seg.end_reason or ""), "went_well": went_well,
            "work_on": work_on, "concepts": concepts, "reveal": reveal}
