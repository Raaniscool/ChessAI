"""Difficulty profile: how hard puzzles should be for this learner, per skill, with the evidence.

Weakness and skill are separate questions. Weaknesses (analysis.history) say *what* to train.
This module says *how hard* — "challenging but realistically solvable" — from four kinds of
evidence, all on the puzzle scale (puzzles.profile decision ratings):

  1. Rating prior          the median rating of the learner's games (else onboarding rating,
                           else self-described experience): puzzle scale = 520 + 0.6 x rating.
                           Puzzle mode (you know there is a tactic) is easier than spotting it in
                           a game, so low-rated players solve puzzles rated above their game
                           rating; the gap shrinks with strength. Weight 2 — a starting point that
                           real evidence quickly outweighs. There is no rating -> "easy/medium/
                           hard" table anywhere.
  2. Game errors           mistakes+blunders per learner move (overall, opening, endgame) and
                           tactics walked into per move (defence), over >= 3 analyzed games:
                           rating = 400 - 1600 * log10(rate / base), base 0.12 (errors) and 0.05
                           (walked into), i.e. ten times fewer errors ~ 1600 points stronger.
                           Weight up to 3 (grows with the number of moves).
  3. Game chances          every position where the learner was suddenly winning
                           (analysis.skill_evidence): found or missed, rated from the winning move
                           itself. Elo performance over those trials + 100 (finding it in a game,
                           unprompted, is harder than in a puzzle). Weight up to 3. Split into
                           pattern recognition (checks/captures) and calculation (quiet moves,
                           sacrifices).
  4. Puzzle results        every finished puzzle (knowledge.usage stats): first-try solves = 1,
                           solved after a mistake = 0.6, hints cost 0.1 each, failed/revealed = 0;
                           a clean solve in <= 20 s counts as solving a puzzle 100 points harder,
                           a solve over 150 s counts 0.85. Elo performance; weight 0.4 per puzzle
                           (max 6), so ~5 puzzles count as much as the rating prior.

Each skill (overall, tactics, pattern_recognition, calculation, defense, endgame, opening) is the
weighted mean of the evidence that applies to it. A concept starts from its skill's estimate
(minus WEAKNESS_SHIFT when it is a weakness from the games — practise it slightly below your
level, never at kindergarten level) and moves with the learner's results on puzzles of exactly
that skill (anchor weight 2).

    target = concept estimate - ZONE_OFFSET    (~58% expected success: challenging, solvable)
    zone   = target - 120 .. target + 150      (the productive range a set spans)
    floor  = target - 250                      (below this a puzzle is trivial: not served
                                                while anything better exists)

Everything is deterministic (no randomness, no AI) and every target carries its evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import rating as R

PRIOR_BASE, PRIOR_SLOPE = 520, 0.6
PRIOR_WEIGHT = 2.0
MIN_GAMES = 3
ERROR_BASE, ALLOWED_BASE, ERROR_SPAN = 0.12, 0.05, 1600
ERROR_MOVES_PER_WEIGHT = 80      # learner moves per unit of weight (max GAME_MAX_WEIGHT)
GAME_MAX_WEIGHT = 3.0
MIN_PHASE_MOVES = 30
CHANCE_BONUS = 100
MIN_CHANCES = 3
PUZZLE_WEIGHT, PUZZLE_MAX_WEIGHT = 0.4, 6.0
FAST_SECONDS, FAST_BONUS = 20, 100
SLOW_SECONDS, SLOW_FACTOR = 150, 0.85
RECENT_DAYS = 30
CONCEPT_ANCHOR = 2.0
WEAKNESS_SHIFT = 50
ZONE_OFFSET = 60
ZONE = (-120, 150)
TRIVIAL_GAP = 250
SESSION_STEP = 100
MIN_SCALE, MAX_SCALE = 400, 2400
SKILLS = ("overall", "tactics", "pattern_recognition", "calculation", "defense", "endgame", "opening")
LEVEL_WORDS = ((800, "beginner"), (1100, "improving"), (1400, "intermediate"), (1800, "advanced"))


def puzzle_scale(game_rating: float) -> int:
    return int(round(PRIOR_BASE + PRIOR_SLOPE * game_rating))


def level_word(estimate: float) -> str:
    """For display only — selection never uses these words."""
    return next((w for top, w in LEVEL_WORDS if estimate < top), "expert")


def _clamp(x: float) -> int:
    return int(max(MIN_SCALE, min(MAX_SCALE, round(x))))


def from_error_rate(errors: int, moves: int, base: float = ERROR_BASE) -> int:
    """Game-scale rating implied by an error rate (smoothed: +0.5 error over +4 moves)."""
    rate = (errors + 0.5) / (moves + 4)
    return R.clamp(400 - ERROR_SPAN * math.log10(rate / base))


def performance(trials: list[tuple[float, float, float]], anchor: float, anchor_weight: float = 1.0) -> int:
    """Elo performance: R with sum w*(score - E(R, rating)) + anchor term = 0 (bisection, exact)."""
    def f(r: float) -> float:
        total = anchor_weight * (0.5 - R.expected(r, anchor))
        return total + sum(w * (s - R.expected(r, rating)) for rating, s, w in trials)
    lo, hi = 100.0, 3000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return int(round((lo + hi) / 2))


@dataclass
class Evidence:
    source: str
    value: int
    weight: float
    detail: str

    def as_dict(self) -> dict:
        return {"source": self.source, "value": self.value, "weight": round(self.weight, 2), "detail": self.detail}


@dataclass
class Skill:
    name: str
    estimate: int
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return round(sum(e.weight for e in self.evidence), 2)

    def as_dict(self) -> dict:
        return {"estimate": self.estimate, "level": level_word(self.estimate), "confidence": self.confidence,
                "evidence": [e.as_dict() for e in self.evidence]}


def _combine(name: str, items: list[Evidence]) -> Skill:
    weight = sum(e.weight for e in items)
    return Skill(name, _clamp(sum(e.value * e.weight for e in items) / weight), items)


# ---------------------------------------------------------------------- puzzle trials
def puzzle_dimension(p) -> list[str]:
    """Which skills a puzzle tests."""
    from ..puzzles.progression import stage_of
    out = ["overall"]
    if p.type == "defense":
        out.append("defense")
    elif p.type == "endgame":
        out.append("endgame")
    elif p.type == "opening":
        out.append("opening")
    else:
        out.append("tactics")
    stage = stage_of(p)
    if stage == "spot":
        out.append("pattern_recognition")
    if stage == "deep" or p.type == "calculation":
        out.append("calculation")
    return out


def trial(p, st: dict, now: datetime) -> tuple[float, float, float] | None:
    """(effective rating, score, weight) for one puzzle's results, or None if never finished."""
    n = int(st.get("attempts") or 0)
    if not n:
        return None
    ftr = float(st.get("first_try_rate") or 0)
    sr = float(st.get("success_rate") or 0)
    hints = int(st.get("hints_used") or 0)
    score = max(0.0, min(1.0, ftr + 0.6 * max(0.0, sr - ftr) - 0.1 * hints / n))
    rating = float(p.rating)
    secs = st.get("average_seconds")
    if ftr >= 1.0 and not hints and secs is not None and secs <= FAST_SECONDS:
        rating += FAST_BONUS
    elif secs is not None and secs > SLOW_SECONDS and score > 0:
        score *= SLOW_FACTOR
    weight = float(n)
    last = st.get("last_resolved")
    if last:
        try:
            then = datetime.fromisoformat(last)
            then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
            if (now - then).days > RECENT_DAYS:
                weight *= 0.5
        except ValueError:
            pass
    return rating, score, weight


def puzzle_trials(usage, lookup, now: datetime | None = None) -> list[tuple[object, tuple]]:
    """[(puzzle, trial)] for every finished puzzle the learner has results on."""
    now = now or datetime.now(timezone.utc)
    out = []
    stats = getattr(usage, "all_puzzle_stats", None)
    if stats is None:
        return out
    for pid, st in sorted(stats().items()):
        p = lookup(pid)
        if p is None:
            continue
        t = trial(p, st, now)
        if t is not None:
            out.append((p, t))
    return out


# ---------------------------------------------------------------------- the profile
@dataclass
class DifficultyProfile:
    skills: dict[str, Skill]
    prior: dict
    games: int
    puzzles: int
    trials: list = field(default_factory=list)   # [(puzzle, trial)]
    weaknesses: set = field(default_factory=set)

    def skill_for_concept(self, concept: str, knowledge) -> str:
        from ..puzzles.model import DEFENSIVE
        if concept in DEFENSIVE:
            return "defense"
        try:
            if "endgames" in knowledge.concepts and concept in knowledge.descendants("endgames"):
                return "endgame"
            if "openings" in knowledge.concepts and concept in knowledge.descendants("openings"):
                return "opening"
        except Exception:
            pass
        return "tactics"

    def concept_estimate(self, concept: str, knowledge) -> tuple[int, dict]:
        from ..puzzles.select import targets
        dim = self.skill_for_concept(concept, knowledge)
        base = self.skills[dim].estimate
        weak = concept in self.weaknesses
        anchor = base - (WEAKNESS_SHIFT if weak else 0)
        wanted = {c for c, (w, _k) in targets(concept, knowledge).items() if w >= 0.95} if concept in \
            knowledge.concepts else {concept}
        mine = [(p, t) for p, t in self.trials if p.concept in wanted]
        n = sum(t[2] for _p, t in mine)
        est = performance([t for _p, t in mine], anchor, CONCEPT_ANCHOR) if mine else anchor
        solved = sum(1 for _p, t in mine if t[1] >= 0.75)
        return _clamp(est), {
            "concept": concept, "skill": dim, "skill_estimate": base, "weakness": weak,
            "weakness_shift": -WEAKNESS_SHIFT if weak else 0, "puzzles": len(mine), "weight": round(n, 2),
            "solved_cleanly": solved, "estimate": _clamp(est),
            "detail": (f"{solved} of {len(mine)} puzzles of this skill solved cleanly" if mine
                       else "no puzzles of this skill yet: starts from your " + dim.replace("_", " ") + " level")}

    def target(self, concept: str | None, knowledge, session_shift: int = 0) -> dict:
        """The difficulty a set should aim at, with every piece of evidence behind it."""
        if concept and concept in knowledge.concepts:
            est, concept_info = self.concept_estimate(concept, knowledge)
            dim = concept_info["skill"]
        else:
            est, concept_info, dim = self.skills["overall"].estimate, None, "overall"
        center = _clamp(est - ZONE_OFFSET + session_shift)
        return {
            "target": center, "zone": [center + ZONE[0], center + ZONE[1]], "floor": center - TRIVIAL_GAP,
            "estimate": est, "skill": dim, "skill_level": level_word(self.skills[dim].estimate),
            "session_shift": session_shift, "concept": concept_info,
            "skill_evidence": self.skills[dim].as_dict(), "overall": self.skills["overall"].estimate,
            "rule": f"target = concept estimate {est} - {ZONE_OFFSET} (about 58% expected success)"
                    + (f" {session_shift:+d} this session" if session_shift else ""),
            "summary": summary_line(self, dim, concept_info),
        }

    def as_dict(self) -> dict:
        return {"skills": {k: s.as_dict() for k, s in self.skills.items()}, "prior": self.prior,
                "games": self.games, "puzzles": self.puzzles}


PRIOR_LABEL = {"median rating of your analyzed games": "your game rating", "rating you entered": "the rating you entered",
               "experience you described": "the experience you described",
               "default starting estimate": "a starting estimate"}


def summary_line(dp: DifficultyProfile, dim: str, concept_info: dict | None = None) -> str:
    """One short, honest line for the card: which level, from what (no numbers for the learner)."""
    parts = []
    if dp.games:
        parts.append(f"{dp.games} analyzed game{'s' if dp.games != 1 else ''}")
    if dp.puzzles:
        parts.append(f"{dp.puzzles} puzzle{'s' if dp.puzzles != 1 else ''}")
    basis = " and ".join(parts) if parts else PRIOR_LABEL.get(dp.prior["source"], dp.prior["source"])
    line = f"Aimed at your {dim.replace('_', ' ')} level ({level_word(dp.skills[dim].estimate)}), from {basis}"
    if concept_info and concept_info["puzzles"]:
        line += f"; {concept_info['solved_cleanly']}/{concept_info['puzzles']} of these solved cleanly so far"
    elif concept_info and concept_info["weakness"]:
        line += "; a weak spot, so pitched slightly lower"
    return line


def _prior(profile) -> dict:
    obs = getattr(profile, "game_observations", None) or {}
    ob = getattr(profile, "onboarding", None) or {}
    if obs.get("rating"):
        game, source = int(obs["rating"]), "median rating of your analyzed games"
    elif ob.get("rating"):
        game, source = R.normalize(ob["rating"], ob.get("platform")), "rating you entered"
    elif ob.get("experience"):
        game, source = R.from_experience(ob["experience"]), "experience you described"
    else:
        game, source = profile.rating if profile is not None else R.DEFAULT_RATING, "default starting estimate"
    return {"game_rating": game, "puzzle_scale": puzzle_scale(game), "source": source}


def build(profile, usage=None, lookup=None, now: datetime | None = None) -> DifficultyProfile:
    """The learner's difficulty profile from the profile (rating, game evidence) and puzzle results."""
    prior = _prior(profile)
    base = Evidence("rating", prior["puzzle_scale"], PRIOR_WEIGHT,
                    f"{prior['source']}: {prior['game_rating']} -> {prior['puzzle_scale']} on the puzzle scale")
    items: dict[str, list[Evidence]] = {k: [base] for k in SKILLS}
    gs = (getattr(profile, "game_skill", None) or {}) if profile is not None else {}
    games = int(gs.get("games") or 0)
    if games >= MIN_GAMES:
        def errors(skill: str, errs: int, moves: int, base_rate: float, what: str) -> None:
            if moves < MIN_PHASE_MOVES:
                return
            game_r = from_error_rate(errs, moves, base_rate)
            items[skill].append(Evidence("games", puzzle_scale(game_r),
                                         min(GAME_MAX_WEIGHT, moves / ERROR_MOVES_PER_WEIGHT),
                                         f"{errs} {what} in {moves} moves over {games} games"))
        errors("overall", gs["errors"], gs["moves"], ERROR_BASE, "mistakes/blunders")
        errors("defense", gs.get("allowed", 0), gs["moves"], ALLOWED_BASE, "times walked into a tactic")
        for phase, skill in (("opening", "opening"), ("endgame", "endgame")):
            c = (gs.get("phases") or {}).get(phase) or {}
            errors(skill, c.get("errors", 0), c.get("moves", 0), ERROR_BASE, f"{phase} mistakes/blunders")
        chances = (gs.get("chances") or {}).get("items") or []
        for skill, kinds in (("tactics", ("pattern", "calculation")), ("overall", ("pattern", "calculation")),
                             ("pattern_recognition", ("pattern",)), ("calculation", ("calculation",))):
            sub = [c for c in chances if c.get("kind") in kinds]
            if len(sub) < MIN_CHANCES:
                continue
            perf = performance([(c["rating"], 1.0 if c["found"] else 0.0, 1.0) for c in sub], prior["puzzle_scale"])
            found = sum(1 for c in sub if c["found"])
            items[skill].append(Evidence("game chances", _clamp(perf + CHANCE_BONUS), min(GAME_MAX_WEIGHT, len(sub) / 4),
                                         f"found {found} of {len(sub)} winning chances in your games"))
    trials = puzzle_trials(usage, lookup, now) if lookup is not None else []
    for skill in SKILLS:
        mine = [t for p, t in trials if skill in puzzle_dimension(p)]
        if len(mine) < 2:
            continue
        perf = performance(mine, prior["puzzle_scale"])
        clean = sum(1 for t in mine if t[1] >= 0.75)
        items[skill].append(Evidence("puzzles", _clamp(perf), min(PUZZLE_MAX_WEIGHT, PUZZLE_WEIGHT * sum(t[2] for t in mine)),
                                     f"{clean} of {len(mine)} puzzles solved cleanly"))
    weaknesses = {w.get("concept") for w in (getattr(profile, "weaknesses", None) or []) if w.get("concept")}
    return DifficultyProfile(skills={k: _combine(k, v) for k, v in items.items()}, prior=prior, games=games,
                             puzzles=len(trials), trials=trials, weaknesses=weaknesses)


def session_shift(results: list[dict]) -> tuple[int, str | None]:
    """In-session adjustment from the last two finished puzzles of the current set."""
    last = results[-2:]
    if len(last) < 2:
        return 0, None
    def instant(r):
        return r.get("solved") and r.get("first_try") and not r.get("hints") and not r.get("revealed") \
            and r.get("seconds") is not None and r["seconds"] <= FAST_SECONDS
    def struggled(r):
        return (not r.get("solved")) or r.get("revealed") or (r.get("hints") or 0) > 0
    if all(instant(r) for r in last):
        return SESSION_STEP, "Stepping up: you solved the last two instantly"
    if all(struggled(r) for r in last):
        return -SESSION_STEP, "Easing off a little: the last two were tough"
    return 0, None


def for_learner(knowledge=None, profile=None, usage=None) -> DifficultyProfile:
    """The current learner's difficulty profile (library + game puzzles resolve result ids)."""
    from ..knowledge.usage import get_usage
    from ..puzzles import get_puzzles
    from ..puzzles.from_game import get_game_puzzles
    from . import get_profile
    if profile is None:
        try:
            profile = get_profile()
        except Exception:  # no learner store: the default starting estimate
            profile = None
    usage = usage if usage is not None else get_usage()
    index = get_puzzles(knowledge)
    games = get_game_puzzles()
    return build(profile, usage, lambda pid: index.get(pid) or games.get(pid))
