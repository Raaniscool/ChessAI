"""Regression tests from the system audit: Chess.com import edge cases.

Each test pins a bug that was found by feeding unusual (but real) PGNs through the
import API.
"""
import json

import pytest

from app.games import store
from app.games.importers.chesscom import ChessComImporter, id_from_link, legacy_ids
from app.games.pgn import read_games, split_pgns
from tests.games_helpers import chesscom_pgn
from tests.test_game_history import client, engine  # noqa: F401 — shared fixtures


def _import(client, pgn, username="RaanTest"):
    res = client.post("/api/games/import", json={"pgn": pgn, "username": username})
    assert res.status_code == 200, res.text
    return res.json()


# --- game ids ------------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://www.chess.com/game/live/123", "chesscom-123"),
    ("https://www.chess.com/live/game/123", "chesscom-123"),
    ("https://www.chess.com/game/123", "chesscom-123"),
    # the review page: previously got a hash id, so the same game imported from the
    # public API ("/game/live/123") was stored twice
    ("https://www.chess.com/analysis/game/live/123?tab=review", "chesscom-123"),
    # daily, live and computer games are numbered separately: they must not share an id
    # (daily game 123 used to overwrite live game 123)
    ("https://www.chess.com/game/daily/123", "chesscom-daily-123"),
    ("https://www.chess.com/daily/game/123", "chesscom-daily-123"),
    ("https://www.chess.com/game/computer/123", "chesscom-computer-123"),
    ("https://example.com/whatever", None),
])
def test_game_ids_from_every_chesscom_url_form(url, expected):
    assert id_from_link(url) == expected


def test_review_link_and_game_link_are_the_same_game(client):
    first = _import(client, chesscom_pgn("e4 e5 Nf3 Nc6", game_no=555))
    again = _import(client, chesscom_pgn("e4 e5 Nf3 Nc6", game_no=None, extra={
        "Link": "https://www.chess.com/analysis/game/live/555?tab=review"}))
    assert first["new"] == 1 and again["new"] == 0
    assert [g["id"] for g in client.get("/api/games").json()["games"]] == ["chesscom-555"]


def test_a_daily_game_never_overwrites_a_live_game_with_the_same_number(client):
    _import(client, chesscom_pgn("e4 e5 Nf3 Nc6", game_no=777))
    daily = _import(client, chesscom_pgn("d4 d5 c4", game_no=None,
                                         extra={"Link": "https://www.chess.com/game/daily/777"}))
    assert daily["new"] == 1
    ids = sorted(g["id"] for g in client.get("/api/games").json()["games"])
    assert ids == ["chesscom-777", "chesscom-daily-777"]
    assert store.load_game("chesscom-777").moves_san == ["e4", "e5", "Nf3", "Nc6"]


def test_a_daily_game_stored_under_the_old_id_is_moved_not_duplicated(client, tmp_path):
    # what an older version stored for the daily game 42: id "chesscom-42"
    record = ChessComImporter().parse(
        chesscom_pgn("d4 d5 c4", game_no=None, extra={"Link": "https://www.chess.com/game/daily/42"}),
        "RaanTest").games[0]
    assert record.id == "chesscom-daily-42" and legacy_ids(record.id) == ["chesscom-42"]
    old = record.to_dict() | {"id": "chesscom-42"}
    (tmp_path / "games").mkdir(exist_ok=True)
    (tmp_path / "games" / "chesscom-42.json").write_text(json.dumps(
        {"schema_version": 1, "game": old, "analysis": {"game_id": "chesscom-42", "moments": []}}))
    res = _import(client, chesscom_pgn("d4 d5 c4", game_no=None,
                                       extra={"Link": "https://www.chess.com/game/daily/42"}))
    assert res["new"] == 0
    assert [g["id"] for g in client.get("/api/games").json()["games"]] == ["chesscom-daily-42"]
    # the old analysis pointed at the old id everywhere: it is redone, not patched
    assert store.load_analysis("chesscom-daily-42") is None


def test_a_different_game_under_the_old_id_is_left_alone(client, tmp_path):
    _import(client, chesscom_pgn("e4 e5", game_no=42))  # live game 42, a different game
    _import(client, chesscom_pgn("d4 d5 c4", game_no=None, extra={"Link": "https://www.chess.com/game/daily/42"}))
    ids = sorted(g["id"] for g in client.get("/api/games").json()["games"])
    assert ids == ["chesscom-42", "chesscom-daily-42"]


# --- PGN reading ---------------------------------------------------------------------------

def test_en_passant_suffix_is_valid_pgn():
    pgn = chesscom_pgn("e4 d5 e5 f5", game_no=15).replace("f5 *", "f5 3. exf6 e.p. *")
    result = read_games(pgn)
    assert not result.errors and result.games[0].moves_san[-1] == "exf6"


def test_a_broken_fen_header_is_reported_as_such_not_as_an_illegal_move():
    pgn = chesscom_pgn("e4", game_no=20).replace(
        "[WhiteElo", '[SetUp "1"]\n[FEN "8/8/8/8/8/8/8/8 w - - 0 1"]\n[WhiteElo')
    result = read_games(pgn)
    assert not result.games
    assert "starting position" in result.errors[0].error


def test_a_game_without_moves_before_another_game_is_reported_not_swallowed():
    headers_only = chesscom_pgn([], game_no=26).split("\n\n")[0]
    text = headers_only + "\n\n" + chesscom_pgn("c4 c5", game_no=27)
    assert len(split_pgns(text)) == 2
    result = read_games(text)
    assert [g.moves_san for g in result.games] == [["c4", "c5"]]
    assert [e.error for e in result.errors] == ["the game has no moves to analyze"]


@pytest.mark.parametrize("name, pgn, ok", [
    ("checkmate, correct result", chesscom_pgn("f3 e5 g4 Qh4#", result="0-1"), True),
    ("checkmate, wrong result", chesscom_pgn("f3 e5 g4 Qh4#", result="1-0"), False),
    ("stalemate called a win", chesscom_pgn(["Qe6"], fen="7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", result="1-0"), False),
    ("stalemate draw", chesscom_pgn(["Qe6"], fen="7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", result="1/2-1/2"), True),
    ("resignation", chesscom_pgn("e4 e5 Nf3 d6", result="0-1",
                                 extra={"Termination": "knightmare42 won by resignation"}), True),
    ("timeout draw", chesscom_pgn("e4 e5", result="1/2-1/2",
                                  extra={"Termination": "Game drawn by timeout vs insufficient material"}), True),
    ("unfinished", chesscom_pgn("e4 e5", result="*"), True),
    ("one move", chesscom_pgn("e4", result="0-1"), True),
    ("from a position, black first", chesscom_pgn(
        ["e5"], fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"), True),
    ("side lines and NAGs", chesscom_pgn("e4 e5 Nf3").replace("Nf3", "Nf3 {good} $1 (2. Qh5 Nc6 (2... g6))"), True),
    ("chess960", chesscom_pgn("e4 e5", extra={"Variant": "Chess960"}), False),
])
def test_results_terminations_and_unusual_pgns(name, pgn, ok):
    result = read_games(pgn)
    assert bool(result.games) is ok, (name, [e.error for e in result.errors])
