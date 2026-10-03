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
                "boden_mate", "ladder_mate", "queen_mate", "rook_mate", "scholars_mate", "early_mate",
                "mate_in_two", "hook_mate", "dovetail_mate", "double_bishop_mate", "epaulette_mate"):
        return "Checkmate! " + mate_sentence(f)
    if kind == "promotion":
        return f"The pawn promotes to a {f['promoted_to']} on {f['square']}."
    if kind == "sacrifice":
        return f"A sacrifice: {f['material_given']} points of material are given up on purpose."
    if kind == "attraction":
        return (f"Decoy! The {_p(f['offered'])} is offered, so the {f['lured']['piece']} has to come to "
                f"{f['square']} — and there it runs into {f['follow_up']}.")
    if kind == "clearance":
        return (f"Clearance: the {_p(f['clearing_piece'])} gets out of the way, so the {_p(f['used_by'])} "
                f"can use {f['cleared_square']} next.")
    if kind == "interference":
        return (f"Interference: the piece on {f['cut_square']} cuts the {_p(f['blocked_piece'])} off from "
                f"{f['guarded_square']}, which is no longer defended.")
    if kind == "underpromotion":
        why = (" A queen there would be stalemate." if f.get("stalemate_if_queen")
               else " Here it does more than a queen would.")
        return f"Underpromotion! The pawn becomes a {f['promoted_to']} on {f['square']}." + why
    if kind == "outside_passed_pawn":
        return (f"The passed pawn on {f['outside_pawn']} is far from all the other pawns: the enemy king has to "
                f"go after it, and meanwhile the other king wins pawns on the far side ({f['king_wins_pawn']}).")
    if kind == "pawn_breakthrough":
        return (f"Breakthrough: {_list(f['sacrifices'])} give{'s' if len(f['sacrifices']) == 1 else ''} up "
                f"material to break the pawn chain, and {f['promotes']} promotes.")
    if kind == "x_ray":
        return (f"X-ray: the {_p(f['slider'])} is lined up through the {_p(f['through'])}. Once that piece is "
                f"gone, the line is open and {f['capture']} lands on {f['square']}.")
    if kind == "greek_gift":
        return (f"The Greek gift: {f['sacrifice']} gives up the bishop on {f['square']}, {f['king_takes']} "
                f"accepts it, and {f['knight_check']} brings the knight in with check.")
    if kind == "windmill":
        return (f"Windmill! {_list(f['discovered_checks'])}: the same piece moves again and again, uncovering "
                f"check each time, and grabs material on the way.")
    if kind == "desperado":
        return (f"Desperado: the {_p(f['desperado'])} can't be saved, so it takes the {_p(f['captured'])} "
                f"before it goes.")
    if kind == "zugzwang":
        return (f"Zugzwang: {f['move']} is a quiet move — no capture, no check. But now the opponent has to "
                f"move, and every move makes their position worse.")
    if kind == "perpetual_check":
        return (f"Perpetual check: {_list(f['checks'])}. {f['side']} is {f['behind_by']} points behind, but "
                f"the king can't get away from the checks, so the game is drawn.")
    if kind == "stalemate_trick":
        return (f"Stalemate trick: after {f['final_move']} {f['stalemated']} has no legal move and is not in "
                f"check. Stalemate is a draw, although {f['side']} was {f['behind_by']} points behind.")
    if kind == "stalemate_trap":
        return (f"Careful: {_list(f['stalemating_moves'])} would be stalemate. {f['key_move']} leaves the "
                f"opponent a move and keeps the win.")
    return None


def mate_sentence(f: dict) -> str:
    checkers = _list([_p(c) for c in f["checkers"]])
    blocked = [e["square"] for e in f.get("escape_squares", []) if "blocked_by" in e]
    covered = [e["square"] for e in f.get("escape_squares", []) if "covered_by" in e]
    text = f"The {f['mated_side']} king on {f['king']} is attacked by the {checkers}"
    if blocked:
        text += f", its own pieces block {_list(blocked)}"
    if covered:  # a semicolon, so the list of squares can't run into the previous clause
        return text + (f"; {_list(covered)} {'is' if len(covered) == 1 else 'are'} covered; and nothing can "
                       f"block or capture the checking piece.")
    return text + ", and nothing can block or capture the checking piece."


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
    elif kind == "attraction":
        out.append("Can you force an enemy piece onto a square where it gets hit? It may cost material.")
        out.append(f"Which enemy piece would you like to bring to {f['square']}?")
    elif kind == "clearance":
        out.append("One of your own pieces is in the way. Move it — with a threat if you can.")
        out.append(f"Which of your pieces would love to use {f['cleared_square']}?")
    elif kind == "interference":
        out.append(f"The {f['blocked_piece']['piece']} on {f['blocked_piece']['square']} guards "
                   f"{f['guarded_square']}. Can you put something in the way?")
    elif kind == "underpromotion":
        out.append("A pawn is about to promote — but is a queen really the best choice here?")
    elif kind == "outside_passed_pawn":
        out.append("Which of your pawns is furthest away from all the others? Use it to pull the enemy king away.")
    elif kind == "pawn_breakthrough":
        out.append("Your pawns face a wall of enemy pawns. Can a sacrifice open a path for one of them?")
    elif kind == "x_ray":
        out.append("One of your long-range pieces is aimed through an enemy piece. What if that piece disappears?")
        out.append(f"Look at the line from your {f['slider']['piece']} on {f['slider']['square']}.")
    elif kind == "greek_gift":
        out.append(f"Is the pawn on {f['square']} defended only by the king?")
        out.append("Bishop, knight, queen: can all three join the attack, one with check after the other?")
    elif kind == "windmill":
        out.append("One of your pieces can uncover a check. Can it do that more than once?")
    elif kind == "desperado":
        out.append("One of your pieces can't be saved anyway. Make it count before it goes.")
    elif kind == "zugzwang":
        out.append("There's no good capture or check. Can a quiet move leave your opponent without a good move?")
    elif kind == "perpetual_check":
        out.append("You are behind in material. Can you give check after check, without end?")
    elif kind == "stalemate_trick":
        out.append("You are lost on material. Could you end up with no legal move at all?")
    elif kind == "stalemate_trap":
        out.append("You are winning easily. After your move, will your opponent still have a legal move?")
    elif "escape_squares" in f:
        if kind == "mate_in_two":
            out.append("It's mate in two: start with a forcing move that leaves no defence.")
        out.append(f"The king on {f['king']} has almost no squares. Which check can't be answered?")
    elif kind == "promotion":
        out.append("A pawn is close to the last rank.")
    if not out:
        out.append("Look for checks, captures and threats first.")
    return out
