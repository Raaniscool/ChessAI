"""Chess.com PGN import: parsing, validation, metadata, player detection and storage."""
from __future__ import annotations

import pytest

from app.games import store
from app.games.importers import PlayerNeeded, get_importer
from app.games.importers.chesscom import opening_from_url, time_control
from app.games.model import GameRecord
from app.games.pgn import PgnError, read_games, split_pgns
from tests.games_helpers import (FOOLS_MATE, FORK_ENDGAME_1, FORK_ENDGAME_2, FRIED_LIVER_GAME, TRAP_GAME,
                                 chesscom_pgn)


def parse(text, username="RaanTest"):
    return get_importer("chesscom").parse(text, username=username)


# --- single games and metadata ---------------------------------------------------
def test_parses_a_chesscom_game_with_metadata():
    result = parse(TRAP_GAME)
    assert result.errors == []
    game = result.games[0]
    assert game.id == "chesscom-123456789"
    assert (game.white, game.black, game.result) == ("RaanTest", "knightmare42", "0-1")
    assert (game.player, game.player_color, game.opponent) == ("RaanTest", "white", "knightmare42")
    assert game.date == "2026-09-20"
    assert (game.time_control, game.time_class) == ("600", "rapid")
    assert game.opening == "Italian Game Blackburne Shilling Gambit"
    assert (game.white_elo, game.black_elo) == (812, 830)
    assert game.url == "https://www.chess.com/game/live/123456789"
    assert game.moves_san[:3] == ["e4", "e5", "Nf3"] and game.moves_san[-1] == "Nf3#"
    assert game.moves_uci[0] == "e2e4" and game.plies == 14
    assert game.learner_result() == "loss"


def test_black_player_and_win_draw_results():
    game = parse(FRIED_LIVER_GAME).games[0]
    assert game.player_color == "black" and game.opponent == "pawnstorm"
    assert game.time_class == "blitz" and game.learner_result() == "loss"
    assert parse(chesscom_pgn("e4 e5", result="1/2-1/2")).games[0].learner_result() == "draw"
    assert parse(chesscom_pgn("f3 e5 g4 Qh4#", result="0-1", white="x", black="RaanTest")
                 ).games[0].learner_result() == "win"


def test_username_is_case_insensitive_and_whitespace_tolerant():
    assert parse(TRAP_GAME, username="  raantest ").games[0].player == "RaanTest"


def test_games_from_a_set_up_position():
    game = parse(FORK_ENDGAME_1).games[0]
    assert game.start_fen.startswith("8/8/8/3n3R/1pk3p1/6K1/8/8 w")
    assert game.boards()[0].fullmove_number == 53 and len(game.boards()) == game.plies + 1


def test_clock_comments_are_kept_and_dont_break_parsing():
    game = parse(chesscom_pgn("e4 e5 Nf3 Nc6", clocks=True)).games[0]
    assert game.moves_san == ["e4", "e5", "Nf3", "Nc6"]
    assert game.clocks[0] == "0:09:59.9"


def test_time_control_and_opening_helpers():
    assert time_control("60") == ("bullet", "1 min")
    assert time_control("180+2")[0] == "blitz"
    assert time_control("600")[0] == "rapid"
    assert time_control("1/86400")[0] == "daily"
    assert time_control(None) == (None, None)
    assert opening_from_url("https://www.chess.com/openings/Sicilian-Defense-Najdorf-Variation-6.Be3") \
        .startswith("Sicilian Defense Najdorf Variation")


def test_opening_falls_back_to_the_opening_index():
    game = parse(chesscom_pgn("e4 c6 d4 d5")).games[0]
    assert game.opening and "Caro-Kann" in game.opening


def test_game_without_link_gets_a_stable_content_id():
    a = parse(chesscom_pgn("e4 e5", game_no=None)).games[0]
    b = parse(chesscom_pgn("e4 e5", game_no=None)).games[0]
    c = parse(chesscom_pgn("d4 d5", game_no=None)).games[0]
    assert a.id.startswith("chesscom-h") and a.id == b.id != c.id


def test_record_round_trips_through_a_dict():
    game = parse(TRAP_GAME).games[0]
    again = GameRecord.from_dict(game.to_dict())
    assert again == game and again.summary()["opponent"] == "knightmare42"


# --- multiple games ----------------------------------------------------------------
def test_multiple_games_with_and_without_blank_lines_between_them():
    for sep in ("\n\n", "\n", ""):
        text = sep.join([TRAP_GAME, FRIED_LIVER_GAME, FORK_ENDGAME_1, FORK_ENDGAME_2])
        result = parse(text)
        assert result.errors == [], sep
        assert [g.id for g in result.games] == ["chesscom-123456789", "chesscom-123456790",
                                                 "chesscom-200000001", "chesscom-200000002"]
        assert [g.player_color for g in result.games] == ["white", "black", "black", "black"]


def test_split_pgns_ignores_bracketed_clock_comments_on_their_own_line():
    text = '[Event "Live Chess"]\n[White "a"]\n[Black "b"]\n\n1. e4 {\n[%clk 0:09:59]} e5 *\n'
    assert len(split_pgns(text)) == 1
    assert read_games(text).games[0].moves_san == ["e4", "e5"]


def test_one_bad_game_does_not_block_the_others():
    bad = chesscom_pgn("e4 e5", game_no=5).replace("1. e4 e5", "1. e4 e5 2. Ke3")
    result = parse(TRAP_GAME + "\n" + bad + "\n" + FOOLS_MATE)
    assert [g.id for g in result.games] == ["chesscom-123456789", "chesscom-300000001"]
    assert len(result.errors) == 1 and result.errors[0].index == 2
    assert "Ke3" in result.errors[0].error and result.errors[0].white == "RaanTest"


def test_common_player_is_inferred_across_games():
    result = get_importer().parse(TRAP_GAME + "\n" + FRIED_LIVER_GAME, username=None)
    assert {g.player for g in result.games} == {"RaanTest"}


def test_single_game_without_username_asks_which_player():
    with pytest.raises(PlayerNeeded) as exc:
        get_importer().parse(TRAP_GAME, username=None)
    assert exc.value.players == ["knightmare42", "RaanTest"]


def test_username_not_in_game_is_reported_per_game():
    result = parse(TRAP_GAME + "\n" + chesscom_pgn("d4 d5", white="a", black="b", game_no=9))
    assert len(result.games) == 1
    assert "didn't play" in result.errors[0].error


# --- malformed and illegal ------------------------------------------------------------
@pytest.mark.parametrize("text, message", [
    ("", "paste at least one PGN"),
    ("   \n  ", "paste at least one PGN"),
    ("hello, this is my game!", "doesn't look like a PGN"),
])
def test_malformed_input_is_rejected_with_a_clear_message(text, message):
    with pytest.raises(PgnError, match=message):
        parse(text)


def test_illegal_move_is_rejected():
    result = read_games(chesscom_pgn("e4 e5").replace("1. e4 e5", "1. e4 e5 2. Qxf7"))
    assert not result.games and "Qxf7" in result.errors[0].error


def test_nonsense_move_tokens_are_rejected():
    result = read_games(chesscom_pgn("e4 e5").replace("1. e4 e5", "1. e4 e5 2. Zz9"))
    assert not result.games and "Zz9" in result.errors[0].error


@pytest.mark.parametrize("body", ["1.e4 e5 2.Nf3!? Nc6?! $1 *", "1. e4 (1. d4 d5 (1... Nf6)) 1... e5 *",
                                  "1. e4 {a comment} e5 ; rest of line\n2. O-O?? *", "1. e4 e5 2. Ke2 Ke7 1/2-1/2"])
def test_annotations_comments_and_side_lines_are_accepted(body):
    text = '[Event "Live Chess"]\n[White "a"]\n[Black "b"]\n\n' + body.replace("O-O??", "Nf3")
    assert read_games(text).games, read_games(text).errors


def test_headers_without_moves_are_rejected():
    result = read_games('[Event "Live Chess"]\n[White "a"]\n[Black "b"]\n[Result "*"]\n\n*\n')
    assert not result.games and "no moves" in result.errors[0].error


def test_bad_fen_header_is_rejected():
    text = chesscom_pgn("Kf2", fen="8/8/8/8/8/8/8/4K3 w - - 0 1")  # no black king
    result = read_games(text)
    assert not result.games and "starting position" in result.errors[0].error


def test_result_contradicting_checkmate_is_rejected():
    result = read_games(chesscom_pgn("f3 e5 g4 Qh4#", result="1-0"))
    assert not result.games and "checkmate" in result.errors[0].error


def test_variants_and_other_sites_are_rejected():
    variant = read_games(chesscom_pgn("e4 e5", extra={"Variant": "Chess960"}))
    assert not variant.games and "standard chess" in variant.errors[0].error
    lichess = chesscom_pgn("e4 e5", game_no=None).replace('[Site "Chess.com"]', '[Site "https://lichess.org/abcd1234"]')
    result = parse(lichess + "\n" + FOOLS_MATE)
    assert len(result.games) == 1 and "lichess.org" in result.errors[0].error


def test_windows_line_endings_are_fine():
    assert parse(TRAP_GAME.replace("\n", "\r\n")).games[0].plies == 14


# --- storage ------------------------------------------------------------------------------
def test_games_are_stored_as_learner_data(tmp_path):
    game = parse(TRAP_GAME).games[0]
    assert store.save_game(game, tmp_path) is True
    assert store.save_game(game, tmp_path) is False  # re-import: not new
    assert store.load_game(game.id, tmp_path) == game
    store.save_analysis(game.id, {"moments": []}, tmp_path)
    store.save_game(game, tmp_path)  # re-importing keeps the analysis
    assert store.load_analysis(game.id, tmp_path) == {"moments": []}
    assert [d["game"]["id"] for d in store.list_docs(tmp_path)] == [game.id]
    store.delete_game(game.id, tmp_path)
    with pytest.raises(store.GameNotFound):
        store.load_game(game.id, tmp_path)


def test_store_rejects_path_tricks(tmp_path):
    with pytest.raises(store.GameNotFound):
        store.load_game("../../etc/passwd", tmp_path)


def test_default_store_lives_under_data_dir_not_the_library():
    from app.knowledge.library import get_knowledge
    assert "games" in store.games_dir().parts
    assert str(get_knowledge().data_dir) not in str(store.games_dir())
