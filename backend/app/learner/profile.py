"""The learner model: what the tutor knows about one learner, updated from what they do.

    onboarding       rating / platform / experience / goals / optional Chess.com username
    skill            overall rating on the tutor's scale, and how sure we are of it
    concepts         per library concept: attempts, first-try solves, hints, reveals, a
                     per-concept rating, recent results (split into one-move and
                     multi-move positions), game evidence, last seen, and explicit
                     explanation requests (learner.help) kept apart from the results
    exercise_log     the last MAX_LOG resolved exercises with their context (lesson/session,
                     exercise, concept, difficulty, result, hints, reveal, explanation requests)
    weaknesses       recurring patterns from the learner's own games (Game History)
    stats            puzzle / lesson / hint totals
    recent topics    what was studied lately
    preferences      explanation style, read-aloud settings

Everything derived (status per concept, mastered / practicing / needs review, the
difficulty window, "understands it but can't calculate it", "solves puzzles but misses
it in games") is computed from those facts in ``views``: nothing is a stored opinion.

Profiles are private to the learner (DATA_DIR/learners/<id>.json) and never enter the
shared Knowledge Library.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import rating as R
from .help import KINDS as HELP_KINDS, aligned_flags

RECENT = 10                 # results remembered per concept
MASTERY_WINDOW = 6          # results a mastery decision looks at
MASTERED_SCORE = 0.8
WEAK_SCORE = 0.45
MIN_FOR_MASTERY = 4
MIN_FOR_WEAK = 3
REVIEW_AFTER = {"mastered": 21, "practicing": 5, "learned": 3, "weak": 2}  # days
MAX_RECENT_TOPICS = 20
MAX_LOG = 200               # exercise_log entries kept (oldest dropped)
EXPLANATION_STYLES = ("brief", "balanced", "detailed")
GOALS = ("tactics", "openings", "endgames", "checkmates", "stop_blundering", "calculation", "strategy",
         "rating")
DEFAULT_ID = "local"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class ConceptState:
    attempts: int = 0
    solved: int = 0
    first_try: int = 0
    hints: int = 0
    reveals: int = 0
    rating: int | None = None             # per-concept skill (same scale as puzzle ratings)
    recent: list[float] = field(default_factory=list)        # scores, newest last
    recent_short: list[float] = field(default_factory=list)  # one-move positions
    recent_long: list[float] = field(default_factory=list)   # positions needing 2+ moves
    lessons_started: int = 0
    lessons_completed: int = 0
    game_misses: int = 0                  # games where this was a recurring mistake
    game_evidence_games: int = 0
    last_seen: str | None = None
    last_result: str | None = None        # solved | failed
    # explicit explanation requests (learner.help): never part of the scores above
    explain_requests: int = 0             # every request about this concept
    explain_kinds: dict = field(default_factory=dict)     # kind -> count (explain / deeper / question)
    explained: int = 0                    # resolved exercises with at least one request
    recent_explained: list[int] = field(default_factory=list)  # 1/0 per entry of `recent`

    @classmethod
    def from_dict(cls, d: dict) -> "ConceptState":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class LearnerProfile:
    id: str = DEFAULT_ID
    created: str = field(default_factory=_iso)
    updated: str = field(default_factory=_iso)
    onboarding: dict = field(default_factory=lambda: {"done": False})
    skill: dict = field(default_factory=lambda: {"rating": R.DEFAULT_RATING, "source": "default", "evidence": 0})
    concepts: dict[str, ConceptState] = field(default_factory=dict)
    weaknesses: list[dict] = field(default_factory=list)
    game_observations: dict = field(default_factory=dict)
    game_skill: dict = field(default_factory=dict)   # analysis.skill_evidence over all analyzed games
    stats: dict = field(default_factory=lambda: {
        "puzzles": {"attempts": 0, "solved": 0, "first_try": 0},
        "lessons": {"started": 0, "completed": 0}, "hints_used": 0, "reveals": 0,
        "explanations": {"explain": 0, "deeper": 0, "question": 0}})
    recent_topics: list[dict] = field(default_factory=list)
    completed_lessons: list[str] = field(default_factory=list)
    preferences: dict = field(default_factory=lambda: {"explanation": "balanced"})
    exercise_log: list[dict] = field(default_factory=list)
    training: dict = field(default_factory=dict)     # assessment.record: Training segments' evidence

    # ------------------------------------------------------------------ io
    def as_dict(self) -> dict:
        d = asdict(self)
        d["concepts"] = {k: asdict(v) for k, v in self.concepts.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LearnerProfile":
        p = cls()
        for name in cls.__dataclass_fields__:
            if name in d and name != "concepts":
                setattr(p, name, d[name])
        p.concepts = {k: ConceptState.from_dict(v) for k, v in (d.get("concepts") or {}).items()}
        base = cls()
        for key, value in base.stats.items():  # older files: fill new counters
            p.stats.setdefault(key, value)
        return p

    # ------------------------------------------------------------------ basics
    @property
    def rating(self) -> int:
        return int(self.skill.get("rating") or R.DEFAULT_RATING)

    @property
    def level(self) -> str:
        return R.level_for(self.rating)

    @property
    def explanation_style(self) -> str:
        style = self.preferences.get("explanation")
        return style if style in EXPLANATION_STYLES else "balanced"

    @property
    def is_new(self) -> bool:
        return not self.onboarding.get("done") and not self.concepts and not self.weaknesses

    def concept(self, cid: str) -> ConceptState:
        return self.concepts.setdefault(cid, ConceptState())

    def concept_rating(self, cid: str) -> int:
        st = self.concepts.get(cid)
        return st.rating if st and st.rating is not None else self.rating

    # ------------------------------------------------------------------ onboarding
    def set_onboarding(self, *, rating: int | None = None, platform: str | None = None,
                       experience: str | None = None, username: str | None = None,
                       goals: list[str] | None = None, explanation: str | None = None,
                       skipped: bool = False) -> None:
        ob = dict(self.onboarding)
        if platform is not None:
            if platform not in R.PLATFORMS:
                raise ValueError(f"platform must be one of {', '.join(R.PLATFORMS)}")
            ob["platform"] = platform
        if experience is not None:
            if experience not in R.EXPERIENCE:
                raise ValueError(f"experience must be one of {', '.join(R.EXPERIENCE)}")
            ob["experience"] = experience
        if rating is not None:
            if not 100 <= int(rating) <= 3500:
                raise ValueError("a rating should be between 100 and 3500")
            ob["rating"] = int(rating)
        elif experience is not None:
            ob.pop("rating", None)  # "no rating, I play casually" replaces an earlier rating
        if username is not None:
            name = username.strip()
            if name and not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name):
                raise ValueError("that doesn't look like a Chess.com username")
            ob["chesscom_username"] = name or None
        if goals is not None:
            ob["goals"] = [g for g in dict.fromkeys(goals) if g in GOALS]
        if explanation is not None:
            if explanation not in EXPLANATION_STYLES:
                raise ValueError(f"explanation must be one of {', '.join(EXPLANATION_STYLES)}")
            self.preferences["explanation"] = explanation
        ob["done"] = True
        ob["skipped"] = bool(skipped)
        ob["at"] = _iso()
        self.onboarding = ob
        # The starting estimate: a real rating beats a self-description; results refine it later.
        if self.skill.get("source") in ("default", "onboarding", "experience"):
            if ob.get("rating"):
                self.skill = {"rating": R.normalize(ob["rating"], ob.get("platform")), "source": "onboarding",
                              "evidence": self.skill.get("evidence", 0)}
            elif ob.get("experience"):
                self.skill = {"rating": R.from_experience(ob["experience"]), "source": "experience",
                              "evidence": self.skill.get("evidence", 0)}
        self.touch()

    def touch(self) -> None:
        self.updated = _iso()

    # ------------------------------------------------------------------ events
    def record_attempt(self, concept: str, puzzle_rating: int, *, solved: bool, first_try: bool,
                       hints: int = 0, revealed: bool = False, learner_moves: int = 1,
                       explanations: dict | None = None, context: dict | None = None) -> dict:
        """One resolved exercise. Returns what changed (for adaptation / the UI).

        `explanations`: explicit requests made while solving it (kind -> count, learner.help);
        they are logged with the result but never change its score or any rating.
        `context`: source / lesson_id / session_id / exercise_id for the exercise log."""
        st = self.concept(concept)
        before = self.concept_rating(concept)
        score = R.score_for(solved, first_try, hints, revealed)
        st.rating = R.updated(before, puzzle_rating, score, st.attempts)
        st.attempts += 1
        st.solved += int(solved and not revealed)
        st.first_try += int(solved and first_try and not revealed and hints == 0)
        st.hints += max(0, hints)
        st.reveals += int(revealed)
        asked = {k: int(v) for k, v in (explanations or {}).items() if k in HELP_KINDS and int(v) > 0}
        flags = aligned_flags(st.recent, st.recent_explained)
        st.recent = (st.recent + [score])[-RECENT:]
        st.recent_explained = (flags + [int(bool(asked))])[-RECENT:]
        for kind, n in asked.items():
            self._count_request(st, kind, n)
        st.explained += int(bool(asked))
        bucket = st.recent_long if learner_moves >= 2 else st.recent_short
        bucket.append(score)
        del bucket[:-RECENT]
        st.last_seen = _iso()
        st.last_result = "solved" if score > 0 else "failed"
        # the overall estimate moves too, more slowly (one concept isn't the whole player)
        total = self.stats["puzzles"]["attempts"]
        self.skill = {**self.skill, "rating": R.clamp(self.rating + 0.5 * R.k_factor(total) *
                                                     (score - R.expected(self.rating, puzzle_rating))),
                      "evidence": self.skill.get("evidence", 0) + 1}
        if self.skill.get("source") == "default":
            self.skill["source"] = "results"
        p = self.stats["puzzles"]
        p["attempts"] += 1
        p["solved"] += int(solved and not revealed)
        p["first_try"] += int(solved and first_try and not revealed and hints == 0)
        self.stats["hints_used"] += max(0, hints)
        self.stats["reveals"] += int(revealed)
        self._topic(concept)
        ctx = context or {}
        self._log({"at": _iso(), "source": ctx.get("source", "lesson"), "lesson_id": ctx.get("lesson_id"),
                   "session_id": ctx.get("session_id"), "exercise_id": ctx.get("exercise_id"),
                   "concept": concept, "difficulty": int(puzzle_rating),
                   "result": "revealed" if revealed else ("solved" if solved else "failed"),
                   "solved": bool(solved and not revealed), "first_try": bool(first_try), "hints": max(0, hints),
                   "revealed": bool(revealed), "score": score, "explanation_requested": bool(asked),
                   "explanations": {k: asked.get(k, 0) for k in HELP_KINDS}, "seq": st.attempts})
        self.touch()
        return {"concept": concept, "score": score, "rating_before": before, "rating_after": st.rating}

    def record_explanation(self, concept: str, kind: str, *, exercise_id: str | None = None,
                           session_id: str | None = None, lesson_id: str | None = None) -> dict:
        """An explicit explanation request about an exercise that is already resolved (e.g.
        "Explain deeper" after the last move), or about a worked example with nothing to solve.
        It is attached to that exercise's logged result. Scores and ratings stay as they were."""
        if kind not in HELP_KINDS:
            raise ValueError(f"kind must be one of {', '.join(HELP_KINDS)}")
        st = self.concept(concept)
        self._count_request(st, kind, 1)
        entry = None
        if exercise_id is not None:
            entry = next((e for e in reversed(self.exercise_log) if e.get("exercise_id") == exercise_id
                          and e.get("session_id") == session_id and e.get("concept") == concept
                          and e.get("result") is not None), None)
        newly = False
        if entry is not None:
            entry["explanations"][kind] = entry["explanations"].get(kind, 0) + 1
            if not entry["explanation_requested"]:
                entry["explanation_requested"] = newly = True
                st.explained += 1
                back = st.attempts - int(entry.get("seq", st.attempts))  # results recorded since
                flags = aligned_flags(st.recent, st.recent_explained)
                if 0 <= back < len(flags):
                    flags[len(flags) - 1 - back] = 1
                st.recent_explained = flags
        else:  # a worked example (nothing to solve): the request is logged on its own
            self._log({"at": _iso(), "source": "lesson", "lesson_id": lesson_id, "session_id": session_id,
                       "exercise_id": exercise_id, "concept": concept, "result": None,
                       "explanation_requested": True, "explanations": {k: int(k == kind) for k in HELP_KINDS}})
        st.last_seen = _iso()
        self.touch()
        return {"concept": concept, "kind": kind, "attached": entry is not None, "newly_explained": newly}

    def _count_request(self, st: ConceptState, kind: str, n: int) -> None:
        st.explain_requests += n
        st.explain_kinds[kind] = st.explain_kinds.get(kind, 0) + n
        counts = self.stats.setdefault("explanations", {k: 0 for k in HELP_KINDS})
        counts[kind] = counts.get(kind, 0) + n

    def _log(self, entry: dict) -> None:
        self.exercise_log = (self.exercise_log + [entry])[-MAX_LOG:]

    def record_lesson(self, concepts: list[str], *, completed: bool, lesson_id: str | None = None) -> None:
        for cid in dict.fromkeys(concepts):
            st = self.concept(cid)
            if completed:
                st.lessons_completed += 1
            else:
                st.lessons_started += 1
            st.last_seen = _iso()
            self._topic(cid)
        key = "completed" if completed else "started"
        self.stats["lessons"][key] += 1
        if completed and lesson_id and lesson_id not in self.completed_lessons:
            self.completed_lessons.append(lesson_id)
        self.touch()

    def _topic(self, concept: str) -> None:
        self.recent_topics = [t for t in self.recent_topics if t["concept"] != concept]
        self.recent_topics.append({"concept": concept, "at": _iso()})
        self.recent_topics = self.recent_topics[-MAX_RECENT_TOPICS:]

    def update_from_history(self, report: dict, *, ratings: list[int] | None = None,
                            username: str | None = None, game_skill: dict | None = None) -> None:
        """Game History findings become part of the learner model (they are evidence, not verdicts)."""
        recurring = [p for p in report.get("patterns", []) if p.get("tier") in ("recurring", "occasional")]
        self.weaknesses = [{
            "key": p["key"], "concept": p.get("concept"), "title": p.get("title"), "tier": p["tier"],
            "kind": p.get("kind"), "game_count": p.get("game_count"), "total_games": p.get("total_games"),
            "occurrences": p.get("occurrences"),
            "significance": p.get("significance"), "updated": _iso()} for p in recurring][:10]
        for p in recurring:
            if p.get("concept") and p["tier"] == "recurring":
                st = self.concept(p["concept"])
                st.game_misses = max(st.game_misses, int(p.get("occurrences") or p.get("game_count") or 1))
                st.game_evidence_games = int(p.get("game_count") or 0)
        self.game_observations = {
            "games_analyzed": report.get("games_analyzed", 0), "results": report.get("results"),
            "mistakes_per_game": (report.get("mistakes") or {}).get("per_game"),
            "mistakes_by_phase": report.get("mistakes_by_phase") or {},
            "openings": [{"name": o.get("name"), "games": o.get("games"), "score": o.get("score")}
                         for o in (report.get("openings") or [])][:6],
            "username": username, "updated": _iso()}
        if ratings:
            recent = sorted(ratings)[len(ratings) // 2]  # median: one lucky or unlucky game doesn't count
            # A real rating from real games replaces a self-estimate; puzzle results keep refining it.
            if self.skill.get("source") in ("default", "experience", "onboarding", "games"):
                self.skill = {"rating": R.normalize(recent, "chesscom"), "source": "games",
                              "evidence": self.skill.get("evidence", 0)}
            self.game_observations["rating"] = recent
        if game_skill is not None:
            self.game_skill = game_skill
        self.touch()


# ---------------------------------------------------------------------- store
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name or DEFAULT_ID)[:60] or DEFAULT_ID


class LearnerStore:
    def __init__(self, root: Path | None = None):
        if root is None:
            from ..config import get_settings
            root = Path(get_settings().data_dir) / "learners"
        self.root = Path(root)
        self._lock = threading.RLock()
        self._cache: dict[str, LearnerProfile] = {}

    def path(self, learner_id: str) -> Path:
        return self.root / f"{_safe(learner_id)}.json"

    def get(self, learner_id: str = DEFAULT_ID) -> LearnerProfile:
        lid = _safe(learner_id)
        with self._lock:
            if lid not in self._cache:
                try:
                    data = json.loads(self.path(lid).read_text(encoding="utf-8"))
                    self._cache[lid] = LearnerProfile.from_dict(data)
                except (OSError, ValueError):
                    self._cache[lid] = LearnerProfile(id=lid)
            return self._cache[lid]

    def save(self, profile: LearnerProfile) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(profile.as_dict(), fh, indent=1)
            os.replace(tmp, self.path(profile.id))
            self._cache[_safe(profile.id)] = profile

    def update(self, learner_id: str, fn):
        """Load, change, save atomically (one learner's events never interleave)."""
        with self._lock:
            profile = self.get(learner_id)
            out = fn(profile)
            self.save(profile)
            return out

    def reset(self, learner_id: str = DEFAULT_ID) -> LearnerProfile:
        with self._lock:
            try:
                self.path(learner_id).unlink()
            except OSError:
                pass
            self._cache.pop(_safe(learner_id), None)
            return self.get(learner_id)


_store: LearnerStore | None = None
_store_lock = threading.Lock()


def get_store() -> LearnerStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = LearnerStore()
        return _store


def set_store(store: LearnerStore | None) -> None:
    global _store
    with _store_lock:
        _store = store


def get_profile(learner_id: str = DEFAULT_ID) -> LearnerProfile:
    return get_store().get(learner_id)


def days_since(ts: str | None, now: datetime | None = None) -> float | None:
    dt = _parse(ts)
    if dt is None:
        return None
    return ((now or _now()) - dt) / timedelta(days=1)
