"""Regression tests from the system audit (second batch).

B4  weaknesses / history / training mixed the games of different players
B5  a game with the same name on both sides was silently analyzed as White's, placeholder
    names ("?") were picked as "the learner", and a game deleted during a batch analysis
    surfaced as a raw "GameNotFound" error
B12 a move that stalemated the opponent (with mate on the board) was headlined as an
    ordinary missed checkmate, hiding that the game ended in a draw
"""
from __future__ import annotations

import pytest

from app.analysis import history, review
from app.games import store
from app.games.importers.chesscom import ChessComImporter
from app.games.pgn import PgnError
from tests.games_helpers import analyze, chesscom_pgn, epd, fork_game, import_one, ScriptedEngine
from tests.test_game_history import client, engine, ndjson  # noqa: F401 — shared fixtures

# --- B4: one learner at a time ------------------------------------------------------------


def _import(client, pgn, username):
    res = client.post("/api/games/import", json={"pgn": pgn, "username": username})
    assert res.status_code == 200, res.text
    return res.json()


def _two_players(client):
    """RaanTest: 4 games with a missed fork. Other: 3 games with the same mistake."""
    raan = [fork_game(game_no=810000000 + i) for i in range(4)]
    other = [fork_game(white="Other", game_no=820000000 + i) for i in range(3)]
    _import(client, "\n\n".join(raan), "RaanTest")
    _import(client, "\n\n".join(other), "Other")
    events = ndjson(client.post("/api/games/analyze", json={}))
    assert sum(e["type"] == "game_done" for e in events) == 7
    return ({f"chesscom-{810000000 + i}" for i in range(4)}, {f"chesscom-{820000000 + i}" for i in range(3)})


def _evidence_games(result):
    return {e["game_id"] for w in result["weaknesses"] + result["seen_once"] for e in w["evidence"]}


def test_weaknesses_never_mix_players(client):
    raan, other = _two_players(client)
    assert _evidence_games(client.get("/api/games/weaknesses", params={"username": "raantest"}).json()) <= raan
    assert _evidence_games(client.get("/api/games/weaknesses", params={"username": "Other"}).json()) <= other
    # no username: the player with the most games, never a blend of both
    default = _evidence_games(client.get("/api/games/weaknesses").json())
    assert default and default <= raan


def test_history_and_training_are_scoped_to_one_player(client):
    raan, other = _two_players(client)
    report = client.get("/api/games/history", params={"count": 10}).json()
    assert report["player"] == "RaanTest"
    assert set(report["game_ids"]) <= raan
    report = client.get("/api/games/history", params={"count": 10, "username": "Other"}).json()
    assert report["player"] == "Other" and set(report["game_ids"]) <= other

    key = client.get("/api/games/weaknesses", params={"username": "Other"}).json()["weaknesses"][0]["key"]
    res = client.post("/api/games/training", json={"keys": [key], "username": "Other"})
    assert res.status_code == 200, res.text
    personal = str(res.json()).lower()
    assert "820000000" in personal and "810000000" not in personal


def test_default_player_is_the_one_with_most_games():
    docs = [{"game": {"player": p, "id": f"chesscom-{i}"}} for i, p in enumerate(["a", "b", "b", "A", "a"])]
    assert history.default_player(docs) == "a"
    kept, name = history.one_player(docs, None)
    assert name == "a" and len(kept) == 3
    kept, name = history.one_player(docs, "B")
    assert name == "B" and len(kept) == 2
    assert history.one_player([], None) == ([], None)


# --- B5: whose moves are they? --------------------------------------------------------------

def test_same_name_on_both_sides_is_refused_not_guessed_as_white():
    pgn = chesscom_pgn("e4 e5 Nf3", white="RaanTest", black="raantest", game_no=830000001)
    result = ChessComImporter().parse(pgn, "RaanTest")
    assert not result.games
    assert "both sides" in result.errors[0].error


def test_placeholder_names_are_never_the_learner():
    games = "\n\n".join(chesscom_pgn("e4 e5", white="?", black=black, game_no=830000010 + i)
                        for i, black in enumerate(["alice", "bob"]))
    # "?" is in both games, but it isn't a person: the importer asks who the learner is
    with pytest.raises(Exception) as exc:
        ChessComImporter().parse(games, None)
    assert type(exc.value).__name__ == "PlayerNeeded"
    assert "?" not in exc.value.players and set(exc.value.players) == {"alice", "bob"}

    anonymous = "\n\n".join(chesscom_pgn("e4 e5", white="?", black="?", game_no=830000020 + i) for i in range(2))
    with pytest.raises(PgnError, match="don't name their players"):
        ChessComImporter().parse(anonymous, None)


class DeletingEngine(ScriptedEngine):
    """Deletes the game from the store while it is being analyzed."""

    def __init__(self, game_id, **kw):
        super().__init__(**kw)
        self.game_id = game_id

    def analyse(self, board, depth=None):
        if store.exists(self.game_id):
            store.delete_game(self.game_id)
        return super().analyse(board, depth)


def test_a_game_deleted_during_analysis_is_skipped_cleanly(client):
    from app.game_api import _analysis_events
    _import(client, "\n\n".join(fork_game(game_no=840000000 + i) for i in range(3)), "RaanTest")
    docs = sorted(store.list_docs(), key=lambda d: d["game"]["id"])
    gone_before = docs[0]["game"]["id"]
    store.delete_game(gone_before)  # deleted after the batch was chosen

    events = list(_analysis_events(docs[:2], DeletingEngine(docs[1]["game"]["id"])))
    skipped = [e for e in events if e["type"] == "skipped"]
    assert [e["game_id"] for e in skipped] == [gone_before, docs[1]["game"]["id"]]
    assert all("deleted" in e["reason"] for e in skipped)
    assert not [e for e in events if e["type"] == "error"]  # no "GameNotFound: ..." noise
    assert not store.exists(docs[1]["game"]["id"])  # nothing was written back for a deleted game


# --- B12: stalemate instead of mate ------------------------------------------------------------

STALEMATE_FEN = "7k/8/6K1/8/8/8/5Q2/8 w - - 0 1"  # Qf8# mates; Qf7?? is stalemate


def test_stalemating_the_opponent_is_the_headline():
    moment = {"category": "blunder", "motif": "missed_checkmate", "san": "Qf7", "fen_before": STALEMATE_FEN,
              "fen_after": "7k/5Q2/6K1/8/8/8/8/8 b - - 1 1", "side": "white", "move_number": 1}
    assert review.headline(moment).startswith("Stalemate! You had checkmate")
    assert "legal move" in review.tip(moment)
    assert any("stalemate" in line for line in review.fact_lines(moment))
    # an ordinary missed mate keeps its headline
    ordinary = {**moment, "san": "Qf6", "fen_after": "7k/8/5QK1/8/8/8/8/8 b - - 1 1"}
    assert review.headline(ordinary) == review.HEADLINES["missed_checkmate"]
    # a stalemate without a mate on the board still says the game is drawn
    assert "draw" in review.headline({**moment, "motif": "hung_piece"})


def test_stalemate_blunder_end_to_end():
    pgn = chesscom_pgn(["Qf7"], fen=STALEMATE_FEN, result="1/2-1/2", game_no=850000001)
    game = import_one(pgn)
    eng = ScriptedEngine(analyses={epd(STALEMATE_FEN): ("M1", ["Qf8#"])})
    analysis = analyze(game, eng)
    moment = next(m for m in analysis["moments"] if m["san"] == "Qf7")
    card = review.review_card(moment)
    assert "Stalemate" in card["headline"] and "draw" in card["headline"]
