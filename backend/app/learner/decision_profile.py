"""Decision Puzzle evidence helpers over the existing ``LearnerProfile``.

This is not a second learner store: ``LearnerProfile.decision_puzzles`` is serialized by the
same LearnerStore as onboarding, games, lessons, and ordinary puzzle results. Decision-specific
ability is calibrated separately so one yes/no task cannot swing the learner's general rating.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import rating as R

BASELINE_RATING = 800
MIN_DIFFICULTY = 400
MAX_DIFFICULTY = 2400
PRACTICE_OFFSET = -80
ABILITY_K = 24.0
ABILITY_CONFIDENCE_PRIOR = 0.05
ABILITY_CONFIDENCE_EVIDENCE = 10.0
MAX_DECISION_HISTORY = 4096
RECENT_RESULT_LIMIT = 20
WEAKNESS_WINDOW = 10
MIN_WEAKNESS_ATTEMPTS = 5
MIN_WEAKNESS_MISSES = 3
MIN_WEAKNESS_MISS_RATE = 0.60
GAME_CORROBORATED_ATTEMPTS = 3
GAME_CORROBORATED_MISSES = 2
ASSESSABLE = frozenset({"correct", "partial", "incorrect"})
EXPLANATION_SCORE = {"correct": 1.0, "partial": 0.55, "incorrect": 0.0}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp_difficulty(value: float) -> int:
    return int(max(MIN_DIFFICULTY, min(MAX_DIFFICULTY, round(value))))


def _prior(profile) -> tuple[int, float, str]:
    """A supplied/recorded rating is a weak prior, never a verdict about ability."""
    rating = int(getattr(profile, "rating", None) or BASELINE_RATING)
    onboarding = getattr(profile, "onboarding", {}) or {}
    game_observations = getattr(profile, "game_observations", {}) or {}
    skill = getattr(profile, "skill", {}) or {}
    if onboarding.get("rating"):
        platform = onboarding.get("platform") or "other"
        return rating, 0.16, "chesscom_rating" if platform == "chesscom" else "onboarding_rating"
    if game_observations.get("rating") or skill.get("source") == "games":
        return rating, 0.18, "analyzed_games"
    if skill.get("source") in ("experience", "onboarding", "results") and skill.get("evidence", 0):
        return rating, 0.12, "existing_learner_evidence"
    return rating or BASELINE_RATING, ABILITY_CONFIDENCE_PRIOR, "baseline"


def ensure_decision_profile(profile) -> dict:
    """Initialize or migrate the Decision Puzzle subdocument without a new storage system."""
    data = getattr(profile, "decision_puzzles", None)
    if not isinstance(data, dict):
        data = {}
        profile.decision_puzzles = data
    rating, confidence, source = _prior(profile)
    ability = data.get("ability")
    if not isinstance(ability, dict):
        ability = {}
        data["ability"] = ability
    ability.setdefault("rating", rating)
    ability.setdefault("confidence", confidence)
    ability.setdefault("evidence", 0)
    ability.setdefault("prior_rating", rating)
    ability.setdefault("prior_confidence", confidence)
    ability.setdefault("prior_source", source)
    ability.setdefault("source", source)
    ability.setdefault("updated_at", None)
    ability["rating"] = _clamp_difficulty(float(ability["rating"]))
    ability["confidence"] = round(max(0.0, min(0.99, float(ability["confidence"]))), 3)
    ability["evidence"] = max(0, int(ability["evidence"] or 0))
    data.setdefault("version", 1)
    data.setdefault("current_difficulty", _clamp_difficulty(ability["rating"] + PRACTICE_OFFSET))
    data["current_difficulty"] = _clamp_difficulty(float(data["current_difficulty"]))
    data.setdefault("last_adjusted_attempt", 0)
    data["last_adjusted_attempt"] = max(0, int(data["last_adjusted_attempt"] or 0))
    stored_results = sum(1 for entry in (data.get("history") or [])
                         if isinstance(entry, dict) and isinstance(entry.get("result"), dict))
    data.setdefault("attempts_total", max(int(ability["evidence"]), stored_results))
    data["attempts_total"] = max(int(data["attempts_total"] or 0), int(ability["evidence"]))
    if not isinstance(data.get("history"), list):
        data["history"] = []
    data["history"] = data["history"][-MAX_DECISION_HISTORY:]
    if not isinstance(data.get("weaknesses"), dict):
        data["weaknesses"] = {}
    if not isinstance(data.get("strengths"), list):
        data["strengths"] = []
    if not isinstance(data.get("recent"), list):
        data["recent"] = []
    data.setdefault("last_updated", None)
    return data


def exposure_identities(profile) -> set[str]:
    data = ensure_decision_profile(profile)
    return {entry.get("identity") for entry in data["history"]
            if isinstance(entry, dict) and isinstance(entry.get("identity"), str)}


def record_decision_exposure(profile, *, identity: str, source_id: str, concept: str,
                             difficulty: int, metadata_rating: int, engine_bonus: int = 0,
                             selection_target: int | None = None,
                             selection_reason: str = "broad_practice") -> None:
    """Persist a presented task even if the learner leaves before answering it."""
    data = ensure_decision_profile(profile)
    entry = next((item for item in reversed(data["history"])
                  if isinstance(item, dict) and item.get("identity") == identity), None)
    now = _iso()
    if entry is None:
        entry = {"identity": identity, "source_id": source_id, "concept": concept,
                 "difficulty": int(difficulty), "metadata_rating": int(metadata_rating),
                 "engine_complexity_bonus": int(engine_bonus), "selection_target": selection_target,
                 "selection_reason": selection_reason if selection_reason in ("targeted_practice", "broad_practice")
                 else "broad_practice", "presented_at": now,
                 "last_presented_at": now, "presentations": 1, "result": None}
        data["history"].append(entry)
    else:
        entry["presentations"] = int(entry.get("presentations", 1)) + 1
        entry["last_presented_at"] = now
        entry["selection_target"] = selection_target or entry.get("selection_target")
        if selection_reason in ("targeted_practice", "broad_practice"):
            entry["selection_reason"] = selection_reason
    data["history"] = data["history"][-MAX_DECISION_HISTORY:]
    data["last_updated"] = now
    if hasattr(profile, "touch"):
        profile.touch()


def decision_score(choice_correct: bool, explanation_assessment: str) -> float:
    """Combine the objective verdict (55%) and assessable reasoning (45%).

    If reasoning is ambiguous or the model is unavailable, score only the objective choice at
    a reduced weight (0.75 / 0.25). No uncertain explanation becomes a confirmed reasoning miss.
    """
    if explanation_assessment in EXPLANATION_SCORE:
        score = 0.55 * int(bool(choice_correct)) + 0.45 * EXPLANATION_SCORE[explanation_assessment]
    else:
        score = 0.75 if choice_correct else 0.25
    return round(max(0.0, min(1.0, score)), 3)


def _update_ability(data: dict, score: float, difficulty: int) -> None:
    ability = data["ability"]
    old = float(ability["rating"])
    expected = R.expected(old, int(difficulty))
    evidence = int(ability.get("evidence", 0))
    k = ABILITY_K if evidence < 10 else ABILITY_K * 0.75
    ability["rating"] = _clamp_difficulty(old + k * (score - expected))
    ability["evidence"] = evidence + 1
    prior_confidence = float(ability.get("prior_confidence", ABILITY_CONFIDENCE_PRIOR))
    ability["confidence"] = round(1 - (1 - prior_confidence) *
                                  math.exp(-ability["evidence"] / ABILITY_CONFIDENCE_EVIDENCE), 3)
    ability["source"] = "decision_results"
    ability["updated_at"] = _iso()


def _update_current_difficulty(data: dict) -> None:
    results = [entry["result"] for entry in data["history"]
               if isinstance(entry, dict) and isinstance(entry.get("result"), dict)]
    attempts = int(data.get("attempts_total", len(results)))
    last_adjusted = int(data.get("last_adjusted_attempt", 0))
    if attempts - last_adjusted < 3 or len(results) < 4:
        return
    window = results[-4:]
    scores = [float(item.get("score", 0.5)) for item in window]
    average = sum(scores) / len(scores)
    step = 0
    if average >= 0.82 and sum(score >= 0.75 for score in scores) >= 3:
        step = 50
    elif average <= 0.42 and sum(score < 0.5 for score in scores) >= 3:
        step = -50
    if step:
        data["current_difficulty"] = _clamp_difficulty(data["current_difficulty"] + step)
        data["last_adjusted_attempt"] = attempts


def _game_weakness(profile, concept: str) -> dict | None:
    for item in getattr(profile, "weaknesses", None) or []:
        if item.get("concept") == concept and item.get("tier") in ("recurring", "occasional"):
            return item
    return None


def _entry_outcome(result: dict) -> str:
    # The Yes/No result is objective because the move was independently checked by Stockfish.
    # Explanation outcomes are separate: uncertainty must never be recast as a reasoning miss.
    if result.get("judgment_correct") is False:
        return "choice_miss"
    status = result.get("explanation_assessment")
    if status == "incorrect":
        return "reasoning_miss"
    if result.get("judgment_correct") and status == "correct":
        return "success"
    if status == "partial":
        return "partial"
    return "unassessed"


def _score_trend(scores: list[float]) -> str:
    if len(scores) < 6:
        return "insufficient"
    delta = sum(scores[-3:]) / 3 - sum(scores[-6:-3]) / 3
    return "improving" if delta >= 0.15 else "slipping" if delta <= -0.15 else "stable"


def refresh_decision_weaknesses(profile, knowledge=None) -> dict[str, dict]:
    """Recompute per-concept evidence with a strict, testable recurrence threshold.

    Decision-only weakness needs five distinct engine-checked tasks and at least three
    Stockfish-backed choice misses or supported explanation misses at a 60% weighted miss rate.
    A recurring analyzed-game signal may corroborate the signal, but still requires three
    Decision attempts and two misses. Low-confidence, malformed, ambiguous, and unavailable
    model results are never counted as explanation misses; a wrong Yes/No remains a separate,
    objective Stockfish-backed result.
    """
    data = ensure_decision_profile(profile)
    history = [entry for entry in data["history"] if isinstance(entry, dict)]
    concepts = {entry.get("concept") for entry in history if entry.get("concept")}
    concepts.update(item.get("concept") for item in (getattr(profile, "weaknesses", None) or [])
                    if item.get("concept"))
    previous = data.get("weaknesses", {})
    summaries: dict[str, dict] = {}
    mastered: list[str] = []

    for concept in sorted(concepts):
        all_results = [entry for entry in history if entry.get("concept") == concept
                       and isinstance(entry.get("result"), dict)]
        assessed = [entry for entry in all_results
                    if entry["result"].get("explanation_assessment") in ASSESSABLE]
        window = all_results[-WEAKNESS_WINDOW:]
        distinct = len({entry.get("identity") for entry in window if entry.get("identity")})
        outcomes = [_entry_outcome(entry["result"]) for entry in window]
        misses = sum(outcome in ("choice_miss", "reasoning_miss") for outcome in outcomes)
        choice_misses = sum(outcome == "choice_miss" for outcome in outcomes)
        reasoning_misses = sum(outcome == "reasoning_miss" for outcome in outcomes)
        successes = sum(outcome == "success" for outcome in outcomes)
        weights = [0.82 ** age for age in reversed(range(len(window)))]
        weighted_total = sum(weights)
        weighted_misses = sum(weight for outcome, weight in zip(outcomes, weights)
                              if outcome in ("choice_miss", "reasoning_miss"))
        miss_rate = weighted_misses / weighted_total if weighted_total else 0.0
        sample_confidence = min(1.0, distinct / 6.0)
        puzzle_confidence = sample_confidence * max(0.0, min(1.0, (miss_rate - 0.35) / 0.65))
        game = _game_weakness(profile, concept)
        game_count = int((game or {}).get("game_count") or 0)
        game_confirmed = bool(game and game.get("tier") == "recurring" and game_count >= 2)
        game_confidence = min(0.45, 0.10 + 0.07 * min(game_count, 5)) if game_confirmed else 0.0
        confidence = 1.0 - (1.0 - puzzle_confidence) * (1.0 - game_confidence)

        decision_only = (distinct >= MIN_WEAKNESS_ATTEMPTS and misses >= MIN_WEAKNESS_MISSES
                         and miss_rate >= MIN_WEAKNESS_MISS_RATE)
        corroborated = (game_confirmed and distinct >= GAME_CORROBORATED_ATTEMPTS
                        and misses >= GAME_CORROBORATED_MISSES and miss_rate >= 0.50)
        old_status = (previous.get(concept) or {}).get("status") if isinstance(previous, dict) else None
        recent_all = all_results[-4:]
        recent_successes = sum(float(item["result"].get("score", 0.0)) >= 0.75 for item in recent_all)
        recent_improvement = len(recent_all) >= 3 and recent_successes >= 3
        score_values = [float(entry["result"].get("score", 0.5)) for entry in all_results]
        trend = _score_trend(score_values)
        targeted_results = [entry for entry in all_results
                            if entry.get("selection_reason") == "targeted_practice"]
        broad_results = [entry for entry in all_results
                         if entry.get("selection_reason") != "targeted_practice"]
        targeted_scores = [float(entry["result"].get("score", 0.5)) for entry in targeted_results]
        broad_scores = [float(entry["result"].get("score", 0.5)) for entry in broad_results]
        targeted_practice = {
            "attempts": len(targeted_scores),
            "mean_score": round(sum(targeted_scores) / len(targeted_scores), 3) if targeted_scores else None,
            "trend": _score_trend(targeted_scores),
        }

        if len(assessed) >= 5 and len(assessed[-5:]) == 5 and all(
                float(entry["result"].get("score", 0.0)) >= 0.8 for entry in assessed[-5:]) and not game_confirmed:
            status = "mastered"
            mastered.append(concept)
        elif decision_only or corroborated:
            status = "improving" if recent_improvement else "needs_work"
        elif (old_status in ("needs_work", "improving") and distinct >= 3
              and miss_rate < 0.50):
            status = "improving"
        elif len(assessed) < MIN_WEAKNESS_ATTEMPTS:
            status = "insufficient"
        else:
            status = "developing"

        last_entry = all_results[-1] if all_results else None
        recent_evidence = []
        for entry in all_results[-6:]:
            result = entry["result"]
            recent_evidence.append({
                "task": str(entry.get("identity", ""))[:12],
                "at": result.get("at"),
                "difficulty": entry.get("difficulty"),
                "selection_reason": entry.get("selection_reason", "broad_practice"),
                "selection_target": entry.get("selection_target"),
                "outcome": _entry_outcome(result),
                "score": result.get("score"),
                "judgment_correct": result.get("judgment_correct"),
                "explanation_assessment": result.get("explanation_assessment"),
            })
        concept_name = concept.replace("_", " ")
        if knowledge is not None:
            concept_obj = getattr(knowledge, "concepts", {}).get(concept)
            concept_name = getattr(concept_obj, "name", concept_name)
        summaries[concept] = {
            "concept": concept, "name": concept_name, "status": status,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "decision_attempts": len(all_results), "assessable_attempts": len(assessed),
            "independent_tasks": distinct, "observed_misses": misses,
            "choice_misses": choice_misses, "reasoning_misses": reasoning_misses,
            "successful_attempts": successes, "weighted_miss_rate": round(miss_rate, 3),
            "last_seen": ((last_entry or {}).get("result") or {}).get("at"),
            "trend": trend,
            "practice_effectiveness": {
                "targeted": targeted_practice,
                "broad": {"attempts": len(broad_scores),
                          "mean_score": round(sum(broad_scores) / len(broad_scores), 3) if broad_scores else None,
                          "trend": _score_trend(broad_scores)},
            },
            "sources": {
                "decision_puzzles": {"attempts": len(all_results), "assessable": len(assessed),
                                      "misses": misses, "choice_misses": choice_misses,
                                      "reasoning_misses": reasoning_misses,
                                      "successes": successes, "recent_evidence": recent_evidence},
                "analyzed_games": ({"tier": game.get("tier"), "game_count": game_count,
                                    "occurrences": game.get("occurrences"), "key": game.get("key"),
                                    "updated": game.get("updated")} if game else None),
            },
            "threshold": {"attempts": MIN_WEAKNESS_ATTEMPTS, "misses": MIN_WEAKNESS_MISSES,
                          "miss_rate": MIN_WEAKNESS_MISS_RATE,
                          "game_corroboration": game_confirmed},
        }

    data["weaknesses"] = summaries
    data["strengths"] = mastered
    data["recent"] = [
        {"at": (entry.get("result") or {}).get("at"), "concept": entry.get("concept"),
         "difficulty": entry.get("difficulty"),
         "selection_reason": entry.get("selection_reason", "broad_practice"),
         "selection_target": entry.get("selection_target"),
         "score": (entry.get("result") or {}).get("score"),
         "judgment_correct": (entry.get("result") or {}).get("judgment_correct"),
         "explanation_assessment": (entry.get("result") or {}).get("explanation_assessment"),
         "assessment_source": (entry.get("result") or {}).get("assessment_source")}
        for entry in history if isinstance(entry.get("result"), dict)
    ][-RECENT_RESULT_LIMIT:]
    return summaries


def record_decision_result(profile, *, identity: str, source_id: str, concept: str,
                           difficulty: int, metadata_rating: int, engine_bonus: int,
                           judgment_correct: bool, selected_choice: str,
                           explanation_assessment: str, explanation_confidence: str,
                           assessment_source: str, engine_category: str,
                           recognized_fact_ids: list[str], missing_fact_ids: list[str],
                           misconception_fact_ids: list[str], selection_target: int | None = None,
                           selection_reason: str = "broad_practice", knowledge=None) -> dict:
    """Persist exposure, objective choice, separate reasoning status, and calibrated evidence."""
    data = ensure_decision_profile(profile)
    entry = next((item for item in reversed(data["history"])
                  if isinstance(item, dict) and item.get("identity") == identity), None)
    if entry is None:
        record_decision_exposure(profile, identity=identity, source_id=source_id, concept=concept,
                                 difficulty=difficulty, metadata_rating=metadata_rating,
                                 engine_bonus=engine_bonus, selection_target=selection_target,
                                 selection_reason=selection_reason)
        entry = data["history"][-1]
    if selection_target is not None:
        entry["selection_target"] = int(selection_target)
    if selection_reason in ("targeted_practice", "broad_practice"):
        entry["selection_reason"] = selection_reason

    score = decision_score(judgment_correct, explanation_assessment)
    assessable = explanation_assessment in ASSESSABLE
    decision_meta = {
        "identity": identity,
        "selected_choice": selected_choice,
        "judgment_correct": bool(judgment_correct),
        "explanation_assessment": explanation_assessment,
        "explanation_confidence": explanation_confidence,
        "assessment_source": assessment_source,
        "engine_category": engine_category,
        "selection_reason": selection_reason,
        "selection_target": selection_target,
        "score": score,
        "recognized_fact_ids": list(recognized_fact_ids)[:6],
        "missing_fact_ids": list(missing_fact_ids)[:6],
        "misconception_fact_ids": list(misconception_fact_ids)[:6],
    }
    profile.record_attempt(
        concept, int(difficulty), solved=score >= 0.75, first_try=score >= 0.99,
        hints=0, revealed=False, learner_moves=1,
        context={"source": "decision_puzzle", "exercise_id": f"decision:{identity[:24]}",
                 "decision": decision_meta},
        score_override=score, rating_k_factor=16.0, update_overall_skill=False,
    )
    result = {
        "at": _iso(), "judgment_correct": bool(judgment_correct),
        "selected_choice": selected_choice, "explanation_assessment": explanation_assessment,
        "explanation_confidence": explanation_confidence, "assessment_source": assessment_source,
        "engine_category": engine_category, "score": score, "reasoning_assessed": assessable,
        "selection_reason": selection_reason, "selection_target": selection_target,
        "recognized_fact_ids": list(recognized_fact_ids)[:6],
        "missing_fact_ids": list(missing_fact_ids)[:6],
        "misconception_fact_ids": list(misconception_fact_ids)[:6],
    }
    entry["result"] = result
    data["history"] = data["history"][-MAX_DECISION_HISTORY:]
    data["attempts_total"] = int(data.get("attempts_total", 0)) + 1
    _update_ability(data, score, difficulty)
    _update_current_difficulty(data)
    data["last_updated"] = result["at"]
    refresh_decision_weaknesses(profile, knowledge)
    if hasattr(profile, "touch"):
        profile.touch()
    return {"score": score, "ability": data["ability"],
            "current_difficulty": data["current_difficulty"],
            "reasoning_assessed": assessable}


def decision_profile_summary(profile, knowledge=None) -> dict:
    """User-facing, explainable view of the persistent Decision Puzzle profile."""
    data = ensure_decision_profile(profile)
    weaknesses = refresh_decision_weaknesses(profile, knowledge)
    items = sorted(weaknesses.values(), key=lambda item: (
        item["status"] not in ("needs_work", "improving"), -item["confidence"], item["concept"]))
    active = [item for item in items if item["status"] in ("needs_work", "improving")]
    strengths = [item for item in items if item["status"] == "mastered"]
    ability = data["ability"]
    return {
        "ability": {"rating": ability["rating"], "confidence": ability["confidence"],
                    "evidence": ability["evidence"], "source": ability["source"],
                    "prior_rating": ability["prior_rating"], "prior_source": ability["prior_source"],
                    "updated_at": ability.get("updated_at")},
        "current_difficulty": data["current_difficulty"],
        "attempts": int(data.get("attempts_total", 0)),
        "exposures": len(data["history"]),
        "recent": data["recent"][-10:],
        "weaknesses": active,
        "skills": items,
        "strengths": strengths,
        "selection_policy": {
            "prior": "Use the existing normalized onboarding/Chess.com rating and analyzed-game or puzzle estimate as a low-confidence prior; otherwise start at the existing 800 baseline.",
            "adaptation": "Decision ability updates with a 24-point Elo step; target difficulty moves by 50 points only after four-result rolling success/failure evidence and at least three results since its last adjustment.",
            "weakness_threshold": {"independent_tasks": MIN_WEAKNESS_ATTEMPTS,
                                   "observed_misses": MIN_WEAKNESS_MISSES,
                                   "miss_rate": MIN_WEAKNESS_MISS_RATE,
                                   "game_corroboration": {"tasks": GAME_CORROBORATED_ATTEMPTS,
                                                          "misses": GAME_CORROBORATED_MISSES,
                                                          "source": "recurring analyzed-game finding"}},
            "evidence": "Ambiguous, unavailable, malformed, or low-confidence explanation assessments are excluded from reasoning misses. Stockfish-backed Yes/No errors are tracked separately and may support a weakness after the same recurrence threshold; analyzed-game findings can corroborate it.",
        },
        "selection": {
            "candidate_score": "absolute candidate-rating distance from the skill-adjusted target + focus penalty + variety penalty - need-priority bonuses; only unseen, verified, non-personal candidates with a clear bounded Stockfish verdict are eligible.",
            "concept_target": {"mix_current_and_concept_rating": [0.7, 0.3], "practice_offset": PRACTICE_OFFSET,
                               "decision_needs_work": -110, "decision_improving": -55,
                               "existing_needs_work": -75, "existing_improving": -35,
                               "mastered_concept": 60},
            "focus_cycle": {"recent_exposures": 4, "targeted_exposures_before_broad_practice": 2,
                            "off_focus_penalty": 180, "focus_after_target_penalty": 150},
            "bonuses": {"existing_need_points_per_priority": 300, "existing_need_cap": 90,
                        "decision_weakness_points_per_confidence": 80,
                        "additional_need_points_per_priority": 250, "additional_need_cap": 50},
            "variety_penalties": {"same_as_last_concept": 55, "mastered_concept": 45},
        },
    }
