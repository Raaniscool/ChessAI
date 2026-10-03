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
# longer solutions and combinations, so stronger learners get positions that stretch them
POOL_HARDER = Path(__file__).resolve().parent / "data" / "lichess_puzzles" / "pool_harder.json"
# the item-9 expansion: decoys, clearance, interference, underpromotion, more mates, pawn endings
# and real-game mistakes. pool_ideas.json was pre-filtered with the concept validators so the
# Stockfish build only checked positions that show the idea.
POOL_EXPANSION = Path(__file__).resolve().parent / "data" / "lichess_puzzles" / "pool_expansion.json"
POOL_IDEAS = Path(__file__).resolve().parent / "data" / "lichess_puzzles" / "pool_ideas.json"
POOL_FINISH = Path(__file__).resolve().parent / "data" / "lichess_puzzles" / "pool_finish.json"
EXPANSION_DATE = "2026-10-02"
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


def _themes(path: Path | None, order, paths: list[Path] | None = None) -> dict[str, list[dict]]:
    """{theme: puzzles}: the given file, or the planner's puzzles followed by the pool."""
    paths = [path] if path else (paths or [PUZZLES, POOL])
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


# Lichess theme -> concepts tried in order; the first whose validator finds the idea wins.
EXPANSION_THEMES = {
    "attraction": ["attraction"], "clearance": ["clearance"], "interference": ["interference"],
    "underPromotion": ["underpromotion"], "mateIn2": ["mate_in_two"], "hookMate": ["hook_mate"],
    "dovetailMate": ["dovetail_mate"],
    "pawnEndgame": ["outside_passed_pawn", "pawn_breakthrough"],
    "zugzwang": ["outside_passed_pawn", "pawn_breakthrough"],
    # finishing item 9: pools pre-filtered with the concept's own validator (pool_finish.json)
    "xRay": ["x_ray"], "doubleBishopMate": ["double_bishop_mate"], "epauletteMate": ["epaulette_mate"],
    "greekGift": ["greek_gift"], "windmill": ["windmill"], "desperado": ["desperado"],
    "zugzwangQuiet": ["zugzwang"], "perpetual": ["perpetual_check"], "stalemateTrick": ["stalemate_tricks"],
    "threatDefence": ["spotting_threats"], "doubleAttack": ["double_attack"], "pawnFork": ["pawn_fork"],
    "relativePin": ["relative_pin"],
}
# the learner's task for concepts without a topic text ("Find the decoy.")
TASKS = {
    "attraction": "Find the decoy.", "clearance": "Clear the way for another piece.",
    "interference": "Cut the defender off.", "underpromotion": "Find the best promotion.",
    "mate_in_two": "Find mate in two.", "hook_mate": "Find the checkmate.", "dovetail_mate": "Find the checkmate.",
    "outside_passed_pawn": "Find the winning plan.", "pawn_breakthrough": "Find the winning plan.",
    "x_ray": "Find the x-ray.", "double_bishop_mate": "Find the checkmate.", "epaulette_mate": "Find the checkmate.",
    "greek_gift": "Attack the king.", "windmill": "Find the windmill.", "desperado": "Find the best move.",
    "zugzwang": "Find the quiet winning move.", "perpetual_check": "Save the game.",
    "stalemate_tricks": "Save the game.", "spotting_threats": "What does your opponent threaten? Stop it.",
    "double_attack": "Attack two things at once.", "pawn_fork": "Find the pawn fork.",
    "relative_pin": "Find the pin.",
}


def expansion_candidates(library, per_concept: int = 6, exclude: set[str] | None = None,
                         paths: list[Path] | None = None) -> list[dict]:
    """Candidates for the item-9 concepts from the expansion pools (simplest first)."""
    exclude = exclude or set()
    themes = _themes(None, lambda p: (len(p["moves"]), p["id"]), paths or [POOL_EXPANSION, POOL_IDEAS, POOL_FINISH])
    out: list[dict] = []
    per: dict[str, int] = {}
    used: set[str] = set()
    for theme, concepts in EXPANSION_THEMES.items():
        for puzzle in themes.get(theme, []):
            if puzzle["id"] in exclude or puzzle["id"] in used:
                continue
            for concept in concepts:
                if per.get(concept, 0) >= per_concept or concept not in library.concepts:
                    continue
                cand = _candidate(puzzle, theme, concept, library, {"task": TASKS.get(concept)},
                                  require_facts=True, import_date=EXPANSION_DATE)
                if cand is None:
                    continue
                per[concept] = per.get(concept, 0) + 1
                used.add(puzzle["id"])
                out.append(cand)
                break
    return out


def _candidate(puzzle: dict, theme: str, concept: str, library, text: dict, require_facts: bool = False,
               import_date: str | None = None) -> dict | None:
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
        if require_facts:
            return None  # the expansion only proposes positions that show the idea
    if facts is not None and concept == "spotting_threats" and (
            rep.boards[key_ply + 1].is_check() or rep.final.is_checkmate()):
        return None  # answering a threat with a check or a mate is not the defensive lesson
    if facts is not None:
        concept, kind, facts = _refine(concept, kind, facts, ctx, library)
    name = library.concepts[concept].name
    learner_moves = len(range(key_ply, len(sans), 2))
    difficulty = {1: 2, 2: 3}.get(learner_moves, 4)
    if learner_moves == 1 and concept in ("hanging_piece", "mate_in_one"):
        difficulty = 1
    notes = {rep.labels[0]: "The opponent's last move. Something is now possible — look closely."}
    note = teaching.key_note(kind, facts) if facts else None
    if note and note.startswith("Checkmate! ") and not rep.boards[key_ply + 1].is_checkmate():
        # a mate in several moves: the mate is described where it happens, not at the first move
        notes[rep.labels[-1]] = note
        mate = note[len("Checkmate! "):]
        note = ("This starts a forced checkmate: whatever the reply, mate follows. In the final position, "
                + mate[0].lower() + mate[1:])
        notes[rep.labels[key_ply]] = "This starts a forced checkmate: whatever the reply, mate follows."
    elif note:
        notes[rep.labels[key_ply]] = note
    final = rep.final
    start = rep.boards[key_ply]
    gained = (material(final, learner) - material(final, not learner)) - \
             (material(start, learner) - material(start, not learner))
    if final.is_stalemate():
        outcome = "The line ends in stalemate: a draw."
        notes.setdefault(rep.labels[-1], "Stalemate: a draw.")
    elif concept == "perpetual_check":
        outcome = "The game is drawn."
    elif final.is_checkmate():
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
                             reference="Lichess puzzle database (database.lichess.org)",
                             **({"import_date": import_date} if import_date else {})),
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


# the expansion's real-game mistakes: the setup move ignores the back rank, or ignores a threat
# that was already there (attacks on f2/f7 and on an exposed king). These Lichess themes are not
# imported as "weakening the king": their setup moves are mostly king walks into skewers or moves
# elsewhere on the board, not weakened king shelters.
EXPANSION_MISTAKE_THEMES = {"backRankMate": "back_rank_weakness", "attackingF2F7": "missed_threat",
                            "exposedKing": "missed_threat", "hangingQueen": "hanging_queen",
                            "poisonedPawn": "poisoned_pawn"}
# (no "early queen" from Lichess: a puzzle starts mid-game, so it can't show that the queen came out
# early — the one candidate was a pinned queen. Early-queen examples are curated whole games.)


def _threat_was_real(puzzle: dict) -> bool:
    """Stricter than the validator, for imports: had the mistaken side passed instead, the same
    reply would have done the same damage (mate, or the same piece captured)."""
    board = chess.Board(puzzle["fen"])
    mistake, reply = (chess.Move.from_uci(u) for u in puzzle["moves"][:2])
    passed = board.copy(stack=False)
    passed.push(chess.Move.null())
    if reply not in passed.legal_moves:
        return False
    actual = board.copy(stack=False)
    actual.push(mistake)
    side = board.turn
    for uci in puzzle["moves"][1:]:  # the whole punishment must work without the mistake too
        move = chess.Move.from_uci(uci)
        if move not in passed.legal_moves:
            return False
        actual.push(move)
        passed.push(move)
    if actual.is_checkmate():
        return passed.is_checkmate()
    return (material(passed, side) - material(passed, not side)) <= \
        (material(actual, side) - material(actual, not side))


def expansion_mistake_candidates(library, per_concept: int = 6, exclude: set[str] | None = None,
                                 paths: list[Path] | None = None) -> list[dict]:
    exclude = exclude or set()
    themes = _themes(None, lambda p: (-len(p["moves"]), p["id"]), paths or [POOL_IDEAS, POOL_FINISH])
    out: list[dict] = []
    taken: dict[str, int] = {}
    for theme, concept in EXPANSION_MISTAKE_THEMES.items():
        if concept not in library.concepts:
            continue
        for puzzle in themes.get(theme, []):
            if taken.get(concept, 0) >= per_concept:
                break
            if puzzle["id"] in exclude or any(c["source"]["source_id"] == puzzle["id"] for c in out):
                continue
            if concept == "missed_threat" and not _threat_was_real(puzzle):
                continue
            cand = _mistake_candidate(puzzle, theme, concept, library, import_date=EXPANSION_DATE)
            if cand is not None:
                out.append(cand)
                taken[concept] = taken.get(concept, 0) + 1
    return out


def _mistake_candidate(puzzle: dict, theme: str, concept: str, library, import_date: str | None = None) -> dict | None:
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
    elif concept == "back_rank_weakness":
        mate = facts["mate"]
        title = "Ignoring the back rank"
        mistake_note = f"The mistake: the {mistaker.lower()} king on {mate['king']} has no escape square."
        lesson = (f"After {mistake} the {mistaker.lower()} king was stuck on its back rank, and {punisher} "
                  f"mated there. Before you move a piece that guards your back rank, check whether a rook or "
                  f"queen could give a check your king can't escape — and give it an escape square in time.")
        punish_note = f"{punisher} goes for the back rank."
        hints = [f"The {mistaker.lower()} king on {mate['king']} has no escape square.",
                 "Which check on the back rank can't be answered?"]
    elif concept == "missed_threat":
        threat = rep.sans[1]
        ending = "and it ends in checkmate" if facts["ends_in_mate"] else \
            f"and {punisher} wins {facts['material_lost']} points of material"
        title = "Missing a threat: checkmate" if facts["ends_in_mate"] else "Missing a threat"
        mistake_note = f"The mistake: {mistaker} plays on as if nothing were threatened."
        lesson = (f"Before {mistake}, {punisher} was already threatening {threat}. {mistaker} ignored it, "
                  f"{punisher} carried out the threat, {ending}. Before every move, ask: what does my "
                  f"opponent threaten right now?")
        punish_note = f"{punisher} carries out the threat."
        hints = ["Your opponent just ignored something you were threatening. What was it?",
                 "Look for checks and captures first."]
    elif concept == "poisoned_pawn":
        grabbed = facts["grabbed"]
        ending = "and it ends in checkmate" if facts["ends_in_mate"] else \
            f"and in the end {mistaker} is {facts['material_lost']} points down"
        title = "Grabbing a poisoned pawn"
        mistake_note = f"The mistake: {mistaker} grabs the {teaching._p(grabbed)}."
        lesson = (f"{mistaker} took the pawn with {mistake}, {punisher} answered {punish}, {ending}. A pawn "
                  f"is not free if taking it costs time, opens lines or leaves a piece loose: before you grab, "
                  f"look at your opponent's best reply.")
        punish_note = f"{punisher} punishes the pawn grab."
        hints = [f"{mistaker} just grabbed a pawn. What did that cost?", "Look for checks, captures and threats."]
    elif concept == "early_queen":
        chased = facts["queen_chased_by"]
        title = "Bringing the queen out too early"
        mistake_note = f"The mistake: the {mistaker.lower()} queen comes out early."
        result = "and the queen is lost" if facts["queen_lost"] else \
            f"and the queen is chased with {teaching._list(chased)} while {punisher} develops"
        lesson = (f"{mistaker} played {mistake} in the opening {result}. Develop knights and bishops first: a "
                  f"queen out early is a target for the opponent's pieces.")
        punish_note = f"{punisher} attacks the queen and gains time."
        hints = ["The queen came out very early. Can you attack it while developing?"]
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
    lost = facts.get("material_lost", 0)
    if final.is_checkmate():
        notes.setdefault(rep.labels[-1], "Checkmate!")
    elif len(sans) > 2 and lost > 0:
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
                             reference="Lichess puzzle database (database.lichess.org)",
                             **({"import_date": import_date} if import_date else {})),
    }
