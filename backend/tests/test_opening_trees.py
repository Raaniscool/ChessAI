"""The verified opening trees (knowledge/opening_trees.py) and the plans built from them.

Data integrity is checked on the shipped build (backend/app/knowledge/data/opening_trees/): every
position replays legally, carries a Stockfish evaluation, and every trap / model game kept by the
build replays and still meets the build's thresholds.
"""
from __future__ import annotations

import json

import chess
import pytest

from app.knowledge.opening_trees import TREE_DIR, get_opening_trees, reset_opening_trees
from app.lessons.schema import parse_lesson
from app.planner.opening_tree_plans import attach_branches, create_opening_tree_plan, find_branch_request
from app.planner.planner import plan_for_goal

SPEC = json.loads((TREE_DIR.parents[1] / "sources" / "data" / "curated" / "opening_trees.json").read_text())
SPEC_IDS = [o["id"] for o in SPEC["openings"]]


@pytest.fixture(scope="module")
def trees():
    reset_opening_trees()
    return get_opening_trees()


def test_every_spec_opening_was_built(trees):
    assert len(SPEC_IDS) == 25
    assert sorted(trees.trees) == sorted(SPEC_IDS)
    report = json.loads((TREE_DIR / "_report.json").read_text())
    assert sorted(report["openings"]) == sorted(SPEC_IDS)


@pytest.mark.parametrize("tree_id", SPEC_IDS)
def test_tree_integrity(trees, tree_id):
    tree = trees.get(tree_id)
    assert len(tree.nodes) - 1 >= 80, "a tree is a real tree, not a single line"
    boards = {0: chess.Board()}
    for node in tree.nodes[1:]:
        assert node.parent < node.i
        board = boards[node.parent].copy(stack=False)
        board.push_san(node.san)  # raises on an illegal move
        boards[node.i] = board
        assert isinstance(node.eval, int)
        assert node.verdict in ("sound", "dubious", "mistake")
    # the main line is one path from the root
    main = [n for n in tree.nodes if n.main]
    for a, b in zip(main, main[1:]):
        assert b.parent == a.i
    assert tree.title in {n.name for n in tree.nodes if n.name} or any(
        (n.name or "").startswith(tree.title) for n in tree.nodes)


@pytest.mark.parametrize("tree_id", SPEC_IDS)
def test_traps_and_games_replay(trees, tree_id):
    tree = trees.get(tree_id)
    for trap in tree.traps:
        board = chess.Board()
        for san in trap["moves"]:
            board.push_san(san)
        assert 0 < trap["mistake"] < len(trap["moves"])
        assert trap["victim"] == ("white" if trap["mistake"] % 2 == 0 else "black")
        assert trap.get("mate") or trap["gain_cp"] >= 150
    for game in tree.games:
        board = chess.Board()
        for san in game["moves"]:
            board.push_san(san)
        assert game["in_tree_plies"] >= 6
        assert game["result"] in ("1-0", "0-1", "1/2-1/2")


@pytest.mark.parametrize("tree_id", SPEC_IDS)
def test_branch_lengths_vary_with_level(trees, tree_id):
    tree = trees.get(tree_id)
    lengths = []
    for level in ("beginner", "intermediate", "advanced"):
        branch = tree.branch(None, level=level, side=tree.side)
        assert branch is not None and len(branch.line) >= 4
        node = 0
        board = chess.Board()
        for ply, san in enumerate(branch.line):  # every learner move in a taught branch is sound
            child = next(c for c in tree.nodes[node].children if tree.nodes[c].san == san)
            if (ply % 2 == 0) == (tree.side == "white"):
                assert tree.nodes[child].verdict == "sound", (level, ply, san)
            board.push_san(san)
            node = child
        lengths.append(len(branch.line))
    assert lengths[0] <= lengths[1] <= lengths[2]
    assert lengths[0] < lengths[2], "material length depends on the level"


def test_branch_length_differs_between_openings(trees):
    advanced = {t: len(trees.get(t).branch(None, level="advanced").line) for t in SPEC_IDS}
    assert max(advanced.values()) - min(advanced.values()) >= 6


@pytest.mark.parametrize("goal,tree_id,name", [
    ("Sicilian Defense", "sicilian_defense", None),
    ("Najdorf", "sicilian_defense", "Sicilian Defense: Najdorf Variation"),
    ("Sicilian Dragon", "sicilian_defense", "Sicilian Defense: Dragon Variation"),
    ("Dutch Defense", "dutch_defense", None),
    ("Grünfeld", "grunfeld_defense", None),
    ("Grunfeld Defense", "grunfeld_defense", None),
    ("King's Gambit", "kings_gambit", None),
    ("Ruy Lopez Berlin", "ruy_lopez", "Ruy Lopez: Berlin Defense"),
    ("Fried Liver Attack", "italian_game", "Italian Game: Two Knights Defense, Fried Liver Attack"),
])
def test_find(trees, goal, tree_id, name):
    match = trees.find(goal)
    assert match is not None and match.tree.id == tree_id
    if name is None:
        assert match.node is None
    else:
        assert match.tree.nodes[match.node].name == name


@pytest.mark.parametrize("goal", ["exchange variation", "knight fork", "rook endgames", "x"])
def test_find_rejects_ambiguous_or_unrelated(trees, goal):
    assert trees.find(goal) is None


def test_named_variation_branch_goes_through_it(trees):
    match = trees.find("Sicilian Dragon")
    branch = match.tree.branch(match.node, level="intermediate", side="black")
    path = match.tree.sans(match.node)
    assert branch.line[:len(path)] == path and "g6" in path
    assert len(branch.line) > len(path)


def test_opening_tree_plan_is_one_branch_with_traps_and_game(trees):
    match = find_branch_request("King's Gambit")
    assert match is not None
    record = create_opening_tree_plan("King's Gambit", match, "beginner")
    plan = record["plan"]
    assert plan["planner"] == "opening_tree"
    assert plan["opening_tree"]["id"] == "kings_gambit"
    assert plan["branches"]["names"]
    for lesson in record["lessons"]:
        parse_lesson(lesson, course_id="_t")
    line = plan["opening_tree"]["branch"]["line"]
    beginner = len(line)
    advanced = create_opening_tree_plan("King's Gambit", match, "advanced")["plan"]["opening_tree"]["branch"]["line"]
    assert len(advanced) > beginner
    # the drill covers every learner move of the branch, however long it is
    drill = record["lessons"][1]
    assert sum(1 for s in drill["steps"] if s["type"] == "exercise") == (len(line) + 1) // 2


def test_catalog_and_library_keep_what_they_already_teach():
    assert find_branch_request("Italian Game") is None  # the course / catalog topic
    assert find_branch_request("Najdorf") is None  # the catalog's Sicilian line goes through it
    assert find_branch_request("Sicilian Dragon") is not None


def test_plan_for_goal_routes_to_the_tree(monkeypatch):
    record = plan_for_goal("I want to learn the Grünfeld Defense", use_qwen=False, clarify=True)
    assert record["plan"]["planner"] == "opening_tree"
    assert record["plan"]["opening_tree"]["id"] == "grunfeld_defense"
    dragon = plan_for_goal("I want to learn the Sicilian Dragon", use_qwen=False, clarify=True)
    assert dragon["plan"]["planner"] == "opening_tree"
    assert "g6" in dragon["plan"]["opening_tree"]["branch"]["line"]


def test_other_planners_get_branch_suggestions():
    record = plan_for_goal("I want to learn the Ruy Lopez", use_qwen=False, clarify=True)
    assert record["plan"]["planner"] != "opening_tree"
    assert record["plan"]["branches"]["opening"] == "Ruy Lopez"
    assert record["plan"]["branches"]["names"]
    # a non-opening plan gets nothing
    plain = {"plan": {"planner": "knowledge", "units": [{"category": "tactics"}]}}
    assert "branches" not in attach_branches(plain, "knight fork")["plan"]


def test_accented_names_are_one_word():
    """"Grünfeld" used to split into "gr" + "nfeld" ("Which Nfeld do you mean?")."""
    from app.planner.catalog import _normalize
    from app.planner.intent import ClarificationNeeded
    assert _normalize("Grünfeld Défense") == ["grunfeld", "defense"]
    with pytest.raises(ClarificationNeeded) as need:  # the Grünfeld or the Neo-Grünfeld: a real question
        plan_for_goal("I want to learn Grünfeld", use_qwen=False, clarify=True)
    labels = [o["label"] for o in need.value.question.as_dict()["options"]]
    assert labels[0].startswith("Grünfeld Defense") and any(label.startswith("Neo-Grünfeld") for label in labels)


def test_unexplained_words_mean_not_this_tree(trees):
    assert trees.find("Philidor position") is None  # the rook endgame
    assert trees.find("Philidor Defense").tree.id == "philidor_defense"


@pytest.mark.parametrize("tree_id", SPEC_IDS)
def test_every_branch_chip_leads_to_its_branch(trees, tree_id):
    """A chip sends "I want to learn <name>": it must come back to exactly that variation."""
    tree = trees.get(tree_id)
    names = tree.branch_names(limit=6)
    assert len(names) == len(set(names))
    for name in names:
        match = trees.find(f"I want to learn {name}")
        assert match is not None and match.tree is tree, name
        assert match.node is not None and tree.nodes[match.node].name.split(",")[0] == name, name
        assert tree.branch(match.node, level="beginner") is not None, name
