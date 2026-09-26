"""Teaching examples from real games: the Lichess puzzle database (CC0).

Sources: planner/data/puzzles.json (the planner's puzzle lessons) and the larger
sources/data/lichess_puzzles/pool.json. Both are puzzles from the Lichess database
that scripts/build_puzzle_library.py already checked move by move with Stockfish.
The planner's puzzles come first, so existing library entries stay stable when the
pool grows.
Each puzzle is a real position from a real game: the opponent's last move,
then the winning continuation.

Import steps (the "concept detection" and "teaching example generation" parts
of the pipeline): map the Lichess theme to a library concept, run the concept
validator to *find* the motif (which piece forks what), refine the concept from
those facts (a fork by a knight is a knight fork), and write the teaching text
from the facts with templates. The result is a candidate — the pipeline decides.
"""
from __future__ import annotations

import json
from pathlib import Path

import chess

from .. import teaching, validators
from ..positions import material, replay
from . import provenance

PUZZLES = Path(__file__).resolve().parents[2] / "planner" / "data" / "puzzles.json"
TOPICS = Path(__file__).resolve().parents[2] / "planner" / "data" / "topics.json"
POOL = Path(__file__).resolve().parent / "data" / "lichess_puzzles" / "pool.json"
LICENSE = "CC0-1.0"

# Lichess theme -> library concept (refined from the facts where possible)
THEMES = {
    "fork": "fork", "pin": "pin", "skewer": "skewer", "discoveredAttack": "discovered_attack",
    "doubleCheck": "double_check", "deflection": "deflection", "capturingDefender": "removing_defender",
    "hangingPiece": "hanging_piece", "trappedPiece": "trapped_piece", "sacrifice": "sacrifice",
    "intermezzo": "zwischenzug", "backRankMate": "back_rank_mate", "smotheredMate": "smothered_mate",
    "anastasiaMate": "anastasia_mate", "arabianMate": "arabian_mate", "bodenMate": "boden_mate",
    "mateIn1": "mate_in_one", "promotion": "promotion",
}


def _refine(concept: str, kind: str, facts: dict, ctx, library) -> tuple[str, str, dict]:
    """A more specific concept when the facts show one (fork by a knight → knight_fork)."""
    if concept == "fork":
        piece = facts["attacker"]["piece"]
        specific = {"knight": "knight_fork", "pawn": "pawn_fork", "queen": "queen_fork"}.get(piece)
        if specific in library.concepts:
            return specific, kind, facts
    if concept == "pin":
        specific = "absolute_pin" if facts["kind"] == "absolute" else "relative_pin"
        if specific in library.concepts:
            return specific, kind, facts
    if concept == "discovered_attack" and facts.get("is_check") and "discovered_check" in library.concepts:
        try:
            return "discovered_check", "discovered_check", validators.run("discovered_check", ctx, {})
        except validators.Fail:
            pass
    if concept == "deflection" and "overloaded_piece" in library.concepts:
        try:
            return "overloaded_piece", "overloaded_piece", validators.run("overloaded_piece", ctx, {})
        except validators.Fail:
            pass
    return concept, kind, facts


def _title(concept_name: str, kind: str, f: dict) -> str:
    if "targets" in f:
        return f"{concept_name}: {' and '.join(t['piece'] for t in f['targets'])}"
    if kind == "hanging_piece":
        return f"{concept_name}: the loose {f['captured']['piece']}"
    if kind == "trapped_piece":
        return f"{concept_name}: trapping the {f['trapped']['piece']}"
    if "pinned" in f:
        return f"{concept_name}: {f['pinned']['piece']} pinned to the {f['behind']['piece']}"
    if "front" in f:
        return f"{concept_name}: {f['front']['piece']} and {f['back']['piece']}"
    return concept_name


def _themes(path: Path | None, order) -> dict[str, list[dict]]:
    """{theme: puzzles}: the given file, or the planner's puzzles followed by the pool."""
    paths = [path] if path else [PUZZLES, POOL]
    merged: dict[str, list[dict]] = {}
    for p in paths:
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as fh:
            for theme, puzzles in json.load(fh)["themes"].items():
                known = {x["id"] for x in merged.get(theme, [])}
                merged.setdefault(theme, []).extend(sorted((x for x in puzzles if x["id"] not in known), key=order))
    return merged


def candidates(library, per_concept: int = 4, path: Path | None = None) -> list[dict]:
    themes = _themes(path, lambda p: (len(p["moves"]), p["id"]))
    with open(TOPICS, encoding="utf-8") as fh:
        topic_text = {t["puzzle_theme"]: t.get("puzzle", {}) for t in json.load(fh)["topics"] if t.get("puzzle_theme")}
    out: list[dict] = []
    per: dict[str, int] = {}
    for theme, concept in THEMES.items():
        for puzzle in themes.get(theme, []):
            cand = _candidate(puzzle, theme, concept, library, topic_text.get(theme, {}))
            if cand is None or per.get(cand["concept"], 0) >= per_concept:
                continue
            if any(c["source"]["source_id"] == puzzle["id"] for c in out):
                continue
            per[cand["concept"]] = per.get(cand["concept"], 0) + 1
            out.append(cand)
    return out


def _candidate(puzzle: dict, theme: str, concept: str, library, text: dict) -> dict | None:
    if concept not in library.concepts:
        return None
    board = chess.Board(puzzle["fen"])
    sans = []
    for uci in puzzle["moves"]:
        move = chess.Move.from_uci(uci)
        sans.append(board.san(move))
        board.push(move)
    rep = replay(puzzle["fen"], sans)
    key_ply = 1
    learner = rep.boards[key_ply].turn
    side = "White" if learner else "Black"
    spec = library.validator_for(concept)
    ctx = validators.Ctx(rep, key_ply=key_ply)
    kind, facts = spec.get("type"), None
    try:
        facts = validators.run(kind, ctx, {k: v for k, v in spec.items() if k != "type"})
    except validators.Fail:
        facts = None  # the pipeline will reject it and report why
    if facts is not None:
        concept, kind, facts = _refine(concept, kind, facts, ctx, library)
    name = library.concepts[concept].name
    learner_moves = len(range(key_ply, len(sans), 2))
    difficulty = {1: 2, 2: 3}.get(learner_moves, 4)
    if learner_moves == 1 and concept in ("hanging_piece", "mate_in_one"):
        difficulty = 1
    notes = {rep.labels[0]: "The opponent's last move. Something is now possible — look closely."}
    note = teaching.key_note(kind, facts) if facts else None
    if note:
        notes[rep.labels[key_ply]] = note
    final = rep.final
    start = rep.boards[key_ply]
    gained = (material(final, learner) - material(final, not learner)) - \
             (material(start, learner) - material(start, not learner))
    if final.is_checkmate():
        outcome = "The line ends in checkmate."
        if len(sans) - 1 > key_ply:
            notes.setdefault(rep.labels[-1], "Checkmate!")
    elif gained > 0:
        outcome = f"{side} comes out {gained} point{'s' if gained != 1 else ''} of material ahead."
        notes.setdefault(rep.labels[-1], outcome)
    else:
        outcome = ""
    task = text.get("task") or f"Find the {name.lower()}."
    explanation = " ".join(x for x in (library.concepts[concept].summary, note or "", outcome) if x)
    return {
        "id": f"lichess_{puzzle['id'].lower()}",
        "title": _title(name, kind, facts) if facts else name,
        "concept": concept,
        "category": library.concepts[concept].category,
        "subcategory": theme,
        "difficulty": difficulty,
        "description": f"A position from a real Lichess game. {side} to move: {task}",
        "start_fen": puzzle["fen"],
        "moves": sans,
        "key_move": rep.labels[key_ply],
        "prompt": f"What would you play here? {task}",
        "hints": teaching.hints(kind, facts or {}, text.get("hint")),
        "notes": notes,
        "explanation": explanation,
        "tags": [theme, "lichess", "real game", f"{learner_moves}-move"],
        "source": provenance("lichess_puzzle", puzzle["id"], LICENSE,
                             url=f"https://lichess.org/training/{puzzle['id']}",
                             reference="Lichess puzzle database (database.lichess.org)"),
    }


# Lichess theme -> mistake concept: the puzzle's setup move is the opponent's
# mistake, the puzzle solution is its punishment.
MISTAKE_THEMES = {"fork": "walked_into_fork", "hangingPiece": "hung_piece"}


def mistake_candidates(library, per_concept: int = 3, exclude: set[str] | None = None,
                       path: Path | None = None) -> list[dict]:
    """Mistake examples ("walking into a fork", "hanging a piece") from real games.

    `exclude` holds puzzle ids already used for tactic entries, so one position
    doesn't enter the library twice. Longer puzzles are tried first, the tactic
    importer takes the shortest ones."""
    exclude = exclude or set()
    themes = _themes(path, lambda p: (-len(p["moves"]), p["id"]))
    out: list[dict] = []
    for theme, concept in MISTAKE_THEMES.items():
        if concept not in library.concepts:
            continue
        taken = 0
        for puzzle in themes.get(theme, []):
            if taken >= per_concept:
                break
            if puzzle["id"] in exclude:
                continue
            cand = _mistake_candidate(puzzle, theme, concept, library)
            if cand is not None:
                out.append(cand)
                taken += 1
    return out


def _mistake_candidate(puzzle: dict, theme: str, concept: str, library) -> dict | None:
    board = chess.Board(puzzle["fen"])
    sans = []
    for uci in puzzle["moves"]:
        move = chess.Move.from_uci(uci)
        sans.append(board.san(move))
        board.push(move)
    rep = replay(puzzle["fen"], sans)
    ctx = validators.Ctx(rep, key_ply=1, mistake_ply=0)
    spec = library.validator_for(concept)
    try:
        facts = validators.run(spec["type"], ctx, {k: v for k, v in spec.items() if k != "type"})
    except validators.Fail:
        return None  # the importer only proposes positions that show the mistake
    mistaker = "White" if rep.boards[0].turn else "Black"
    punisher = "Black" if rep.boards[0].turn else "White"
    mistake, punish = rep.labels[0], rep.labels[1]
    if concept == "walked_into_fork":
        fork = facts["fork"]
        targets = teaching._list([teaching._p(t) for t in fork["targets"]])
        title = f"Walking into a fork: {' and '.join(t['piece'] for t in fork['targets'])}"
        mistake_note = f"The mistake: now the {fork['attacker']['piece']} can attack two pieces at once."
        others = [t["piece"] for t in fork["targets"] if t["piece"] != "king"]
        if len(others) < len(fork["targets"]) and others:
            result = f"the king has to get out of check, so the {teaching._list(others)} is lost"
        else:
            result = "only one of them can be saved"
        lesson = (f"{mistaker} played {mistake} and walked into a fork: the {teaching._p(fork['attacker'])} "
                  f"attacks the {targets}, and {result}. Before every move, check where your opponent's "
                  f"pieces can go next, and whether one square would attack two of your pieces at once.")
        punish_note = teaching.key_note("fork", fork)
        hints = teaching.hints("fork", fork)
    else:
        hung = facts["hung"]
        title = f"Hanging a piece: the loose {hung['piece']}"
        mistake_note = f"The mistake: after this move the {teaching._p(hung)} can be won."
        lesson = (f"After {mistake} the {teaching._p(hung)} could be captured, and {punisher} took it. "
                  f"Before every move, ask: after I play this, which of my pieces are attacked, and are "
                  f"they defended enough?")
        punish_note = f"{punisher} wins the {hung['piece']}."
        hints = [f"Which {mistaker.lower()} piece is attacked and not defended enough?",
                 f"Look at the {hung['piece']} on {hung['square']}."]
    notes = {mistake: mistake_note, punish: punish_note}
    final = rep.final
    lost = facts["material_lost"]
    if final.is_checkmate():
        notes.setdefault(rep.labels[-1], "Checkmate!")
    elif len(sans) > 2:
        notes.setdefault(rep.labels[-1], f"{punisher} comes out {lost} point{'s' if lost != 1 else ''} of material ahead.")
    return {
        "id": f"lichess_{puzzle['id'].lower()}_mistake",
        "title": title,
        "concept": concept,
        "category": library.concepts[concept].category,
        "subcategory": theme,
        "difficulty": 2 if len(sans) <= 3 else 3,
        "description": f"A position from a real Lichess game. {mistaker} is about to make a mistake.",
        "start_fen": puzzle["fen"],
        "moves": sans,
        "mistake_move": mistake,
        "key_move": punish,
        "prompt": f"{mistaker} just played {mistake}. You are {punisher}: how do you punish it?",
        "hints": hints,
        "notes": notes,
        "explanation": lesson,
        "tags": [theme, "mistake", "lichess", "real game"],
        "source": provenance("lichess_puzzle", puzzle["id"], LICENSE,
                             url=f"https://lichess.org/training/{puzzle['id']}",
                             reference="Lichess puzzle database (database.lichess.org)"),
    }
