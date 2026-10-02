"""Game-analysis allowance: the first 25 games, then 10 (free) or 20 (paid) new games a day.

Games are charged once, only when their analysis is saved; re-analysis and downloads are free.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.games.quota import DAILY, INITIAL_GAMES, AnalysisQuota, QuotaExceeded, set_quota
from tests.test_game_history import client, engine, gid, import_games, ndjson  # noqa: F401  (fixtures)


class Clock:
    def __init__(self, day=date(2026, 10, 1)):
        self.day = day

    def __call__(self):
        return self.day


def quota(tmp_path, tier="free", clock=None, enabled=True):
    return AnalysisQuota(tmp_path / "q.json", tier=tier, enabled=enabled, today=clock or Clock())


def test_first_25_then_the_daily_allowance(tmp_path):
    clock = Clock()
    q = quota(tmp_path, clock=clock)
    s = q.status()
    assert (s["initial"]["left"], s["daily"]["left"], s["left"]) == (25, 10, 35)
    for i in range(INITIAL_GAMES):
        assert q.charge(f"g{i}")
    s = q.status()
    assert (s["initial"]["left"], s["daily"]["left"], s["left"]) == (0, 10, 10)
    for i in range(10):
        q.charge(f"d{i}")
    assert q.status()["left"] == 0
    with pytest.raises(QuotaExceeded) as exc:
        q.check(["new"])
    assert "More become available tomorrow" in str(exc.value)
    clock.day += timedelta(days=1)  # a new day: the daily allowance is back (it doesn't accumulate)
    assert q.status()["left"] == 10 and q.status()["daily"]["used"] == 0


def test_paid_tier_has_20_a_day_and_unknown_tiers_are_free(tmp_path):
    assert quota(tmp_path, "paid").status()["daily"]["allowance"] == DAILY["paid"] == 20
    assert quota(tmp_path, "gold").status()["tier"] == "free"


def test_a_game_is_charged_once(tmp_path):
    q = quota(tmp_path)
    assert q.charge("g1") and not q.charge("g1")
    assert q.status()["initial"]["used"] == 1
    assert q.new_games(["g1", "g2", "g2"]) == ["g2"]


def test_a_selection_that_doesnt_fit_says_how_many_are_available(tmp_path):
    q = quota(tmp_path)
    for i in range(30):
        q.charge(f"g{i}")
    with pytest.raises(QuotaExceeded) as exc:
        q.check([f"n{i}" for i in range(6)] + ["g1"])  # g1 is already analyzed: not counted
    assert exc.value.needed == 6
    assert "5 are available right now. Choose 5 or fewer." in str(exc.value)


def test_limits_can_be_turned_off(tmp_path):
    q = quota(tmp_path, enabled=False)
    for i in range(60):
        q.charge(f"g{i}")
    assert q.check([f"n{i}" for i in range(50)]) and q.status()["left"] is None


# ---------------------------------------------------------------- API
@pytest.fixture()
def limited(tmp_path):
    q = quota(tmp_path)
    set_quota(q)
    yield q
    set_quota(None)


def test_the_game_list_shows_the_allowance(client, limited):
    import_games(client, ["clean", "fork"])
    res = client.get("/api/games").json()
    assert len(res["games"]) == 2 and res["quota"]["left"] == 35
    assert client.get("/api/games/quota").json()["initial"]["allowance"] == 25


def test_analyzing_a_chosen_set_analyzes_exactly_those_games(client, limited):
    import_games(client, {i: "fork" if i in (2, 5) else "clean" for i in range(1, 13)})
    chosen = [gid(2), gid(5), gid(9)]
    res = client.post("/api/games/history/analyze", json={"game_ids": chosen, "username": "RaanTest"})
    assert res.status_code == 200, res.text
    events = ndjson(res)
    assert events[0]["selected"] == [gid(9), gid(5), gid(2)]  # newest first
    done = events[-1]
    assert done["type"] == "done" and done["report"]["games_analyzed"] == 3
    assert done["quota"]["initial"]["used"] == 3 and done["quota"]["left"] == 32
    # analyzing them again (and with reanalyze) costs nothing
    again = ndjson(client.post("/api/games/history/analyze", json={"game_ids": chosen, "reanalyze": True}))
    assert again[-1]["quota"]["initial"]["used"] == 3


def test_a_selection_over_the_allowance_is_refused_before_any_engine_work(client, limited, engine):
    for i in range(33):
        limited.charge(f"earlier{i}")  # 2 left
    import_games(client, ["clean", "clean", "clean"])
    calls = engine.calls
    res = client.post("/api/games/history/analyze", json={"game_ids": [gid(1), gid(2), gid(3)]})
    assert res.status_code == 429
    body = res.json()
    assert body["needed"] == 3 and body["quota"]["left"] == 2 and "Choose 2 or fewer" in body["error"]
    assert engine.calls == calls
    ok = client.post("/api/games/history/analyze", json={"game_ids": [gid(1), gid(2)]})
    assert ok.status_code == 200 and ndjson(ok)[-1]["quota"]["left"] == 0


def test_single_game_analysis_is_counted_too(client, limited):
    import_games(client, ["fork"])
    events = ndjson(client.post("/api/games/analyze", json={"game_ids": [gid(1)]}))
    assert events[-1]["quota"]["initial"]["used"] == 1


def test_games_analyzed_before_limits_existed_are_reanalyzed_for_free(client, limited):
    import_games(client, ["fork", "clean"])
    from app.games import store
    store.save_analysis(gid(1), {"schema_version": -1, "moments": [], "stats": {}})  # an old analysis
    events = ndjson(client.post("/api/games/analyze", json={"game_ids": [gid(1), gid(2)]}))
    assert events[-1]["quota"]["initial"]["used"] == 1  # only the never-analyzed game counts


def test_unknown_chosen_games_are_a_clear_error(client, limited):
    res = client.post("/api/games/history/analyze", json={"game_ids": ["chesscom-1"]})
    assert res.status_code == 422 and "import them first" in res.json()["error"]


def test_fetch_defaults_to_100_and_accepts_small_counts():
    from app.analysis import history
    from app.game_api import FetchRequest
    assert FetchRequest(username="x").count == 100
    assert history.validate_fetch_count(1) == 1
    with pytest.raises(history.HistoryError):
        history.validate_fetch_count(101)
