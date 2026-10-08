"""Shared helpers for the game-analysis tests: Chess.com-style PGNs and a scripted engine.

The engine is deterministic. By default it plays "grab the most material" one move
deep (via knowledge_helpers.FakeEngine); `analyses` pins what it reports for
chosen positions, and `deep` overrides what it says at the confirmation depth,
so tests can show that a quick-pass false alarm is dropped.
"""
from __future__ import annotations

import chess

from app.engine.classification import Score
from app.engine.service import Analysis
from tests.knowledge_helpers import FakeEngine

# --- positions -----------------------------------------------------------------
FORK_FEN = "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"            # Nc7+ forks king and queen
FORK_AND_BISHOP_FEN = "q3k3/8/8/1N6/8/8/3B4/4K3 w - - 0 1"  # Ba5?? hangs the bishop too
BACK_RANK_FEN = "6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1"    # Rd8# (back-rank mate)
BACK_RANK_DEFENCE_FEN = "6k1/5ppp/8/r7/8/8/5PPP/3R2K1 b - - 0 1"  # Black must stop Rd8#
STALEMATE_FEN = "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"           # Qg7# wins, Qe6 stalemates


def epd(fen: str, *sans: str) -> str:
    board = chess.Board(fen)
    for san in sans:
        board.push_san(san)
    return board.epd()


def movetext(sans: list[str], fen: str | None = None) -> str:
    board = chess.Board(fen) if fen else chess.Board()
    parts = []
    for san in sans:
        if board.turn == chess.WHITE:
            parts.append(f"{board.fullmove_number}.")
        elif not parts:
            parts.append(f"{board.fullmove_number}...")
        parts.append(san)
        board.push_san(san)
    return " ".join(parts)


def chesscom_pgn(sans: list[str] | str, white: str = "RaanTest", black: str = "knightmare42",
                 result: str = "*", fen: str | None = None, game_no: int | None = 123456789,
                 date: str = "2026.09.20", time_control: str = "600", eco_url: str | None = None,
                 clocks: bool = False, extra: dict[str, str] | None = None) -> str:
    """A PGN shaped like Chess.com's Share → PGN export."""
    moves = sans.split() if isinstance(sans, str) else list(sans)
    headers = {"Event": "Live Chess", "Site": "Chess.com", "Date": date, "Round": "-",
               "White": white, "Black": black, "Result": result}
    if fen:
        headers.update({"SetUp": "1", "FEN": fen})
    headers.update({"WhiteElo": "812", "BlackElo": "830", "TimeControl": time_control,
                    "Termination": f"{white} won by checkmate" if result == "1-0" else "Game over"})
    if eco_url:
        headers["ECOUrl"] = eco_url
    if game_no is not None:
        headers["Link"] = f"https://www.chess.com/game/live/{game_no}"
    headers.update(extra or {})
    head = "\n".join(f'[{k} "{v}"]' for k, v in headers.items())
    if clocks:
        board = chess.Board(fen) if fen else chess.Board()
        tokens = []
        for i, san in enumerate(moves):
            if board.turn == chess.WHITE:
                tokens.append(f"{board.fullmove_number}.")
            elif i == 0:
                tokens.append(f"{board.fullmove_number}...")
            tokens.append(f"{san} {{[%clk 0:09:{59 - i:02d}.9]}}")
            board.push_san(san)
        body = " ".join(tokens)
    else:
        body = movetext(moves, fen)
    return f"{head}\n\n{body} {result}\n"


# Real-looking games (the same ones used for the manual Stockfish trial).
TRAP_GAME = chesscom_pgn("e4 e5 Nf3 Nc6 Bc4 Nd4 Nxe5 Qg5 Nxf7 Qxg2 Rf1 Qxe4+ Be2 Nf3#", result="0-1",
                         eco_url="https://www.chess.com/openings/Italian-Game-Blackburne-Shilling-Gambit")
FRIED_LIVER_GAME = chesscom_pgn(
    "e4 e5 Nf3 Nc6 Bc4 Nf6 Ng5 d5 exd5 Nxd5 Nxf7 Kxf7 Qf3+ Ke6 Nc3 Nb4 a3 Nxc2+ Kd1 Nxa1 "
    "Nxd5 Qh4 Nxc7+ Kd7 Qf7+ Be7 Nxa8", white="pawnstorm", black="RaanTest", result="1-0", game_no=123456790,
    time_control="180+2")
FORK_ENDGAME_1 = chesscom_pgn("Kxg4 b3 Rh1 b2 Rb1 Kc3 Kf3 Nc7", white="endgamer", black="RaanTest",
                              fen="8/8/8/3n3R/1pk3p1/6K1/8/8 w - - 0 53", game_no=200000001)
FORK_ENDGAME_2 = chesscom_pgn("Ke3 Nc6 Rc7+ Kb6 Rxc6+ Kxc6 Kd4", white="rookie", black="RaanTest",
                              fen="8/4R3/4p3/2kpP3/3n1K2/8/8/8 w - - 4 69", game_no=200000002)
FOOLS_MATE = chesscom_pgn("f3 e5 g4 Qh4#", white="RaanTest", black="speedy", result="0-1", game_no=300000001)


def fork_game(bad: str = "Kd2", game_no: int = 400000001, white: str = "RaanTest", black: str = "opp") -> str:
    """Learner (White) skips the knight fork Nc7+ in FORK_FEN and plays `bad`."""
    return chesscom_pgn([bad], white=white, black=black, fen=FORK_FEN, game_no=game_no)


# --- engine --------------------------------------------------------------------
def _score(value) -> Score:
    if isinstance(value, Score):
        return value
    if isinstance(value, str):
        return Score("mate", int(value.replace("M", "")))
    return Score("cp", int(value))


class ScriptedEngine(FakeEngine):
    """FakeEngine plus `analyse()`, the call the game analyzer uses for every position.

    analyses: {epd: (score, [pv sans])}  what the engine reports for that position
    deep:     same shape, but only at depth >= deep_from (the analyzer's confirmation pass)
    Anything else: the greedy one-move-deep material search of FakeEngine."""

    def __init__(self, analyses=None, deep=None, deep_from: int = 99, table=None, positions=None,
                 flat: bool = False):
        super().__init__(table=table, positions=positions)
        self.flat = flat  # every move is worth 0 (unless it mates): a game with no mistakes
        self.analyses = analyses or {}
        self.deep = deep or {}
        self.deep_from = deep_from
        self.analyse_calls: list[tuple[str, int]] = []

    def _score(self, board: chess.Board, move: chess.Move) -> Score:
        if self.flat and board.epd() not in self.table:
            after = board.copy(stack=False)
            after.push(move)
            if after.is_checkmate():
                return Score("mate", 1 if board.turn == chess.WHITE else -1)
            return Score("cp", 0)
        return super()._score(board, move)

    def _greedy_pv(self, board: chess.Board, plies: int = 4) -> list[str]:
        board = board.copy(stack=False)
        pv = []
        for _ in range(plies):
            if board.is_game_over():
                break
            line = FakeEngine.analyse_lines(self, board, multipv=1)[0]
            pv.append(line.san)
            board.push(line.move)
        return pv

    def analyse(self, board: chess.Board, depth: int | None = None) -> Analysis:
        depth = depth or 12
        self.analyse_calls.append((board.epd(), depth))
        scripted = None
        if depth >= self.deep_from:
            scripted = self.deep.get(board.epd())
        if scripted is None:
            scripted = self.analyses.get(board.epd())
        if scripted is not None:
            score, pv = _score(scripted[0]), list(scripted[1])
        else:
            best = FakeEngine.analyse_lines(self, board, multipv=1)
            score = best[0].score if best else Score("cp", 0)
            pv = self._greedy_pv(board)
        best_san = pv[0] if pv else None
        best_uci = board.parse_san(best_san).uci() if best_san else None
        return Analysis(fen=board.fen(), best_move_uci=best_uci, best_move_san=best_san, score=score,
                        pv_san=pv, depth=depth)


def fork_engine(**extra) -> ScriptedEngine:
    """Knows that FORK_FEN is winning because of Nc7+ (the greedy search can't see it)."""
    analyses = {epd(FORK_FEN): (850, ["Nc7+", "Kd7", "Nxa8"]),
                epd(FORK_AND_BISHOP_FEN): (850, ["Nc7+", "Kd7", "Nxa8"])}
    analyses.update(extra.pop("analyses", {}))
    return ScriptedEngine(analyses=analyses, **extra)


def analyze(game, engine, depth: int = 8, confirm_depth: int = 10) -> dict:
    """Run the analyzer to completion and return the analysis dict."""
    from app.analysis import GameAnalyzer
    analysis = None
    for event in GameAnalyzer(engine, depth=depth, confirm_depth=confirm_depth).iter_analysis(game):
        if event["type"] == "analysis":
            analysis = event["analysis"]
    assert analysis is not None
    return analysis


def import_one(pgn: str, username: str | None = "RaanTest"):
    from app.games.importers import get_importer
    result = get_importer("chesscom").parse(pgn, username=username)
    assert result.games, result.errors
    return result.games[0]
