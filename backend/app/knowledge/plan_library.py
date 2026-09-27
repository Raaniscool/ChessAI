"""Verified plans: reusable plan templates in the Knowledge Library.

A custom plan that passed every check can be stored here, so the next learner asking for
the same thing (same intent signature) gets the verified plan straight away instead of a
new generation. Rules:

  * Only candidates whose ValidationReport is "verified" AND carries the candidate's own
    fingerprint are accepted (a report can't vouch for a different plan). Unverified or
    needs-review candidates never get in — they go to the review queue instead.
  * Provenance is kept: who proposed it (composer / Qwen), which checks verified it,
    when, with which engine, and any warnings.
  * Tiers: `global` (shared, generic content only) and `personal/<learner>` (plans that
    target a learner's weakness, use their games, or contain their personal positions).
    A plan with personal content can never be stored globally.
  * Near-identical plans aren't stored twice: `similar()` finds an equivalent plan
    (same signature, or ≥80% of the same content), and promotion returns that one.

Stored under DATA_DIR/knowledge/plans/ (runtime data, not the repository).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

PIPELINE_VERSION = "custom-plans-1"
SIMILARITY = 0.8


class PromotionError(ValueError):
    """Refused: the candidate isn't verified, the report is for something else, or personal
    content was about to enter the shared library."""


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", (name or "anonymous").lower())[:60] or "anonymous"


class PlanLibrary:
    def __init__(self, root: Path | None = None):
        if root is None:
            from ..config import get_settings
            root = Path(get_settings().data_dir) / "knowledge" / "plans"
        self.root = Path(root)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ storage
    def _dir(self, tier: str, learner: str | None) -> Path:
        return self.root / "global" if tier == "global" else self.root / "personal" / _safe(learner)

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def templates(self, tier: str | None = None, learner: str | None = None) -> list[dict]:
        dirs = []
        if tier in (None, "global"):
            dirs.append(self.root / "global")
        if tier in (None, "personal") and learner:
            dirs.append(self._dir("personal", learner))
        out = []
        for d in dirs:
            for p in sorted(d.glob("*.json")) if d.is_dir() else []:
                try:
                    t = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if t.get("status") == "verified":
                    out.append(t)
        return out

    # ------------------------------------------------------------------ lookup
    def find(self, signature: str, learner: str | None = None, personal_key: str | None = None) -> dict | None:
        """A verified plan for this intent: the learner's own first, then the shared one."""
        for t in self.templates("personal", learner) if learner else []:
            if t["signature"] == signature and (t.get("personal_key") or None) == personal_key:
                return t
        if personal_key:
            return None  # a weakness plan is never served from someone else's / the shared tier
        for t in self.templates("global"):
            if t["signature"] == signature:
                return t
        return None

    def similar(self, cand, learner: str | None = None) -> dict | None:
        sig = (cand.intent or {}).get("signature")
        refs = cand.content_refs()
        personal_key = (cand.personal or {}).get("weakness")
        for t in self.templates(None, learner):
            if (t.get("personal_key") or None) != personal_key:
                continue
            if t.get("fingerprint") == cand.fingerprint():
                return t
            if sig and t["signature"] == sig:
                return t
            theirs = set(t.get("content_refs", []))
            if refs and theirs and len(refs & theirs) / len(refs | theirs) >= SIMILARITY:
                return t
        return None

    # ------------------------------------------------------------------ promotion
    def promote(self, cand, report, *, generated_by: str | None = None) -> dict:
        if report is None or report.status != "verified" or report.errors or report.uncertain:
            raise PromotionError("only fully verified plans can be stored in the library")
        if report.fingerprint != cand.fingerprint():
            raise PromotionError("the verification report belongs to a different plan")
        personal = cand.personal_content()
        tier = "personal" if personal else "global"
        learner = cand.learner or (cand.personal or {}).get("learner")
        if tier == "personal" and not learner:
            raise PromotionError("a personal plan needs a learner to belong to")
        with self._lock:
            twin = self.similar(cand, learner if tier == "personal" else None)
            if twin is not None:
                return twin  # no near-identical copies
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            tid = f"tpl_{cand.fingerprint()[:12]}"
            template = {
                "id": tid, "status": "verified", "tier": tier, "learner": learner if tier == "personal" else None,
                "signature": (cand.intent or {}).get("signature"), "intent": cand.intent,
                "personal_key": (cand.personal or {}).get("weakness"),
                "fingerprint": cand.fingerprint(), "content_refs": sorted(cand.content_refs()),
                "candidate": cand.as_dict(),
                "provenance": {
                    "generated_by": generated_by or ("ai (Qwen)" if cand.proposer == "qwen" else "ChessAI composer"),
                    "proposer": cand.proposer, "created": now, "verified_at": report.validated_at,
                    "verified_by": ["python-chess (rules)", "Stockfish (correctness)" if report.engine else
                                    "verified library/catalog content (no new positions)", "plan checks"],
                    "checks": report.checks, "warnings": [i.as_dict() for i in report.issues],
                    "engine": report.engine, "pipeline_version": PIPELINE_VERSION,
                },
            }
            if tier == "global":
                self._assert_global_safe(template)
            self._write(self._dir(tier, learner) / f"{tid}.json", template)
            return template

    @staticmethod
    def _assert_global_safe(template: dict) -> None:
        cand = template["candidate"]
        if cand.get("personal") or cand.get("learner"):
            raise PromotionError("learner data can't enter the shared library")
        for u in cand.get("units", []):
            for it in u.get("items", []):
                if str(it.get("ref", "")).startswith("mygen_") or it.get("provenance") == "personal":
                    raise PromotionError("a learner's personal position can't enter the shared library")

    def retire(self, template_id: str, reason: str, learner: str | None = None) -> bool:
        """Take a stored plan out of use (e.g. its content was later found wrong)."""
        for tier in ("global", "personal"):
            p = self._dir(tier, learner) / f"{template_id}.json"
            if p.exists():
                t = json.loads(p.read_text(encoding="utf-8"))
                t["status"] = "retired"
                t["retired"] = {"reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                self._write(p, t)
                return True
        return False


class ReviewQueue:
    """Candidates that failed or couldn't be fully verified: kept for a human, never shown."""

    def __init__(self, root: Path | None = None):
        if root is None:
            from ..config import get_settings
            root = Path(get_settings().data_dir) / "knowledge" / "plan_review"
        self.root = Path(root)

    def add(self, cand, report, reason: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{cand.fingerprint()}.json"
        path.write_text(json.dumps({"reason": reason, "candidate": cand.as_dict(), "report": report.as_dict(),
                                    "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                   indent=1, sort_keys=True), encoding="utf-8")
        return path

    def items(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*.json"))]
