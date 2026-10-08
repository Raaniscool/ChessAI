"""Qwen as a *proposer* of plan structure — never as an authority.

Qwen sees the request, the learner's level and, for each requested component, a short
list of verified options under aliases (r1, r2, ...) — never the whole library. It may:
  * choose and order options into units, write unit titles, objectives and short texts;
  * propose new positions (FEN + SAN moves + a claim such as "win" or "mate_in_1").
Everything it returns becomes an ordinary CandidatePlan and is validated like any other:
unknown aliases become "unknown" items (rejected), proposed positions are untrusted until
python-chess and Stockfish have checked them, and texts are checked against verified facts.
On a retry Qwen is shown the previous rejection reasons.
"""
from __future__ import annotations

import logging

from .candidate import CandidatePlan, Item, Unit
from .composer import compose

log = logging.getLogger(__name__)

MAX_OPTIONS = 10
_CLAIMS = {"win": {"result": "win"}, "draw": {"result": "draw"}, "best": {"best": True}}


def _options(base: CandidatePlan) -> tuple[dict[str, Item], list[str]]:
    aliases: dict[str, Item] = {}
    lines: list[str] = []
    by_comp: dict[str, list[Item]] = {}
    labels: dict[str, str] = {}
    for u in base.units:
        key = repr(sorted(u.component.items(), key=str))
        by_comp.setdefault(key, []).extend(u.items)
        labels[key] = u.component.get("label") or u.title
    for c, (key, items) in enumerate(by_comp.items(), start=1):
        opts = []
        for it in items[:MAX_OPTIONS]:
            alias = f"r{len(aliases) + 1}"
            aliases[alias] = it
            opts.append(f"{alias} (difficulty {it.difficulty}: {it.title[:50]})")
        lines.append(f"C{c} {labels[key]}: " + ", ".join(opts))
    return aliases, lines


def build_messages(base: CandidatePlan, feedback: list[str], level: str | None) -> tuple[list[dict], dict, dict]:
    aliases, lines = _options(base)
    comps = {f"C{c}": u.component for c, u in enumerate(_first_units(base), start=1)}
    fb = ("\nYour previous plan was rejected for these reasons — fix them:\n- " + "\n- ".join(feedback[:8])) if feedback else ""
    user = (
        f"Request: {base.goal!r}\nMeaning: {base.intent.get('interpretation') or base.title}\n"
        f"Learner level: {level or 'unknown'}\n"
        "Verified material for each part of the request (only these may be used):\n" + "\n".join(lines) + "\n\n"
        "Organise a plan. Rules: only the parts listed (C1, C2, ...); each unit belongs to one part; first a 'learn' "
        "unit (easiest first), then a 'practice' unit; never repeat an option; don't mention specific moves in texts. "
        "You may add up to 2 new positions per part only if you are sure they are legal; they will be checked by a "
        "chess engine and dropped if wrong.\n"
        'Answer JSON: {"title": str, "summary": str, "units": [{"part": "C1", "role": "learn"|"practice", '
        '"title": str, "objective": str, "text": str, "items": ["r1", ...]}], '
        '"positions": [{"part": "C1", "fen": str, "moves": [SAN, ...], "claim": "win"|"draw"|"mate_in_1"|"best", '
        '"text": str}]}' + fb)
    messages = [{"role": "system", "content": "You organise chess lesson plans from verified material. "
                                              "Answer with one JSON object only."},
                {"role": "user", "content": user}]
    return messages, aliases, comps


def _first_units(base: CandidatePlan) -> list[Unit]:
    seen, out = set(), []
    for u in base.units:
        key = repr(sorted(u.component.items(), key=str))
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def parse_proposal(data: dict, base: CandidatePlan, aliases: dict[str, Item], comps: dict[str, dict]) -> CandidatePlan:
    """Strictly turn Qwen's JSON into a candidate. Nothing is dropped silently: an alias we
    don't know becomes an 'unknown' item, so the validator rejects the plan and says why."""
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        raise ValueError("no units")
    units: list[Unit] = []
    for raw in data["units"][:10]:
        if not isinstance(raw, dict):
            continue
        comp = comps.get(str(raw.get("part", "")).strip())
        if comp is None:
            comp = {"kind": "concept", "id": str(raw.get("part", "?")), "label": str(raw.get("part", "?"))}
        role = raw.get("role") if raw.get("role") in ("learn", "practice") else "learn"
        items: list[Item] = []
        for k, alias in enumerate(raw.get("items") or []):
            src = aliases.get(str(alias).strip())
            if src is None:
                items.append(Item("unknown", str(alias)[:40], "practice", "unverified", title=str(alias)[:40]))
                continue
            copy = Item.from_dict(src.as_dict())
            copy.example = src.example
            if copy.kind not in ("topic", "opening_line", "definition"):
                copy.role = "practice" if role == "practice" else ("demonstration" if k == 0 else "guided")
            items.append(copy)
        units.append(Unit(str(raw.get("title") or "").strip()[:80], str(raw.get("objective") or "").strip()[:200],
                          comp, role if units_role_ok(comp) else "topic", items, str(raw.get("text") or "").strip()[:600]))
    for n, raw in enumerate((data.get("positions") or [])[:6]):
        if not isinstance(raw, dict):
            continue
        comp = comps.get(str(raw.get("part", "")).strip())
        target = next((u for u in units if comp is not None and u.component == comp and u.role == "practice"), None) \
            or next((u for u in units if comp is not None and u.component == comp), None)
        if target is None:
            continue
        claim = str(raw.get("claim") or "")
        claims = dict(_CLAIMS.get(claim, {}))
        if claim.startswith("mate_in_"):
            try:
                claims = {"mate_in": int(claim.rsplit("_", 1)[1])}
            except ValueError:
                pass
        moves = [str(m) for m in (raw.get("moves") or [])][:12]
        target.items.append(Item("proposed", f"qwen_{n + 1}", "practice", "unverified", fen=str(raw.get("fen") or ""),
                                 moves=moves, key_index=0, claims=claims,
                                 concept=_concept_for(target.component, claims),
                                 title=f"New position {n + 1}", text=str(raw.get("text") or "")[:400], difficulty=3))
    return CandidatePlan(goal=base.goal, intent=base.intent, title=str(data.get("title") or base.title)[:100],
                         summary=str(data.get("summary") or base.summary)[:400], units=units, proposer="qwen",
                         level=base.level, personal=base.personal, learner=base.learner, skipped=base.skipped,
                         notes=base.notes)


def units_role_ok(comp: dict) -> bool:
    return comp.get("kind") in ("concept", "material")


def _concept_for(comp: dict, claims: dict) -> str | None:
    if comp.get("kind") == "concept":
        return comp.get("id")
    if claims.get("mate_in") == 1:
        return "mate_in_one"
    return None


def qwen_teacher(teacher=None):
    """The Qwen teacher to organise plans with, or None when Qwen isn't set up."""
    from ...config import get_settings
    from ...teacher.qwen import QwenTeacher, TeacherUnavailable

    if teacher is not None:
        return teacher
    if not get_settings().qwen_configured():
        return None
    try:
        return QwenTeacher()
    except TeacherUnavailable:
        return None


def organize(base: CandidatePlan, intent, ctx, *, feedback: list[str] | None = None,
             teacher=None) -> CandidatePlan | None:
    """Qwen reorganises a composed plan (order, grouping, objectives). No engine use: safe to run
    in a background thread while the composed plan is being verified."""
    from ...teacher.qwen import TeacherUnavailable
    from ..planner import extract_json

    if teacher is None or not base.units:
        return None
    if all(u.role in ("topic", "opening", "definition") for u in base.units):
        return None  # nothing to organise: catalog lessons already come in order
    messages, aliases, comps = build_messages(base, feedback or [], intent.level or ctx.level)
    try:
        data = extract_json(teacher.complete(messages, max_tokens=900))
        cand = parse_proposal(data, base, aliases, comps)
    except (TeacherUnavailable, ValueError, TypeError) as exc:
        log.info("Qwen plan proposal unusable: %s", exc)
        return None
    # topic / opening / definition units aren't Qwen's to reorganise: keep the verified ones
    keep = [u for u in base.units if u.role in ("topic", "opening", "definition")]
    cand.units = keep + [u for u in cand.units if u.role not in ("topic",)]
    return cand


def propose(intent, ctx, *, feedback: list[str] | None = None, teacher=None, generate=None) -> CandidatePlan | None:
    """A Qwen-organised candidate, or None when Qwen isn't available or answers nonsense."""
    teacher = qwen_teacher(teacher)
    if teacher is None:
        return None
    return organize(compose(intent, ctx, generate=generate), intent, ctx, feedback=feedback, teacher=teacher)
