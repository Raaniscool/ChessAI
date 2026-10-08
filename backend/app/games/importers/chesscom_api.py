"""Fetch a player's recent games from Chess.com's official Published-Data API (PubAPI).

    GET https://api.chess.com/pub/player/{username}/games/archives  -> {"archives": [month URLs, oldest first]}
    GET {month URL}                                                  -> {"games": [{pgn, url, end_time, rules, ...}]}

Read-only public data: no login, no scraping. Chess.com asks API users to make requests
one at a time (parallel requests get "429 Too Many Requests") and to send a User-Agent
with contact info, so this client is strictly serial and identifies itself.
(https://support.chess.com/en/articles/9650547)

Only standard chess is kept (no Chess960, Bughouse, Crazyhouse…). The PGNs then go
through the normal importer (chesscom.py), so fetched games are validated by python-chess
exactly like pasted ones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

API_ROOT = "https://api.chess.com/pub/player"
USER_AGENT = "ChessAI-tutor/1.0 (+https://github.com/Raaniscool/ChessAI)"
TIMEOUT = 20.0
MAX_MONTHS = 24          # how far back to look for enough games
MAX_FETCH = 100
USERNAME = re.compile(r"^[A-Za-z0-9_-]{3,25}$")


class ChessComFetchError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class FetchResult:
    username: str
    pgns: list[str] = field(default_factory=list)   # newest first
    skipped_variants: int = 0
    months_checked: int = 0


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                        follow_redirects=True, transport=transport)


def _get(client: httpx.Client, url: str) -> dict:
    try:
        res = client.get(url)
    except httpx.TimeoutException:
        raise ChessComFetchError("Chess.com didn't answer in time. Try again in a moment.", 504) from None
    except httpx.HTTPError as exc:
        raise ChessComFetchError(f"Couldn't reach Chess.com ({type(exc).__name__}). Check your internet "
                                 "connection.", 502) from None
    if res.status_code == 429:
        raise ChessComFetchError("Chess.com is rate-limiting requests right now. Wait a minute and try again.", 429)
    if res.status_code in (404, 410):
        raise ChessComFetchError("not found", 404)
    if res.status_code != 200:
        raise ChessComFetchError(f"Chess.com answered with an error ({res.status_code}). Try again later.", 502)
    try:
        return res.json()
    except ValueError:
        raise ChessComFetchError("Chess.com sent something that isn't valid data. Try again later.", 502) from None


def _with_link(pgn: str, url: str | None) -> str:
    """The importer identifies a game by its [Link] header; add it from the API if missing."""
    if not url or "[Link " in pgn:
        return pgn
    return f'[Link "{url}"]\n' + pgn.lstrip()


def fetch_recent_games(username: str, count: int, client: httpx.Client | None = None,
                       max_months: int = MAX_MONTHS) -> FetchResult:
    """The `count` most recent standard-chess games of `username`, newest first."""
    name = (username or "").strip()
    if not USERNAME.match(name):
        raise ChessComFetchError("That doesn't look like a Chess.com username (3–25 letters, numbers, _ or -).", 422)
    if not 1 <= count <= MAX_FETCH:
        raise ChessComFetchError(f"Choose between 1 and {MAX_FETCH} games.", 422)
    own = client is None
    client = client or make_client()
    result = FetchResult(username=name)
    try:
        try:
            archives = _get(client, f"{API_ROOT}/{name.lower()}/games/archives").get("archives") or []
        except ChessComFetchError as exc:
            if exc.status == 404:
                raise ChessComFetchError(f"There's no Chess.com player called “{name}”.", 404) from None
            raise
        games: list[dict] = []
        for month_url in reversed(archives[-max_months:]):  # newest month first
            if len(games) >= count:
                break
            result.months_checked += 1
            try:
                month = _get(client, month_url).get("games") or []
            except ChessComFetchError as exc:
                if exc.status == 404:  # an empty or removed month
                    continue
                raise
            for g in month:
                if g.get("rules", "chess") != "chess" or not g.get("pgn"):
                    result.skipped_variants += 1
                    continue
                games.append(g)
            games.sort(key=lambda g: g.get("end_time") or 0, reverse=True)
        result.pgns = [_with_link(g["pgn"], g.get("url")) for g in games[:count]]
        return result
    finally:
        if own:
            client.close()
