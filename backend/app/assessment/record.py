"""The persistent Training evidence: LearnerProfile.training (private, DATA_DIR/learners/<id>.json).

    segments, moves              totals
    phases[phase]                segments, moves, errors (mistakes+blunders), inaccuracies, best moves,
                                 loss_sum (cp, capped per move), accuracy (last RECENT_ACC segments,
                                 newest last) — long-term = the counts, recent = the accuracy list
    concepts[concept]            trials, found, missed, allowed, last, and `events` (newest last,
                                 EVENTS_KEEP kept): {t, r (1 found / 0 missed or allowed), kind, phase, seg}
    tested                       the hidden-idea tests {t, concept, rating, found} (for learner.difficulty)
    seen                         Training position ids, oldest first (repeat prevention)
    history                      one line per segment (newest last): what was played and how it went

Only evidence from assessment.evaluate goes in (validator-backed concepts, engine-graded moves).
One segment is one data point; nothing here decides that something is a weakness (assessment.needs
does, with confidence from the amount of evidence).
"""
from __future__ import annotations

from datetime import datetime, timezone

PHASES = ("opening", "middlegame", "endgame")
RECENT_ACC = 12
EVENTS_KEEP = 30
TESTED_KEEP = 80
SEEN_KEEP = 300
HISTORY_KEEP = 40


def empty() -> dict:
    return {"version": 1, "segments": 0, "moves": 0,
            "phases": {p: {"segments": 0, "moves": 0, "errors": 0, "inaccuracies": 0, "best": 0, "loss_sum": 0,
                           "accuracy": []} for p in PHASES},
            "concepts": {}, "tested": [], "seen": [], "history": []}


def ensure(training: dict | None) -> dict:
    """An up-to-date structure (older or missing data filled in, never discarded)."""
    out = training if isinstance(training, dict) else {}
    base = empty()
    for key, value in base.items():
        out.setdefault(key, value)
    for p in PHASES:
        out["phases"].setdefault(p, base["phases"][p])
        for key, value in base["phases"][p].items():
            out["phases"][p].setdefault(key, value)
    return out


def mark_seen(training: dict, position_id: str) -> None:
    training = ensure(training)
    seen = [s for s in training["seen"] if s != position_id] + [position_id]
    training["seen"] = seen[-SEEN_KEEP:]


def apply(training: dict, seg, result: dict, now: datetime | None = None) -> dict:
    """Add one analysed segment (assessment.evaluate.analyze output) to the evidence."""
    training = ensure(training)
    t = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    training["segments"] += 1
    training["moves"] += result["learner_moves"]
    for phase, c in result["phases"].items():
        ph = training["phases"].setdefault(phase, empty()["phases"]["opening"])
        for key in ("moves", "errors", "inaccuracies", "best", "loss_sum"):
            ph[key] += int(c.get(key) or 0)
    start = training["phases"][seg.position.phase]
    start["segments"] += 1
    if result.get("accuracy") is not None:
        start["accuracy"] = (start["accuracy"] + [result["accuracy"]])[-RECENT_ACC:]
    for ev in result["concepts"]:
        st = training["concepts"].setdefault(ev["concept"], {"trials": 0, "found": 0, "missed": 0, "allowed": 0,
                                                             "last": None, "events": []})
        st["trials"] += 1
        st[ev["result"]] = st.get(ev["result"], 0) + 1
        st["last"] = t
        st["events"] = (st["events"] + [{"t": t, "r": 1 if ev["result"] == "found" else 0, "kind": ev["kind"],
                                         "phase": ev.get("phase"), "seg": seg.id}])[-EVENTS_KEEP:]
        if ev["kind"] == "tested" and ev.get("rating") is not None:
            training["tested"] = (training["tested"] + [{"t": t, "concept": ev["concept"], "rating": ev["rating"],
                                                         "found": ev["result"] == "found"}])[-TESTED_KEEP:]
    mark_seen(training, seg.position.id)
    training["history"] = (training["history"] + [{
        "id": seg.id, "t": t, "mode": seg.mode, "phase": seg.position.phase, "position": seg.position.id,
        "concept": (seg.position.hidden or {}).get("concept"), "moves": result["learner_moves"],
        "accuracy": result.get("accuracy"), "errors": result["mistakes"] + result["blunders"],
        "bot_rating": seg.bot.get("rating")}])[-HISTORY_KEEP:]
    return training
