"""Fetching recent games by Chess.com username (official PubAPI), with a mocked HTTP transport.

The client must: find the newest months first, keep only standard chess, return the N most
recent games newest-first, stay serial, identify itself, and turn API errors (unknown player,
rate limit, network) into clear messages. The endpoint then imports the games through the
normal PGN importer and the history analysis can be restricted to that player.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.games.importers import chesscom_api
from app.games.importers.chesscom_api import ChessComFetchError, fetch_recent_games
from tests.test_game_history import FIRST_GAME, client, engine, game_pgn, gid, report_of, run_history  # noqa: F401

API = "https://api.chess.com/pub/player"
REAL_CLIENT = chesscom_api.make_client


def month(i_from: int, i_to: int, extra: list[dict] | None = None, drop_link: bool = False) -> dict:
    games = []
    for i in range(i_from, i_to + 1):
        pgn = game_pgn(i, "fork" if i % 2 else "clean")
        if drop_link:
            pgn = "\n".join(line for line in pgn.splitlines() if not line.startswith("[Link "))
        games.append({"url": f"https://www.chess.com/game/live/{FIRST_GAME + i}", "pgn": pgn,
                      "end_time": 1_700_000_000 + i * 3600, "rules": "chess", "time_class": "rapid"})
    return {"games": games + (extra or [])}


class FakeApi:
    """Serves archives for 'raantest': 2026/07 (games 1-4), 2026/08 (5-8), 2026/09 (9-12)."""

    def __init__(self, months: dict[str, dict] | None = None, status: dict[str, int] | None = None):
        self.months = months if months is not None else {
            "2026/07": month(1, 4), "2026/08": month(5, 8), "2026/09": month(9, 12)}
        self.status = status or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for fragment, code in self.status.items():
            if fragment in url:
                return httpx.Response(code, json={"code": 0, "message": "error"})
        if url == f"{API}/raantest/games/archives":
            return httpx.Response(200, json={"archives": [f"{API}/raantest/games/{m}" for m in sorted(self.months)]})
        for m, data in self.months.items():
            if url == f"{API}/raantest/games/{m}":
                return httpx.Response(200, json=data)
        return httpx.Response(404, json={"code": 0, "message": "not found"})

    def client(self) -> httpx.Client:
        return REAL_CLIENT(httpx.MockTransport(self))


def game_numbers(pgns: list[str]) -> list[int]:
    out = []
    for pgn in pgns:
        link = next(line for line in pgn.splitlines() if line.startswith("[Link "))
        out.append(int(link.split("/")[-1].rstrip('"]')) - FIRST_GAME)
    return out


# ---------------------------------------------------------------------------- the client
def test_fetches_the_most_recent_games_newest_first():
    api = FakeApi()
    result = fetch_recent_games("RaanTest", 6, client=api.client())
    assert game_numbers(result.pgns) == [12, 11, 10, 9, 8, 7]
    assert result.months_checked == 2  # stopped once it had enough: 2026/07 never requested
    assert not any("2026/07" in str(r.url) for r in api.requests)


def test_username_is_lowercased_in_the_url_and_requests_identify_the_app():
    api = FakeApi()
    fetch_recent_games("RaanTest", 2, client=api.client())
    assert str(api.requests[0].url) == f"{API}/raantest/games/archives"
    assert all("ChessAI" in r.headers["user-agent"] for r in api.requests)


def test_variants_and_games_without_pgn_are_skipped():
    extra = [{"url": "x960", "pgn": "[Variant \"Chess960\"]", "end_time": 1_800_000_000, "rules": "chess960"},
             {"url": "bug", "end_time": 1_800_000_001, "rules": "bughouse"},
             {"url": "nopgn", "end_time": 1_800_000_002, "rules": "chess"}]
    api = FakeApi({"2026/09": month(9, 12, extra=extra)})
    result = fetch_recent_games("raantest", 10, client=api.client())
    assert game_numbers(result.pgns) == [12, 11, 10, 9]
    assert result.skipped_variants == 3


def test_fewer_games_than_asked_returns_what_exists():
    api = FakeApi({"2026/09": month(9, 10)})
    result = fetch_recent_games("raantest", 10, client=api.client())
    assert game_numbers(result.pgns) == [10, 9]


def test_missing_link_header_is_added_from_the_api_url():
    api = FakeApi({"2026/09": month(9, 9, drop_link=True)})
    pgn = fetch_recent_games("raantest", 1, client=api.client()).pgns[0]
    assert pgn.startswith(f'[Link "https://www.chess.com/game/live/{FIRST_GAME + 9}"]')


def test_an_unavailable_month_is_skipped():
    api = FakeApi(status={"2026/09": 404})
    result = fetch_recent_games("raantest", 3, client=api.client())
    assert game_numbers(result.pgns) == [8, 7, 6]


@pytest.mark.parametrize("fragment,code,status,text", [
    ("archives", 404, 404, "no Chess.com player"),
    ("archives", 410, 404, "no Chess.com player"),
    ("archives", 429, 429, "rate-limiting"),
    ("2026/09", 429, 429, "rate-limiting"),
    ("archives", 500, 502, "error (500)"),
])
def test_api_errors_become_clear_messages(fragment, code, status, text):
    api = FakeApi(status={fragment: code})
    with pytest.raises(ChessComFetchError) as err:
        fetch_recent_games("raantest", 5, client=api.client())
    assert err.value.status == status and text in str(err.value)


def test_network_failure_is_explained():
    def boom(request):
        raise httpx.ConnectError("no route", request=request)
    with pytest.raises(ChessComFetchError) as err:
        fetch_recent_games("raantest", 5, client=chesscom_api.make_client(httpx.MockTransport(boom)))
    assert "internet" in str(err.value) and err.value.status == 502


@pytest.mark.parametrize("name", ["", "ab", "has space", "x" * 26, "../etc", "a/b"])
def test_bad_usernames_are_rejected_before_any_request(name):
    api = FakeApi()
    with pytest.raises(ChessComFetchError) as err:
        fetch_recent_games(name, 5, client=api.client())
    assert err.value.status == 422 and not api.requests


# ---------------------------------------------------------------------------- the endpoint
@pytest.fixture()
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(chesscom_api, "make_client", lambda transport=None: fake.client())
    return fake


def test_fetch_endpoint_imports_the_last_games(client, api):
    res = client.post("/api/games/fetch", json={"username": "RaanTest", "count": 10})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["fetched"] == 10 and body["new"] == 10 and body["username"] == "RaanTest"
    assert [g["id"] for g in body["imported"]][:2] == [gid(12), gid(11)]
    assert all(g["player"] == "RaanTest" for g in body["imported"])
    # fetching again recognizes the games: nothing is duplicated
    again = client.post("/api/games/fetch", json={"username": "raantest", "count": 10}).json()
    assert again["new"] == 0
    assert len(client.get("/api/games").json()["games"]) == 10


def test_fetch_then_history_analysis_covers_that_players_games(client, api):
    assert client.post("/api/games/fetch", json={"username": "RaanTest", "count": 10}).status_code == 200
    # someone else's game pasted earlier must not end up in RaanTest's history
    other = game_pgn(50, "fork").replace("RaanTest", "SomeoneElse")
    assert client.post("/api/games/import", json={"pgn": other, "username": "SomeoneElse"}).status_code == 200
    report = report_of(run_history(client, 10, username="RaanTest"))
    assert report["games_analyzed"] == 10
    assert gid(50) not in json.dumps(report)
    assert client.get("/api/games/history", params={"count": 10, "username": "raantest"}).json()["games_analyzed"] == 10


@pytest.mark.parametrize("status,body,code,text", [
    ({"archives": 404}, {"username": "RaanTest", "count": 10}, 404, "no Chess.com player"),
    ({"archives": 429}, {"username": "RaanTest", "count": 10}, 429, "rate-limiting"),
    ({}, {"username": "bad name", "count": 10}, 422, "username"),
    ({}, {"username": "RaanTest", "count": 101}, 422, "between 1 and 100"),  # downloads: 1..100 (analysis has its own rules)
])
def test_fetch_endpoint_errors(client, api, status, body, code, text):
    api.status = status
    res = client.post("/api/games/fetch", json=body)
    assert res.status_code == code
    assert text in res.json()["error"]


def test_fetch_endpoint_with_no_games(client, monkeypatch):
    fake = FakeApi(months={})
    monkeypatch.setattr(chesscom_api, "make_client", lambda transport=None: fake.client())
    res = client.post("/api/games/fetch", json={"username": "RaanTest", "count": 10})
    assert res.status_code == 404 and "No finished standard chess games" in res.json()["error"]
