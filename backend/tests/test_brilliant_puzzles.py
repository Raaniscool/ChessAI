"""Brilliant Finder guided play: verified lines, safe replies, result recording and difficulty."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import chess
import pytest
from fastapi.testclient import TestClient

from app.engine import Classification, MoveFeedback, Score, set_engine
from app.knowledge.library import get_knowledge
from app.knowledge.usage import get_usage
from app.learner import LearnerProfile, LearnerStore, get_store
from app.puzzles.brilliant import (
    MANUAL_LEVELS,
    BrilliantFinderError,
    BrilliantTaskStore,
    _complexity_key,
    _first_sacrifice_index,
    _relative_score,
    _sacrifice_indices,
    _verdict,
    adaptive_calibration,
    eligible_puzzles,
    get_brilliant_task_store,
    issue_task,
    manual_bands,
    process_task_move,
    profile_summary,
    retry_task,
    reveal_task,
    set_brilliant_task_store,
    set_preference,
    task_payload,
)


class FinderEngine:
    """Deterministic engine adapter that returns legal principal variations."""

    def __init__(self, *, loss=0, best_is_move=True, raise_error=None, incomplete=False,
                 before=None, after=None):
        self.loss = loss
        self.best_is_move = best_is_move
        self.raise_error = raise_error
        self.incomplete = incomplete
        self.before = before
        self.after = after
        self.calls = []

    def evaluate_move(self, board, move, depth=None, timeout=None):
        self.calls.append((board.fen(), move.uci(), depth, timeout))
        if self.raise_error:
            raise self.raise_error
        after_board = board.copy()
        after_board.push(move)
        best = move if self.best_is_move else next((m for m in board.legal_moves if m != move), move)
        if self.before is not None:
            before_score, after_score = self.before, self.after
            reported_loss = self.loss
        else:
            is_white = board.turn == chess.WHITE
            before_white = 0 if is_white else 0
            after_white = -self.loss if is_white else self.loss
            before_score = Score("cp", before_white)
            after_score = Score("cp", after_white)
            reported_loss = 0 if best == move else self.loss
        best_san = board.san(best)
        best_line = [best_san]
        best_board = board.copy()
        best_board.push(best)
        if not best_board.is_checkmate() and best_board.legal_moves.count():
            reply = next(iter(best_board.legal_moves))
            best_line.append(best_board.san(reply))
        reply_line = []
        if not after_board.is_checkmate() and after_board.legal_moves.count():
            reply = next(iter(after_board.legal_moves))
            reply_line.append(after_board.san(reply))
        return MoveFeedback(
            fen_before=board.fen(),
            fen_after=after_board.fen(),
            user_move_uci=move.uci(),
            user_move_san=board.san(move),
            category=Classification.EXCELLENT if reported_loss <= 15 else Classification.GOOD,
            loss_cp=reported_loss,
            best_move_uci=None if self.incomplete else best.uci(),
            best_move_san=None if self.incomplete else best_san,
            eval_before=None if self.incomplete else before_score,
            eval_after=None if self.incomplete else after_score,
            best_pv_san=best_line,
            reply_pv_san=reply_line,
            depth=depth or 10,
        )

    def close(self):
        pass


class BlockingEngine(FinderEngine):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.proceed = Event()

    def evaluate_move(self, board, move, depth=None, timeout=None):
        self.entered.set()
        assert self.proceed.wait(5), "test did not release the final engine analysis"
        return super().evaluate_move(board, move, depth, timeout)


@pytest.fixture(autouse=True)
def fresh_finder_store():
    set_brilliant_task_store(BrilliantTaskStore())
    yield
    set_brilliant_task_store(None)
    set_engine(None)


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _puzzle(puzzle_id: str):
    return next(p for p in eligible_puzzles(get_knowledge()) if p.id == puzzle_id)


def _play(task, *, engine=None, step_index=None, move=None, store=None):
    index = task.step_index if step_index is None else step_index
    chosen = move or task.puzzle.steps[index]["uci"]
    return process_task_move(task.task_id, index, chosen, before_fen=task.current_fen,
                             knowledge=get_knowledge(), profile_store=get_store(), usage=get_usage(),
                             engine=engine, store=store)


def add_finder_history(profile, scores):
    for score in scores:
        profile._log({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "brilliant_finder",
            "result": "solved" if score >= 0.75 else "failed",
            "brilliant_finder": {"scored": True, "success_score": score, "verdict": "test"},
        })


def test_pool_includes_verified_sacrifices_after_real_setup_moves():
    knowledge = get_knowledge()
    candidates = eligible_puzzles(knowledge)
    assert len(candidates) == 171
    assert len({p.id for p in candidates}) == len(candidates)
    later = [p for p in candidates if (_first_sacrifice_index(p) or 0) > 0]
    assert len(later) == 17
    assert _first_sacrifice_index(_puzzle("lichess_2gvas")) == 2
    assert _first_sacrifice_index(_puzzle("lichess_9n3iz")) == 2

    for puzzle in candidates:
        board = chess.Board(puzzle.fen)
        assert puzzle.verification_state == "verified"
        assert puzzle.engine_profiled is True
        assert puzzle.clear_start is True
        assert puzzle.tier == "global"
        assert board.is_valid()
        assert board.turn == (puzzle.side_to_move == "white")
        assert _sacrifice_indices(puzzle)
        for san in puzzle.solution:
            move = board.parse_san(san)
            assert move in board.legal_moves
            board.push(move)
    bands = manual_bands(candidates)
    assert tuple(bands) == MANUAL_LEVELS
    assert all(bands[level] for level in MANUAL_LEVELS)
    assert len({p.id for members in bands.values() for p in members}) == len(candidates)
    for lower, higher in zip(MANUAL_LEVELS, MANUAL_LEVELS[1:]):
        assert max(_complexity_key(p) for p in bands[lower]) < min(_complexity_key(p) for p in bands[higher])


def test_manual_categories_and_public_task_payload_keep_solution_hidden():
    knowledge = get_knowledge()
    profile = LearnerProfile()
    usage = get_usage()
    bands = manual_bands(eligible_puzzles(knowledge))
    store = BrilliantTaskStore()
    for level in MANUAL_LEVELS:
        set_preference(profile, level)
        task = issue_task(knowledge, profile, usage, store=store)
        assert task.preference == level
        assert task.complexity_band == level
        assert task.puzzle.id in {p.id for p in bands[level]}
        payload = task_payload(task)
        assert "solution" not in payload and "steps" not in payload
        assert "solution_moves" not in payload["difficulty"]["complexity"]
        assert "sacrifice_step" not in payload
        assert payload["difficulty"]["label"] == level.title()
        assert "Play the full combination yourself" in payload["prompt"]


def test_adaptive_uses_chesscom_training_game_and_puzzle_evidence():
    knowledge = get_knowledge()
    usage = get_usage()
    profile = LearnerProfile()
    no_rating = profile_summary(knowledge, profile, usage)
    assert no_rating["difficulty"] == "adaptive"
    assert no_rating["adaptive"]["rating_reference"]["available"] is False
    assert isinstance(no_rating["adaptive"]["target"], int)

    profile.set_onboarding(rating=1200, platform="chesscom")
    rating_only = adaptive_calibration(knowledge, profile, usage)
    assert rating_only["prior"]["source"] == "rating you entered"
    assert rating_only["target"] == 1180

    profile.training = {"tested": [
        {"concept": "sacrifice", "rating": 1550 + 40 * i, "found": True} for i in range(4)
    ]}
    training = adaptive_calibration(knowledge, profile, usage)
    assert training["concept"]["recognition"]["found"] == 4
    assert training["adaptation_evidence"] >= 4
    assert training["target"] > rating_only["target"]
    assert training["target"] <= rating_only["target"] + 160

    profile.game_skill = {
        "games": 3, "errors": 4, "moves": 100, "allowed": 1,
        "phases": {"opening": {"errors": 1, "moves": 45}, "endgame": {"errors": 1, "moves": 35}},
        "chances": {"items": [
            {"kind": "calculation", "rating": 1750 + i * 30, "found": True} for i in range(4)
        ]},
    }
    game = adaptive_calibration(knowledge, profile, usage)
    assert any(item["source"] == "game chances" for item in game["skill_evidence"]["evidence"])
    assert game["adaptation_evidence"] >= training["adaptation_evidence"]
    assert game["target"] > rating_only["target"]

    eligible = [p for p in eligible_puzzles(knowledge) if p.concept == "sacrifice"]
    low, high = min(eligible, key=lambda p: p.rating), max(eligible, key=lambda p: p.rating)
    usage.record_resolved(low.id, True, True, concept=low.concept)
    usage.record_resolved(high.id, False, False, concept=high.concept)
    mixed = adaptive_calibration(knowledge, profile, usage)
    assert mixed["concept"]["puzzles"] >= 2
    assert mixed["adaptation_evidence"] >= 2


def test_missing_and_stale_chesscom_elo_never_blocks_a_position():
    knowledge = get_knowledge()
    usage = get_usage()
    store = BrilliantTaskStore()
    profile = LearnerProfile()
    task = issue_task(knowledge, profile, usage, store=store)
    assert task.puzzle.id
    assert profile_summary(knowledge, profile, usage)["adaptive"]["rating_reference"]["available"] is False

    profile.set_onboarding(rating=2100, platform="chesscom")
    profile.onboarding["at"] = (datetime.now(timezone.utc) - timedelta(days=730)).isoformat()
    stale = profile_summary(knowledge, profile, usage)
    assert stale["adaptive"]["rating_reference"]["available"] is True
    assert stale["adaptive"]["rating_reference"]["stale"] is True
    assert issue_task(knowledge, profile, usage, exclude=[task.puzzle.id], store=store).puzzle.id != task.puzzle.id


def test_finder_adaptation_waits_for_multiple_results_and_moves_in_small_steps():
    knowledge = get_knowledge()
    usage = get_usage()
    profile = LearnerProfile()
    baseline = adaptive_calibration(knowledge, profile, usage)["target"]
    add_finder_history(profile, [0.0])
    one_miss = adaptive_calibration(knowledge, profile, usage)
    assert one_miss["target"] == baseline
    assert one_miss["finder_shift"] == 0

    add_finder_history(profile, [0.0, 0.0, 0.0])
    repeated_misses = adaptive_calibration(knowledge, profile, usage)
    assert repeated_misses["finder_shift"] == -40
    assert baseline - 40 <= repeated_misses["target"] < baseline


def test_engine_verdict_policy_and_score_perspective_remain_stable():
    assert _verdict(sacrifice=True, tactical=True, loss_cp=0) == "sound_sacrifice"
    assert _verdict(sacrifice=True, tactical=True, loss_cp=50) == "sound_sacrifice"
    assert _verdict(sacrifice=True, tactical=True, loss_cp=51) == "playable_sacrifice"
    assert _verdict(sacrifice=True, tactical=True, loss_cp=120) == "playable_sacrifice"
    assert _verdict(sacrifice=True, tactical=True, loss_cp=121) == "incorrect_sacrifice"
    assert Score("cp", 125).for_side(False) == -125
    assert _relative_score(Score("mate", 3), True)["player_perspective"] == {"kind": "mate", "value": 3}
    assert _relative_score(Score("mate", 3), False)["label"] == "You are mated in 3"
    assert _relative_score(Score("mate", -2), False)["label"] == "Mate in 2 for you"
    assert _relative_score(Score.checkmate(white_won=True), False)["label"] == "You are checkmated"


def test_player_plays_every_setup_move_and_sacrifice_with_only_verified_replies():
    puzzle = _puzzle("lichess_2gvas")
    profile_store = get_store()
    usage = get_usage()
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "adaptive", "advanced", {})
    engine = FinderEngine()

    assert len(puzzle.steps) == 3 and _first_sacrifice_index(puzzle) == 2
    first = _play(task, engine=engine, store=store)
    assert first["outcome"] == "correct" and first["done"] is False
    assert first["player_move"]["san"] == "f3+"
    assert first["opponent_move"]["san"] == puzzle.steps[0]["reply_san"] == "Kg4"
    assert task.step_index == 1
    assert task.current_fen == first["opponent_move"]["after_fen"]
    assert engine.calls == []

    second = _play(task, engine=engine, store=store)
    assert second["outcome"] == "correct"
    assert second["player_move"]["san"] == "f2"
    assert second["opponent_move"]["san"] == puzzle.steps[1]["reply_san"] == "Ra1"
    assert task.step_index == 2
    assert engine.calls == []

    final = _play(task, engine=engine, store=store)
    assert final["outcome"] == "complete" and final["done"] is True
    assert final["player_move"]["san"] == "Be1"
    assert final["opponent_move"] is None
    assert task.status == "complete"
    result = final["result"]
    assert result["solved"] is True and result["recorded"] is True
    assert result["analysis_status"] == "complete"
    assert result["explanation"]["preparatory_line"] == "f3+ → f2"
    assert result["explanation"]["preparatory_moves"] == ["f3+", "f2"]
    assert result["explanation"]["sacrifice_move"] == "Be1"
    assert result["explanation"]["source_explanation"].startswith("A discovered check")
    assert result["verified_solution"]["moves"] == list(puzzle.solution)
    assert [move["role"] for move in task.played] == ["player", "opponent", "player", "opponent", "player"]
    assert engine.calls[0][1] == puzzle.steps[2]["uci"]
    assert usage.puzzle_stats(puzzle.id)["attempts"] == 1
    event = profile_store.get("local").exercise_log[-1]
    assert event["source"] == "brilliant_finder"
    assert event["brilliant_finder"]["difficulty"] == "adaptive"
    assert event["brilliant_finder"]["success_score"] == 1.0


def test_incorrect_move_retries_same_turn_without_advancing_or_replying():
    puzzle = _puzzle("lichess_2gvas")
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "adaptive", "beginner", {})
    board = chess.Board(task.current_fen)
    expected = puzzle.steps[0]["uci"]
    wrong = next(move for move in board.legal_moves
                 if move.uci() != expected
                 and move.uci() not in puzzle.steps[0]["accepted"]
                 and move.uci() not in puzzle.steps[0]["good"])
    engine = FinderEngine()
    first = _play(task, engine=engine, move=wrong.uci(), store=store)
    repeated = process_task_move(task.task_id, 0, wrong.uci(), before_fen=puzzle.fen,
                                 knowledge=get_knowledge(), profile_store=get_store(), usage=get_usage(),
                                 engine=engine, store=store)
    assert first["outcome"] == "incorrect"
    assert first["opponent_move"] is None
    assert first["current_fen"] == puzzle.fen == task.current_fen
    assert task.step_index == 0 and task.mistakes == 1
    assert repeated == first
    assert task.mistakes == 1  # duplicate input is idempotent

    retry = _play(task, engine=engine, store=store)
    assert retry["outcome"] == "correct"
    assert task.step_index == 1
    assert retry["opponent_move"]["san"] == puzzle.steps[0]["reply_san"]
    assert engine.calls == []


def test_engine_approved_alternative_without_a_verified_branch_is_not_guessed():
    puzzle = _puzzle("endgame_breakthrough_three_pawns")
    assert _first_sacrifice_index(puzzle) == 1
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "adaptive", "intermediate", {})
    alternative = puzzle.steps[0]["accepted"][0]
    response = _play(task, move=alternative, store=store)
    assert response["outcome"] == "unverified_alternative"
    assert response["opponent_move"] is None
    assert response["current_fen"] == puzzle.fen
    assert task.step_index == 0 and task.mistakes == 0


def test_verified_terminal_alternative_is_accepted_and_playable_move_retries():
    puzzle = _puzzle("lichess_3092o")
    assert puzzle.steps[-1]["accepted"]
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "adaptive", "advanced", {})
    engine = FinderEngine()
    for _ in range(len(puzzle.steps) - 1):
        response = _play(task, engine=engine, store=store)
        assert response["outcome"] == "correct"
    accepted = puzzle.steps[-1]["accepted"][0]
    result = _play(task, engine=engine, move=accepted, store=store)
    assert result["outcome"] == "complete" and result["done"] is True
    assert result["result"]["outcome"] == "verified_alternative"
    assert result["result"]["solved"] is True
    assert result["opponent_move"] is None

    # The existing engine profile marks these as playable but inferior during the combination.
    open_puzzle = _puzzle("endgame_breakthrough_three_pawns")
    task2 = store.issue(open_puzzle, "adaptive", "intermediate", {})
    for _ in range(3):
        _play(task2, engine=engine, store=store)
    assert task2.step_index == 3
    good_move = open_puzzle.steps[3]["good"][0]
    playable = _play(task2, engine=engine, move=good_move, store=store)
    assert playable["outcome"] == "playable"
    assert playable["opponent_move"] is None
    assert task2.step_index == 3 and task2.mistakes == 0
    expected = _play(task2, engine=engine, store=store)
    assert expected["outcome"] == "correct"


def test_engine_inconclusive_does_not_create_a_false_engine_verdict():
    puzzle = _puzzle("lichess_0arm5")
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "adaptive", "advanced", {})
    for _ in range(len(puzzle.steps) - 1):
        _play(task, store=store)
    final = _play(task, engine=None, store=store)
    result = final["result"]
    assert result["solved"] is True  # the stored engine-verified line was completed
    assert result["analysis_status"] == "inconclusive"
    assert result["analysis"]["verdict"] == "inconclusive"
    assert result["analysis"]["reason"] == "stockfish_unavailable"
    assert result["explanation"]["engine_verdict"] is None
    assert result["explanation"]["engine_analysis_status"] == "inconclusive"
    assert result["recorded"] is True
    event = get_store().get("local").exercise_log[-1]
    assert event["brilliant_finder"]["scored"] is True
    assert event["brilliant_finder"]["engine_verdict"] is None


def test_reveal_returns_full_verified_line_records_reveal_and_retry_is_fresh():
    puzzle = _puzzle("lichess_2gvas")
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "beginner", "easy", {})
    first = _play(task, store=store)
    position_after_reply = task.current_fen
    revealed = reveal_task(task.task_id, knowledge=get_knowledge(), profile_store=get_store(),
                           usage=get_usage(), store=store)
    assert revealed["done"] is True
    assert revealed["result"]["revealed"] is True
    assert revealed["result"]["verified_solution"]["moves"] == list(puzzle.solution)
    assert revealed["current_fen"] == position_after_reply == task.current_fen
    assert task.step_index == 1  # reveal never auto-plays the learner's remaining moves
    assert get_usage().puzzle_stats(puzzle.id)["attempts"] == 1
    assert get_usage().puzzle_stats(puzzle.id)["last_result"] == "revealed"
    assert get_usage().puzzle_stats(puzzle.id)["success_rate"] == 0.0
    assert get_store().get("local").exercise_log[-1]["result"] == "revealed"

    retry = retry_task(task.task_id, store=store)
    assert retry.task_id != task.task_id
    assert retry.puzzle.id == task.puzzle.id
    assert retry.preference == "beginner" and retry.complexity_band == "easy"
    assert retry.current_fen == puzzle.fen and retry.step_index == 0


def test_concurrent_and_repeated_move_submissions_cannot_duplicate_reply_or_analysis():
    puzzle = _puzzle("lichess_0arm5")
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "adaptive", "advanced", {})
    engine = BlockingEngine()
    for _ in range(len(puzzle.steps) - 1):
        _play(task, engine=engine, store=store)
    index = task.step_index
    move = puzzle.steps[index]["uci"]
    before = task.current_fen
    args = (task.task_id, index, move)
    kwargs = {"before_fen": before, "knowledge": get_knowledge(), "profile_store": get_store(),
              "usage": get_usage(), "engine": engine, "store": store}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process_task_move, *args, **kwargs)
        assert engine.entered.wait(3)
        with pytest.raises(BrilliantFinderError, match="already being processed"):
            process_task_move(*args, **kwargs)
        engine.proceed.set()
        first = future.result(timeout=5)
    duplicate = process_task_move(*args, **kwargs)
    assert duplicate == first
    assert engine.calls and len(engine.calls) == 1
    assert len(task.played) == len(puzzle.steps) + len(puzzle.steps) - 1
    assert task.status == "complete"


def test_adaptive_and_manual_results_are_recorded_through_existing_profile():
    knowledge = get_knowledge()
    usage = get_usage()
    profile_store = get_store()
    profile = profile_store.get("local")
    set_preference(profile, "beginner")
    puzzle = _puzzle("lichess_0arm5")
    store = BrilliantTaskStore()
    task = store.issue(puzzle, "beginner", "beginner", {})
    engine = FinderEngine()
    for _ in range(len(puzzle.steps)):
        response = _play(task, engine=engine, store=store)
    assert response["result"]["recorded"] is True
    event = profile_store.get("local").exercise_log[-1]
    assert event["source"] == "brilliant_finder"
    assert event["brilliant_finder"]["difficulty"] == "beginner"
    assert event["solved"] is True
    assert usage.puzzle_stats(puzzle.id)["attempts"] == 1


def test_api_preferences_moves_replies_and_manual_band_are_integrated(client):
    set_engine(FinderEngine())
    profile = client.get("/api/puzzles/brilliant/profile")
    assert profile.status_code == 200
    assert profile.json()["available"] == 171
    assert profile.json()["difficulty"] == "adaptive"

    first = client.post("/api/puzzles/brilliant/next", json={"exclude": []})
    assert first.status_code == 200, first.text
    current = first.json()
    assert "solution" not in current and "steps" not in current
    original_fen = current["fen"]
    original_band = current["difficulty"]["preference"]

    saved = client.post("/api/puzzles/brilliant/preferences", json={"difficulty": "beginner"})
    assert saved.status_code == 200 and saved.json()["difficulty"] == "beginner"
    active = get_brilliant_task_store().get(current["task_id"])
    assert active.puzzle.fen == original_fen
    assert active.preference == original_band == "adaptive"

    puzzle = active.puzzle
    response = None
    for index, step in enumerate(puzzle.steps):
        response = client.post(f"/api/puzzles/brilliant/{current['task_id']}/move", json={
            "step_index": index, "move": step["uci"], "before_fen": active.current_fen,
        })
        assert response.status_code == 200, response.text
        action = response.json()
        assert action["step_index"] == index
        assert action["player_move"]["uci"] == step["uci"]
        assert action["opponent_move"]["uci"] == step["reply_uci"] if step["reply_uci"] else action["opponent_move"] is None
    assert response.json()["done"] is True
    assert response.json()["result"]["recorded"] is True

    saved = client.post("/api/puzzles/brilliant/preferences", json={"difficulty": "expert"})
    assert saved.status_code == 200
    following = client.post("/api/puzzles/brilliant/next", json={"exclude": [current["id"]]})
    assert following.status_code == 200, following.text
    assert following.json()["difficulty"]["preference"] == "expert"
    assert following.json()["difficulty"]["complexity_band"] == "expert"

    reloaded = LearnerStore(get_store().root).get("local")
    assert reloaded.preferences["brilliant_finder"]["difficulty"] == "expert"
    assert get_usage().puzzle_stats(current["id"])["attempts"] == 1
    assert get_store().get("local").exercise_log[-1]["source"] == "brilliant_finder"


def test_api_invalid_input_retry_and_reveal_routes_do_not_advance_the_task(client):
    set_engine(FinderEngine())
    invalid = client.post("/api/puzzles/brilliant/preferences", json={"difficulty": "impossible"})
    assert invalid.status_code == 422
    response = client.post("/api/puzzles/brilliant/next", json={"exclude": []})
    assert response.status_code == 200
    public_task = response.json()
    task = get_brilliant_task_store().get(public_task["task_id"])

    illegal = client.post(f"/api/puzzles/brilliant/{task.task_id}/move", json={
        "step_index": 0, "move": "a1a1", "before_fen": task.current_fen,
    })
    assert illegal.status_code == 422
    assert task.step_index == 0 and task.current_fen == task.puzzle.fen

    reveal = client.post(f"/api/puzzles/brilliant/{task.task_id}/reveal")
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["result"]["revealed"] is True
    retry = client.post(f"/api/puzzles/brilliant/{task.task_id}/retry")
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == public_task["id"]
    assert retry.json()["task_id"] != public_task["task_id"]

    # An illegal/foreign move does not consume the turn; the fresh retry task is playable.
    fresh = get_brilliant_task_store().get(retry.json()["task_id"])
    valid_move = fresh.puzzle.steps[0]["uci"]
    valid = client.post(f"/api/puzzles/brilliant/{fresh.task_id}/move", json={
        "step_index": 0, "move": valid_move, "before_fen": fresh.current_fen,
    })
    assert valid.status_code == 200, valid.text
    assert valid.json()["outcome"] == "correct" or valid.json()["done"]


def test_api_rejects_stale_turns_and_same_turn_duplicates_are_idempotent():
    from app.main import app

    set_engine(FinderEngine())
    with TestClient(app) as test_client:
        created = test_client.post("/api/puzzles/brilliant/next", json={"exclude": []})
        assert created.status_code == 200
        public_task = created.json()
        task = get_brilliant_task_store().get(public_task["task_id"])
        move = task.puzzle.steps[0]["uci"]
        body = {"step_index": 0, "move": move, "before_fen": task.current_fen}
        first = test_client.post(f"/api/puzzles/brilliant/{task.task_id}/move", json=body)
        assert first.status_code == 200, first.text
        duplicate = test_client.post(f"/api/puzzles/brilliant/{task.task_id}/move", json=body)
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json() == first.json()
        if not first.json()["done"]:
            stale = test_client.post(f"/api/puzzles/brilliant/{task.task_id}/move", json=body)
            assert stale.status_code == 200  # exact duplicate is replayed, never re-applied
            changed = test_client.post(f"/api/puzzles/brilliant/{task.task_id}/move", json={
                "step_index": 0, "move": task.puzzle.steps[0]["uci"], "before_fen": task.puzzle.fen,
            })
            assert changed.status_code == 200
            next_index = task.step_index
            if next_index < len(task.puzzle.steps):
                stale_turn = test_client.post(f"/api/puzzles/brilliant/{task.task_id}/move", json={
                    "step_index": 0, "move": task.puzzle.steps[next_index]["uci"],
                    "before_fen": task.current_fen,
                })
                assert stale_turn.status_code == 409
