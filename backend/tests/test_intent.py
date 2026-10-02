"""Intent understanding: when to ask, what to ask, and remembering the answer.

The requests below are samples of *kinds* of request (piece coordination, partial names,
side conflicts, level conflicts, exclusions, vague requests), not a list the code knows.
"""
import chess
import pytest

from app.planner.intent import (ClarificationError, ClarificationNeeded, IntentMemory, MaterialSpec,
                                qwen_question, understand)
from app.planner.intent.model import OTHER, can_force_mate


def ask(goal, **kw):
    with pytest.raises(ClarificationNeeded) as exc:
        understand(goal, **kw)
    return exc.value.question


# ---------------------------------------------------------------- ambiguous requests
@pytest.mark.parametrize("goal, key", [
    ("I want to learn knight and bishop endgames", "coordination:B+N:endgame"),
    ("endgames with a knight and a bishop", "coordination:B+N:endgame"),
    ("bishop and knight endings", "coordination:B+N:endgame"),
    ("rook and bishop endgames", "coordination:B+R:endgame"),
    ("queen and rook checkmate", "coordination:Q+R:mate"),
    ("the Philidor", "lexical:philidor"),
    ("teach me the Indian", "lexical:indian"),
    ("Sicilian as White", "side:sicilian_defense:white"),
    ("the London System as black", "side:london_system:black"),
    ("forks but no tactics", "exclude:concept:fork:concept:tactics"),
    ("endgames for beginners and advanced players", "level:advanced+beginner"),
    ("teach me something", "vague"),
])
def test_ambiguous_requests_ask(goal, key):
    q = ask(goal)
    assert q.key == key
    assert len(q.options) >= 2
    assert q.text and len(q.text) < 120  # concise


def test_same_ambiguity_in_other_words_has_one_key():
    keys = {ask(g).key for g in ("knight and bishop endgames", "endgames with knight and bishop",
                                 "bishop and knight endgames", "Knight & Bishop endgames")}
    assert keys == {"coordination:B+N:endgame"}


# ---------------------------------------------------------------- clear requests
@pytest.mark.parametrize("goal", [
    "knight endgames", "pins and skewers", "tactics for beginners", "I want to get better at chess",
    "rook and pawn endgames", "king and pawn endgames", "knight forks", "greek gift sacrifice",
    "the philidor position", "windmill", "stalemate tricks", "zorblax gambit", "Italian Game",
    "two knights defense", "an opening like the italian but as black, two knights",
    "I want to learn how to play as black against e4", "I want to learn attacking chess",
    "control the center", "I am not a beginner, teach me rook endgames", "queen mate",
])
def test_clear_requests_are_not_asked(goal):
    understand(goal)  # no ClarificationNeeded


@pytest.mark.parametrize("goal, component", [
    ("bishop and knight checkmate", {"pieces": ["B", "N"], "relation": "together", "head": "mate"}),
    ("knight vs bishop endgames", {"pieces": ["N", "B"], "relation": "versus", "head": "endgame"}),
    ("rook vs bishop", {"pieces": ["R", "B"], "relation": "versus", "head": "endgame"}),
    ("endgames with a knight and bishop together", {"pieces": ["B", "N"], "relation": "together", "head": "endgame"}),
    ("minor piece endgames", {"pieces": ["N", "B"], "relation": "any", "head": "endgame"}),
    ("opposite colored bishops endgames", {"pieces": ["B", "B"], "relation": "versus", "head": "endgame",
                                           "bishops": "opposite"}),
])
def test_clear_material_requests_become_structured(goal, component):
    intent = understand(goal)
    assert intent.structured
    # compare meanings: the legacy one-against-one dicts still load as the same spec
    assert [c.material for c in intent.components] == [MaterialSpec.from_dict(component)]


def test_markers_settle_coordination():
    sep = understand("knight and bishop endgames separately")
    assert [c.key() for c in sep.components] == ["topic:knight_endgames", "topic:bishop_endgames"]
    either = understand("knight or bishop endgames")
    assert [c.key() for c in either.components] == ["topic:knight_endgames", "topic:bishop_endgames"]


def test_chess_decides_which_readings_exist():
    """A lone bishop or knight can't force mate, so 'separately' isn't offered for B+N mates."""
    assert not can_force_mate(("B",)) and not can_force_mate(("N",)) and can_force_mate(("B", "N"))
    understand("checkmate with bishop and knight")  # one reading: not asked
    q = ask("queen and rook checkmate")
    assert {o.id for o in q.options} == {"separate", "together"}  # versus makes no sense for mates


def test_facing_an_opening_is_clear():
    intent = understand("how to play against the Sicilian")
    assert [c.key() for c in intent.components] == ["opening_as:sicilian_defense:white"]
    intent = understand("how do I beat the london")
    assert [c.key() for c in intent.components] == ["opening_as:london_system:black"]


def test_plural_family_means_the_whole_family():
    intent = understand("Indian defenses")
    ids = [c.id for c in intent.components]
    assert "kings_indian" in ids and "nimzo_indian" in ids and len(ids) >= 3


def test_exclusions_become_constraints():
    intent = understand("tactics without forks")
    assert [c.key() for c in intent.components] == ["concept:tactics"]
    assert intent.exclude == ["concept:fork"]


# ---------------------------------------------------------------- multiple choice
def test_multiple_choice_options_are_distinct_and_have_something_else():
    q = ask("I want to learn knight and bishop endgames")
    d = q.as_dict()
    ids = [o["id"] for o in d["options"]]
    assert ids == ["separate", "together", "versus", OTHER]
    assert d["question"] == "What do you mean by “knight and bishop endgames”?"
    sigs = {o.signature() for o in q.options}
    assert len(sigs) == len(q.options)  # genuinely different readings
    assert "separately" in d["options"][0]["label"] and "♞" in d["options"][0]["label"]


@pytest.mark.parametrize("choice, keys", [
    ("separate", ["topic:knight_endgames", "topic:bishop_endgames"]),
    ("together", ['material:{"head": "endgame", "pieces": ["B", "N"], "relation": "together"}']),
    ("versus", ['material:{"against": ["B"], "head": "endgame", "pieces": ["N"], "relation": "versus"}']),
])
def test_choosing_an_option_gives_that_structure(choice, keys):
    intent = understand("knight and bishop endgames", answers={"coordination:B+N:endgame": {"choice": choice}})
    assert [c.key() for c in intent.components] == keys
    assert intent.clarified[0]["choice"] == choice and intent.source == "clarified"


def test_unknown_choice_is_an_error():
    with pytest.raises(ClarificationError):
        understand("knight and bishop endgames", answers={"coordination:B+N:endgame": {"choice": "nope"}})


def test_side_conflict_choices():
    key = "side:sicilian_defense:white"
    own = understand("Sicilian as White", answers={key: {"choice": "own"}})
    face = understand("Sicilian as White", answers={key: {"choice": "face"}})
    assert [c.key() for c in own.components] == ["topic:sicilian_defense"]
    assert [c.key() for c in face.components] == ["opening_as:sicilian_defense:white"]


def test_lexical_choice_maps_to_a_subject():
    q = ask("the Philidor")
    labels = " | ".join(o.label for o in q.options)
    assert "Philidor Defense" in labels and "Smothered mate" in labels
    opening = next(o for o in q.options if "Philidor Defense" in o.label)
    intent = understand("the Philidor", answers={"lexical:philidor": {"choice": opening.id}})
    assert [c.key() for c in intent.components] == ["opening_db:Philidor Defense:black"]


def test_level_conflict_choice_sets_level():
    intent = understand("endgames for beginners and advanced players",
                        answers={"level:advanced+beginner": {"choice": "advanced"}})
    assert intent.level == "advanced"


# ---------------------------------------------------------------- something else
def test_something_else_reinterprets_the_learners_words():
    intent = understand("knight and bishop endgames",
                        answers={"coordination:B+N:endgame": {"choice": OTHER, "text": "knight against bishop"}})
    assert [c.material for c in intent.components] == [MaterialSpec(("N",), "versus", against=("B",))]
    assert intent.clarified[0]["choice"] == OTHER and intent.clarified[0]["original"] == "knight and bishop endgames"


def test_something_else_can_be_plain_words():
    intent = understand("the Philidor", answers={"lexical:philidor": {"choice": OTHER, "text": "smothered mate"}})
    assert intent.goal == "smothered mate" and not intent.components


def test_something_else_needs_words():
    with pytest.raises(ClarificationError):
        understand("knight and bishop endgames", answers={"coordination:B+N:endgame": {"choice": OTHER, "text": " "}})


def test_something_else_can_itself_be_ambiguous():
    q = ask("the Philidor", answers={"lexical:philidor": {"choice": OTHER, "text": "rook and bishop endgames"}})
    assert q.key == "coordination:B+R:endgame"


# ---------------------------------------------------------------- persistence
def test_answers_are_remembered_across_wordings(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    understand("knight and bishop endgames", memory=mem, answers={"coordination:B+N:endgame": {"choice": "versus"}})
    again = understand("endgames with a bishop and a knight", memory=mem)  # not asked again
    assert again.source == "remembered" and again.clarified[0]["remembered"] is True
    assert again.components[0].material.relation == "versus"
    assert IntentMemory(tmp_path / "intents.json").get("coordination:B+N:endgame")["choice"] == "versus"


def test_reclarify_asks_again_and_other_keys_still_ask(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    understand("knight and bishop endgames", memory=mem, answers={"coordination:B+N:endgame": {"choice": "separate"}})
    assert ask("knight and bishop endgames", memory=mem, reclarify=True).key == "coordination:B+N:endgame"
    assert ask("rook and bishop endgames", memory=mem).key == "coordination:B+R:endgame"


def test_stale_remembered_choice_is_asked_again(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    mem.put("coordination:B+N:endgame", "a-choice-that-no-longer-exists")
    assert ask("knight and bishop endgames", memory=mem).key == "coordination:B+N:endgame"


def test_something_else_is_remembered_too(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    understand("the Philidor", memory=mem, answers={"lexical:philidor": {"choice": OTHER, "text": "smothered mate"}})
    again = understand("teach me Philidor", memory=mem)
    assert again.goal == "smothered mate" and again.source == "remembered"


def test_vague_answers_are_not_remembered(tmp_path):
    mem = IntentMemory(tmp_path / "intents.json")
    understand("teach me something", memory=mem, answers={"vague": {"choice": "tactics"}})
    assert mem.get("vague") is None


def test_signature_is_the_meaning_not_the_words():
    a = understand("knight vs bishop endgames")
    b = understand("knight against bishop endings")
    assert a.signature() == b.signature()


# ---------------------------------------------------------------- material predicates
@pytest.mark.parametrize("fen, spec, expected", [
    ("8/8/8/4k3/8/8/2N5/1B2K3 w - - 0 1", MaterialSpec(("B", "N"), "together", "mate"), True),
    ("8/8/8/4k3/8/8/2N5/1B2K3 w - - 0 1", MaterialSpec(("N", "B"), "versus"), False),
    ("8/8/3b4/4k3/8/8/2N5/4K3 w - - 0 1", MaterialSpec(("N", "B"), "versus"), True),
    ("8/8/3b4/4k3/8/8/2N5/4K3 w - - 0 1", MaterialSpec(("N", "B"), "any"), True),
    ("8/8/3b4/4k3/8/8/2R5/4K3 w - - 0 1", MaterialSpec(("N", "B"), "any"), False),
    ("8/8/3b4/4k3/8/8/2B5/4K3 w - - 0 1", MaterialSpec(("B", "B"), "versus", bishops="opposite"), True),   # d6/c2
    ("8/8/2b5/4k3/8/8/2B5/4K3 w - - 0 1", MaterialSpec(("B", "B"), "versus", bishops="opposite"), False),  # c6/c2
    ("8/8/2b5/4k3/8/8/2B5/4K3 w - - 0 1", MaterialSpec(("B", "B"), "versus", bishops="same"), True),
    ("8/5p2/8/4k3/8/8/2R5/4K3 w - - 0 1", MaterialSpec(("R",), "only"), True),
])
def test_material_spec_matches(fen, spec, expected):
    assert spec.matches(chess.Board(fen)) is expected


# ---------------------------------------------------------------- Qwen never picks
def test_qwen_suggestions_become_a_question_never_a_choice():
    from app.knowledge.library import get_knowledge
    from app.planner.catalog import get_catalog
    lib, cat = get_knowledge(), get_catalog()
    q = qwen_question("that sneaky thing with the knight", ["knight_fork", "smothered_mate"], lib, cat)
    assert q is not None and len(q.options) == 2 and q.allow_other
    assert qwen_question("something", ["knight_fork"], lib, cat) is None  # one reading: nothing to choose
    assert qwen_question("something", ["knight_fork", "knight_fork"], lib, cat) is None
