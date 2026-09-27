# Understanding requests and building custom plans

This covers two connected systems in the learning-plan pipeline:

1. **Intent clarification.** Work out what the learner means. If a request has several
   readings that would produce different plans, ask a question instead of guessing.
2. **Custom plans.** When the library has no ready-made plan, build one from verified
   content, validate it, and show it only if it passes.

The division of responsibility stays the same as everywhere else in ChessAI:

| Role | Who |
|---|---|
| Rules (legal positions, legal moves) | python-chess |
| Correctness (evaluations, best moves, claims) | Stockfish |
| Reusable verified knowledge | Knowledge Library + catalog + plan library |
| Structure proposals, wording, candidate positions | Qwen (never the final authority) |

## Pipeline

```
request ─► understand() ──(several readings?)──► question card ─► answer (remembered)
               │                                                        │
               ▼                                                        ▼
         LearningIntent  ◄──────────────────────────────────────────────┘
               │
   plain single subject? ──yes──► existing planners (Knowledge Library → catalog → fallback)
               │ no (material, flipped side, exclusions, several subjects, clarified)
               ▼
   verified plan for the same intent in the plan library? ──yes──► re-validate ─► reuse
               │ no
               ▼
   propose (Qwen ×2, then the deterministic composer) ─► validate ─► show
               │ rejected: feedback + failed items excluded ─► next proposer
               ▼ all rejected
   broader verified plan, labelled as broader ─► or review queue (never shown)
```

Code: `backend/app/planner/intent/` (understanding) and `backend/app/planner/custom/` (plans),
`backend/app/knowledge/plan_library.py` (storage), with routing in `planner.plan_for_goal()`.

## 1. Intent clarification (`planner/intent/`)

`understand(goal)` parses the request (`parse.py`) against a lexicon built from the
catalog, Knowledge Library concepts, glossary and Lichess opening families (`lexicon.py`).
It returns a `LearningIntent`, which is a list of `Component`s (topic, concept, glossary term,
material spec, opening from a side, database opening family) plus level, side and
exclusions. When the request is ambiguous it raises `ClarificationNeeded(question)` instead.

The detectors work on **kinds** of ambiguity, not on specific phrases:

| Kind | Example | Readings |
|---|---|---|
| coordination | "knight and bishop endgames", "rook and queen mates" | separately / one side has both / against each other |
| relation conflict | "bishop and knight together, separately" | the two stated relations |
| lexical | "the Philidor" (opening, endgame position, mating pattern) | each subject the name belongs to |
| side conflict | "the Sicilian as White" | face it as White / play it as Black |
| level conflict | "beginner advanced rook endgames" | each level |
| exclusion conflict | "forks but no tactics" | keep forks / drop tactics |
| vague | "teach me something" | starting points |
| Qwen suggestions | an unknown phrase that Qwen maps to 2+ concepts | each suggestion |

Rules that keep questions rare and meaningful:

- **Only different plans count.** A question is asked only when at least two readings
  produce different components or a different level. Readings that chess rules out are
  dropped first: a lone bishop or knight can't force mate, so "bishop and knight
  checkmate" has only one reading and no question is asked.
- **Clear words settle it.** "vs", "or", "together", "separately", "against the
  Sicilian" and plural family nouns ("Indian defenses") are treated as already answered.
- **Always an escape.** Every question ends with "Something else — let me explain". The
  typed text is understood again. Empty text is refused (HTTP 422). Follow-up questions about the typed text
  are limited to two levels.
- **Never asked twice.** Each ambiguity has a key such as `coordination:B+N:endgame` or
  `side:sicilian_defense:white`. The same ambiguity phrased differently gets the same key.
  Answers are stored in `IntentMemory` (`<data_dir>/intents.json`) and reused, and the
  plan shows *"Understood as … (from your earlier answer)"* with an **Ask me again** link
  (`reclarify: true`).
- **Qwen never picks.** If Qwen maps an unknown phrase to one known concept, that is used.
  If it maps it to 2 or more, they become a question when the caller can ask. When it can't
  ask, nothing is chosen and the normal fallback runs.

### API

`POST /api/plans {goal, library: true}` responds with either a plan or
`{"clarify": {key, kind, term, question, options:[{id,label,icon}], allow_other}, "goal"}`.

To answer, send the request again with `clarification: {key, choice, text?}`. The
`reclarify: true` flag forgets the stored answer and asks again. Questions are only asked
for chat requests (`library: true`); other callers keep the old behaviour.

## 2. Custom plans (`planner/custom/`)

A `CandidatePlan` is a list of `Unit`s (title, objective, the component it serves, role)
made of `Item`s. An item is either a reference to verified content or a new position with
its FEN, moves, key move, claims and text. Candidates come from:

- **composer.py**: deterministic. It picks verified positions that match each component
  (material pools come from actual boards, not tags), splits them into learn/practice units
  easiest first, orders units by prerequisites, and respects exclusions. When a material
  pool is empty it generates new positions with the Knowledge Library generator; each one
  goes through the full verification pipeline.
- **qwen_plans.py**: Qwen gets short reference ids (`r1…`), never FENs or the whole library.
  It returns a unit structure and may propose positions with claims. Unknown ids are
  reported back as errors. Its proposals go through exactly the same validation.

### Validation (`validate.py`)

Checks are registered by family and all run on every candidate. Any `error` rejects the plan.
An `uncertain` result (for example, a new position with no Stockfish available) means *needs
review*. Neither is shown to the learner as correct.

| Family | Checks |
|---|---|
| legality | valid FEN; legal board (`is_valid`, game not already over); every move legal; key move inside the solution |
| provenance | "verified" items really are verified library/catalog entries; no personal positions in shared plans; unknown references rejected |
| correctness (Stockfish) | key move sound; claims (win / draw / mate in N / best move) match the engine; exercises discriminate (not "every move is equal"); the rest of the solution is sound; opening lines screened move by move; concept items go through the Knowledge Library's own verification pipeline |
| education | size bounds; every unit has an objective; the learner practises; demonstrations before exercises, learn before practice; prerequisites before dependents (concept graph + catalog topics); difficulty doesn't jump back down; fits the learner's level; no repeated positions; no redundant units |
| consistency | units stay in scope (nothing unrequested); every requested subject is covered (or the skip is explained); items match their unit (material seen on the board, mates that really mate, concept family); exclusions respected; texts mention only verified moves and don't contradict verified results |
| duplication | new positions that already exist in the library; equivalent verified plan already stored (warning, and reuse) |
| personalization | a weakness plan trains that weakness (most positions on it), is backed by evidence from several games, and never presents the learner's own game positions as new puzzles |

A crashing check counts as `uncertain`, so it can never let a plan through.

### Regeneration and fallback (`pipeline.py`)

Each rejected attempt goes to the **review queue** (`<data_dir>/knowledge/plan_review/`). The next
proposer gets the error messages and the set of failed items to avoid. If every proposer
fails, a plan for the **broader** idea (e.g. checkmate instead of K+B+N mates) is built and
labelled as such. If that fails too, the planner falls back to the existing
verified-library fallback. A request never errors just because the library lacks an exact plan.

### Promotion (`knowledge/plan_library.py`)

`PlanLibrary.promote(candidate, report)` stores a plan only if:

- the report is `verified` with no errors or uncertain results, and
- the report's fingerprint matches the candidate (a plan changed after verification is refused).

Near-identical plans are not stored twice (same intent signature, same fingerprint, or ≥80%
shared content): the existing one is returned instead. Stored templates carry provenance:
`generated_by` ("ai (Qwen)" or "ChessAI composer"), proposer, created, verified_at,
verified_by, checks, warnings, engine and pipeline version.

- **Global** (`<data_dir>/knowledge/plans/global/`): plans made only of shared verified content.
  A final guard refuses learner data.
- **Personal** (`<data_dir>/knowledge/plans/personal/<learner>/`): anything built for a weakness or
  containing personal positions. Only that learner is served it, and never from the shared tier.

Reused plans are re-validated every time. A stored plan whose content no longer validates
(say an example was removed) is retired and rebuilt.

## Tests

- `backend/tests/test_intent.py`: ambiguous and clear requests, every ambiguity kind,
  answers, "Something else", memory and reclarify.
- `backend/tests/test_custom_plans.py`: missing library plans, generation (including real
  Stockfish), invalid positions and illegal moves, wrong Stockfish claims, bad sequencing,
  wrong concept/material matching, exclusions, text vs facts, personalization, duplicates,
  failed validation → review, regeneration, Qwen proposals (fake teacher), promotion and
  provenance, personal vs global separation, reuse and retirement, API round trip.
- `frontend/tests/plan-view.test.mjs` and `scripts/ui_e2e.mjs` section 8b: the question
  card, "Something else", the verified badge and "Ask me again".

## Limitations

- Qwen's plan proposals and reading suggestions are tested with a fake teacher. Their
  real-world quality depends on the local model, but validation doesn't.
- New positions are only generated for **material mates** (e.g. K+B+N vs K). Other
  empty material pools fall back to the broader verified idea instead of inventing positions.
- Text-vs-fact checking covers move mentions and win/draw wording. It doesn't understand
  free prose in general.
- Ambiguity detection is lexicon-based. Phrasings outside the catalog, library, glossary and
  opening database go through the existing fallback (and Qwen, if enabled).
