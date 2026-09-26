"""Deterministic teaching text from verified facts (no LLM needed).

Importers use these templates to turn a source position plus its validator
facts into titles, notes, hints and explanations. Because the text is built
from the facts, it cannot contradict the board — and the pipeline's
explanation check verifies that anyway. Qwen can later *rephrase* or deepen
this text for a particular learner, always from the same facts.
"""
from __future__ import annotations


def _p(f: dict) -> str:
    return f"{f['piece']} on {f['square']}"


def _side(color: str) -> str:
    return color.capitalize()


def _list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def key_note(kind: str, f: dict) -> str | None:
    if kind == "fork":
        text = f"Fork! The {_p(f['attacker'])} attacks the {_list([_p(t) for t in f['targets']])} at once."
        return text + (" It's check, so the king must move first." if f.get("gives_check") else "")
    if kind == "pin":
        return (f"The {_p(f['pinner'])} pins the {_p(f['pinned'])} to the {_p(f['behind'])}"
                + (" — the pinned piece can't legally move." if f["kind"] == "absolute" else "."))
    if kind == "skewer":
        return (f"Skewer! The {_p(f['attacker'])} attacks the {_p(f['front'])}; when it moves, the "
                f"{_p(f['back'])} behind it falls.")
    if kind == "discovered_attack":
        return (f"The {_p(f['moved_piece'])} steps aside and uncovers the {_p(f['revealed_attacker'])}, "
                f"which now attacks the {_p(f['target'])}.")
    if kind == "discovered_check":
        return f"Discovered check: moving the {_p(f['moved_piece'])} reveals check from the {_p(f['checking_piece'])}."
    if kind == "double_check":
        return (f"Double check from the {_list([_p(c) for c in f['checkers']])} — the only defence is a king move.")
    if kind == "hanging_piece":
        return f"The {_p(f['captured'])} was {'defended too cheaply' if f['was_defended'] else 'undefended'} — take it!"
    if kind == "trapped_piece":
        return f"The {_p(f['trapped'])} is trapped: every square it can go to is covered."
    if kind in ("checkmate", "back_rank_mate", "smothered_mate", "anastasia_mate", "arabian_mate",
                "boden_mate", "ladder_mate", "queen_mate", "rook_mate", "scholars_mate", "early_mate"):
        return "Checkmate! " + mate_sentence(f)
    if kind == "promotion":
        return f"The pawn promotes to a {f['promoted_to']} on {f['square']}."
    if kind == "sacrifice":
        return f"A sacrifice: {f['material_given']} points of material are given up on purpose."
    return None


def mate_sentence(f: dict) -> str:
    checkers = _list([_p(c) for c in f["checkers"]])
    blocked = [e["square"] for e in f.get("escape_squares", []) if "blocked_by" in e]
    covered = [e["square"] for e in f.get("escape_squares", []) if "covered_by" in e]
    parts = [f"The {f['mated_side']} king on {f['king']} is attacked by the {checkers}"]
    if blocked:
        parts.append(f"its own pieces block {_list(blocked)}")
    if covered:
        parts.append(f"{_list(covered)} {'is' if len(covered) == 1 else 'are'} covered")
    return ", ".join(parts) + ", and nothing can block or capture the checking piece."


def hints(kind: str, f: dict, concept_hint: str | None = None) -> list[str]:
    """Progressive hints: idea → piece → target. Never the move itself."""
    out = []
    if concept_hint:
        out.append(concept_hint)
    if not f:  # no verified facts (the candidate will be rejected): only generic hints
        return out or ["Look for checks, captures and threats first."]
    if kind == "fork":
        out.append(f"Your {f['attacker']['piece']} can attack two things at once.")
        out.append("Find a square that hits the " + _list([t["piece"] for t in f["targets"]]) + ".")
    elif kind == "pin":
        out.append(f"Look for a line through the {f['pinned']['piece']} to the {f['behind']['piece']} behind it.")
    elif kind == "skewer":
        out.append(f"The {f['front']['piece']} and the {f['back']['piece']} stand on one line.")
    elif kind in ("discovered_attack", "discovered_check"):
        out.append("One of your pieces is hiding another one. Move the front piece with a threat.")
    elif kind == "double_check":
        out.append("Can you give check with two pieces at the same time?")
    elif kind == "hanging_piece":
        out.append(f"Is the {f['captured']['piece']} on {f['captured']['square']} really protected?")
    elif kind == "trapped_piece":
        out.append(f"The {f['trapped']['piece']} has very few squares. Take them away.")
    elif "escape_squares" in f:
        out.append(f"The king on {f['king']} has almost no squares. Which check can't be answered?")
    elif kind == "promotion":
        out.append("A pawn is close to the last rank.")
    if not out:
        out.append("Look for checks, captures and threats first.")
    return out
