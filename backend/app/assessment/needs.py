"""What the learner needs most right now, per concept and per phase — with confidence and reasons.

One model, read by the Training feedback, Practice (weighting Mixed sets) and Personalized (cards).

Per concept, every observation is a score in 0..1 (1 = handled it, 0 = didn't) with a weight:
    training   hidden idea found (1) or missed (0); a validator-named mistake (0)     weight 1.0
    puzzles    the concept's recent puzzle scores (learner profile, newest last)       weight 0.5
               (puzzle mode tells you there is something to find: weaker evidence)
    games      a recurring mistake from analysed games: one 0 per game it appeared in  weight 1.0
               (at most GAME_CAP games)

    struggle   = (sum w*d*(1 - score) + PRIOR_WEIGHT * BASELINE) / (sum w*d + PRIOR_WEIGHT)
                 with d = 0.5 ** (age / HALF_LIFE), age = observations since this one within its
                 source (newest = 0): recent evidence counts more, so the estimate follows
                 improvement; `long_term` is the same without the decay
    confidence = n / (n + PRIOR_WEIGHT), n = sum of weights (how much evidence there is)
    priority   = max(0, struggle - BASELINE) * confidence

So a single miss moves little (struggle 0.35 -> 0.51 with confidence 0.25: priority 0.04) and a run
of misses moves a lot; solving it again lowers it step by step. BASELINE is the share of these
positions a learner is expected to miss anyway (they are chosen at a challenging level).

Status: insufficient (n < MIN_EVIDENCE) | needs_work (struggle >= NEEDS_WORK) | improving (long-term
was a need, recent clearly better) | solid (struggle <= SOLID) | ok. Every concept carries reasons
in plain words ("Training: missed 3 of 4 chances to use a pin"). No AI, no hidden score.

Per phase (opening/middlegame/endgame): mistakes+blunders per move from Training and analysed games,
shrunk towards the usual rate (learner.difficulty.ERROR_BASE) with PHASE_PRIOR_MOVES moves; recent
and long-term Training accuracy.
"""
from __future__ import annotations

PRIOR_WEIGHT = 3.0
BASELINE = 0.35
HALF_LIFE = 4.0
MIN_EVIDENCE = 3.0
NEEDS_WORK = 0.5
SOLID = 0.2
TREND_GAP = 0.12
GAME_CAP = 6
SOURCE_WEIGHT = {"training": 1.0, "puzzles": 0.5, "games": 1.0}
PHASE_PRIOR_MOVES = 20
PHASE_MIN_MOVES = 10
PHASE_NEEDS_WORK, PHASE_SOLID = 0.18, 0.07
FOCUS_MAX = 0.5             # Practice: the most a need can raise (or hold back) a puzzle's score
FOCUS_FULL = 0.3            # ... reached at this priority
PHASE_NAMES = {"opening": "beginning of the game", "middlegame": "middlegame", "endgame": "endgame"}


def _name(knowledge, concept: str) -> str:
    c = knowledge.concepts.get(concept) if knowledge is not None else None
    return c.name if c else concept.replace("_", " ")


def _estimate(obs: list[tuple[float, float, int]], decay: bool) -> float:
    num = PRIOR_WEIGHT * BASELINE
    den = PRIOR_WEIGHT
    for score, weight, age in obs:
        d = 0.5 ** (age / HALF_LIFE) if decay else 1.0
        num += weight * d * (1 - score)
        den += weight * d
    return num / den


def _aged(scores: list[float], weight: float) -> list[tuple[float, float, int]]:
    n = len(scores)
    return [(float(s), weight, n - 1 - i) for i, s in enumerate(scores)]


def concept_needs(profile, knowledge) -> dict[str, dict]:
    from . import record
    training = record.ensure(getattr(profile, "training", None) if profile is not None else None)
    concepts: set[str] = set(training["concepts"])
    puzzle_states = (getattr(profile, "concepts", None) or {}) if profile is not None else {}
    concepts |= {c for c, st in puzzle_states.items() if getattr(st, "recent", None)}
    weaknesses = {}
    for w in (getattr(profile, "weaknesses", None) or []) if profile is not None else []:
        if w.get("concept"):
            weaknesses[w["concept"]] = max(weaknesses.get(w["concept"], 0), int(w.get("game_count") or 0))
    concepts |= set(weaknesses)
    out = {}
    for concept in concepts:
        if knowledge is not None and concept not in knowledge.concepts:
            continue
        name = _name(knowledge, concept)
        obs: list[tuple[float, float, int]] = []
        reasons: list[str] = []
        sources: dict = {}
        tr = training["concepts"].get(concept)
        if tr and tr.get("events"):
            events = tr["events"]
            t_obs = _aged([e["r"] for e in events], SOURCE_WEIGHT["training"])
            obs += t_obs
            found, missed, allowed = tr.get("found", 0), tr.get("missed", 0), tr.get("allowed", 0)
            sources["training"] = {"trials": tr.get("trials", 0), "found": found, "missed": missed,
                                   "allowed": allowed, "struggle": round(_estimate(t_obs, True), 3),
                                   "confidence": round(len(t_obs) / (len(t_obs) + PRIOR_WEIGHT), 3)}
            parts = []
            tested = found + sum(1 for e in events if e["kind"] == "tested" and e["r"] == 0)
            if tested:
                parts.append(f"found {found} of {tested} hidden {name.lower()} chances")
            m_only = sum(1 for e in events if e["kind"] == "missed")
            if m_only:
                parts.append(f"missed {m_only} more {'chance' if m_only == 1 else 'chances'} to use it")
            if allowed:
                parts.append(f"allowed it against you {allowed} {'time' if allowed == 1 else 'times'}")
            if parts:
                reasons.append("Training: " + ", ".join(parts))
        st = puzzle_states.get(concept)
        recent = list(getattr(st, "recent", None) or []) if st is not None else []
        if recent:
            obs += _aged(recent, SOURCE_WEIGHT["puzzles"])
            solved = sum(1 for s in recent if s >= 0.75)
            sources["puzzles"] = {"recent": len(recent), "solved": solved,
                                  "mean": round(sum(recent) / len(recent), 3)}
            reasons.append(f"Puzzles: solved {solved} of your last {len(recent)}")
        games = min(GAME_CAP, weaknesses.get(concept, 0))
        if games:
            obs += [(0.0, SOURCE_WEIGHT["games"], i) for i in range(games)]
            sources["games"] = {"games": weaknesses[concept]}
            reasons.append(f"Your games: a recurring mistake in {weaknesses[concept]} analysed "
                           f"{'game' if weaknesses[concept] == 1 else 'games'}")
        if not obs:
            continue
        n = sum(w for _s, w, _a in obs)
        struggle, long_term = _estimate(obs, True), _estimate(obs, False)
        confidence = n / (n + PRIOR_WEIGHT)
        priority = max(0.0, struggle - BASELINE) * confidence
        if n < MIN_EVIDENCE:
            status = "insufficient"
        elif long_term >= NEEDS_WORK and struggle <= long_term - TREND_GAP:
            status = "improving"
        elif struggle >= NEEDS_WORK:
            status = "needs_work"
        elif struggle <= SOLID:
            status = "solid"
        else:
            status = "ok"
        trend = "improving" if struggle <= long_term - TREND_GAP else \
            "slipping" if struggle >= long_term + TREND_GAP else None
        out[concept] = {"concept": concept, "name": name, "struggle": round(struggle, 3),
                        "long_term": round(long_term, 3), "confidence": round(confidence, 3),
                        "evidence": round(n, 2), "priority": round(priority, 4), "status": status, "trend": trend,
                        "reasons": reasons, "sources": sources, "why": why_line(name, status, confidence, reasons)}
    return out


def why_line(name: str, status: str, confidence: float, reasons: list[str]) -> str:
    sure = "fairly sure" if confidence >= 0.6 else "not sure yet" if confidence < 0.4 else "getting clearer"
    head = {"needs_work": f"{name} needs practice", "improving": f"{name} is improving",
            "solid": f"{name} looks solid", "ok": f"{name} is going fine",
            "insufficient": f"Not enough evidence about {name.lower()} yet"}[status]
    return f"{head} ({sure}). " + "; ".join(reasons) + ("." if reasons else "")


def phase_needs(profile) -> dict[str, dict]:
    from ..learner.difficulty import ERROR_BASE
    from . import record
    training = record.ensure(getattr(profile, "training", None) if profile is not None else None)
    games = ((getattr(profile, "game_skill", None) or {}).get("phases") or {}) if profile is not None else {}
    out = {}
    for phase in record.PHASES:
        t = training["phases"][phase]
        g = games.get(phase) or {}
        moves, errors = t["moves"] + int(g.get("moves") or 0), t["errors"] + int(g.get("errors") or 0)
        rate = (errors + ERROR_BASE * PHASE_PRIOR_MOVES) / (moves + PHASE_PRIOR_MOVES)
        confidence = moves / (moves + 30)
        acc = t["accuracy"]
        recent = round(sum(acc[-5:]) / len(acc[-5:]), 1) if acc else None
        long_term = round(sum(acc) / len(acc), 1) if acc else None
        if moves < PHASE_MIN_MOVES:
            status = "insufficient"
        elif rate >= PHASE_NEEDS_WORK:
            status = "needs_work"
        elif rate <= PHASE_SOLID:
            status = "solid"
        else:
            status = "ok"
        reasons = []
        if t["moves"]:
            reasons.append(f"Training: {t['errors']} mistakes in {t['moves']} moves over {t['segments']} "
                           f"{'segment' if t['segments'] == 1 else 'segments'}"
                           + (f", recent accuracy {recent:.0f}%" if recent is not None else ""))
        if g.get("moves"):
            reasons.append(f"Your games: {g.get('errors', 0)} mistakes in {g['moves']} moves")
        out[phase] = {"phase": phase, "name": PHASE_NAMES[phase], "segments": t["segments"],
                      "moves": moves, "errors": errors, "error_rate": round(rate, 3),
                      "confidence": round(confidence, 3), "accuracy_recent": recent, "accuracy_long_term": long_term,
                      "avg_loss_cp": round(t["loss_sum"] / t["moves"]) if t["moves"] else None,
                      "best_move_rate": round(t["best"] / t["moves"], 2) if t["moves"] else None,
                      "status": status, "reasons": reasons}
    return out


def top_needs(needs: dict[str, dict], limit: int = 5) -> list[dict]:
    flagged = [n for n in needs.values() if n["status"] in ("needs_work", "improving") and n["priority"] > 0]
    return sorted(flagged, key=lambda n: (-n["priority"], n["concept"]))[:limit]


def summary(profile, knowledge) -> dict:
    """The explainable Training profile (GET /api/puzzles/training/profile)."""
    from . import record
    training = record.ensure(getattr(profile, "training", None) if profile is not None else None)
    needs = concept_needs(profile, knowledge)
    phases = phase_needs(profile)
    ranked = sorted(needs.values(), key=lambda n: (-n["priority"], -n["evidence"], n["concept"]))
    return {"segments": training["segments"], "moves": training["moves"], "phases": phases,
            "needs": top_needs(needs), "concepts": ranked,
            "strengths": [n for n in ranked if n["status"] == "solid"][:5],
            "recent": list(reversed(training["history"][-10:])),
            "method": "Each observation counts; recent ones more. Confidence grows with the amount of evidence, "
                      "so one bad move never makes something a weakness."}


# ---------------------------------------------------------------------- Practice / Personalized
def _ancestors(concept: str, knowledge) -> set[str]:
    out, stack = set(), [concept]
    while stack:
        c = stack.pop()
        if c in out or c not in knowledge.concepts:
            continue
        out.add(c)
        stack.extend(knowledge.concepts[c].parents)
    return out


class PracticeFocus:
    """Practice weighting from the learner's needs (built by practice_focus; used by puzzles.select).

    Each flagged need (needs_work / improving) has a boost b = FOCUS_MAX * min(1, priority / FOCUS_FULL)
    and covers its concept and every descendant (a need for "pin" covers absolute and relative pins).
    Its share of the session follows the evidence instead of a fixed percentage:

        natural  = the share of the pool that belongs to the need (what plain selection would give)
        target   = natural + (1 - natural) * b / (1 + b)
        share    = the need's puzzles / all puzzles, this session so far (served + this set)
        weight   = 1 + b * (1 - share / target), kept within [1 - b, 1 + b]

    Below its target share the need's puzzles are favoured, at it neutral, above it held back — so a
    weak spot comes up clearly more often, the set stays varied, and as the priority falls with
    improvement (assessment.needs) b and the target shrink back towards plain selection."""

    def __init__(self, boosts: dict[str, float], knowledge, pool=None, served: dict[str, int] | None = None):
        self.boosts = boosts
        self.knowledge = knowledge
        self._cache: dict[str, str | None] = {}
        counts: dict[str, int] = {}
        pool = list(pool or [])
        for p in pool:
            k = self.key(p)
            if k:
                counts[k] = counts.get(k, 0) + 1
        self.target = {}
        for k, b in boosts.items():
            natural = counts.get(k, 0) / len(pool) if pool else 0.0
            self.target[k] = natural + (1 - natural) * b / (1 + b)
        self.served: dict[str, int] = {}
        self.served_total = 0
        for concept, n in (served or {}).items():
            self.served_total += n
            k = self._key_of(concept)
            if k:
                self.served[k] = self.served.get(k, 0) + n

    def _key_of(self, concept: str) -> str | None:
        if concept not in self._cache:
            found = [(self.boosts[c], c) for c in _ancestors(concept, self.knowledge) if c in self.boosts]
            self._cache[concept] = max(found)[1] if found else None
        return self._cache[concept]

    def key(self, p) -> str | None:
        """The need a puzzle serves (None: no flagged need covers it)."""
        return self._key_of(p.concept)

    def boost(self, p) -> float:
        k = self.key(p)
        return self.boosts[k] if k else 0.0

    def weight(self, p, in_set: dict[str, int] | None = None, set_size: int = 0) -> float:
        k = self.key(p)
        if not k:
            return 1.0
        b = self.boosts[k]
        total = self.served_total + set_size
        share = (self.served.get(k, 0) + (in_set or {}).get(k, 0)) / total if total else 0.0
        return max(1 - b, min(1 + b, 1 + b * (1 - share / self.target[k])))

    def as_dict(self) -> list[dict]:
        return [{"concept": k, "boost": round(b, 3), "target_share": round(self.target[k], 3),
                 "served": self.served.get(k, 0)} for k, b in sorted(self.boosts.items(), key=lambda x: -x[1])]


def practice_focus(needs: dict[str, dict], knowledge, served: dict[str, int] | None = None, pool=None):
    """The Practice weighting for these needs (PracticeFocus), or None when no need is flagged
    (then selection is exactly as without Training). `served`: concept -> puzzles already given this
    session; `pool`: the candidate puzzles (for each need's natural share)."""
    boosts = {c: FOCUS_MAX * min(1.0, n["priority"] / FOCUS_FULL) for c, n in needs.items()
              if n["status"] in ("needs_work", "improving") and n["priority"] > 0}
    return PracticeFocus(boosts, knowledge, pool, served) if boosts else None


def card_factor(need: dict | None) -> float:
    """How Training evidence adjusts a Personalized card from games/puzzles: up when Training confirms
    the struggle, down when the learner now handles it in Training (bounded 0.6 .. 1.4)."""
    t = (need or {}).get("sources", {}).get("training")
    if not t:
        return 1.0
    return max(0.6, min(1.4, 1 + (t["struggle"] - BASELINE) * t["confidence"] * 2))
