"""Game history analysis: the learner's last N games, analyzed *together*.

Individual game analysis (analyzer.py) says what went wrong in one game. This layer
answers a different question: *what keeps going wrong across my games, and what
should I practise because of it?* It adds no chess judgement of its own. Every
occurrence it counts is a moment Stockfish already confirmed and motifs.py already
proved on the board. It only aggregates, and it does so deterministically.

    Chess.com importer -> GameRecord -> GameAnalyzer (per game, cached on disk)
        -> THIS MODULE: select the last N games, group findings by Knowledge Library
           concept (weaknesses.collect), tier + score each pattern, overall stats
        -> Knowledge Library retrieval + training.py (the existing tutor lessons)

Tiers: a pattern's label depends only on how many *different games* it appears in.
    one_time    1 game. An incident, never called a weakness.
    occasional  2+ games, but below the recurring bar (or not enough history yet).
    recurring   at least recurring_threshold(N) games, and only when N >= MIN_HISTORY_GAMES.
                recurring_threshold(N) = max(3, ceil(15% of N)): 3 of 10, 3 of 20, 5 of 30, 8 of 50.

Significance (ranking): frequency alone is not importance. A small inaccuracy repeated
8 times must not outrank a blunder repeated 3 times.
    impact(occurrence) = severity weight (blunder 3, mistake 2, inaccuracy 1, habit 1)
                         x (1 + min(loss in centipawns, 1000) / 1000)    -> 1.0 .. 6.0
    An occurrence in a position already counted for this pattern (the same opening trap
    reached again) counts at half weight. It's one lesson, not two.
    per game  = the biggest impact in that game + 0.25 x the rest (repeats inside one
                game add a little, never as much as another game would)
    score     = sum over games, rounded to 2 decimals.
    Patterns are listed recurring -> occasional -> one-time, each tier by score.

The learner's positions only ever appear in this private report and in personal
training plans. Nothing here writes to the Knowledge Library.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .weaknesses import recurring_weaknesses

MIN_HISTORY_GAMES = 10          # the full recurring-pattern analysis needs this many analyzed games
DEFAULT_COUNT = 10
PRESET_COUNTS = (10, 20, 30, 50)
MAX_COUNT = 100                 # analysis cost grows linearly; 100 recent games is plenty
RECURRING_MIN_GAMES = 3
RECURRING_SHARE = 0.15
OCCASIONAL_MIN_GAMES = 2
SEVERITY_WEIGHT = {"blunder": 3, "mistake": 2, "inaccurate": 1, "habit": 1}
LOSS_CAP = 1000
REPEAT_IN_GAME_WEIGHT = 0.25
REPEAT_POSITION_WEIGHT = 0.5
TIERS = ("recurring", "occasional", "one_time")
TOP_MISTAKES = 5
MAX_PATTERN_EVIDENCE = 30       # stored per pattern (the counts always cover every occurrence)
HABIT_MOTIFS = {"early_queen", "repeated_moves", "poor_development", "missed_castling"}


class HistoryError(ValueError):
    pass


# ------------------------------------------------------------------------ selection
def validate_count(count) -> int:
    try:
        n = int(count)
    except (TypeError, ValueError):
        raise HistoryError("the number of games must be a whole number") from None
    if n < MIN_HISTORY_GAMES:
        raise HistoryError(f"choose at least {MIN_HISTORY_GAMES} games: fewer isn't enough to find patterns")
    if n > MAX_COUNT:
        raise HistoryError(f"choose at most {MAX_COUNT} games")
    return n


def validate_fetch_count(count) -> int:
    """How many games to download: the same bounds as a history analysis."""
    return validate_count(count)


def games_of(docs: list[dict], username: str | None) -> list[dict]:
    """Only the games `username` played (case-insensitive); every game when no name is given."""
    name = (username or "").strip().lower()
    if not name:
        return docs
    return [d for d in docs if (d.get("game", {}).get("player") or "").lower() == name]


def _norm_date(value: str | None) -> str:
    return (value or "").replace(".", "-").strip() if value and "?" not in value else ""


def recency_key(game: dict) -> tuple:
    """Newest-first sort key for a stored game (a GameRecord dict).

    Chess.com PGNs carry EndDate/EndTime (or UTCDate/UTCTime); the game number in the
    link grows over time and breaks ties; the import time is the last resort."""
    h = game.get("headers") or {}
    date = _norm_date(h.get("EndDate")) or _norm_date(h.get("UTCDate")) or _norm_date(h.get("Date")) \
        or (game.get("date") or "")
    time = h.get("EndTime") or h.get("UTCTime") or h.get("StartTime") or ""
    number = re.search(r"(\d+)$", game.get("id") or "")
    return (date, time, int(number.group(1)) if number else 0, game.get("imported_at") or "")


def select_recent(docs: list[dict], count: int) -> list[dict]:
    """The `count` most recent games the learner played (newest first)."""
    playable = [d for d in docs if d.get("game", {}).get("player_color")]
    return sorted(playable, key=lambda d: recency_key(d["game"]), reverse=True)[:count]


# ------------------------------------------------------------------------ scoring
def recurring_threshold(n_games: int) -> int:
    return max(RECURRING_MIN_GAMES, math.ceil(RECURRING_SHARE * n_games))


def tier_for(game_count: int, n_games: int) -> str:
    if game_count < OCCASIONAL_MIN_GAMES:
        return "one_time"
    if n_games >= MIN_HISTORY_GAMES and game_count >= recurring_threshold(n_games):
        return "recurring"
    return "occasional"


def impact(evidence: dict) -> float:
    weight = SEVERITY_WEIGHT.get(evidence.get("severity"), evidence.get("severity_weight") or 1)
    loss = max(0, min(LOSS_CAP, evidence.get("loss_cp") or 0))
    return weight * (1 + loss / LOSS_CAP)


def _position(fen: str | None) -> str:
    return " ".join((fen or "").split()[:4])


def significance(evidence: list[dict]) -> float:
    ordered = sorted(evidence, key=lambda e: (e["game_id"], e["ply"]))
    counted: set[str] = set()
    by_game: dict[str, list[float]] = {}
    for e in ordered:
        value = impact(e)
        pos = _position(e.get("fen"))
        if pos and pos in counted:
            value *= REPEAT_POSITION_WEIGHT
        counted.add(pos)
        by_game.setdefault(e["game_id"], []).append(value)
    total = 0.0
    for values in by_game.values():
        values.sort(reverse=True)
        total += values[0] + REPEAT_IN_GAME_WEIGHT * sum(values[1:])
    return round(total, 2)


def _kind(evidence: list[dict]) -> str:
    """What sort of pattern this is, from *what* went wrong (a fork missed in an endgame is a tactic)."""
    motifs = {e.get("motif") for e in evidence}
    if motifs <= HABIT_MOTIFS:
        return "habit"
    if motifs <= {"endgame_mistake"}:
        return "endgame"
    if motifs <= {"opening_mistake"}:
        return "opening"
    return "tactic"


def _where(games: int, n: int) -> str:
    if games == 1:
        return "in 1 game"
    if games == n:
        return "in both games" if n == 2 else f"in all {n} games"
    return f"in {games} of your {n} games"


def _pattern(w: dict, n: int) -> dict:
    evidence = w["evidence"]
    severities = Counter(e["severity"] for e in evidence)
    losses = [min(LOSS_CAP, e["loss_cp"]) for e in evidence if e.get("loss_cp") is not None]
    tier = tier_for(w["game_count"], n)
    times = f" ({len(evidence)} times)" if len(evidence) > w["game_count"] else ""
    return {
        **{k: v for k, v in w.items() if k != "evidence"},
        "tier": tier,
        "significance": significance(evidence),
        "kind": _kind(evidence),
        "description": f"{w['title']}: {_where(w['game_count'], n)}{times}.",
        "found_in": _where(w["game_count"], n).removeprefix("in "),
        "total_games": n,
        "occurrences": len(evidence),
        "frequency": round(w["game_count"] / n, 2) if n else 0,
        "distinct_positions": len({_position(e.get("fen")) for e in evidence}),
        "severity_counts": {s: severities[s] for s in SEVERITY_WEIGHT if severities[s]},
        "avg_loss_cp": round(sum(losses) / len(losses)) if losses else None,
        "max_loss_cp": max(losses) if losses else None,
        "phases": dict(Counter(e.get("phase") or "unknown" for e in evidence)),
        "evidence": sorted(evidence, key=lambda e: (-impact(e), e["game_id"], e["ply"])),  # capped later
    }


def _fold_children(patterns: list[dict], library) -> list[dict]:
    """Knight forks in 2 games + pawn forks in 2 games = forks in 4 games: when the parent
    concept reaches a higher tier, it's listed and its children fold into it."""
    if library is None:
        return patterns
    by_key = {p["key"]: p for p in patterns}
    moments = {p["key"]: {(e["game_id"], e["ply"]) for e in p["evidence"]} for p in patterns}
    out = []
    for p in patterns:
        concept = library.concepts.get(p["key"])
        parents = [by_key[k] for k in (concept.parents if concept else []) if k in by_key]
        home = next((q for q in parents if TIERS.index(q["tier"]) < TIERS.index(p["tier"])
                     and moments[p["key"]] <= moments[q["key"]]), None)
        if home is None:
            out.append(p)
        else:
            home.setdefault("includes", []).append(p["title"])
    return out


# ------------------------------------------------------------------------ the report
def _opening_family(name: str | None) -> str | None:
    if not name:
        return None
    return re.split(r"[:,]", name)[0].strip() or None


def _game_ref(game: dict) -> dict:
    return {"game_id": game["id"], "opponent": game.get("opponent") or (
        game.get("black") if game.get("player_color") == "white" else game.get("white")),
        "date": game.get("date"), "url": game.get("url"), "opening": game.get("opening")}


def _important_mistakes(selected: list[dict]) -> list[dict]:
    moments = []
    for doc in selected:
        for m in (doc.get("analysis") or {}).get("moments", []):
            moments.append((doc["game"], m))
    moments.sort(key=lambda gm: (-SEVERITY_WEIGHT.get(gm[1]["severity"], 2), -(gm[1].get("loss_cp") or 0),
                                 gm[1]["game_id"], gm[1]["ply"]))
    out = []
    for game, m in moments[:TOP_MISTAKES]:
        out.append({**_game_ref(game), "moment_id": m["id"], "ply": m["ply"], "move_number": m["move_number"],
                    "side": m["side"], "san": m["san"], "severity": m["severity"], "loss_cp": m.get("loss_cp"),
                    "best_move": m.get("best_move"), "concept": m.get("concept"), "motif": m.get("motif"),
                    "phase": m.get("phase"), "fen": m.get("fen_before")})
    return out


def _openings(selected: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for doc in selected:
        game = doc["game"]
        family = _opening_family(game.get("opening"))
        if not family:
            continue
        row = rows.setdefault(family, {"opening": family, "games": 0, "win": 0, "loss": 0, "draw": 0,
                                       "opening_mistakes": 0, "habits": 0})
        row["games"] += 1
        result = _learner_result(game)
        if result in ("win", "loss", "draw"):
            row[result] += 1
        analysis = doc.get("analysis") or {}
        row["opening_mistakes"] += sum(1 for m in analysis.get("moments", []) if m.get("phase") == "opening")
        row["habits"] += len(analysis.get("habits", []))
    return sorted(rows.values(), key=lambda r: (-r["games"], r["opening"]))


def _learner_result(game: dict) -> str | None:
    color, result = game.get("player_color"), game.get("result")
    if not color or result not in ("1-0", "0-1", "1/2-1/2"):
        return None
    if result == "1/2-1/2":
        return "draw"
    return "win" if (result == "1-0") == (color == "white") else "loss"


def _observations(n: int, phases: Counter, habit_games: int, habit_names: list[str], endgame_games: int,
                  openings: list[dict]) -> list[str]:
    notes = []
    big = sum(phases.values())
    if big >= 3:
        phase, count = max(sorted(phases.items()), key=lambda kv: kv[1])
        if count * 2 >= big:
            notes.append(f"Most of your big mistakes ({count} of {big}) came in the {phase}.")
    if habit_games:
        what = f" ({', '.join(habit_names[:3])})" if habit_names else ""
        notes.append(f"Opening habits cost you something in {habit_games} of {n} games{what}.")
    if phases.get("endgame"):
        notes.append(f"{phases['endgame']} big mistake{'s' if phases['endgame'] != 1 else ''} came in endgames, "
                     f"in {endgame_games} game{'s' if endgame_games != 1 else ''}.")
    repeated = [o for o in openings if o["games"] >= 2]
    if repeated:
        o = repeated[0]
        record = ", ".join(f"{o[k]} {w}" for k, w in (("win", "won"), ("loss", "lost"), ("draw", "drawn")) if o[k])
        notes.append(f"You played the {o['opening']} in {o['games']} games ({record or 'no result recorded'}).")
    return notes


def build_report(selected: list[dict], library=None, requested: int | None = None, available: int | None = None,
                 failed: list[dict] | None = None) -> dict:
    """Aggregate the analyzed games among `selected` (newest first) into one report."""
    analyzed = [d for d in selected if d.get("analysis")]
    n = len(analyzed)
    grouped = recurring_weaknesses([d["analysis"] for d in analyzed], library,
                                   min_games=OCCASIONAL_MIN_GAMES, max_evidence=None,
                                   rank=lambda games: tier_for(games, n))
    patterns = _fold_children([_pattern(w, n) for w in grouped["weaknesses"] + grouped["seen_once"]], library)
    patterns.sort(key=lambda p: (TIERS.index(p["tier"]), -p["significance"], p["title"]))
    for p in patterns:
        p["evidence"] = p["evidence"][:MAX_PATTERN_EVIDENCE]

    results = Counter(_learner_result(d["game"]) or "unfinished" for d in analyzed)
    by_category: Counter = Counter()
    phases: Counter = Counter()
    habit_names: Counter = Counter()
    habit_games = endgame_games = 0
    for d in analyzed:
        a = d["analysis"]
        by_category.update(a["stats"]["by_category"])
        phases.update(a["stats"].get("mistakes_by_phase", {}))
        if a.get("habits"):
            habit_games += 1
            habit_names.update(_habit_name(h) for h in a["habits"])
        if a["stats"].get("mistakes_by_phase", {}).get("endgame"):
            endgame_games += 1
    openings = _openings(analyzed)
    enough = n >= MIN_HISTORY_GAMES
    report = {
        "requested": requested,
        "available": available,
        "games_analyzed": n,
        "game_ids": [d["game"]["id"] for d in analyzed],
        "games": [{**_game_ref(d["game"]), "result": _learner_result(d["game"]),
                   "analyzed": bool(d.get("analysis"))} for d in selected],
        "not_analyzed": [d["game"]["id"] for d in selected if not d.get("analysis")],
        "failed": failed or [],
        "enough_history": enough,
        "min_games": MIN_HISTORY_GAMES,
        "recurring_threshold": recurring_threshold(n) if enough else None,
        "results": {"win": results["win"], "loss": results["loss"], "draw": results["draw"],
                    "unfinished": results["unfinished"]},
        "mistakes": {"blunders": by_category.get("blunder", 0), "mistakes": by_category.get("mistake", 0),
                     "inaccuracies": by_category.get("inaccurate", 0),
                     "per_game": round((by_category.get("blunder", 0) + by_category.get("mistake", 0)) / n, 1)
                     if n else 0},
        "mistakes_by_phase": dict(phases),
        "important_mistakes": _important_mistakes(analyzed),
        "openings": openings[:6],
        "observations": _observations(n, phases, habit_games, [k for k, _ in habit_names.most_common()],
                                      endgame_games, openings),
        "patterns": patterns,
        "recurring": [p["key"] for p in patterns if p["tier"] == "recurring"],
        "scoring": {"severity_weight": SEVERITY_WEIGHT, "loss_cap_cp": LOSS_CAP,
                    "repeat_in_game_weight": REPEAT_IN_GAME_WEIGHT,
                    "repeat_position_weight": REPEAT_POSITION_WEIGHT,
                    "recurring": f"max({RECURRING_MIN_GAMES}, ceil({RECURRING_SHARE:.0%} of games)), "
                                 f"with at least {MIN_HISTORY_GAMES} games"},
    }
    report["notice"] = notice(report)
    report["summary"] = summary_lines(report)
    return report


def _habit_name(h: dict) -> str:
    return (h.get("motif") or "habit").replace("_", " ")


# ------------------------------------------------------------------------ words
def notice(report: dict) -> str | None:
    n, requested, available = report["games_analyzed"], report.get("requested"), report.get("available")
    if n < MIN_HISTORY_GAMES:
        missing = MIN_HISTORY_GAMES - n
        have = f"{n} analyzed game{'s' if n != 1 else ''}" if n else "no analyzed games"
        return (f"There isn't enough game history for the full pattern analysis yet: you have {have}, and it "
                f"needs at least {MIN_HISTORY_GAMES}. Import {missing} more game{'s' if missing != 1 else ''} "
                "from Chess.com. Until then I only point out what happened more than once, without calling "
                "it a pattern.")
    if requested and available is not None and available < requested:
        return f"You asked for your last {requested} games, but only {available} are imported, so I used those."
    return None


def _subject(p: dict) -> str:
    name = p["title"]
    return name[0].lower() + name[1:] if not name[:2].isupper() else name


def summary_lines(report: dict) -> list[str]:
    """The deterministic summary shown at the top of the report (and the teacher's fallback)."""
    n = report["games_analyzed"]
    if not n:
        return ["No analyzed games yet. Import your Chess.com games and start the analysis."]
    lines = []
    recurring = [p for p in report["patterns"] if p["tier"] == "recurring"]
    occasional = [p for p in report["patterns"] if p["tier"] == "occasional"]
    if recurring:
        lines.append(f"I found {len(recurring)} recurring pattern{'s' if len(recurring) != 1 else ''} "
                     f"in your last {n} games.")
        top = recurring[0]
        lines.append(f"The most important one is {_subject(top)}: found in {top['found_in']}"
                     f"{_severity_phrase(top)}.")
        for p in recurring[1:3]:
            lines.append(f"You also had {_subject(p)} in {p['found_in'].replace('of your', 'of')}.")
    elif report["enough_history"]:
        lines.append(f"No mistake came back often enough in your last {n} games to call it a pattern "
                     f"(that takes {report['recurring_threshold']} games).")
    if occasional:
        names = ", ".join(f"{_subject(p)} ({p['game_count']} games)" for p in occasional[:3])
        lines.append(f"Seen more than once, but not a pattern{' yet' if report['enough_history'] else ''}: "
                     f"{names}.")
    if not recurring and not occasional:
        lines.append("Nothing repeated across these games: every big mistake was a one-off.")
    return lines


def _severity_phrase(p: dict) -> str:
    counts = p.get("severity_counts", {})
    bits = [f"{counts[s]} {w}{'s' if counts[s] != 1 else ''}" for s, w in
            (("blunder", "blunder"), ("mistake", "mistake")) if counts.get(s)]
    return f", with {' and '.join(bits)}" if bits else ""


# ------------------------------------------------------------------------ the teacher
def _pawns(cp: int | None) -> str | None:
    if cp is None:
        return None
    if cp >= LOSS_CAP:
        return "a decisive amount (mate or most of the material)"
    return f"about {cp / 100:.1f} pawns"


def history_facts(report: dict, library=None, level: str = "beginner", max_patterns: int = 4) -> list[str]:
    """Structured, verified facts for Qwen. The analysis decided every pattern; Qwen only explains."""
    n = report["games_analyzed"]
    r = report["results"]
    facts = [f"The student's last {n} games were analyzed by the Stockfish chess engine: {r['win']} won, "
             f"{r['loss']} lost, {r['draw']} drawn.",
             f"Big mistakes: {report['mistakes']['blunders']} blunders and {report['mistakes']['mistakes']} "
             f"mistakes ({report['mistakes']['per_game']} per game)."]
    if not report["enough_history"]:
        facts.append(f"Only {n} games are analyzed; at least {MIN_HISTORY_GAMES} are needed to call anything a "
                     "recurring pattern, so nothing below is a pattern yet.")
    shown = [p for p in report["patterns"] if p["tier"] != "one_time"][:max_patterns]
    for p in shown:
        label = {"recurring": "RECURRING PATTERN", "occasional": "seen more than once, not a pattern"}[p["tier"]]
        sev = ", ".join(f"{v} {k}" for k, v in p.get("severity_counts", {}).items())
        cost = _pawns(p.get("avg_loss_cp"))
        facts.append(f"{label}: {p['title']} (concept {p.get('concept') or p['key']}): {p['found_in']}, "
                     f"{p['occurrences']} times; severity: {sev}" + (f"; average cost {cost}" if cost else "") + ".")
        for e in p["evidence"][:2]:
            who = f" against {e['opponent']}" if e.get("opponent") else ""
            best = f", Stockfish's move was {e['best_move']}" if e.get("best_move") else ""
            facts.append(f"  Example{who}, move {e['move_number']}: the student played {e['san']}{best}.")
        concept = library.concepts.get(p.get("concept") or "") if library is not None else None
        if concept is not None:
            examples = library.examples_for(concept.id)
            if examples:
                facts.append(f"  Verified lesson examples for {concept.name}: {len(examples)} "
                             f"(e.g. \"{examples[0].title}\"). The student can practise them.")
    one_time = [p["title"] for p in report["patterns"] if p["tier"] == "one_time"][:4]
    if one_time:
        facts.append("One-off mistakes (happened in only one game, NOT patterns): " + ", ".join(one_time) + ".")
    facts += [f"Observation: {o}" for o in report["observations"][:3]]
    facts.append(f"Student level: {level}.")
    return facts


def fallback_explanation(report: dict) -> str:
    lines = list(report["summary"])
    if report.get("notice"):
        lines.insert(0, report["notice"])
    top = next((p for p in report["patterns"] if p["tier"] == "recurring"), None)
    if top is not None:
        lines.append(f"Practising {_subject(top)} is the best next step: start with the verified examples, "
                     "then the positions from your own games.")
    return "\n\n".join(lines)


_GAMES_CLAIM = re.compile(r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:out\s+)?of\s+"
                          r"(?:your\s+|the\s+|these\s+|those\s+|your\s+last\s+|the\s+last\s+)?(\d+)\s+games?\b", re.I)
_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_BROAD = {"tactics", "beginner_mistakes", "basics", "openings", "endgames", "checkmate", "mistakes"}


def conflicts(text: str, report: dict, library=None) -> list[str]:
    """Where a teacher reply claims more than the analysis found (then it's replaced)."""
    problems = []
    n = report["games_analyzed"]
    counts = {(p["game_count"], n) for p in report["patterns"]}
    counts |= {(v, n) for v in report["results"].values()} | {(n, n)}
    for m in _GAMES_CLAIM.finditer(text):
        a = m.group(1).lower()
        pair = (_WORDS.get(a) or int(a), int(m.group(2)))
        if pair not in counts:
            problems.append(f"claims '{m.group(0)}', which no finding says")
    if library is not None:
        reported = " | ".join((p["title"] + " " + (p.get("concept_name") or "")).lower() for p in report["patterns"])
        for concept in library.concepts.values():
            name = concept.name.lower()
            if concept.id in _BROAD or len(name) < 3 or name in reported:
                continue
            if re.search(rf"\b{re.escape(name)}(?:s|es)?\b", text.lower()):
                problems.append(f"mentions {concept.name}, which the analysis didn't find")
    if not report["enough_history"] and re.search(r"\brecurring\b|\bpattern\b(?!\s+yet)", text, re.I) \
            and not re.search(r"\bnot\b[^.]{0,40}\b(recurring|pattern)", text, re.I):
        problems.append("calls something a pattern without enough games")
    return problems
