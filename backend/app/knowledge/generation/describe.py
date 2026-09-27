"""Words for a generated position, built only from verified facts.

The text is filled in from the concept validator's facts (which piece stands where,
what it attacks) — never from a model's description of the position. The pipeline's
explanation stage then re-checks every piece/square/move the text mentions.
"""
from __future__ import annotations


_VALUE = {"pawn": 1, "knight": 3, "bishop": 3, "rook": 5, "queen": 9, "king": 100}


def _p(fact: dict, with_color: bool = True) -> str:
    return f"{fact['color'] + ' ' if with_color else ''}{fact['piece']} on {fact['square']}"


def _side(color: str) -> str:
    return color.capitalize()


def describe(motif: str, facts: dict, key_label: str, side: str) -> dict:
    """{title, description, explanation, prompt, hints, tags} for one motif."""
    you, them = _side(side), _side("black" if side == "white" else "white")
    if motif.endswith("_fork"):
        f = facts
        a = f["attacker"]
        t1, t2 = sorted(f["targets"], key=lambda t: (t["piece"] != "king", -_VALUE.get(t["piece"], 0)))[:2]
        piece = a["piece"]
        won = next((t["piece"] for t in (t1, t2) if t["piece"] != "king"), t2["piece"])
        return {
            "title": f"{piece.capitalize()} fork: win the {won}",
            "description": f"{you} to move. One {piece} move can attack two {them.lower()} pieces at once.",
            "explanation": (f"{key_label} puts the {piece} on {a['square']}, where it attacks the {_p(t1)} and the "
                            f"{_p(t2)} at the same time. {them} can only save one of them"
                            + (" — and the king has to get out of check first." if f.get("gives_check") else ".")),
            "prompt": f"{you} to move. Find the {piece} move that attacks two things at once.",
            "hints": [f"Look at every square your {piece} can reach in one move.",
                      f"Which {them.lower()} pieces are not protected, or are worth more than your {piece}?"],
            "tags": ["fork", f"{piece} fork", "generated"],
        }
    if motif == "hanging_piece":
        c = facts["captured"]
        return {
            "title": f"Free {c['piece']}: take it",
            "description": f"{you} to move. Is anything of {them}'s simply undefended?",
            "explanation": (f"The {_p(c)} is not defended by anything, so {key_label} wins it for free. Before every "
                            "move, check which enemy pieces are attacked and not protected."),
            "prompt": f"{you} to move. Can you win material for free?",
            "hints": ["Look at every capture you can make.", "For each capture, ask: can my opponent take back?"],
            "tags": ["hanging piece", "free piece", "generated"],
        }
    if motif in ("back_rank_mate", "support_mate"):
        king = facts.get("king", "")
        checker = (facts.get("checkers") or [{}])[0]
        if motif == "back_rank_mate":
            boxed = facts.get("boxed_in_by") or []
            text = (f"The {facts['mated_side']} king on {king} is boxed in by its own pawns"
                    + (f" (on {', '.join(b['square'] for b in boxed[:3])})" if boxed else "")
                    + f". {key_label} gives check along the back rank, and nothing can block it or take the "
                      f"{checker.get('piece', 'piece')}: checkmate.")
            title, tags = "Back-rank mate in one", ["back-rank mate", "mate in one", "generated"]
            hints = ["The enemy king has no room to breathe: its own pawns block it.",
                     "Look for a check on the king's back rank."]
        else:
            text = (f"{key_label} puts the queen right next to the {facts['mated_side']} king on {king}. Your king "
                    "protects the queen, so it can't be taken, and every escape square is covered: checkmate.")
            title, tags = "Queen and king: mate in one", ["queen mate", "mate in one", "generated"]
            hints = ["The enemy king is on the edge of the board.",
                     "A queen next to the king is checkmate when your own king protects it."]
        return {"title": title, "description": f"{you} to move and checkmate in one move.", "explanation": text,
                "prompt": f"{you} to move. Checkmate in one move.", "hints": hints, "tags": tags}
    if motif == "material_mate":
        checker = (facts.get("checkers") or [{}])[0]
        piece = checker.get("piece", "piece")
        covered = [e for e in facts.get("escape_squares", []) if "covered_by" in e]
        by_type: dict[str, set] = {}
        for e in covered:
            if e["covered_by"]["piece"] != "king":
                by_type.setdefault(e["covered_by"]["piece"], set()).add(e["covered_by"]["square"])
        guards = [p + ("s" if len(sq) > 1 else "") for p, sq in sorted(by_type.items())]
        plural = len(guards) > 1 or any(g.endswith("s") for g in guards)
        guard_text = (f" Your {' and '.join(guards)} cover{'' if plural else 's'} the escape squares"
                      if guards else " Every escape square is covered")
        return {
            "title": "Checkmate in one: pieces working together",
            "description": f"{you} to move and checkmate in one move.",
            "explanation": (f"After {key_label} the {piece} gives check, and the {facts['mated_side']} king on "
                            f"{facts['king']} has nowhere to go.{guard_text}, so it's checkmate. Mates like this "
                            "happen on the edge, where the king has fewest squares."),
            "prompt": f"{you} to move. Find the checkmate — only one move works.",
            "hints": ["The enemy king is on the edge: count the squares it could escape to.",
                      "Look for a check that also leaves every escape square covered."],
            "tags": ["checkmate", "mate in one", "generated"],
        }
    if motif == "threat":
        t = facts["threat"]
        return {
            "title": f"Spot the threat: save the {t['target']['piece']}",
            "description": f"{you} to move. Before you do anything else: what is {them} threatening?",
            "explanation": (f"The {_p(t['attacker'])} attacks the {_p(t['target'])}, and it isn't protected well "
                            f"enough. {key_label} deals with the threat. Ask \"what does my opponent want?\" before "
                            "every move — most lost pieces are pieces that were already attacked."),
            "prompt": f"{you} to move. {them} is threatening something. Find the move that deals with it.",
            "hints": [f"Which of your pieces are attacked by {them}?",
                      "Move the attacked piece to a safe square, or protect it."],
            "tags": ["threats", "spotting threats", "generated"],
        }
    if motif == "skewer":
        a, front, back = facts["attacker"], facts["front"], facts["back"]
        return {
            "title": f"Skewer: win the {back['piece']}",
            "description": f"{you} to move. Two {them.lower()} pieces stand on the same line.",
            "explanation": (f"{key_label} brings the {a['piece']} to {a['square']}, where it attacks the "
                            f"{_p(front)}. When it moves out of the way, the {_p(back)} behind it is lost."),
            "prompt": f"{you} to move. Win material with a skewer.",
            "hints": ["Look for two enemy pieces on one line: rank, file or diagonal.",
                      "Attack the more valuable one — the piece behind it is the real target."],
            "tags": ["skewer", "generated"],
        }
    if motif == "absolute_pin":
        pinner, pinned, behind = facts["pinner"], facts["pinned"], facts["behind"]
        return {
            "title": f"Pin the {pinned['piece']}",
            "description": f"{you} to move. A {them.lower()} piece stands in front of its "
                           f"{'king' if behind['piece'] == 'king' else behind['piece']}.",
            "explanation": (f"{key_label} pins the {_p(pinned)} to the {_p(behind)}: if it moves, the "
                            f"{behind['piece']} behind it is exposed"
                            + (" — so it may not move at all." if behind["piece"] == "king" else ".")
                            + f" {them} loses material."),
            "prompt": f"{you} to move. Find the pin that wins material.",
            "hints": ["Look for a piece standing in front of a more valuable one on the same line.",
                      "Put your rook, bishop or queen on that line."],
            "tags": ["pin", "generated"],
        }
    if motif == "opposition":
        return {
            "title": "King and pawn: take the opposition",
            "description": f"{you} to move. Only one move keeps the win.",
            "explanation": (f"{key_label} takes the opposition: the kings stand face to face with one square between "
                            f"them and {them} to move. {them}'s king has to give way, and your king leads the pawn "
                            "forward."),
            "prompt": f"{you} to move. Win this king and pawn ending: which king move keeps the win?",
            "hints": ["In king and pawn endings, the king goes first — the pawn follows.",
                      "Put your king directly opposite the enemy king, with one square between them."],
            "tags": ["opposition", "king and pawn", "generated"],
        }
    raise KeyError(motif)
