"""Game history analysis: the last N games, analyzed together.

The per-game analyzer is reused unchanged; these tests pin down what the history layer adds:
selection of the recent games, the recurring / occasional / one-time tiers, the documented
significance score, evidence, concept roll-up, caching, partial batches, the training hand-off,
privacy of the learner's positions and the facts Qwen gets (and may not contradict).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analysis import history
from app.analysis.analyzer import SCHEMA_VERSION
from app.engine import EngineUnavailable, set_engine
from app.knowledge.library import get_knowledge
from app.session import SessionManager, set_manager
from tests.games_helpers import BACK_RANK_FEN, FORK_FEN, ScriptedEngine, chesscom_pgn, epd, fork_engine
from tests.session_helpers import confirm_advance

FORK2_FEN = "8/q3k3/8/8/1N6/8/8/4K3 w - - 0 1"  # the same idea elsewhere: Nc6+ forks king and queen
FIRST_GAME = 700000000
PLAYS = {
    "clean": (["Nc7+"], FORK_FEN),   # finds the fork: nothing to report
    "fork": (["Kd2"], FORK_FEN),     # misses the knight fork
    "fork2": (["Kd2"], FORK2_FEN),   # misses a knight fork in a different position
    "mate": (["h3"], BACK_RANK_FEN),  # misses a back-rank mate in one
}


class CountingEngine(ScriptedEngine):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def analyse(self, board, depth=None):
        self.calls += 1
        return super().analyse(board, depth)


def history_engine() -> CountingEngine:
    base = fork_engine(analyses={epd(FORK2_FEN): (850, ["Nc6+", "Kd6", "Nxa7"]),
                                 epd(BACK_RANK_FEN): ("M1", ["Rd8#"])})
    return CountingEngine(analyses=base.analyses)


def game_pgn(i: int, kind: str = "clean", result: str = "*") -> str:
    """Game number i (a higher number is a more recent game)."""
    sans, fen = PLAYS[kind]
    day = date(2026, 1, 1) + timedelta(days=i)
    return chesscom_pgn(sans, fen=fen, result=result, game_no=FIRST_GAME + i, date=day.strftime("%Y.%m.%d"),
                        extra={"UTCDate": day.strftime("%Y.%m.%d"), "UTCTime": "12:00:00"})


def gid(i: int) -> str:
    return f"chesscom-{FIRST_GAME + i}"


def ndjson(res):
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


@pytest.fixture()
def engine():
    return history_engine()


@pytest.fixture()
def client(tmp_path, monkeypatch, engine):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_engine(engine)
    set_manager(SessionManager())
    from app.main import app
    yield TestClient(app)
    set_engine(None)


def import_games(client, kinds: dict[int, str] | list[str], **kw):
    items = kinds.items() if isinstance(kinds, dict) else enumerate(kinds, start=1)
    pgn = "\n\n".join(game_pgn(i, k, **kw) for i, k in items)
    res = client.post("/api/games/import", json={"pgn": pgn, "username": "RaanTest"})
    assert res.status_code == 200, res.text
    return res.json()


def run_history(client, count=10, **body):
    res = client.post("/api/games/history/analyze", json={"count": count, **body})
    assert res.status_code == 200, res.text
    return ndjson(res)


def report_of(events):
    assert events[-1]["type"] == "done"
    return events[-1]["report"]


def pattern(report, key):
    return next((p for p in report["patterns"] if p["key"] == key), None)


def ten_games_with_forks():
    """Games 1..10: knight forks missed in games 2, 5, 7 (other position) and 9; a missed mate in 4."""
    kinds = ["clean"] * 10
    for i in (2, 5, 9):
        kinds[i - 1] = "fork"
    kinds[7 - 1] = "fork2"
    kinds[4 - 1] = "mate"
    return kinds


# --- selection -------------------------------------------------------------------------------
def test_exactly_ten_games_are_analyzed_together(client):
    import_games(client, ten_games_with_forks())
    events = run_history(client)
    report = report_of(events)
    assert events[0]["type"] == "select" and events[0]["requested"] == 10 and events[0]["to_analyze"] == 10
    assert report["games_analyzed"] == 10 and report["enough_history"] and report["notice"] is None
    assert report["game_ids"] == [gid(i) for i in range(10, 0, -1)]  # newest first
    assert report["recurring"] == ["knight_fork"]
    assert report["mistakes"]["blunders"] == 5
    assert report["summary"][0] == "I found 1 recurring pattern in your last 10 games."
    assert report["summary"][1] == \
        "The most important one is missed knight fork: found in 4 of your 10 games, with 4 blunders."


@pytest.mark.parametrize("count,threshold", [(20, 3), (30, 5), (50, 8)])
def test_larger_histories_pick_the_most_recent_games(client, count, threshold):
    total = count + 3
    kinds = {i: "clean" for i in range(1, total + 1)}
    kinds.update({1: "fork", 2: "fork", 3: "fork"})  # too old: outside the last `count` games
    newest = list(range(total, total - threshold, -1))
    kinds.update({i: "fork" for i in newest})
    import_games(client, kinds)
    report = report_of(run_history(client, count))
    assert report["games_analyzed"] == count and report["recurring_threshold"] == threshold
    assert report["game_ids"] == [gid(i) for i in range(total, total - count, -1)]
    fork = pattern(report, "knight_fork")
    assert fork["tier"] == "recurring" and fork["game_count"] == threshold
    assert sorted(fork["games"]) == sorted(gid(i) for i in newest)


def test_one_game_short_of_the_threshold_is_occasional_not_recurring(client):
    kinds = {i: "clean" for i in range(1, 31)}
    kinds.update({30: "fork", 29: "fork", 28: "fork", 27: "fork"})  # 4 of 30 < ceil(15% of 30) = 5
    import_games(client, kinds)
    report = report_of(run_history(client, 30))
    assert pattern(report, "knight_fork")["tier"] == "occasional" and report["recurring"] == []
    assert "not a pattern" in " ".join(report["summary"])


def test_custom_counts_and_invalid_ones(client):
    import_games(client, ["clean"] * 12)
    assert report_of(run_history(client, 12))["games_analyzed"] == 12
    assert client.get("/api/games/history?count=11").json()["games_analyzed"] == 11
    for bad in (9, 0, -5, history.MAX_COUNT + 1):
        res = client.post("/api/games/history/analyze", json={"count": bad})
        assert res.status_code == 422, bad
        assert client.get(f"/api/games/history?count={bad}").status_code == 422
    assert "at least 10" in client.get("/api/games/history?count=9").json()["error"]
    assert client.get("/api/games/history?count=ten").status_code == 422


def test_fewer_than_ten_games_explains_there_is_not_enough_history(client):
    import_games(client, ["fork", "fork", "fork", "fork", "clean", "mate"])
    events = run_history(client)
    report = report_of(events)
    assert events[0]["available"] == 6 and report["games_analyzed"] == 6
    assert not report["enough_history"] and report["recurring_threshold"] is None
    assert "isn't enough game history" in report["notice"] and "4 more games" in report["notice"]
    fork = pattern(report, "knight_fork")
    assert fork["game_count"] == 4 and fork["tier"] == "occasional"  # 4 games, but not a pattern yet
    assert report["recurring"] == []
    assert not any("recurring" in line for line in report["summary"])


def test_asking_for_more_games_than_imported_says_so(client):
    import_games(client, ["clean"] * 12)
    report = report_of(run_history(client, 20))
    assert report["games_analyzed"] == 12 and report["enough_history"]
    assert report["notice"] == "You asked for your last 20 games, but only 12 are imported, so I used those."


def test_only_games_the_learner_played_count():
    docs = [{"game": {"id": "x", "player_color": None}}, {"game": {"id": "y", "player_color": "white"}}]
    assert [d["game"]["id"] for d in history.select_recent(docs, 10)] == ["y"]


def test_recency_uses_the_end_date_then_the_game_number():
    older = {"id": "chesscom-5", "headers": {"UTCDate": "2026.03.01", "UTCTime": "23:00:00"}}
    newer = {"id": "chesscom-4", "headers": {"UTCDate": "2026.03.02", "UTCTime": "01:00:00"}}
    ended_later = {"id": "chesscom-3", "headers": {"UTCDate": "2026.03.01", "EndDate": "2026.03.03"}}
    tie_low = {"id": "chesscom-10", "headers": {"UTCDate": "2026.03.01", "UTCTime": "23:00:00"}}
    docs = [{"game": {**g, "player_color": "white"}} for g in (older, newer, ended_later, tie_low)]
    assert [d["game"]["id"] for d in history.select_recent(docs, 10)] == \
        ["chesscom-3", "chesscom-4", "chesscom-10", "chesscom-5"]


# --- duplicates and bad input ----------------------------------------------------------------
def test_duplicate_games_are_imported_once(client):
    pgn = game_pgn(1, "fork")
    res = client.post("/api/games/import", json={"pgn": "\n\n".join([pgn, game_pgn(2), pgn]),
                                                 "username": "RaanTest"}).json()
    assert len(res["imported"]) == 2
    assert [e["error"] for e in res["errors"]] == ["the same game as game 1 (skipped)"]
    import_games(client, ["clean"] * 10)  # games 1..10 again, plus nothing new for 1 and 2
    assert len(client.get("/api/games").json()["games"]) == 10
    report = report_of(run_history(client))
    assert len(set(report["game_ids"])) == 10


def test_one_paste_can_hold_the_largest_history():
    from app.games.importers import get_importer
    from app.games.pgn import MAX_GAMES
    assert MAX_GAMES >= history.MAX_COUNT
    result = get_importer("chesscom").parse("\n\n".join(game_pgn(i) for i in range(1, MAX_GAMES + 2)),
                                            username="RaanTest")
    assert len(result.games) == MAX_GAMES
    assert result.errors[-1].error == f"only {MAX_GAMES} games can be imported at once"


def test_malformed_games_are_reported_and_the_rest_imported(client):
    good = [game_pgn(i) for i in range(1, 11)]
    head, moves = game_pgn(50).rsplit("\n\n", 1)
    broken = head + "\n\n" + moves.replace("Nc7+", "Qh5")  # White has no queen: illegal
    res = client.post("/api/games/import", json={"pgn": "\n\n".join(good[:5] + [broken] + good[5:]),
                                                 "username": "RaanTest"}).json()
    assert len(res["imported"]) == 10 and len(res["errors"]) == 1 and res["errors"][0]["index"] == 6
    assert report_of(run_history(client))["games_analyzed"] == 10


def test_one_failing_game_does_not_stop_the_batch(client, monkeypatch):
    import_games(client, ten_games_with_forks())
    from app.analysis.analyzer import GameAnalyzer
    real = GameAnalyzer.iter_analysis

    def flaky(self, game):
        if game.id == gid(5):
            raise ValueError("corrupt position")
        yield from real(self, game)

    monkeypatch.setattr(GameAnalyzer, "iter_analysis", flaky)
    events = run_history(client)
    errors = [e for e in events if e["type"] == "error"]
    assert [e["game_id"] for e in errors] == [gid(5)] and "corrupt position" in errors[0]["error"]
    assert len([e for e in events if e["type"] == "game_done"]) == 9
    report = report_of(events)
    assert report["failed"] == [{"game_id": gid(5), "error": "ValueError: corrupt position"}]
    assert report["games_analyzed"] == 9 and report["not_analyzed"] == [gid(5)]
    assert "isn't enough game history" in report["notice"]  # 9 analyzed games are not 10
    # the next run only retries the failed game
    monkeypatch.setattr(GameAnalyzer, "iter_analysis", real)
    events = run_history(client)
    assert events[0]["to_analyze"] == 1 and events[1]["games"] == [gid(5)]
    assert report_of(events)["recurring"] == ["knight_fork"]


def test_losing_the_engine_mid_batch_keeps_finished_games(client, monkeypatch):
    import_games(client, ["clean"] * 10)
    from app.analysis.analyzer import GameAnalyzer
    real = GameAnalyzer.iter_analysis

    def dies(self, game):
        if game.id == gid(6):  # the 5th game analyzed (newest first)
            raise EngineUnavailable("Stockfish crashed")
        yield from real(self, game)

    monkeypatch.setattr(GameAnalyzer, "iter_analysis", dies)
    events = run_history(client)
    assert [e["game_id"] for e in events if e["type"] == "game_done"] == [gid(i) for i in (10, 9, 8, 7)]
    assert [e["game_id"] for e in events if e["type"] == "error"] == [gid(i) for i in range(6, 0, -1)]
    report = report_of(events)
    assert report["games_analyzed"] == 4 and len(report["failed"]) == 6
    monkeypatch.setattr(GameAnalyzer, "iter_analysis", real)
    assert run_history(client)[0]["to_analyze"] == 6  # the 4 finished games are kept


def test_no_engine_is_503_only_when_there_is_work(client, monkeypatch):
    import_games(client, ["clean"] * 10)
    run_history(client)

    def broken():
        raise EngineUnavailable("no engine here")

    monkeypatch.setattr("app.game_api.get_engine", broken)
    assert report_of(run_history(client))["games_analyzed"] == 10  # all cached: no engine needed
    assert client.post("/api/games/history/analyze", json={"reanalyze": True}).status_code == 503


# --- progress and caching ---------------------------------------------------------------------
def test_progress_is_reported_game_by_game(client):
    import_games(client, ["clean"] * 10)
    events = run_history(client)
    types = [e["type"] for e in events]
    assert types[:2] == ["select", "start"] and types[-1] == "done"
    done = [e for e in events if e["type"] == "game_done"]
    assert [e["index"] for e in done] == list(range(1, 11)) and all(e["count"] == 10 for e in done)
    progress = [e for e in events if e["type"] == "progress"]
    assert progress and all(1 <= e["index"] <= 10 and e["count"] == 10 for e in progress)
    first_done = types.index("game_done")
    assert all(e["index"] == 1 for e in events[:first_done] if e["type"] == "progress")


def test_repeat_analysis_uses_the_cache(client, engine):
    import_games(client, ten_games_with_forks())
    first = report_of(run_history(client))
    calls = engine.calls
    assert calls > 0
    events = run_history(client)
    assert events[0]["cached"] == 10 and events[0]["to_analyze"] == 0
    assert engine.calls == calls  # nothing re-analyzed
    assert report_of(events)["patterns"] == first["patterns"]
    # a 20-game request after 5 new games only analyzes the new ones
    import_games(client, {i: "clean" for i in range(11, 16)})
    events = run_history(client, 20)
    assert events[0]["to_analyze"] == 5 and events[0]["cached"] == 10
    # asking again explicitly redoes everything
    assert run_history(client, 20, reanalyze=True)[0]["to_analyze"] == 15


def test_an_analysis_from_an_older_analyzer_is_redone(client):
    import_games(client, ["clean"] * 10)
    run_history(client)
    from app.games import store
    doc = store.load_doc(gid(3))
    store.save_analysis(gid(3), {**doc["analysis"], "schema_version": SCHEMA_VERSION - 1})
    assert client.get("/api/games/history").json()["not_analyzed"] == [gid(3)]
    events = run_history(client)
    assert events[0]["to_analyze"] == 1 and events[1]["games"] == [gid(3)]


def test_the_report_is_available_without_the_engine(client):
    import_games(client, ten_games_with_forks())
    before = client.get("/api/games/history").json()
    assert before["games_analyzed"] == 0 and before["not_analyzed"] == [gid(i) for i in range(10, 0, -1)]
    assert before["summary"] == ["No analyzed games yet. Import your Chess.com games and start the analysis."]
    run_history(client)
    after = client.get("/api/games/history").json()
    assert after["recurring"] == ["knight_fork"] and after["not_analyzed"] == []


# --- patterns: tiers, evidence, frequency, significance --------------------------------------
def test_a_pattern_across_games_is_recurring_with_its_evidence(client):
    import_games(client, ten_games_with_forks())
    fork = pattern(report_of(run_history(client)), "knight_fork")
    assert fork["tier"] == "recurring" and fork["kind"] == "tactic"
    assert fork["concept"] == "knight_fork" and fork["title"] == "Missed knight fork"
    assert fork["game_count"] == 4 and fork["occurrences"] == 4 and fork["total_games"] == 10
    assert fork["frequency"] == 0.4 and fork["found_in"] == "4 of your 10 games"
    assert sorted(fork["games"]) == sorted(gid(i) for i in (2, 5, 7, 9))
    assert fork["severity_counts"] == {"blunder": 4} and fork["distinct_positions"] == 2
    ev = {e["game_id"]: e for e in fork["evidence"]}
    assert set(ev) == set(fork["games"])
    assert ev[gid(7)]["fen"] == FORK2_FEN and ev[gid(7)]["best_move"] == "Nc6+"
    assert ev[gid(2)]["fen"] == FORK_FEN and ev[gid(2)]["best_move"] == "Nc7+"
    for e in fork["evidence"]:
        assert e["move_number"] == 1 and e["san"] == "Kd2" and e["severity"] == "blunder"
        assert e["moment_id"] == f"{e['game_id']}:0" and e["motif"] == "missed_fork" and e["loss_cp"] > 900


def test_a_mistake_in_one_game_is_never_recurring(client):
    import_games(client, ten_games_with_forks())
    report = report_of(run_history(client))
    mate = pattern(report, "back_rank_mate")
    assert mate["tier"] == "one_time" and mate["game_count"] == 1 and mate["found_in"] == "1 game"
    assert "back_rank_mate" not in report["recurring"]
    assert report["patterns"][-1]["key"] == "back_rank_mate"  # one-offs come after the patterns
    # ...even though it's the biggest single mistake of the ten games
    assert report["important_mistakes"][0]["concept"] == "back_rank_mate"
    assert mate["significance"] > pattern(report, "knight_fork")["significance"] / 4


def test_tiers():
    assert history.recurring_threshold(10) == 3 and history.recurring_threshold(20) == 3
    assert history.recurring_threshold(30) == 5 and history.recurring_threshold(50) == 8
    assert history.tier_for(1, 50) == "one_time"
    assert history.tier_for(2, 10) == "occasional" and history.tier_for(3, 10) == "recurring"
    assert history.tier_for(7, 50) == "occasional" and history.tier_for(8, 50) == "recurring"
    assert history.tier_for(9, 9) == "occasional"  # not enough history: never recurring


def ev(game: int, severity: str, loss: int, ply: int = 10, fen: str | None = None) -> dict:
    return {"game_id": gid(game), "ply": ply, "severity": severity, "loss_cp": loss,
            "fen": fen or f"8/8/8/8/8/8/{game}/{ply} w - - 0 1"}


def test_significance_formula():
    assert history.impact(ev(1, "blunder", 500)) == 4.5         # 3 x (1 + 0.5)
    assert history.impact(ev(1, "blunder", 5000)) == 6.0        # losses are capped at 1000
    assert history.impact(ev(1, "inaccurate", 100)) == pytest.approx(1.1)
    assert history.impact({"severity": "habit", "loss_cp": None}) == 1.0
    # one game: the biggest counts fully, the rest a quarter
    assert history.significance([ev(1, "blunder", 0, 1), ev(1, "mistake", 0, 2)]) == 3.5
    # the same position reached again (in another game) counts half
    same = "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"
    assert history.significance([ev(1, "blunder", 0, fen=same), ev(2, "blunder", 0, fen=same)]) == 4.5
    assert history.significance([ev(1, "blunder", 0), ev(2, "blunder", 0)]) == 6.0


def test_three_blunders_outrank_eight_small_mistakes():
    small = history.significance([ev(i, "inaccurate", 60) for i in range(1, 9)])
    big = history.significance([ev(i, "blunder", 400) for i in range(1, 4)])
    assert big > small
    # but frequency still matters between mistakes of the same size
    assert history.significance([ev(i, "mistake", 200) for i in range(1, 5)]) > \
        history.significance([ev(i, "mistake", 200) for i in range(1, 4)])


# Synthetic analyses: the aggregator only reads stored analyses, so tests can describe them directly.
def moment(game: int, ply: int, motif: str, concept: str | None, severity="blunder", loss=400, family="missed",
           phase="middlegame", fen=None) -> dict:
    return {"id": f"{gid(game)}:{ply}", "game_id": gid(game), "ply": ply, "move_number": ply // 2 + 1,
            "side": "white", "san": "Kd2", "fen_before": fen or f"8/8/8/8/8/{game}/8/{ply} w - - 0 1",
            "severity": severity, "category": severity, "severity_weight": history.SEVERITY_WEIGHT.get(severity, 1), "loss_cp": loss,
            "best_move": "Nc7+", "phase": phase, "motif": motif, "concept": concept,
            "findings": [{"motif": motif, "concept": concept, "family": family}]}


def doc(game: int, moments=(), habits=(), result="1-0", opening=None) -> dict:
    by_category = {}
    phases = {}
    for m in moments:
        by_category[m["severity"]] = by_category.get(m["severity"], 0) + 1
        phases[m["phase"]] = phases.get(m["phase"], 0) + 1
    return {"game": {"id": gid(game), "player_color": "white", "result": result, "opening": opening,
                     "headers": {"UTCDate": f"2026.02.{game:02d}"}, "white": "RaanTest", "black": "opp"},
            "analysis": {"game_id": gid(game), "schema_version": SCHEMA_VERSION, "moments": list(moments),
                         "habits": list(habits),
                         "stats": {"by_category": by_category, "mistakes_by_phase": phases}}}


def clean_docs(games):
    return [doc(i) for i in games]


def test_ranking_puts_significance_over_raw_counts():
    lib = get_knowledge()
    docs = {i: doc(i) for i in range(1, 11)}
    for i in range(1, 9):  # a small positional slip in 8 games...
        docs[i] = doc(i, [moment(i, 20, "missed_check", "check", severity="mistake", loss=100)])
    for i in (8, 9, 10):  # ...and a big tactical one in 3
        docs[i]["analysis"]["moments"].append(moment(i, 30, "missed_fork", "knight_fork", loss=900))
    report = history.build_report(list(docs.values()), lib)
    recurring = [p for p in report["patterns"] if p["tier"] == "recurring"]
    # 8 real mistakes (2 x 1.1 each, in 8 games) edge out 3 blunders (3 x 1.9): frequency counts too,
    # and the two land close together instead of the count winning 8 to 3
    assert [p["key"] for p in recurring] == ["check", "knight_fork"]
    check, fork = pattern(report, "check"), pattern(report, "knight_fork")
    assert check["occurrences"] == 8 and fork["occurrences"] == 3
    assert fork["significance"] == pytest.approx(3 * 3 * 1.9)   # 17.1
    assert check["significance"] == pytest.approx(8 * 2 * 1.1)  # 17.6: 8 games do count for something
    assert report["recurring"] == [p["key"] for p in recurring]
    assert report["summary"][2] == "You also had missed knight fork in 3 of 10 games."
    # with 8 *inaccuracy-sized* slips the big tactic clearly leads
    for i in range(1, 9):
        m = docs[i]["analysis"]["moments"][0]
        m.update(severity="inaccurate", severity_weight=1, loss_cp=60)
    report = history.build_report(list(docs.values()), lib)
    assert report["recurring"][0] == "knight_fork"


def test_concepts_roll_up_across_games():
    """A knight fork in 2 games and a pawn fork in 2 others are forks in 4 games."""
    lib = get_knowledge()
    docs = clean_docs(range(1, 11))
    for i in (1, 2):
        docs[i - 1] = doc(i, [moment(i, 12, "missed_fork", "knight_fork")])
    for i in (3, 4):
        docs[i - 1] = doc(i, [moment(i, 14, "missed_fork", "pawn_fork")])
    report = history.build_report(docs, lib)
    fork = pattern(report, "fork")
    assert fork["tier"] == "recurring" and fork["game_count"] == 4 and fork["occurrences"] == 4
    assert sorted(fork["includes"]) == ["Missed knight fork", "Missed pawn fork"]
    assert pattern(report, "knight_fork") is None and pattern(report, "pawn_fork") is None  # folded in
    assert report["recurring"] == ["fork"]
    # but when the specific concept is the recurring one, it's listed by its own name
    for i in (5, 6):
        docs[i - 1] = doc(i, [moment(i, 12, "missed_fork", "knight_fork")])
    report = history.build_report(docs, lib)
    assert pattern(report, "knight_fork")["tier"] == "recurring"
    assert "knight_fork" in report["recurring"]


def test_patterns_of_every_kind_share_one_scale():
    lib = get_knowledge()
    docs = clean_docs(range(1, 11))
    for i in (1, 2, 3):
        docs[i - 1] = doc(i, [moment(i, 40, "endgame_mistake", "endgames", phase="endgame", family="phase")],
                          habits=[moment(i, 11, "early_queen", "early_queen", severity="habit", loss=80,
                                         family="habit", phase="opening")])
    docs[4] = doc(5, [moment(5, 20, "hung_piece", "hung_piece", family="allowed")])
    report = history.build_report(docs, lib)
    assert pattern(report, "early_queen")["kind"] == "habit"
    assert pattern(report, "endgames")["kind"] == "endgame"
    assert pattern(report, "hung_piece")["kind"] == "tactic" and pattern(report, "hung_piece")["tier"] == "one_time"
    assert set(report["recurring"]) == {"early_queen", "endgames"}
    assert report["mistakes_by_phase"] == {"endgame": 3, "middlegame": 1}
    assert any("endgame" in o for o in report["observations"])
    assert any("Opening habits cost you something in 3 of 10 games (early queen)." == o
               for o in report["observations"])


def test_overview_counts_results_and_openings():
    docs = [doc(i, result=r, opening=o) for i, (r, o) in enumerate(
        [("1-0", "Italian Game: Giuoco Piano"), ("0-1", "Italian Game: Two Knights"), ("1/2-1/2", "Caro-Kann"),
         ("1-0", None), ("0-1", "Caro-Kann Defense")] + [("1-0", None)] * 5, start=1)]
    report = history.build_report(docs, get_knowledge())
    assert report["results"] == {"win": 7, "loss": 2, "draw": 1, "unfinished": 0}
    italian = next(o for o in report["openings"] if o["opening"] == "Italian Game")
    assert italian == {"opening": "Italian Game", "games": 2, "win": 1, "loss": 1, "draw": 0,
                       "opening_mistakes": 0, "habits": 0}
    assert "You played the Italian Game in 2 games (1 won, 1 lost)." in report["observations"]
    assert report["summary"][-1] == "Nothing repeated across these games: every big mistake was a one-off."


def test_scoring_is_deterministic():
    lib = get_knowledge()
    docs = clean_docs(range(1, 11))
    for i in (1, 4, 6, 9):
        docs[i - 1] = doc(i, [moment(i, 12, "missed_pin", "pin"), moment(i, 16, "missed_fork", "knight_fork")])
    a = history.build_report(docs, lib)
    b = history.build_report(list(reversed(docs)), lib)
    assert [(p["key"], p["significance"], p["tier"]) for p in a["patterns"]] == \
        [(p["key"], p["significance"], p["tier"]) for p in b["patterns"]]


# --- library, training, privacy ---------------------------------------------------------------
def test_patterns_link_to_the_verified_library(client):
    import_games(client, ten_games_with_forks())
    report = report_of(run_history(client))
    lib = get_knowledge()
    fork = pattern(report, "knight_fork")
    assert fork["library_examples"] == lib.count_for("knight_fork") > 0
    assert all(e.status == "verified" for e in lib.examples_for("knight_fork"))


def test_training_from_the_history_builds_a_personal_plan(client):
    import_games(client, ten_games_with_forks())
    report = report_of(run_history(client))
    res = client.post("/api/games/training", json={"keys": report["recurring"], "game_ids": report["game_ids"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "games" and body["plan"]["planner"] == "games"
    course = next(c for c in client.get("/api/courses").json()["courses"] if c["id"] == body["course_id"])
    lesson_ids = [lesson["id"] for lesson in course["lessons"]]
    # library first: a verified example lesson, then the learner's own positions
    from tests.test_api import FakeEngine as TutorEngine
    set_engine(TutorEngine())
    boards = []
    for lesson_id in lesson_ids:
        start = client.post(f"/api/lessons/{lesson_id}/start").json()
        sid, step = start["session_id"], start["step"]
        for _ in range(30):
            if step.get("board"):
                boards.append((lesson_id, step["board"]["fen"]))
            if step["type"] in ("exercise", "complete", "summary"):
                break
            step = confirm_advance(client, sid)["step"]
    own = [fen for lid, fen in boards if lid.endswith("01b")]
    assert FORK_FEN in own or FORK2_FEN in own
    import chess
    library_positions = set()  # every position along a verified example's line
    for example in get_knowledge().examples_for("knight_fork"):
        board = chess.Board(example.start_fen)
        library_positions.add(board.board_fen())
        for san in example.moves:
            board.push_san(san)
            library_positions.add(board.board_fen())
    shown = [chess.Board(fen).board_fen() for lid, fen in boards if lid.endswith("01a")]
    assert sum(1 for b in shown if b in library_positions) >= 2


def test_training_accepts_a_rolled_up_pattern_from_the_history(client):
    """The history can report "forks" (knight forks in 2 games + pawn forks in 2) as one pattern."""
    from app.games import store
    import_games(client, ["clean"] * 10)
    run_history(client)
    for i, concept in ((1, "knight_fork"), (2, "knight_fork"), (3, "pawn_fork"), (4, "pawn_fork")):
        analysis = store.load_doc(gid(i))["analysis"]
        m = moment(i, 0, "missed_fork", concept, fen=FORK_FEN)
        store.save_analysis(gid(i), {**analysis, "moments": [m]})
    report = client.get("/api/games/history").json()
    assert report["recurring"] == ["fork"]
    res = client.post("/api/games/training", json={"keys": ["fork"], "game_ids": report["game_ids"]})
    assert res.status_code == 200, res.text


def test_the_learners_positions_never_enter_the_library(client):
    lib = get_knowledge()
    before = {cid: [e.id for e in lib.examples_for(cid, include_personal=False)] for cid in lib.concepts}
    import_games(client, ten_games_with_forks())
    report = report_of(run_history(client))
    client.post("/api/games/training", json={"keys": report["recurring"], "game_ids": report["game_ids"]})
    client.post("/api/games/history/explain", json={})
    lib = get_knowledge()
    after = {cid: [e.id for e in lib.examples_for(cid, include_personal=False)] for cid in lib.concepts}
    assert after == before
    trusted = {e.start_fen.split()[0] for cid in lib.concepts for e in lib.examples_for(cid)}
    assert FORK2_FEN.split()[0] not in trusted


# --- the teacher ------------------------------------------------------------------------------
def fork_report():
    lib = get_knowledge()
    docs = clean_docs(range(1, 11))
    for i in (2, 5, 9, 7):
        docs[i - 1] = doc(i, [moment(i, 0, "missed_fork", "knight_fork", loss=1450)])
    docs[3] = doc(4, [moment(4, 0, "missed_checkmate", "back_rank_mate", loss=9490)])
    return history.build_report(docs, lib), lib


def test_qwen_gets_structured_verified_findings():
    report, lib = fork_report()
    facts = history.history_facts(report, lib, "beginner")
    text = "\n".join(facts)
    assert facts[0].startswith("The student's last 10 games were analyzed by the Stockfish chess engine")
    assert "RECURRING PATTERN: Missed knight fork (concept knight_fork): 4 of your 10 games, 4 times; " \
           "severity: 4 blunder; average cost a decisive amount" in text
    assert "Stockfish's move was Nc7+" in text
    assert "Verified lesson examples for Knight fork" in text
    assert "One-off mistakes (happened in only one game, NOT patterns): Missed back-rank mate." in text
    assert facts[-1] == "Student level: beginner."
    from app.teacher.prompts import build_history_messages
    messages = build_history_messages(facts, "beginner")
    assert messages[0]["role"] == "system"
    assert "Missed knight fork" in messages[-1]["content"] and "Do not add weaknesses" in messages[-1]["content"]


def test_without_enough_games_qwen_is_told_nothing_is_a_pattern():
    lib = get_knowledge()
    docs = [doc(i, [moment(i, 0, "missed_fork", "knight_fork")]) for i in range(1, 5)]
    report = history.build_report(docs, lib)
    facts = history.history_facts(report, lib)
    assert any("nothing below is a pattern yet" in f for f in facts)
    assert not any(f.startswith("RECURRING PATTERN") for f in facts)


def test_conflicting_teacher_claims_are_caught():
    report, lib = fork_report()
    ok = ("You missed a knight fork in 4 of your 10 games. Knight forks win material, so practise spotting "
          "them. The missed mate happened only once.")
    assert history.conflicts(ok, report, lib) == []
    assert history.conflicts("You missed knight forks in 6 of your 10 games.", report, lib)
    assert history.conflicts("Pins keep costing you material.", report, lib)  # not found by the analysis
    few = history.build_report([doc(i, [moment(i, 0, "missed_fork", "knight_fork")]) for i in (1, 2)], lib)
    assert history.conflicts("Knight forks are a recurring pattern for you.", few, lib)
    assert history.conflicts("Knight forks are not a pattern yet, but watch for them.", few, lib) == []


def test_explain_streams_the_fallback_without_qwen(client):
    assert client.post("/api/games/history/explain", json={}).status_code == 422  # nothing analyzed
    import_games(client, ten_games_with_forks())
    run_history(client)
    events = ndjson(client.post("/api/games/history/explain", json={"count": 10, "level": "beginner"}))
    done = events[-1]
    assert done["type"] == "done" and done["teacher"] == "fallback"
    assert "I found 1 recurring pattern in your last 10 games." in done["text"]
    assert "Practising missed knight fork is the best next step" in done["text"]


def test_explain_passes_the_facts_to_qwen_and_rejects_invented_weaknesses(client, monkeypatch):
    from app.config import get_settings
    from app.teacher import qwen as qwen_mod
    from tests.test_streaming import FakeStream, sse
    monkeypatch.setattr(get_settings(), "qwen_model", "qwen3:4b")
    import_games(client, ten_games_with_forks())
    run_history(client)
    sent = {}
    reply = {"text": "You missed a knight fork in 4 of your 10 games. Practise knight forks first."}

    def fake_stream(method, url, json, headers, timeout):
        sent["messages"] = json["messages"]
        return FakeStream(sse(reply["text"]))

    monkeypatch.setattr(qwen_mod.httpx, "stream", fake_stream)
    done = ndjson(client.post("/api/games/history/explain", json={}))[-1]
    assert done["teacher"] == "qwen" and done["text"] == reply["text"]
    assert "RECURRING PATTERN: Missed knight fork" in sent["messages"][-1]["content"]
    reply["text"] = "Your biggest weakness is skewers: you missed them in 7 of your 10 games."
    done = ndjson(client.post("/api/games/history/explain", json={}))[-1]
    assert done["teacher"] == "fallback" and done.get("corrected") and "skewer" not in done["text"].lower()
