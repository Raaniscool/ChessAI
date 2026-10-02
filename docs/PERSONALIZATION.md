# Personalized Training — Architecture Proposal

> Status: the game-import → analysis → weakness → training MVP is **implemented** — see
> [GAME_ANALYSIS.md](GAME_ANALYSIS.md) for what was built (module names differ slightly from this proposal).
> Second pillar of the AI Chess Tutor: *study how the player actually plays, find
> recurring weaknesses across games, and build training that fixes them.*

## 1. What already exists (and is reused, not rewritten)

| Existing system | Reused for personalization |
|---|---|
| `chess_system.py` (python-chess helpers, validation) | PGN parsing, position replay, validating every generated exercise |
| `engine/service.py` + `classification.py` | Per-move verdicts in game analysis; solution verification for mined puzzles; position-aware classification is already generic |
| `teacher/` (Qwen client + fallback, facts-only prompts) | New prompt builders that receive **structured findings** instead of single-move facts; same "never invent analysis" rules |
| `lessons/` schema + loader | **Generated training sessions are lessons** — same JSON schema, validated the same way |
| `session/` manager + lesson runner | Personalized training runs through the identical teach → demo → practice → feedback loop |
| `config.py` | Feature/plan limits and data paths become settings |
| Frontend board + lesson UI | Training view, game-analysis viewer, report view all reuse the board |

Nothing in the tutor needs to change structurally. Personalization **plugs into** the
exercise/session primitives already proven by the MVP loop.

## 2. New backend systems

```
backend/app/
├── games/            # PGN import + storage
│   ├── importer.py       # parse pasted/uploaded PGN (python-chess), multi-game, strict validation
│   └── store.py          # data/games/{game_id}.pgn + {game_id}.analysis.json (local only — privacy)
├── analysis/
│   ├── game_analyzer.py  # drives UciEngine over every ply → structured MoveVerdict list
│   └── moments.py        # filters "important moments" (classification + context), no prose
├── patterns/
│   ├── detectors/        # one small detector per pattern (pluggable registry)
│   ├── categories.py     # category registry: id → metadata (open-ended, JSON-backed)
│   └── aggregator.py     # evidence across games → RecurringWeakness (≥2 games = recurring)
├── profile/
│   ├── model.py          # PlayerProfile: evidence log + computed concept states
│   └── store.py          # data/profiles/{id}.json
├── training/
│   ├── generator.py      # weakness → training plan (lesson JSON + origin metadata)
│   ├── mining.py         # positions from the user's own games → exercises
│   └── results.py        # training results → profile updates
├── plans/
│   └── limits.py         # free/paid feature limits: data, one enforcement point
```

Storage: plain JSON/PGN files under `data/` (gitignored) — no database needed until
multi-user production. Every stored analysis is structured; prose is generated at
display time, never stored as the source of truth.

## 3. Data flow

```
PGN import ──► python-chess parses & validates
   │
   ▼
GameAnalyzer (Stockfish) ──► per-ply MoveVerdict {fen, move, category, loss,
   │                          best_move, evals, phase, flags}  ← facts only
   ▼
Detectors (per game) ──► Evidence items {category, game_id, ply, fen, move,
   │                          detail}
   ▼
Aggregator (across games) ──► RecurringWeakness {category, severity, confidence,
   │                          evidence[], sample_positions[]}
   ▼
Qwen (structured findings in) ──► beginner-friendly weakness report  ← explains
   │
   ▼
TrainingGenerator ──► lesson JSON (existing schema): explain → demonstrate →
   │                  mined exercises → progressive hints → completion
   ▼
Session runner (existing) ──► results ──► Profile update ──► next training adapts
```

**Stockfish communicates with the analysis system** through the existing
`UciEngine` interface: `analyse(board, depth)` and `evaluate_move(board, move)`
return `Analysis`/`MoveFeedback` dataclasses. Game analysis adds a batch driver
with a time budget (fast pass → re-check only critical plies at higher depth) and
caches results by `(fen, depth)` so re-analysis is cheap. The engine stays the
sole authority on evals; analysis output contains zero prose.

**Qwen receives structured JSON**: `{weaknesses: [{category, severity, games,
evidence: [{fen, move, best, blunder…}], sample_positions}], plan, player_level}`.
Prompt rules are inherited from `teacher/prompts.py`: explain the facts, never
invent them, never classify what the engine can classify. The fallback teacher
can produce a basic report from templates alone, so the free tier works offline.

## 4. Answers to the ten design questions

1. **What exists / reuse** — see §1: the whole loop primitives (validation,
   engine, teacher, lessons, sessions, board UI) are reused verbatim.

2. **New backend systems** — §2. Core insight: *analysis, patterns, profile,
   training-generation, plans* are new; *everything interactive* is existing.

3. **New frontend/UI** — three views added to the existing single-page app:
   **Games** (paste/upload PGN, list, analyze button), **Report** (weakness cards
   with expandable evidence + "Start training"), **Profile** (strengths/weakness
   status + trend). Game-analysis viewer reuses the board with a move list and
   moment navigation. No new frameworks.

4. **Stockfish → analysis** — existing `UciEngine` dataclasses; batch driver with
   depth budget + caching; classification thresholds stay in
   `engine/classification.py` (single source of truth shared with the tutor).

5. **Qwen → structured findings** — new prompt builders in `teacher/prompts.py`
   (`build_weakness_report_messages`, `build_training_intro_messages`); same
   client, same fallback-degradation, same "engine facts are the source of truth".

6. **Storing game-analysis results** — `data/games/{id}.pgn` +
   `{id}.analysis.json` (versioned schema: `{"schema_version": 1, "verdicts": [...]}`).
   Findings reference `(game_id, ply)`; nothing is duplicated.

7. **Player profile representation** — versioned JSON document: an append-only
   **evidence log** plus **computed** category states
   (`{status: weak|improving|stable|mastered, score, evidence_count, trend}`).
   States are *derived deterministically* from evidence + training results —
   the AI never "decides" someone is weak at something without data.

8. **Personalized lessons** — generated as ordinary lesson JSON with extra
   metadata: `"origin": {"type": "personalized", "weaknesses": ["missed_tactic"],
   "evidence": [...]}`. The loader validates them like any lesson; the UI marks
   them as "built from your games". Adding fields never breaks old lessons
   (schema tolerates extra keys).

9. **Free/paid enforcement** — `plans/limits.py` defines plan limits as **data**
   (free: 5 games/run, top 3 weaknesses, 1 training session, 5 puzzles, basic
   explanations; paid: 15 games, all detectors, cross-game comparison, trends,
   re-analysis, longer training, deeper explanations). Enforcement happens in
   **one service layer choke point** (`require_feature(plan, key)`) that raises a
   structured `UpgradeRequired` response the UI renders nicely. UI hides what's
   unavailable, but the server is the authority (client-side hiding is UX, not
   security). Upgrading later = billing system sets `profile.plan` — no code
   paths change. The free tier runs the complete loop: import → analyze →
   weaknesses → training → improvement.

10. **Expandability of categories** — two registries: **detectors** (Python,
    registered by id) and **categories** (JSON metadata: id, title, description,
    severity weights, which training templates apply). The aggregator is
    category-agnostic — it only sees `(category, evidence[])`. Adding a new
    training category = drop a JSON file + (optionally) one detector function.
    Categories from the brief (opening, development, king safety, tactics,
    calculation, piece safety, pawn structure, middlegame strategy, endgames,
    conversion, time management) become the initial JSON catalog, not enums.

## 5. MVP for this feature (P1) — smallest useful slice

Scope (builds on the completed core loop, deliberately excludes the rest):

1. **PGN import**: paste/upload one or many games → parse & validate → store
   locally. Reject invalid PGN loudly (correctness before confidence).
2. **Game analysis**: `GameAnalyzer` runs Stockfish over each game with a bounded
   budget; produces verdicts + "important moments" (blunders/mistakes/missed
   winning moves/opening errors). Progress reported to the UI.
3. **Three starter detectors** proving the pattern pipeline:
   - `lost_material` (piece safety — losing material to tactics/undefended pieces)
   - `opening_development` (poor development, delayed castling)
   - `missed_tactic` (engine's best move was a winning capture/check the player
     didn't play)
   - *(forks, pins/skewers, endgame, conversion… come in P2 as more detectors)*
4. **Aggregation**: findings across imported games → recurring weaknesses
   (≥ 2 games) with severity + evidence + sample positions from their real games.
5. **Report**: structured findings → Qwen (or fallback) → beginner-friendly
   report. Free limits applied (5 games, 3 weaknesses).
6. **One personalized training session**: generated lesson JSON from the top
   weakness — concept explanation → board demonstration → 2–3 exercises,
   including at least one **mined from the user's own game**, with progressive
   hints — executed by the existing lesson runner.
7. **Profile update**: completing the training records results; profile shows
   `weak → improving` for that category.

Explicitly **not** in P1: paid gating UI (only `PlanLimits` data + endpoint),
third-party platform APIs, trends over time, fork/endgame detectors, automated
re-analysis, accounts/multi-user.

**Testing plan** (per quality requirements): PGN import (valid/invalid/multi),
analyzer verdicts against known games (canned engine results for speed + one
live-engine integration test), detector unit tests on synthetic positions,
aggregator recurrence logic, generated-lesson validation through the existing
loader, profile state transitions, plan-limit enforcement.

## 6. Roadmap mapping

- **Phase 1 (current):** core tutor MVP — chess/engine/teacher/lessons ✅, session API + web UI (in progress).
- **Phase 2:** more courses *and* personalization P1 (this document) — import, analyze, report, first personalized session.
- **Phase 3:** puzzle system (shared by tutor hints + mined puzzles) + more detectors (forks, pins, endgames).
- **Phase 4–5:** game-analysis UX deepens, trends, re-analysis loop, plan gating UI → the full feedback loop:
  *play → analyze → weaknesses → training → results → better training.*

## 7. What was built: the personal coach

The proposal above is implemented as `backend/app/learner/`, and every planner uses it.

**Onboarding (optional, one card):** rating and platform, or an experience level when the
rating is unknown (new / rules / casual / club / strong). Also an optional Chess.com username,
goals and explanation style. The tutor works fully without a username. With one, the thank-you
message offers **🔍 Analyze my recent games**, which fetches and analyzes them in one click. A
skipped onboarding counts as no evidence, not as "beginner".

**Learner profile** (`profile.py`, `GET /api/profile`):
- rating estimate and per-concept status (new / practicing / learned / mastered / weak /
  needs_review) with a rating for each concept;
- puzzle and lesson results, hint and reveal use, recurring weaknesses from analyzed games,
  recent topics, explanation style and TTS preferences.

Every attempt updates it through `record_attempt`. Game findings enter as weaknesses only when
they recur across several games.

**Difficulty** (`views.py`, used by lessons and plans): `target_rating` = the concept's own
rating, or the overall one, plus a purpose offset. Puzzles use the evidence-based calibration in
`learner/difficulty.py` instead (rating prior + game evidence + puzzle results, per skill and
concept); see "Difficulty calibration" in `PUZZLE_LIBRARY.md`. The purpose offsets:

| Purpose | Offset |
|---|---|
| learn | −190 |
| practice | −80 |
| review | −120 |
| challenge | +60 |
| simplify | −330 |

`lesson_shape` then decides how many worked examples come before practice (fewer as skill
grows), whether to use calculation-length lines, and whether to use real-game positions (for
ideas solved in puzzles but missed in games). Library tiers span roughly 700–1700, so a
beginner's knight forks are one-movers while a club player gets longer lines.

**In-lesson adaptation** (`adapt.py`, at most 2 changes per lesson):
- easy success → a harder position;
- repeated failure → an easier one or a prerequisite;
- found the key move but lost the line → a calculation position.

**Plans follow the learner** (`personalize.order_topics`): weak and due topics come first, with a
note explaining why; mastered topics go last, or are listed as "left out" when the plan is full.
Nothing unrequested is added. Review lessons that would be far too easy are dropped
(`REVIEW_TOO_EASY`).

**Explanation length** (`teacher/importance.py`): each moment is critical, important, supporting
or obvious. The word budget per level is:

| Importance | Word budget |
|---|---|
| critical | 70 |
| important | 45 |
| supporting | 20 |
| obvious | 8 |

The budget is scaled by the learner's style (brief ×0.6, detailed ×1.5). Obvious moves get a
line, not a paragraph, and they skip the AI call entirely.

**Lessons read like a coach** (`knowledge_lessons.Said`): later examples don't repeat what the
lesson already said (the concept summary, template sentences, the provenance note). Role lines
("Your turn…") appear only when the task changes. The intro gives the reason for the lesson's
shape without counting the examples twice.

**Questions and skills:**
- "What is en passant?" and "how does castling work?" are treated as lesson requests, so they get
  the verified lesson.
- "How does the knight move?" gets an instant rules answer.
- A glossary term can declare a *skill* practice filter. Calculation uses it to select verified
  positions that need 2–3+ of the learner's own moves, near their level, across different tactics.

**Speed:**
- Library plans take 0.01–0.1 s, and custom plans without Qwen take 0.5–2 s.
- Qwen organizes plans in the background within `QWEN_PLAN_BUDGET` (8 s), so the learner is never
  kept waiting on it.
- `POST /api/knowledge/answer` answers definitions instantly.
- Every `/api/*` response carries `Server-Timing`, and requests over 3 s are logged.

Tests: `test_learner.py`, `test_adaptive_lessons.py`, `test_lesson_wording.py`,
`test_questions_and_skills.py`, `test_importance.py`, `test_request_matching.py`, and the UI e2e
onboarding flow.
