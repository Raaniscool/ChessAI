"""Recurring weaknesses: the same concept going wrong in several games.

One mistake is an incident, not a weakness. A concept only becomes a *recurring
weakness* when it shows up in at least MIN_GAMES different games; everything
seen in a single game is reported separately as "seen once" and never labelled
a weakness. Each weakness carries its evidence (game, move, position, severity)
so the learner — and the tutor — can see exactly why it was flagged.

Grouping uses the Knowledge Library's concept graph: a knight fork in one game
and a pawn fork in another are both forks. Only one level up, and never into
the broad roots ("tactics", "beginner mistakes"): "you make tactical mistakes"
is not actionable.
"""
from __future__ import annotations

MIN_GAMES = 2
BROAD = {"tactics", "beginner_mistakes", "basics", "openings", "endgames", "checkmate"}
MAX_EVIDENCE = 12



def _key(item: dict, finding: dict) -> str | None:
    return finding.get("concept") or (finding["motif"] if finding["motif"] not in
                                      ("positional_mistake", "opening_mistake") else None)


def _evidence(item: dict, finding: dict) -> dict:
    src = item.get("source") or {}
    return {
        "game_id": item["game_id"], "moment_id": item.get("id"), "ply": item["ply"],
        "move_number": item["move_number"], "side": item.get("side"), "san": item.get("san"),
        "fen": item.get("fen_before"), "severity": item["severity"], "loss_cp": item.get("loss_cp"),
        "severity_weight": item["severity_weight"], "motif": finding["motif"],
        "concept": finding.get("concept"), "family": finding.get("family"),
        "best_move": item.get("best_move"), "opponent": src.get("opponent"), "date": src.get("date"),
        "url": src.get("url"), "phase": item.get("phase"), "topic": finding.get("topic"),
    }


def collect(analyses: list[dict]) -> dict[str, list[dict]]:
    """{concept-or-motif: [evidence]} over every moment and habit of every analysis."""
    groups: dict[str, list[dict]] = {}
    for analysis in analyses:
        for item in list(analysis.get("moments", [])) + list(analysis.get("habits", [])):
            seen: set[str] = set()
            for f in item.get("findings", []):
                key = _key(item, f)
                if key is None or key in seen:
                    continue
                seen.add(key)
                groups.setdefault(key, []).append(_evidence(item, f))
    return groups


def _parents(library, key: str) -> list[str]:
    concept = library.concepts.get(key) if library is not None else None
    if concept is None:
        return []
    return [p for p in concept.parents if p not in BROAD and p in library.concepts]


def _title(library, key: str, evidence: list[dict]) -> str:
    concept = library.concepts.get(key) if library is not None else None
    name = concept.name if concept else key.replace("_", " ").capitalize()
    if {e["family"] for e in evidence} == {"missed"} and not name.lower().startswith("missed"):
        first = name.split()[0]
        keep = first.endswith("'s")
        return "Missed " + (name if keep else name[0].lower() + name[1:])
    return name


def _describe(title: str, games: int, total: int, occurrences: int) -> str:
    where = (f"{games} of your {total} games" if total > games
             else "both games" if games == 2 else f"all {games} games")
    times = f" ({occurrences} times)" if occurrences > games else ""
    return f"{title}: in {where}{times}."


def summarize(key: str, evidence: list[dict], total_games: int, library=None,
              max_evidence: int | None = MAX_EVIDENCE) -> dict:
    games = sorted({e["game_id"] for e in evidence})
    evidence = sorted(evidence, key=lambda e: (-e["severity_weight"], e["game_id"], e["ply"]))
    concept = library.concepts.get(key) if library is not None else None
    title = _title(library, key, evidence)
    topics = list(concept.topics) if concept else []
    topics = list(dict.fromkeys(topics + [e["topic"] for e in evidence if e.get("topic")]))
    return {
        "key": key,
        "concept": key if concept else None,
        "concept_name": concept.name if concept else None,
        "category": concept.category if concept else None,
        "title": title,
        "description": _describe(title, len(games), total_games, len(evidence)),
        "motifs": sorted({e["motif"] for e in evidence}),
        "games": games,
        "game_count": len(games),
        "total_games": total_games,
        "occurrences": len(evidence),
        "frequency": round(len(games) / total_games, 2) if total_games else 0,
        "severity_score": sum(e["severity_weight"] for e in evidence),
        "max_severity": evidence[0]["severity"] if evidence else None,
        "evidence": evidence[:max_evidence] if max_evidence else evidence,
        "library_examples": library.count_for(key) if (library is not None and concept) else 0,
        "topics": topics,
    }


def recurring_weaknesses(analyses: list[dict], library=None, min_games: int = MIN_GAMES,
                         max_evidence: int | None = MAX_EVIDENCE, rank=None) -> dict:
    """{"total_games", "weaknesses": [...recurring...], "seen_once": [...single-game patterns...]}.

    `max_evidence=None` keeps every occurrence (the history report scores all of them).
    `rank(game_count)` (optional) grades patterns; a parent concept is then only explained
    away by children of the same grade (two "occasional" child concepts don't hide a
    parent that is "recurring" once they're added up)."""
    total = len(analyses)
    groups = collect(analyses)
    # Roll specific concepts up one level (knight fork + pawn fork -> fork).
    rolled: dict[str, list[dict]] = {k: list(v) for k, v in groups.items()}
    for key, evidence in groups.items():
        for parent in _parents(library, key):
            bucket = rolled.setdefault(parent, [])
            known = {(e["game_id"], e["ply"]) for e in bucket}
            bucket += [e for e in evidence if (e["game_id"], e["ply"]) not in known]

    def game_set(k: str) -> set[str]:
        return {e["game_id"] for e in rolled[k]}

    recurring = {k for k in rolled if len(game_set(k)) >= min_games}
    # Prefer the specific concept: drop a parent whose recurrence is already explained by
    # recurring children (and don't list a child next to a parent that says more).
    report = set(recurring)
    for key in recurring:
        children = [c for c in recurring if key in _parents(library, c)
                    and (rank is None or rank(len(game_set(c))) == rank(len(game_set(key))))]
        if children and set().union(*(game_set(c) for c in children)) >= game_set(key):
            report.discard(key)
    weaknesses = [summarize(k, rolled[k], total, library, max_evidence) for k in report]
    weaknesses.sort(key=lambda w: (-w["game_count"], -w["severity_score"], w["title"]))

    covered = {e["moment_id"] for k in report for e in rolled[k]}
    seen_once = [summarize(k, v, total, library, max_evidence) for k, v in groups.items()
                 if len({e["game_id"] for e in v}) < min_games and k not in report
                 and not all(e["moment_id"] in covered for e in v)]
    seen_once.sort(key=lambda w: (-w["severity_score"], w["title"]))
    return {"total_games": total, "min_games": min_games, "weaknesses": weaknesses, "seen_once": seen_once}
