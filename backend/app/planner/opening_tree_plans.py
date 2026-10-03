"""Opening lessons from the verified opening trees (knowledge/opening_trees.py).

A request about an opening is taught as ONE branch of its tree, never as the whole tree or one
giant sequence:

  "teach me the Grünfeld"           the opening's main branch, as deep as the learner's level
  "Sicilian Dragon", "Najdorf"      that named variation, continued along its main theory
  "Petrov as a beginner"            a shorter branch (the first named milestone deep enough)

How long the line is comes from the tree itself (where its named positions are), so the Scandinavian
main line is short and a Najdorf line for an advanced player is long (variable-length material).

The plan has up to three units, all from build-time-verified data:
  1. the branch: moves and ideas, then play the whole branch from memory (the existing opening
     lesson format), with where the branch fits and the main alternatives;
  2. traps in this opening that share the branch's first moves (verified by Stockfish at build time);
  3. a historical model game that reaches the branch (replayed and checked at build time).

When the catalog or the Knowledge Library already teaches exactly what was asked (the Italian Game,
a library-covered variation, the catalog's Sicilian line), those planners answer as before; the tree
then only adds "other branches" suggestions (attach_branches).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import chess

from ..knowledge.opening_trees import Branch, Match, OpeningTree, get_opening_trees
from ..lessons.schema import parse_lesson
from .generator import _format_line, _line_exercise, _move_label, move_hints, opening_lessons

MAX_TRAPS = 2


def _covered_elsewhere(match: Match, catalog=None, library=None) -> bool:
    """Does the catalog or the verified library already teach exactly this?"""
    tree = match.tree
    if match.node is None:
        return bool(tree.catalog_topic or tree.library_concept)
    path = tree.sans(match.node)
    if tree.catalog_topic:
        from .catalog import get_catalog
        topic = (catalog or get_catalog()).get(tree.catalog_topic)
        if topic is not None and list(topic.line or [])[:len(path)] == path:
            return True
    if tree.library_concept:
        if library is None:
            from ..knowledge.library import get_knowledge
            library = get_knowledge()
        for entry in library.verified():
            moves = list(entry.moves or [])
            if entry.category == "openings" and entry.start_fen in (None, "", "startpos", chess.STARTING_FEN) \
                    and moves[:len(path)] == path:
                return True
    return False


def find_branch_request(goal: str, catalog=None, library=None, force: bool = False) -> Match | None:
    """The opening-tree match for a request this module should answer (None: let others answer)."""
    trees = get_opening_trees()
    if not len(trees):
        return None
    match = trees.find(goal)
    if match is None:
        return None
    if not force and _covered_elsewhere(match, catalog, library):
        return None
    return match


def _learner_side(goal: str, tree: OpeningTree) -> str:
    words = goal.lower().split()
    if "as" in words:
        i = words.index("as")
        if i + 1 < len(words) and words[i + 1] in ("white", "black"):
            return words[i + 1]
    return tree.side


def _where_it_fits(branch: Branch) -> str:
    parts = []
    if len(branch.milestones) > 1:
        names = [n for _p, n in branch.milestones]
        parts.append("On the way you pass: " + " → ".join(_short(n) for n in names) + ".")
    for alt in branch.alternatives:
        label = _move_label(alt["ply"] - 1, alt["san"])
        who = "Your opponent can also play" if alt["by"] == "opponent" else "You could also play"
        parts.append(f"{who} {label} ({_short(alt['name'])}).")
    if branch.alternatives:
        parts.append("Each of those is its own branch — ask for it by name to learn it.")
    return " ".join(parts)


def _short(name: str) -> str:
    return name.split(": ", 1)[1] if ": " in name else name


def _verified_note(branch: Branch) -> str:
    tree = branch.tree
    c = tree.counts
    depth = tree.verification.get("depth", 14)
    note = (f"Verified: the moves come from the Lichess opening database (CC0) and every position in the "
            f"{tree.title} tree ({c.get('positions', 0)} positions) was checked by Stockfish at depth {depth}; "
            f"every move you play in this branch is rated sound.")
    if branch.engine_plies:
        note += (f" The last {branch.engine_plies} move{'s' if branch.engine_plies != 1 else ''} of this line "
                 "continue the named theory with Stockfish's best moves.")
    return note


def branch_lessons(prefix: str, branch: Branch, level: str) -> list[dict]:
    tree = branch.tree
    side = branch.side
    title = branch.name if branch.name != tree.title else tree.title
    ideas = list(tree.plans)
    summary = tree.summary
    if branch.name != tree.title:
        summary = f"{tree.summary} This lesson teaches one branch: {_short(branch.name)}."
    lessons = opening_lessons(prefix, title, side, branch.line, summary, ideas, level,
                              verified_note=_verified_note(branch))
    # the drill covers the whole branch, however long it is (the generic cap is for catalog lines)
    drill = lessons[1]
    have = sum(1 for s in drill["steps"] if s["type"] == "exercise")
    board = chess.Board()
    plies = []
    for ply, san in enumerate(branch.line):
        move = board.parse_san(san)
        if (ply % 2 == 0) == (side == "white"):
            plies.append((ply, board.copy(), move))
        board.push(move)
    for ply, before, move in plies[have:]:
        drill["steps"].append(_line_exercise(title, branch.line, side, ply, before, move, drill["concepts"]))
    # where this branch fits in the tree, and what the opponent may play instead
    fits = _where_it_fits(branch)
    watch = " ".join(tree.watch_for)
    extra = {"type": "teach", "text": " ".join(x for x in (fits, ("Watch out: " + watch) if watch else "") if x),
             "board": {"fen": tree.board(branch.node).fen()}}
    if extra["text"]:
        lessons[0]["steps"].insert(len(lessons[0]["steps"]) - 1, extra)
    for lesson in lessons:
        parse_lesson(lesson, course_id="_generated")
    return lessons


def trap_lesson(lesson_id: str, tree: OpeningTree, traps: list[dict], side: str, level: str) -> dict:
    steps: list[dict] = [{
        "type": "teach",
        "text": (f"Traps in the {tree.title}: short lines where one natural-looking move loses at once. "
                 "Each one was checked by Stockfish: the mistake really loses, and the punishment really works."),
        "board": {"fen": chess.STARTING_FEN},
    }]
    concepts = ["opening", tree.title, "trap"]
    for trap in traps:
        moves, k = trap["moves"], trap["mistake"]
        board = chess.Board()
        ucis = []
        for san in moves[:k + 1]:
            move = board.parse_san(san)
            ucis.append(move.uci())
            board.push(move)
        mistake = _move_label(k, moves[k])
        steps.append({"type": "demonstrate", "fen": chess.STARTING_FEN, "moves": ucis,
                      "text": f"{trap['name']}: watch the moves up to {mistake} — that is the mistake."})
        trapper = "black" if trap["victim"] == "white" else "white"
        rest = moves[k + 1:]
        if trapper == side and rest:
            punish = board.parse_san(rest[0])
            steps.append({"type": "exercise", "fen": board.fen(), "side": side,
                          "prompt": f"Your opponent just played {mistake}. Punish it!",
                          "hints": move_hints(board, punish), "advance_on": "accepted_move",
                          "accepted": [rest[0]],
                          "continue_text": f"Yes — {_move_label(k + 1, rest[0])}!", "concepts": concepts})
            board.push(punish)
            rest = rest[1:]
            start_fen = board.fen()
        else:
            start_fen = board.fen()
        if rest:
            ucis = []
            for san in rest:
                move = board.parse_san(san)
                ucis.append(move.uci())
                board.push(move)
            steps.append({"type": "demonstrate", "fen": start_fen, "moves": ucis,
                          "text": "How it ends: " + _tail(moves, len(moves) - len(rest)) + "."})
        ending = "checkmate" if trap.get("mate") else f"about {round((trap.get('gain_cp') or 0) / 100)} pawns up"
        advice = ("Spring it when your opponent allows it." if trapper == side
                  else f"Don't fall for it: avoid {mistake}.")
        steps.append({"type": "teach", "text": f"{trap['explanation']} Result: {ending} for {trapper.capitalize()}. {advice}",
                      "board": {"fen": board.fen()}})
    lesson = {"id": lesson_id, "title": f"{tree.title}: traps to know", "description": f"Verified traps in the {tree.title}.",
              "difficulty": level, "concepts": concepts, "steps": steps,
              "completion": {"text": f"You know the main traps of the {tree.title}."}}
    parse_lesson(lesson, course_id="_generated")
    return lesson


def _tail(moves: list[str], start: int) -> str:
    out = []
    for ply in range(start, len(moves)):
        out.append(_move_label(ply, moves[ply]) if ply % 2 == 0 or ply == start else moves[ply])
    return " ".join(out)


def game_lesson(lesson_id: str, tree: OpeningTree, game: dict, level: str) -> dict:
    moves = game["moves"]
    board = chess.Board()
    ucis = []
    for san in moves:
        move = board.parse_san(san)
        ucis.append(move.uci())
        board.push(move)
    split = min(game.get("in_tree_plies", 0), len(moves))
    who = f"{game['white']} – {game['black']}, {game['event']} {game['year']}"
    steps = [{"type": "teach", "board": {"fen": chess.STARTING_FEN},
              "text": f"Model game: {who}. {game['lesson']}"}]
    if split >= 2:
        steps.append({"type": "demonstrate", "fen": chess.STARTING_FEN, "moves": ucis[:split],
                      "text": f"The opening: the first {split // 2 + split % 2} moves follow the {tree.title} tree."})
        mid = chess.Board()
        for san in moves[:split]:
            mid.push_san(san)
        steps.append({"type": "demonstrate", "fen": mid.fen(), "moves": ucis[split:],
                      "text": "The rest of the game."})
    else:
        steps.append({"type": "demonstrate", "fen": chess.STARTING_FEN, "moves": ucis, "text": "The whole game."})
    end = "Checkmate." if board.is_checkmate() else f"{game['result']} — {game.get('result_check', '')}."
    steps.append({"type": "teach", "board": {"fen": board.fen()},
                  "text": f"Final position. {end} The game score is a historical record; it was replayed move by "
                          "move and its result checked against the final position."})
    lesson = {"id": lesson_id, "title": f"Model game: {game['white'].split()[-1]} – {game['black'].split()[-1]}, {game['year']}",
              "description": who, "difficulty": level, "concepts": ["opening", tree.title, "model game"],
              "steps": steps, "completion": {"text": "Model game complete."}}
    parse_lesson(lesson, course_id="_generated")
    return lesson


def create_opening_tree_plan(goal: str, match: Match, level: str | None = None) -> dict | None:
    tree = match.tree
    level = level if level in ("beginner", "intermediate", "advanced") else "beginner"
    side = _learner_side(goal, tree)
    branch = tree.branch(match.node, level=level, side=side)
    if branch is None or len(branch.line) < 2:
        return None
    plan_id = uuid.uuid4().hex[:8]
    units, lessons = [], []

    def add(title, reason, verified_by, new):
        units.append({"topic_id": tree.catalog_topic, "title": title, "category": "opening", "reason": reason,
                      "verified_by": verified_by, "lesson_ids": [x["id"] for x in new]})
        lessons.extend(new)

    n = 1
    specific = match.node is not None
    title = branch.name
    add(title, f"One branch of the {tree.title}, {len(branch.line)} half-moves deep for your level "
               f"({level}). {tree.summary}", "opening database + Stockfish",
        branch_lessons(f"plan_{plan_id}_{n:02d}", branch, level))
    # for a named variation, only traps/games that follow the moves up to that variation
    shared = tree.nodes[match.node].depth if specific else 0
    traps = tree.traps_for(branch.line if specific else None, min_shared=shared)[:MAX_TRAPS]
    if traps:
        n += 1
        add(f"{tree.title}: traps", "Short verified traps from this opening.", "Stockfish (build time)",
            [trap_lesson(f"plan_{plan_id}_{n:02d}a", tree, traps, side, level)])
    games = tree.games_for(branch.line if specific else None, min_shared=max(shared, 6))[:1]
    if games:
        n += 1
        add("Model game", "A famous game in this opening, replayed and checked.", "python-chess + Stockfish (build time)",
            [game_lesson(f"plan_{plan_id}_{n:02d}a", tree, games[0], level)])

    others = [b for b in tree.branch_names(limit=6, exclude=branch.name.split(",")[0]) if b != branch.name]
    plan_title = f"Learn: {branch.name}"
    summary = (f"{_short(branch.name) if branch.name != tree.title else tree.title} — one branch of the {tree.title} tree "
               f"({tree.counts.get('positions', 0)} verified positions). {len(branch.line)} half-moves: "
               f"{_format_line(branch.line)}.")
    plan = {
        "id": plan_id, "goal": goal, "title": plan_title, "summary": summary,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner": "opening_tree", "units": units, "skipped": [], "related": [], "prerequisites": [],
        "branches": {"opening": tree.title, "names": others},
        "opening_tree": {"id": tree.id, "title": tree.title, "positions": tree.counts.get("positions", 0),
                         "branch": branch.as_dict(), "matched": match.matched, "level": level},
    }
    course = {"id": f"plan_{plan_id}", "title": plan_title, "description": summary, "kind": "plan",
              "lessons": [{"id": x["id"], "title": x["title"]} for x in lessons]}
    return {"plan": plan, "course": course, "lessons": lessons}


def attach_branches(record: dict, goal: str) -> dict:
    """Plans from other planners about an opening with a tree: offer its other branches."""
    plan = record.get("plan") or {}
    if plan.get("branches") or plan.get("planner") == "opening_tree":
        return record
    trees = get_opening_trees()
    if not len(trees):
        return record
    tree = None
    for unit in plan.get("units", []):
        if unit.get("category") == "opening" and unit.get("topic_id"):
            tree = trees.for_topic(unit["topic_id"])
            if tree:
                break
    if tree is None:
        match = trees.find(goal)  # "Fried Liver Attack" (a library plan) → the Italian Game's branches
        tree = match.tree if match is not None else None
    if tree is None:
        return record
    teaches_opening = any(u.get("category") in ("opening", "openings") for u in plan.get("units", []))
    if teaches_opening or plan.get("planner") == "knowledge":
        names = tree.branch_names(limit=6)
        if names:
            plan["branches"] = {"opening": tree.title, "names": names}
    return record
