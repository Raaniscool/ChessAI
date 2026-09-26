"""Chess.com PGN importer: Chess.com's PGN dialect -> GameRecord.

Only PGN is needed — nothing is scraped. On Chess.com: open a game, Share → PGN,
copy (or download PGNs from your game archive). What this adds on top of the
generic PGN reader (games/pgn.py):

- game id from the ``Link`` header ("https://www.chess.com/game/live/123" -> chesscom-123)
- opening name from ``ECOUrl`` (".../openings/Italian-Game-Giuoco-Piano-4.c3") or ``Opening``,
  else from the Lichess opening database (CC0) by the game's moves
- ``TimeControl`` in Chess.com's formats ("600", "180+2", daily "1/86400") -> time class
- clock times from ``{[%clk 0:09:58.5]}`` comments (kept, not required)
- which side the learner played, from their Chess.com username

PGNs that clearly come from another site are refused: this milestone supports
Chess.com only (other sites = another importer, same GameRecord).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from ..model import GameRecord
from ..pgn import ParsedGame, ParseIssue, PgnError, read_games
from .base import ImportResult, PlayerNeeded

SOURCE = "chesscom"
_LINK_ID = re.compile(r"chess\.com/(?:game/)?(?:live|daily|computer)?/?(?:game/)?(\d+)", re.I)
_MOVE_TOKEN = re.compile(r"^\d+\.")
_OTHER_SITES = ("lichess.org", "chess24", "playstrategy", "chessbase", "fics", "icc")


def _game_id(headers: dict, parsed: ParsedGame) -> str:
    for key in ("Link", "Site"):
        m = _LINK_ID.search(headers.get(key, ""))
        if m:
            return f"{SOURCE}-{m.group(1)}"
    digest = hashlib.sha1("|".join([
        headers.get("White", ""), headers.get("Black", ""), headers.get("Date", ""),
        headers.get("StartTime", ""), parsed.start_fen, " ".join(parsed.moves_uci)]).encode()).hexdigest()
    return f"{SOURCE}-h{digest[:12]}"


def opening_from_url(url: str) -> str | None:
    """"https://www.chess.com/openings/Sicilian-Defense-Alapin-Variation-2...d5" -> "Sicilian Defense Alapin Variation"."""
    m = re.search(r"/openings/([^?#]+)", url or "")
    if not m:
        return None
    words = []
    for token in m.group(1).split("-"):
        if _MOVE_TOKEN.match(token) or not token:
            break
        words.append(token)
    name = " ".join(words).strip()
    return name or None


def time_control(raw: str | None) -> tuple[str | None, str | None]:
    """(time_class, label) for Chess.com TimeControl values: "600", "180+2", "1/86400"."""
    if not raw or raw in ("-", "?"):
        return None, None
    if raw.startswith("1/"):
        try:
            days = int(raw[2:]) // 86400
        except ValueError:
            return "daily", "Daily"
        return "daily", f"Daily ({days} day{'s' if days != 1 else ''} per move)"
    try:
        base, _, inc = raw.partition("+")
        base_s, inc_s = int(base), int(inc or 0)
    except ValueError:
        return None, raw
    estimate = base_s + 40 * inc_s
    klass = "bullet" if estimate < 180 else "blitz" if estimate < 600 else "rapid"
    minutes = base_s / 60
    label = f"{minutes:g} min" if base_s >= 60 else f"{base_s} s"
    if inc_s:
        label += f" + {inc_s} s"
    return klass, label


def _date(headers: dict) -> str | None:
    raw = headers.get("UTCDate") or headers.get("Date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y.%m.%d").date().isoformat()
    except ValueError:
        return None


def _elo(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _opening(headers: dict, parsed: ParsedGame) -> tuple[str | None, str | None]:
    eco = headers.get("ECO")
    name = opening_from_url(headers.get("ECOUrl", "")) or headers.get("Opening")
    if name is None and parsed.start_fen == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1":
        try:
            from ...knowledge.sources.lichess_openings import get_opening_index
            match = get_opening_index().longest_prefix(parsed.moves_san[:30])
        except Exception:  # the opening index is a nicety, never a reason to fail an import
            match = None
        if match is not None:
            name, eco = match.name, eco or match.eco
    return eco, name


def _other_site(headers: dict) -> str | None:
    where = " ".join(headers.get(k, "") for k in ("Site", "Link", "Event")).lower()
    if "chess.com" in where:
        return None
    return next((s for s in _OTHER_SITES if s in where), None)


def to_record(parsed: ParsedGame) -> GameRecord:
    h = parsed.headers
    klass, label = time_control(h.get("TimeControl"))
    eco, opening = _opening(h, parsed)
    return GameRecord(
        id=_game_id(h, parsed), source=SOURCE,
        white=h.get("White", "White"), black=h.get("Black", "Black"),
        result=h.get("Result", "*") if h.get("Result") in ("1-0", "0-1", "1/2-1/2") else "*",
        start_fen=parsed.start_fen, moves_san=parsed.moves_san, moves_uci=parsed.moves_uci,
        date=_date(h), time_control=h.get("TimeControl"), time_class=klass, time_label=label,
        eco=eco, opening=opening, white_elo=_elo(h.get("WhiteElo")), black_elo=_elo(h.get("BlackElo")),
        termination=h.get("Termination"), url=h.get("Link") or (h.get("Site") if "/game/" in h.get("Site", "") else None),
        clocks=parsed.clocks, headers=h, pgn=parsed.pgn,
        imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _common_player(games: list[GameRecord]) -> str | None:
    if len(games) < 2:
        return None
    names = [{g.white.lower(), g.black.lower()} for g in games]
    common = set.intersection(*names)
    if len(common) != 1:
        return None
    low = common.pop()
    g = games[0]
    return g.white if g.white.lower() == low else g.black


class ChessComImporter:
    source = SOURCE
    label = "Chess.com"

    def parse(self, text: str, username: str | None = None) -> ImportResult:
        """Parse pasted Chess.com PGN(s); every game is validated with python-chess.

        Raises PgnError when nothing usable was found, PlayerNeeded when the learner's
        side can't be told (single game, no username)."""
        parsed = read_games(text)
        result = ImportResult(errors=list(parsed.errors))
        records: list[tuple[ParsedGame, GameRecord]] = []
        for game in parsed.games:
            site = _other_site(game.headers)
            if site:
                result.errors.append(ParseIssue(game.index, f"this game is from {site}; only Chess.com games "
                                                            "are supported for now",
                                                game.headers.get("White"), game.headers.get("Black")))
                continue
            records.append((game, to_record(game)))

        name = (username or "").strip() or _common_player([r for _, r in records])
        if not name and records:
            players = sorted({p for _, r in records for p in (r.white, r.black)}, key=str.lower)
            raise PlayerNeeded(players)
        for game, record in records:
            if record.white.lower() == name.lower():
                record.player, record.player_color = record.white, "white"
            elif record.black.lower() == name.lower():
                record.player, record.player_color = record.black, "black"
            else:
                result.errors.append(ParseIssue(
                    game.index, f"{name} didn't play in this game (White: {record.white}, Black: {record.black})",
                    record.white, record.black))
                continue
            result.games.append(record)
        if not result.games and not result.errors:
            raise PgnError("no games found in that text")
        return result
