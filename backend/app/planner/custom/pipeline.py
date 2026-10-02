"""Custom plans when the library has no ready-made one: propose → validate → present.

    intent (already clarified)
      → an equivalent verified plan in the plan library?  → re-check statically → use it
      → propose: Qwen (organises verified options, may add positions)  ─┐
                 Qwen again, shown the rejection reasons                 ├→ validate each
                 deterministic composer                                  │   (validate.py)
                 composer again without the items that failed           ─┘
      → first fully verified candidate: promote to the plan library (global or the
        learner's personal tier), build the record, present it
      → nothing verified: every candidate goes to the review queue; a *broader* verified
        plan is offered, clearly labelled — or the caller falls back further.

Nothing that failed validation is ever presented, and nothing unverified enters the
plan library. Qwen proposes; python-chess, Stockfish and the checks decide.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..intent.model import Component, LearningIntent, can_force_mate
from .build import build_record
from .candidate import CandidatePlan
from .composer import compose
from .content import get_content_index
from .validate import Context, ValidationReport, validate

log = logging.getLogger(__name__)


@dataclass
class CustomResult:
    status: str                         # verified | reused | fallback | failed
    record: dict | None = None
    report: ValidationReport | None = None
    attempts: list[dict] = field(default_factory=list)
    template: dict | None = None
    candidate: CandidatePlan | None = None


MATERIAL_BUDGET = 30.0  # seconds for exact-material endgame positions (each is fully verified)


def _material_generator(ctx: Context, budget: float):
    """New verified mate positions for material specs whose pieces can force mate."""
    made: dict = {}

    def generate_for(spec, n: int):
        key = repr(spec)
        if key not in made:
            made[key] = _generate(spec, n)
        return made[key]

    def _generate(spec, n: int):
        if spec.relation == "versus" and spec.head == "endgame":
            return _material_endgames(spec, n)
        if spec.head != "mate" or spec.relation not in ("together", "only") or not can_force_mate(spec.pieces):
            return []
        if ctx.personal:  # learner plans don't write into the shared generated tier
            return []
        engine = ctx.get_engine()
        if engine is None:
            return []
        from ...knowledge.generation.constructors import register_material_mate
        from ...knowledge.generation.generator import generate
        try:
            name = register_material_mate(tuple(spec.pieces))
            result = generate("mate_in_one", ctx.library, engine, count=max(1, n), time_budget=budget,
                              use_qwen=False, plans=[("mate_in_one", name)])
        except Exception as exc:  # generation never breaks planning
            log.warning("material generation failed: %s", exc)
            return []
        return result.accepted

    def _material_endgames(spec, n: int):
        """Exact-material practice positions (knowledge.generation.material_positions). They are
        generic verified positions, not learner data, so they're kept for the next request."""
        objective = ctx.intent.objective or "general"
        engine = ctx.get_engine()
        if engine is None:
            log_[spec.slug()] = {"spec": spec.slug(), "objective": objective, "accepted": [], "attempts": 0,
                                 "stopped": "Stockfish is not available, so nothing can be verified"}
            return []
        from ...knowledge.generation.material_positions import generate as generate_material
        try:
            res = generate_material(spec, ctx.library, engine, count=max(1, n), objective=objective,
                                    time_budget=max(budget, MATERIAL_BUDGET), depth=ctx.depth)
        except Exception as exc:  # generation never breaks planning; the plan says what's missing
            log.warning("material endgame generation failed: %s", exc)
            log_[spec.slug()] = {"spec": spec.slug(), "objective": objective, "accepted": [], "attempts": 0,
                                 "stopped": f"generation failed: {exc}"}
            return []
        log_[spec.slug()] = res.as_dict()
        return res.accepted

    log_: dict = {}
    generate_for.log = log_
    return generate_for


def _from_template(template: dict, library) -> CandidatePlan:
    cand = CandidatePlan.from_dict(template["candidate"])
    for it in cand.items():
        if not it.trusted:  # verified when the plan was promoted; the record travels with it
            it.provenance = "plan-library"
            it.verification = {"status": "verified", "template": template["id"],
                               "verified_at": template["provenance"]["verified_at"]}
    return cand


def build_custom_plan(intent: LearningIntent, *, level: str | None = None, library=None, catalog=None,
                      engine=None, engine_factory=None, use_qwen: bool = True, teacher=None,
                      personal: dict | None = None, learner: str | None = None, plan_library=None, review=None,
                      generate_budget: float = 15.0, promote: bool = True, depth: int = 12,
                      proposers: list | None = None) -> CustomResult:
    from ...knowledge.library import get_knowledge
    from ...knowledge.plan_library import PlanLibrary, ReviewQueue
    from ..catalog import get_catalog

    library = library or get_knowledge()
    catalog = catalog or get_catalog()
    plan_library = plan_library if plan_library is not None else PlanLibrary()
    review = review if review is not None else ReviewQueue()
    learner = learner or (personal or {}).get("learner")
    ctx = Context(library, catalog, get_content_index(library, catalog), intent, level or intent.level,
                  engine=engine, engine_factory=engine_factory, depth=depth, personal=personal,
                  plan_library=plan_library, learner=learner)
    generate = _material_generator(ctx, generate_budget)

    # 1. an equivalent plan that was verified before
    template = plan_library.find(intent.signature(), learner, (personal or {}).get("weakness"))
    if template is not None:
        cand = _from_template(template, library)
        cand.goal, cand.intent = intent.goal, intent.as_dict()  # this request's words; same meaning
        report = validate(cand, ctx)
        if report.status == "verified":
            record = build_record(cand, report, library=library, catalog=catalog, status="reused", template=template)
            return CustomResult("reused", record, report, [], template, cand)
        plan_library.retire(template["id"], "; ".join(i.message for i in report.errors[:3]) or report.status, learner)

    # 2. propose → validate → regenerate
    if proposers is None and use_qwen:
        fast = _compose_and_organize(intent, ctx, personal, learner, teacher, generate, review, plan_library,
                                     library, catalog, promote)
        if fast.done is not None:
            return fast.done
        attempts, feedback, exclude, last = fast.attempts, fast.feedback, fast.exclude, fast.last
        # what's left: the composer once more without the positions that failed
        proposers = [lambda i, c, fb, ex: compose(i, c, exclude_refs=ex, generate=generate) if ex else None]
        return _propose_loop(intent, ctx, proposers, personal, learner, review, plan_library, library, catalog,
                             promote, depth, attempts, feedback, exclude, last)
    if proposers is None:
        proposers = []
        if use_qwen:
            from .qwen_plans import propose as qwen_propose
            proposers += [lambda i, c, fb, ex: qwen_propose(i, c, feedback=fb, teacher=teacher, generate=generate)] * 2
        proposers += [lambda i, c, fb, ex: compose(i, c, generate=generate),
                      lambda i, c, fb, ex: compose(i, c, exclude_refs=ex, generate=generate) if ex else None]
    return _propose_loop(intent, ctx, proposers, personal, learner, review, plan_library, library, catalog,
                         promote, depth, [], [], set(), None)


def _verified_result(cand, report, attempts, plan_library, library, catalog, promote) -> CustomResult:
    stored = None
    if promote:
        from ...knowledge.plan_library import PromotionError
        try:
            stored = plan_library.promote(cand, report)
        except PromotionError as exc:
            log.info("verified plan not stored: %s", exc)
    record = build_record(cand, report, library=library, catalog=catalog, status="verified",
                          template=stored, attempts=attempts)
    return CustomResult("verified", record, report, attempts, stored, cand)


@dataclass
class _Fast:
    done: CustomResult | None
    attempts: list
    feedback: list
    exclude: set
    last: tuple | None


def _compose_and_organize(intent, ctx, personal, learner, teacher, generate, review, plan_library, library,
                          catalog, promote) -> _Fast:
    """Latency: the plan is composed once; Qwen reorganises it in a background thread while
    Stockfish verifies the composed version. Qwen's plan is preferred when it verifies within
    the time budget (it's retried once with the validator's feedback, as before). When the budget
    runs out and the composed plan is verified, the learner gets that one instead of waiting.
    Every candidate — Qwen's or not — goes through the same validation."""
    import copy
    import time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    from ...config import get_settings
    from .qwen_plans import organize, qwen_teacher

    attempts: list[dict] = []
    feedback: list[str] = []
    exclude: set[str] = set()
    last = None
    deadline = time.monotonic() + max(0.0, get_settings().qwen_plan_budget)
    base = compose(intent, ctx, generate=generate)
    base.personal = base.personal or personal
    base.learner = base.learner or learner
    teacher = qwen_teacher(teacher)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen-plan") if teacher is not None else None

    def ask(fb):
        return pool.submit(organize, copy.deepcopy(base), intent, ctx, feedback=fb, teacher=teacher)

    try:
        future = ask([]) if pool is not None and base.units else None
        base_report = validate(base, ctx) if base.units else None
        base_ok = base_report is not None and base_report.status == "verified"
        for round_ in range(2):
            if future is None:
                break
            try:
                cand = future.result(timeout=max(0.0, deadline - time.monotonic()) if base_ok else None)
            except FutureTimeout:
                log.info("Qwen plan not ready within %.1fs: using the verified composed plan",
                         get_settings().qwen_plan_budget)
                break
            except Exception as exc:  # a broken proposer is just a failed attempt
                log.warning("plan proposer failed: %s", exc)
                break
            if cand is None:
                break
            cand.personal = cand.personal or personal
            cand.learner = cand.learner or learner
            report = validate(cand, ctx)
            attempts.append({"proposer": cand.proposer, "status": report.status,
                             "issues": [f"{i.check}: {i.message}" for i in report.errors + report.uncertain][:6]})
            if report.status == "verified":
                return _Fast(_verified_result(cand, report, attempts, plan_library, library, catalog, promote),
                             attempts, feedback, exclude, last)
            review.add(cand, report, "needs review" if report.status == "needs_review" else "rejected")
            feedback = [i.message for i in report.errors + report.uncertain]
            exclude |= {i.item for i in report.issues if i.item and i.severity in ("error", "uncertain")}
            last = (cand, report)
            out_of_time = base_ok and time.monotonic() >= deadline
            future = ask(feedback) if round_ == 0 and not out_of_time else None
        if base_report is not None:
            attempts.append({"proposer": base.proposer, "status": base_report.status,
                             "issues": [f"{i.check}: {i.message}" for i in
                                        base_report.errors + base_report.uncertain][:6]})
            if base_ok:
                return _Fast(_verified_result(base, base_report, attempts, plan_library, library, catalog, promote),
                             attempts, feedback, exclude, last)
            review.add(base, base_report, "needs review" if base_report.status == "needs_review" else "rejected")
            feedback = [i.message for i in base_report.errors + base_report.uncertain]
            exclude |= {i.item for i in base_report.issues if i.item and i.severity in ("error", "uncertain")}
            last = (base, base_report)
        return _Fast(None, attempts, feedback, exclude, last)
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)  # a late Qwen answer is simply dropped


def _propose_loop(intent, ctx, proposers, personal, learner, review, plan_library, library, catalog, promote,
                  depth, attempts, feedback, exclude, last) -> CustomResult:
    for proposer in proposers:
        try:
            cand = proposer(intent, ctx, feedback, exclude)
        except Exception as exc:  # a broken proposer is just a failed attempt
            log.warning("plan proposer failed: %s", exc)
            continue
        if cand is None:
            continue
        cand.personal = cand.personal or personal
        cand.learner = cand.learner or learner
        report = validate(cand, ctx)
        attempts.append({"proposer": cand.proposer, "status": report.status,
                         "issues": [f"{i.check}: {i.message}" for i in report.errors + report.uncertain][:6]})
        if report.status == "verified":
            return _verified_result(cand, report, attempts, plan_library, library, catalog, promote)
        review.add(cand, report, "needs review" if report.status == "needs_review" else "rejected")
        feedback = [i.message for i in report.errors + report.uncertain]
        exclude |= {i.item for i in report.issues if i.item and i.severity in ("error", "uncertain")}
        last = (cand, report)

    # 3. nothing verified: a broader verified plan, labelled as such
    broader = _broader(intent, library)
    if broader is not None:
        b_intent = LearningIntent(intent.goal, [broader], level=intent.level, interpretation=broader.label,
                                  clarified=intent.clarified, source=intent.source)
        ctx_b = Context(library, catalog, ctx.content, b_intent, ctx.level, engine=ctx.engine, depth=depth,
                        personal=personal, learner=learner)
        cand = compose(b_intent, ctx_b)
        report = validate(cand, ctx_b)
        if report.status == "verified":
            wanted = ", ".join(c.label for c in intent.components) or intent.goal
            cand.intent = intent.as_dict()
            note = (f"I couldn't build a plan with verified positions for exactly “{wanted}”, so this plan uses "
                    f"verified {broader.label.lower()} material — the broader idea, not exactly what you asked for.")
            record = build_record(cand, report, library=library, catalog=catalog, status="fallback",
                                  attempts=attempts, note=note)
            return CustomResult("fallback", record, report, attempts, None, cand)
    return CustomResult("failed", None, last[1] if last else None, attempts, None, last[0] if last else None)


def _broader(intent: LearningIntent, library) -> Component | None:
    # An exact material request is never weakened to "endgames" or "checkmates": without
    # verified positions for exactly that material the planner says so (planner._custom).
    if any(c.kind == "material" for c in intent.components):
        return None
    for c in intent.components:
        if c.kind == "material":
            cid = "checkmate" if c.material.head == "mate" else "endgames"
            if cid in library.concepts:
                return Component("concept", cid, library.concepts[cid].name)
        if c.kind == "concept" and c.id in library.concepts:
            for parent in library.concepts[c.id].parents:
                if parent in library.concepts and library.count_for(parent):
                    return Component("concept", parent, library.concepts[parent].name)
    return None
