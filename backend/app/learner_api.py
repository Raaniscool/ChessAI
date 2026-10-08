"""/api/profile: onboarding, the learner model summary, preferences, suggestions.

A single local learner ("local") — ChessAI runs on the learner's own machine. The
Chess.com username is optional everywhere; nothing here needs it.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .learner import get_store, summary
from .learner.recommend import suggestions

router = APIRouter(prefix="/api/profile")

SPEEDS = ("slow", "normal", "fast")
READ_KEYS = ("lessons", "explanations", "hints", "puzzles", "analysis")


class Onboarding(BaseModel):
    rating: int | None = None
    platform: str | None = None       # chesscom | lichess | fide | otb | other | none
    experience: str | None = None     # new | rules | casual | club | strong (when no rating)
    username: str | None = None       # Chess.com, optional
    goals: list[str] | None = None
    explanation: str | None = None    # brief | balanced | detailed
    skipped: bool = False


class Preferences(BaseModel):
    explanation: str | None = None
    tts: dict | None = None           # {enabled, provider, voice, speed, read: {lessons, ...}}
    board_sounds: bool | None = None  # move / capture / check sounds on the board


def _library():
    try:
        from .knowledge.library import get_knowledge
        return get_knowledge()
    except Exception:  # pragma: no cover
        return None


def _out(profile) -> dict:
    from .learner.training_level import skill_profile
    library = _library()
    out = summary(profile, library)
    # one skill profile drives Puzzles and Training difficulty (games, puzzles, lesson results)
    out["skill_profile"] = skill_profile(profile, library) if library is not None else None
    return {"profile": out, "suggestions": suggestions(profile, library)}


@router.get("")
def get_profile_summary() -> dict:
    return _out(get_store().get())


@router.put("/onboarding")
def put_onboarding(body: Onboarding):
    store = get_store()
    try:
        store.update("local", lambda p: p.set_onboarding(**body.model_dump()))
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    return _out(store.get())


def clean_tts(raw: dict) -> dict:
    """Keep only known read-aloud settings, with safe values."""
    out: dict = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    for key in ("provider", "voice", "browser_voice"):
        if isinstance(raw.get(key), str) and len(raw[key]) <= 80:
            out[key] = raw[key]
    if raw.get("speed") in SPEEDS:
        out["speed"] = raw["speed"]
    if isinstance(raw.get("read"), dict):
        out["read"] = {k: bool(v) for k, v in raw["read"].items() if k in READ_KEYS}
    return out


@router.put("/preferences")
def put_preferences(body: Preferences):
    store = get_store()

    def change(p):
        if body.explanation is not None:
            if body.explanation not in ("brief", "balanced", "detailed"):
                raise ValueError("explanation must be brief, balanced or detailed")
            p.preferences["explanation"] = body.explanation
        if body.board_sounds is not None:
            p.preferences["board_sounds"] = bool(body.board_sounds)
        if body.tts is not None:
            p.preferences["tts"] = {**(p.preferences.get("tts") or {}), **clean_tts(body.tts)}
        p.touch()
    try:
        store.update("local", change)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    return _out(store.get())


@router.post("/reset")
def reset_profile() -> dict:
    return _out(get_store().reset("local"))


@router.get("/next")
def next_steps(concepts: str = "") -> dict:
    """Suggestions, optionally right after a lesson on `concepts` (comma-separated ids)."""
    just = [c for c in concepts.split(",") if c][:4]
    return {"suggestions": suggestions(get_store().get(), _library(), just_studied=just)}
