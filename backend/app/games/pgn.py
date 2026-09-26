"""Source-agnostic PGN reading: split, parse and validate every move with python-chess.

Importers (chesscom.py, later others) call ``read_games`` and then add their
site-specific metadata. python-chess is the only judge of legality here: a game
whose moves don't replay from its start position is rejected, never "repaired".
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import chess
import chess.pgn

MAX_PGN_CHARS = 2_000_000
MAX_GAMES = 100  # one paste can hold the largest history (analysis.history.MAX_COUNT)

_TAG = re.compile(r'^\[[A-Za-z0-9_]+\s+"')
_CLOCK = re.compile(r"\[%clk\s+([0-9:.]+)\]")
_MOVETEXT_HINT = re.compile(r"\b1\s*\.\s*(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8]|O-O)")


class PgnError(ValueError):
    """The text is not a usable game (malformed, illegal, empty...)."""


@dataclass
class ParsedGame:
    """One validated game, before any site-specific interpretation."""

    index: int                       # 1-based position in the pasted text
    headers: dict[str, str]
    start_fen: str
    moves_san: list[str]
    moves_uci: list[str]
    clocks: list[str | None]
    final: chess.Board
    pgn: str


@dataclass
class ParseIssue:
    index: int
    error: str
    white: str | None = None
    black: str | None = None


@dataclass
class ParseResult:
    games: list[ParsedGame] = field(default_factory=list)
    errors: list[ParseIssue] = field(default_factory=list)


class _Collect(chess.pgn.GameBuilder):
    """GameBuilder that keeps parse errors instead of logging them."""

    def handle_error(self, error: Exception) -> None:  # noqa: D401 — python-chess hook
        self.game.errors.append(error)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return text.strip()


def _clean_headers(game: chess.pgn.Game) -> dict[str, str]:
    return {k: v for k, v in game.headers.items() if v not in ("", "?", "????.??.??")}


_SAN_TOKEN = re.compile(r"^(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=?[QRBN])?|O-O(?:-O)?|0-0(?:-0)?|--|Z0)"
                        r"[+#]?[!?]*$")
_SKIP_TOKEN = re.compile(r"^(?:\d+\.+|\$\d+|[!?]+|1-0|0-1|1/2-1/2|\*)$")


def _movetext(raw: str) -> str:
    """The move section of one game's text, without headers, comments and side lines."""
    lines = [ln for ln in raw.split("\n") if not _TAG.match(ln.strip()) and not ln.lstrip().startswith("%")]
    text = re.sub(r"\{[^}]*\}", " ", "\n".join(lines))
    text = re.sub(r";[^\n]*", " ", text)
    while True:  # side lines can nest
        stripped = re.sub(r"\([^()]*\)", " ", text)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"(\d+\.+)(?=[^\s.])", r"\1 ", text)  # "1.e4" / "1...e5" -> "1. e4" / "1... e5"


def unreadable_token(raw: str) -> str | None:
    """python-chess skips text it can't read; we don't. Returns the first unreadable token."""
    for token in _movetext(raw).split():
        if not (_SKIP_TOKEN.match(token) or _SAN_TOKEN.match(token)):
            return token
    return None


def _validate(game: chess.pgn.Game, index: int, raw: str) -> ParsedGame:
    headers = _clean_headers(game)
    if game.errors:
        first = game.errors[0]
        raise PgnError(_describe_error(first))
    bad = unreadable_token(raw)
    if bad is not None:
        raise PgnError(f"the moves contain text that isn't a chess move: {bad[:20]!r}")
    variant = game.headers.get("Variant", "Standard")
    if variant.lower() not in ("standard", "chess", "from position"):
        raise PgnError(f"only standard chess is supported (this game is {variant})")
    try:
        board = game.board()
    except ValueError as exc:
        raise PgnError(f"the starting position (FEN header) is invalid: {exc}") from exc
    if board.status() != chess.STATUS_VALID:
        raise PgnError("the starting position (FEN header) is not a legal chess position")
    start_fen = board.fen()
    sans, ucis, clocks = [], [], []
    for node in game.mainline():
        move = node.move
        if move is None or move not in board.legal_moves:
            # python-chess records these as errors; double-check anyway (legality is not negotiable).
            raise PgnError(f"move {len(sans) + 1} is illegal in the position reached")
        sans.append(board.san(move))
        ucis.append(move.uci())
        m = _CLOCK.search(node.comment or "")
        clocks.append(m.group(1) if m else None)
        board.push(move)
    if not sans:
        if not headers:
            raise PgnError("this doesn't look like a PGN (no game headers or moves found)")
        raise PgnError("the game has no moves to analyze")
    result = game.headers.get("Result", "*")
    if board.is_checkmate():
        expected = "0-1" if board.turn == chess.WHITE else "1-0"
        if result not in (expected, "*"):
            raise PgnError(f"the result says {result}, but the final position is checkmate ({expected})")
    if board.is_stalemate() and result not in ("1/2-1/2", "*"):
        raise PgnError(f"the result says {result}, but the final position is stalemate (a draw)")
    return ParsedGame(index=index, headers=headers, start_fen=start_fen, moves_san=sans, moves_uci=ucis,
                      clocks=clocks if any(clocks) else [], final=board, pgn=raw.strip())


def _describe_error(error: Exception) -> str:
    text = str(error)
    if "illegal san" in text.lower() or "illegal" in text.lower():
        return f"illegal move: {text}"
    if "ambiguous" in text.lower():
        return f"ambiguous move: {text}"
    if "invalid san" in text.lower():
        return f"unreadable move: {text}"
    return text


def _name(game: chess.pgn.Game, key: str) -> str | None:
    value = game.headers.get(key)
    return None if value in (None, "", "?") else value


def split_pgns(text: str) -> list[str]:
    """Raw text of each game (for storing the PGN exactly as pasted).

    A new game starts at a header line after movetext (or a blank line + header)."""
    chunks, current, seen_moves = [], [], False
    for line in text.split("\n"):
        stripped = line.strip()
        is_tag = bool(_TAG.match(stripped))
        if is_tag and seen_moves:
            chunks.append("\n".join(current))
            current, seen_moves = [], False
        if stripped and not is_tag:
            seen_moves = True
        current.append(line)
    if "".join(current).strip():
        chunks.append("\n".join(current))
    return [c.strip() for c in chunks if c.strip()]


def read_games(text: str) -> ParseResult:
    """Parse one or more PGNs. Each game is accepted or rejected on its own."""
    if not isinstance(text, str) or not text.strip():
        raise PgnError("paste at least one PGN")
    if len(text) > MAX_PGN_CHARS:
        raise PgnError(f"that's too much text at once (limit {MAX_PGN_CHARS // 1000} KB) — import fewer games")
    text = _normalize(text)
    if "[" not in text and not _MOVETEXT_HINT.search(text):
        raise PgnError("this doesn't look like a PGN. On Chess.com open the game, click Share → PGN, "
                       "and copy the text")
    out = ParseResult()
    index = 0
    # Each pasted game is parsed on its own: python-chess needs a blank line between games,
    # and people pasting several PGNs by hand often leave it out.
    for raw in split_pgns(text):
        stream = io.StringIO(raw)
        while True:
            try:
                game = chess.pgn.read_game(stream, Visitor=_Collect)
            except (ValueError, IndexError) as exc:  # defensive: python-chess normally collects errors
                index += 1
                out.errors.append(ParseIssue(index, f"could not read this game: {exc}"))
                break
            if game is None:
                break
            index += 1
            if index > MAX_GAMES:
                out.errors.append(ParseIssue(index, f"only {MAX_GAMES} games can be imported at once"))
                return out
            try:
                out.games.append(_validate(game, index, raw))
            except PgnError as exc:
                out.errors.append(ParseIssue(index, str(exc), _name(game, "White"), _name(game, "Black")))
    if not out.games and not out.errors:
        raise PgnError("no games found in that text")
    return out
