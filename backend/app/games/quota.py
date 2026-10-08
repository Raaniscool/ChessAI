"""How many games the learner can have deeply analyzed (Stockfish), and what's left today.

Rules (the tier is configuration, CHESSAI_TIER; no payments are handled here):

- The first INITIAL_GAMES games are a one-time allowance ("analyze your last 25 games").
- On top of that, DAILY[tier] new games a day (free 10, paid 20). The daily allowance doesn't
  accumulate across days.
- A game is charged once. Re-analyzing it (a newer analyzer version, "reanalyze") is free, and
  so is a failed analysis: a game is charged only when its analysis is saved.
- Importing or downloading games is never limited, only engine analysis.

The free version is a complete loop. The limit only bounds how much engine time is spent per day.
"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from pathlib import Path

from ..config import get_settings

INITIAL_GAMES = 25
DAILY = {"free": 10, "paid": 20}
KEEP_DAYS = 14  # older day counters are dropped


class QuotaExceeded(Exception):
    def __init__(self, needed: int, status: dict):
        self.needed, self.status = needed, status
        left = status["left"]
        if left == 0:
            msg = (f"You've used today's {status['daily']['allowance']} game analyses"
                   f"{' and your first ' + str(INITIAL_GAMES) if status['initial']['left'] == 0 else ''}. "
                   f"More become available tomorrow.")
        else:
            msg = (f"You selected {needed} new game{'s' if needed != 1 else ''} to analyze, but "
                   f"{left} {'is' if left == 1 else 'are'} available right now. Choose {left} or fewer.")
        super().__init__(msg)


class AnalysisQuota:
    def __init__(self, path: Path | None = None, tier: str | None = None, enabled: bool | None = None,
                 today=None):
        settings = get_settings()
        self.path = Path(path) if path else settings.data_dir / "analysis_quota.json"
        self.tier = (tier or settings.analysis_tier or "free").lower()
        if self.tier not in DAILY:
            self.tier = "free"
        self.enabled = settings.analysis_limits if enabled is None else enabled
        self._today = today or date.today
        self._lock = threading.Lock()

    # --- storage
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"charged": list(data.get("charged", [])), "initial_used": int(data.get("initial_used", 0)),
                "days": dict(data.get("days", {}))}

    def _save(self, data: dict) -> None:
        cutoff = (self._today() - timedelta(days=KEEP_DAYS)).isoformat()
        data["days"] = {d: n for d, n in data["days"].items() if d >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # --- queries
    def _status(self, data: dict) -> dict:
        today = self._today()
        daily_allowance = DAILY[self.tier]
        daily_used = int(data["days"].get(today.isoformat(), 0))
        initial_left = max(0, INITIAL_GAMES - data["initial_used"])
        daily_left = max(0, daily_allowance - daily_used)
        return {
            "tier": self.tier, "enabled": self.enabled,
            "initial": {"allowance": INITIAL_GAMES, "used": data["initial_used"], "left": initial_left},
            "daily": {"allowance": daily_allowance, "used": daily_used, "left": daily_left,
                      "resets_on": (today + timedelta(days=1)).isoformat()},
            "left": (initial_left + daily_left) if self.enabled else None,
            "analyzed_games": len(data["charged"]),
        }

    def status(self) -> dict:
        with self._lock:
            return self._status(self._load())

    def new_games(self, game_ids) -> list[str]:
        """The ids among `game_ids` that would be charged (not analyzed before)."""
        with self._lock:
            charged = set(self._load()["charged"])
        seen, out = set(), []
        for gid in game_ids:
            if gid not in charged and gid not in seen:
                seen.add(gid)
                out.append(gid)
        return out

    def check(self, game_ids) -> list[str]:
        """Raise QuotaExceeded if the new games among `game_ids` don't fit; return them."""
        new = self.new_games(game_ids)
        if self.enabled and new:
            status = self.status()
            if len(new) > status["left"]:
                raise QuotaExceeded(len(new), status)
        return new

    def charge(self, game_id: str) -> bool:
        """Count one analyzed game (once per game). False if it was already counted."""
        with self._lock:
            data = self._load()
            if game_id in data["charged"]:
                return False
            data["charged"].append(game_id)
            if data["initial_used"] < INITIAL_GAMES:
                data["initial_used"] += 1
            else:
                key = self._today().isoformat()
                data["days"][key] = int(data["days"].get(key, 0)) + 1
            self._save(data)
            return True


_quota: AnalysisQuota | None = None


def get_quota() -> AnalysisQuota:
    global _quota
    if _quota is None:
        _quota = AnalysisQuota()
    return _quota


def set_quota(quota: AnalysisQuota | None) -> None:
    global _quota
    _quota = quota
