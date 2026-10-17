# ChessAI Project Context

Persistent handoff for future AI coding chats. This guide describes the repository as it currently exists; implementation and tests are authoritative over stale counts or aspirational roadmap language. Last source, Git-state and lightweight-test cross-check: 2026-10-17 (Section 17 separates checks run in this checkout from earlier snapshots and their limits). For design rationale and selective commit-anchored milestones, see [CHESSAI_DESIGN_CONTEXT.md](CHESSAI_DESIGN_CONTEXT.md); regression entries do not infer unrecorded production root causes. Current branch publishing, task records and mainline-integration limits are documented in Section 24. Keeping this file current is mandatory: every major change must update it in the same task, and every final report must say whether that happened — see Section 22, rule 14.

## 1. What ChessAI Is

ChessAI is a local-first chess-learning web app: a learner can ask for a lesson in ordinary language, explore a board position, practice with puzzles, review imported games, and get guidance adapted to observed skill. It combines authored course material, generated lesson plans, curated chess knowledge, Python-chess rule checks, Stockfish analysis, and an optional Qwen-compatible language model.

It is intended to feel like a flexible coach, not a rigid command parser. A learner may ask a brief chess question in the middle of a lesson, change subjects, return to a lesson, or ask for a puzzle. These are distinct intents and must not accidentally reset or replace one another.

This is a single-user/local-oriented application today, not a hosted multi-tenant service. “Local-first” does not mean every feature works without external software: Qwen needs a compatible model server, and engine-backed analysis needs Stockfish.

## 2. Core Design Philosophy

- **Interpret the current request in context.** The active lesson is useful context, not a command that overrides a clear new request. Preserve it for greetings, temporary questions, explanations, and genuine ambiguity; change it when the learner clearly asks to switch.
- **Clarify instead of guessing.** Low-confidence or ambiguous lesson/practice requests should ask a concise question and preserve the current state.
- **Keep chess truth deterministic.** Python-chess owns rules and legal moves. Stockfish supplies evaluations and best-move facts when chess strength is at issue. Qwen explains supplied facts; it is not the authority for legality or engine verdicts.
- **Keep one authoritative live board.** The backend session owns the live `chess.Board`. `LearningState` carries only an opaque board reference, not a copied FEN or board.
- **Verify before teaching from a board.** Position setup and browser rendering have explicit validation/recovery. Do not continue from a position that failed verification.
- **Prefer existing systems to parallel ones.** Conversation routing, planner intent, knowledge retrieval, session state, engine service, assessment, and performance instrumentation already exist. Extend their contracts and tests rather than creating another router, board store, learner profile, or engine wrapper.
- **Optimize without reducing correctness.** Avoid unnecessary model calls, but do not skip required engine searches, move validation, board verification, or explanation checks to make a path appear faster.
- **Separate shared chess knowledge from private learner/game material.** User-game positions and personal examples must not silently become shared library content.

## 3. System Architecture at a Glance

| Part | Implementation | Responsibility |
| --- | --- | --- |
| Web application | Static HTML/CSS and browser JavaScript in `frontend/` | Lessons, coach chat, board interaction, Puzzles/Training, game analysis, and presentation of backend results. No frontend build step is required. |
| API | FastAPI in `backend/app/main.py` plus focused API modules | Requests, session lifecycle, routing, game/puzzle endpoints, and static app serving. Several primary routes—including coach, session, and plan routes—live in `main.py`; do not assume one API file per feature. |
| Chess rules | `python-chess` | FEN/PGN parsing, legal moves, replay, check/mate/stalemate, and board facts. |
| Engine | UCI Stockfish via `backend/app/engine/` | Evaluations, best lines, move feedback, analysis, and verification where required. |
| Language model | Optional OpenAI-compatible Qwen endpoint via `backend/app/teacher/qwen.py` | Conversational explanations, semantic route interpretation, and selected planning/generation work. It may be unavailable; much deterministic behavior remains local. |
| Chess knowledge | `backend/app/knowledge/`, `basic_explanations/`, planner catalog and lesson data | Verified examples, concepts, opening trees, glossary, curated multi-level explanations, and lesson content. |
| Learner data | `backend/app/learner/`, assessment and game stores | Local learner profile, evidence, calibration, review, game history, and personalization. |
| Tests | `backend/tests/` and `frontend/tests/` | Python API/domain tests plus Node tests for frontend behavior and board safety. |

The backend consumes the model server's OpenAI-style response; Qwen streaming arrives from that server as SSE. The app's browser-facing session/coach streams are NDJSON (`application/x-ndjson`), parsed by `frontend/app.js`/`frontend/net.js`. These are two different protocol boundaries.

## 4. Request and Conversation Flow

Typical coach turn:

1. The browser sends the current message, session identifier when present, recent conversation, and structured learning-state context to `POST /api/coach/route`.
2. The backend uses a narrow local fast path where intent is unambiguous (for example, greetings, discovery, lesson-status questions, or a locally answerable question). Otherwise `backend/app/planner/intent/conversation.py` asks the configured semantic interpreter and validates its structured response. Offline fallback is confidence-aware.
3. The route result is dispatched as a greeting/discovery, clarification, temporary question, lesson/topic/mode change, puzzle/practice request, or board request. A plan request goes through the existing planner and plan/session endpoints; a route decision by itself must not silently mutate the active session.
4. For an existing lesson, the server resolves the session from `SessionManager` and supplies its structured lesson context to the answer path. A temporary answer preserves the lesson. A clear topic switch starts a new plan/session and the frontend parks the prior lesson for possible return.
5. A board question resolves the position from the server's authoritative session board. Legality and whose-turn questions use Python-chess facts; move strength, evaluation, or best-move requests require Stockfish facts.
6. The server streams answer events to the browser as NDJSON. Qwen is used only when configured and needed; local/verified fallbacks remain available for supported paths.

Move feedback has a related but separate path: the browser sends a UCI move and `expected_fen`; the backend checks the expected position and legality, updates the authoritative board, runs the required engine analysis, and returns deterministic feedback. A later streamed explanation may elaborate on those facts. Do not route a board fact through ordinary lesson text and pretend that it was engine-verified.

Fast routes are deliberately narrow. They should not turn every message with a chess noun into a new lesson. “What can you teach me?” is discovery; “teach me pins” is a learning request; “what is a pin?” is an explanation; “work on pins” may need clarification.

## 5. Semantic Intent and Routing

There are **two related but distinct intent layers**:

- `backend/app/planner/intent/conversation.py` interprets the learner's current conversational action. Supported actions include `greeting`, `discovery`, `general_chat`, `start_lesson`, `continue_lesson`, `resume_lesson`, `lesson_question`, `board_question`, `explanation`, `practice`, `puzzle`, `topic_change`, `mode_change`, and `clarify`.
- `backend/app/planner/intent/understand.py` and the existing planner resolve the chess topic, side/variation, ambiguity, and plan content. They use shared catalog, glossary, opening, and knowledge data; they are not a substitute conversational state machine.

The conversation interpreter returns a validated schema with action, topic, subtopic, mode, objective, confidence, `preserve_context`, `requires_engine`, and optional clarification. The current minimum confidence is `0.68`; malformed/low-confidence results should clarify or use a conservative fallback, not be silently rewritten as a lesson. The prompt instructs the router that explicit new intent wins over old context, temporary questions preserve context, puzzles can inherit a relevant active topic, and board facts must come from the session.

Qwen is the primary semantic classifier when configured, but there are small regex/local classifiers for closed fast intents and offline fallback. Those are deliberately limited; topic resolution still uses shared planner intent. Do **not** replace this design with a growing list of keyword-to-action rules. The local `QuestionIntent` classifier in `backend/app/knowledge/answers.py` also distinguishes short definitions, examples, openings, personal coaching, and position questions before selecting local knowledge or a tutor path. Its result can be reused by routing so an ordinary contextual question does not trigger a redundant semantic-model call.

Regression coverage in `backend/tests/test_coach_conversation.py` includes multi-turn greetings, discovery, starting lessons, temporary questions, clear/repeated topic switches, puzzle topic inheritance, ambiguity, board-reference-only context, lesson continuation, low-confidence clarification, offline fallback, and when engine facts are required. Fast paths explicitly avoid passing browser FEN to the semantic router. Preserve and extend those cases whenever routing behavior changes.

## 6. Lesson and Conversation State

`SessionManager` in `backend/app/session.py` owns live in-memory lesson sessions and their authoritative boards. A structured `LearningState` is derived for routing/context; its current fields are:

- `topic`, `topic_id`, `subtopic`
- `mode`, `objective`, `stage`, `progress`, `difficulty`
- `relevant_context`
- `board_state_ref` (opaque reference to the server session board; **not** a FEN copy)

The session itself also tracks the active plan/lesson, current step/progress, board and move history, conversation/transcript context, and any per-session teaching state. Consult `backend/app/session.py` and its `as_dict()` methods before changing the schema. When the browser supplies a session ID, the backend derives lesson state from that session and does not trust a client copy; with no session, the routing endpoint can use the structured client context. The narrow status fast path reads the session-derived state. The semantic router filters state fields and strips `fen`/`board_fen`.

A temporary question or greeting must not mutate or discard the active lesson. A clear topic switch supersedes it; a later “continue the lesson” should resume the same parked lesson when its in-memory session is still available. The frontend parks lesson entries in UI memory; plan identifiers have limited browser persistence, but live backend sessions are in memory, not durable storage. Do not promise a lesson survives a server restart or a browser reload unless persistence is implemented and tested.

Do not add duplicate board state or duplicate free-form lesson state to the browser to work around the session API. If state changes, update the `LearningState` builder, route contract, frontend consumers, and tests together.

## 7. Board and Position Authority

- **Backend authority:** `SessionManager` owns the live `python-chess` `Board`. Session move APIs parse UCI/SAN as appropriate, validate legality, and mutate that board only after validation. `expected_fen` is a stale-position/concurrency guard; it is not permission to replace the server board with an arbitrary client board.
- **Frontend model and rendering:** browser-side chess state and `VerifiedBoard` in `frontend/board-state.js` are used to render, interact, and check that the visible pieces/side-to-move agree with the intended position. `frontend/position.js` validates position payloads. They are not the authority for server legality or analysis.
- **Setup and recovery:** `showPosition`/the board-state setup path verifies both the browser chess model and rendered squares. A transient missing piece is detected and repaired by a full-position retry; persistent mismatch is an error, not a successful setup. Moves and markers are applied only to a verified position. Review `frontend/tests/board-state.test.mjs` and `frontend/tests/position.test.mjs` before changing this boundary.
- **References, not copies:** conversation state may carry `board_state_ref` so the server can identify the relevant session. The semantic model is not sent the browser's FEN and must not infer a position from chat history. Board questions resolve through the backend session.
- **Teaching safety:** a lesson step's expected position/sequence is checked against the authoritative board. A failed setup or stale expected position must stop/recover before any explanation, grade, or next move is based on it.

**Diagnosing a setup failure:** this repository uses `chess.js` (`frontend/vendor/chess.mjs/Chess.js`) for the browser chess model and the vendored `cm-chessboard` (`frontend/vendor/cm-chessboard/src/Chessboard.js`) for rendering; it does **not** use Chessground. First verify the source FEN/line with Python-chess and the backend lesson/puzzle payload. Then use `parsePosition()` and `inspectBoardPosition()` in `frontend/position.js`/`board-state.js`: compare the expected FEN, the chess.js-derived board, cm-chessboard's position/piece accessor, and rendered SVG squares. A mismatch in the parsed model points to missing/invalid input or chess.js/state update; a correct model with a different rendered map points to cm-chessboard rendering/animation/order; a frontend model and render that agree while the API rejects `expected_fen` points to the session/request sequence or backend board. `VerifiedBoard` serializes operations, retries setup without animation, restores a previous/fallback position (or leaves a safe blank failure state), and reports mismatch layers. Capture those four states for the same lesson step rather than changing libraries speculatively.

The same rule applies to game analysis and puzzles: replay is validated with Python-chess; browser display is verified; and generated or stored examples must pass the knowledge validators. Never introduce a second independently mutable “current board” in lesson state.

## 8. Stockfish and Chess Facts

`backend/app/engine/` wraps the UCI engine and centralizes engine startup, analysis, feedback, and shutdown. Resolution order is `ENGINE_CMD`, bundled Node Stockfish WASM when available, then a system `stockfish` executable. Actual availability depends on the host. Game analysis, verified generation, and several puzzle paths intentionally start the engine lazily; static concept definitions should not.

Use Python-chess for legality and board-rule facts. Use Stockfish when answering evaluation, best move, move quality, engine alternatives, or any claim whose truth depends on chess strength. Examples and lessons can use locally curated verified facts without a fresh search when the request is about those facts. If required engine analysis is unavailable, report/degrade explicitly; do not invent a verdict or label an unverified candidate “engine checked.”

Move classification lives in `backend/app/engine/classification.py` and uses side-to-move evaluation loss rather than raw centipawn deltas alone. Central thresholds are excellent ≤15 cp, good ≤50, inaccurate ≤120, mistake ≤300, blunder >300, with missed-forced-mate escalation and damping for already-decided positions. The same policy is reused by lesson feedback and game analysis. Stockfish decides strength; Python-chess decides legality; review code and the teacher turn those facts into language.

Engine lines are replayed/validated before they become teaching facts. Qwen may explain structured verdicts but must not override them. `backend/tests/test_verdict_consistency.py` and `backend/app/teacher/consistency.py` protect against a rejected move being praised or an engine verdict being contradicted. Keep engine searches, verification, and their existing performance logs when optimizing; report cold startup/cache conditions when quoting timing.

## 9. Qwen / Ollama: Configuration, Streaming, Warm-up, Fallbacks and Latency

`QwenTeacher` in `backend/app/teacher/qwen.py` speaks an OpenAI-compatible chat-completions API, which can be provided by Ollama, LM Studio, vLLM, or another compatible server. Qwen is optional; configuration is controlled by `backend/app/config.py` and environment variables, not by assuming the `.env.example` file is active.

Relevant code defaults (not proof of the runtime configuration):

| Setting | Code default / behavior |
| --- | --- |
| `QWEN_MODEL` | Empty/unconfigured; a model name is required to enable the teacher. |
| `QWEN_BASE_URL` | `http://localhost:11434/v1` (teacher appends `/chat/completions`). |
| `QWEN_API_KEY` | `ollama` default (local servers commonly ignore it); use a real secret only for a secured remote-compatible endpoint. |
| `QWEN_TIMEOUT` | 120 seconds for ordinary completion/stream reads; semantic routing has a separate five-second HTTP request deadline so timed-out inference is not left running in the background. |
| `QWEN_MAX_TOKENS` | 300 default output-token cap where the caller does not override it. |
| `QWEN_THINKING` | `auto`; hybrid Qwen3 builds receive `/no_think`; `-instruct` builds already do not think, and `-thinking` builds ignore that switch. `on`/`off` can force policy. |
| `QWEN_PLANNER` | `auto`; known catalog topics avoid a planner-model call; use the model when the catalog cannot resolve a custom goal. |
| `QWEN_PLAN_BUDGET` | 8 seconds for the bounded plan-completion path. |
| `QWEN_WARMUP` | Enabled by default when configured; app startup sends a one-token warm-up from a daemon background thread. There is no per-request model-unload call. |

The example configuration may recommend `qwen3:4b-instruct`; that is a recommendation, **not** evidence of the user's active model, quantization, context size, CPU/GPU offload, or keep-alive. Check the target process, endpoint, and logs. The project README documents Ollama's default five-minute idle unload and suggests `OLLAMA_KEEP_ALIVE=2h` when appropriate; that is server policy, not a ChessAI guarantee. The actual Ollama version, `OLLAMA_KEEP_ALIVE`, and `ollama ps` state were not inspected in the audit environment. A warm-up request or successful health/configuration response does not establish that a model is reachable or remains loaded.

The model is used selectively for conversational answers, semantic intent classification, and selected planner/generation work. Local fast routes, deterministic facts, and offline fallbacks avoid model calls where possible. Qwen receives structured lesson/engine/knowledge context for explanation; it is not the source of board legality or the move verdict.

For streaming, `QwenTeacher.stream()` consumes upstream SSE. `ThinkFilter` buffers split tags and removes `<think>...</think>` and reasoning fields so chain-of-thought is not shown. If a stray closing tag means already-emitted text was reasoning, the stream can emit a reset marker so the consumer can retract it. The application then translates the visible answer/events to its browser-facing NDJSON stream. If Qwen fails or returns unusable/conflicting text, the applicable local deterministic fallback or consistency checks remain in effect; do not bypass them.

Latency evidence is limited. `docs/LATENCY_AUDIT.md` reports route/request topology and pipeline measurements made with a **mock OpenAI-compatible model with a fixed 50 ms first-token delay**, plus some real Stockfish timings. For scale only: the offline move-quality sample took about 458 ms with a cold depth-14 Stockfish startup/search (~180/~255 ms); a paired warm search was ~36 ms, so that pair is not an apples-to-apples speedup. With the artificial mock, post-change active-lesson questions measured ~99–106 ms TTFO and ~100–106 ms total. Qwen was unconfigured in that audit environment; no real-Qwen cold/warm load time, TTFO, tokens/second, or target-machine performance is established. API loopback did not measure browser paint or network delay; use backend `chessai.performance` logs and the app's `?debug=1` browser timing on the target machine. Do not present mock results as real model benchmarks or claim an optimization is safe if it removes required engine/validation work.

## 10. Knowledge Systems

ChessAI has several deliberately separate local knowledge stores:

1. **Verified Knowledge Library** — `backend/app/knowledge/` with concepts, examples, facts, retrieval, opening trees, difficulty, provenance, and validation. Source data includes `backend/app/knowledge/data/concepts.json`, `glossary.json`, and `opening_trees/*.json`; additional user/runtime data may be stored outside the checked-in seed files. Examples may be verified, pending review, or otherwise untrusted. Only eligible verified examples should supply teaching facts or puzzle candidates by default.
2. **Basic Explanation Library** — `backend/app/basic_explanations/data/explanations.json` plus `library.py`. Curated beginner/intermediate/advanced wording, key ideas, common misconceptions, aliases, and references to verified examples. Beginner wording may reuse a Knowledge Library summary or Glossary definition rather than copy it.
3. **Legacy/domain Glossary** — concise chess term definitions and aliases in the knowledge package. It is a source/reference, not the conversational router.
4. **Planner catalog and opening-tree data** — resolves course topics, sides, named lines, and training plans. It is separate from a general-purpose prose corpus.

`backend/app/knowledge/answers.py` and `backend/app/planner/intent/` classify the task before selecting a source: a direct definition, example request, opening question, personalized coaching question, and current-position question must not all be answered by whichever keyword ranks first. Position/personal questions go to the appropriate session/game tutor path; local curated definitions can answer without Qwen or Stockfish.

To add chess knowledge, follow the schema, source/provenance, deduplication, replay, engine, and explanation validators in `backend/app/knowledge/`. Keep personal examples opt-in and separated from shared examples. Do not quote historical library totals as current: old reports/docs disagree (268/489/617); counts are dynamic and need a fresh run against the actual data directory before publication. For reference, a fresh count of the **checked-in seed data** at commit `cc71992` (2026-10-08) is **617 verified examples** — tactics 331, checkmates 145, mistakes 68, endgames 37, basics 26, openings 10 — with 89 concepts, 75 registered validators, 25 opening trees, 8 glossary terms and 63 Basic Explanation Library entries. That is a snapshot of the repository's seed files only and excludes any runtime `DATA_DIR` content (generated/personal tiers), so treat it as a floor, not a live total.

## 11. Lessons and Plans

- Authored course JSON lives under `backend/app/lessons/data/`; the checked-in authored course currently includes the Italian Game. Loaders and schema handling are in `backend/app/lessons/`.
- `POST /api/plans` and planner code combine catalog/lexicon intent, Knowledge Library content, opening-tree plans, custom requests, and optional Qwen planning. `POST /api/lessons/{id}/start` starts an authored lesson; plan/session endpoints in `backend/app/main.py` create or advance interactive lessons. Inspect current route definitions rather than assuming `plan_api.py` or `session_api.py` exists (neither does).
- `backend/app/session.py` orchestrates the active step, board, move evaluation, feedback, structured lesson state, and stream context. Lesson goals and step/progress state belong in this structured model, not inferred from the transcript alone.
- The engine returns deterministic move facts/classification. The teacher explains them; accepted moves, missed ideas, retries, hints, and progress remain distinct. Limited adaptation uses profile context to adjust lesson behavior, but it should not silently rewrite the lesson goal or board.
- Conversation routing controls whether to start, continue, resume, ask a temporary question, clarify, or switch. Do not make a direct answer create a lesson as a side effect.

## 12. Puzzles and Training

The top-level Practice area contains the Puzzles dashboard, with **Personalized**, **By theme** (practice), and **Training** submodes. Ordinary puzzles use `/api/puzzles` in `backend/app/puzzle_api.py`: dashboard/selection, set and next requests, result recording, difficulty, and adaptation. Selection uses verified library examples, concept/weakness evidence, learner calibration, session outcomes, and recent exposure. If the eligible library is short, the API can request a small number of strictly verified generated positions; this costs Stockfish time. It is not an excuse to return unverified positions.

**Training is implemented but is a distinct flow.** It is exposed as a submode in the Puzzles dashboard under the top-level Practice area and has its own `backend/app/training_api.py` endpoints and assessment logic. It exercises a learner against a bot/position sequence and gathers assessment evidence; it is not the same thing as selecting and solving a standalone library puzzle. Preserve its separate route/state/assessment contract even though it is presented beside Puzzles.

> **Decision Puzzles — PLANNED / NOT YET IMPLEMENTED.** No dedicated Decision Puzzles module, API, or UI flow was found in the current implementation. This label is distinct from ordinary puzzles and the existing bot Training mode. The proposed future concept is a decision-making loop that presents a position, asks what the learner notices/chooses, reacts to the choice, and coaches the underlying concept. That is future intent only, not a shipped capability. A future implementation should reuse the authoritative board, Python-chess, verified position setup, Stockfish where needed, existing assessment/profile signals, and Qwen only for explanation; it should not copy board truth into a new state store. `docs/LATENCY_AUDIT.md` discusses readiness assumptions, not proof that the feature exists or that real-Qwen latency has been certified.

## 13. Game Analysis

Game import/storage/API code is in `backend/app/game_api.py`, `backend/app/games/`, and `backend/app/analysis/`. Confirmed input sources include pasted PGN and Chess.com public-game fetching. Do not promise an importer for another platform unless it is found in current code.

Analysis validates and replays the game with Python-chess, performs a quick Stockfish pass, then deeper confirmation for candidate mistakes/missed mates. Confirmed moments can include validated alternatives when available, plus move facts, motifs, and review text. Displayed moments are capped (currently eight per game) while statistics and confirmed-ply data preserve additional evidence; small inaccuracies are not all promoted to headline events. Categories use the same centralized move-classification policy as lessons. The current classifier has no “brilliant move” class; do not invent a brilliant-move badge or claim it is supported.

`backend/app/analysis/review.py` creates deterministic headlines/facts and a deterministic fallback. Qwen can explain those facts, but consistency checks reject replies that contradict the engine verdict or refer to unsupported moves/pieces. Stalemate receives a draw-specific headline rather than being described as an ordinary missed mate.

Recurring weaknesses and personalized game-derived training are separate from a single-game report. Game-history aggregation is scoped to one learner identity, and recurring patterns require enough evidence; a one-off mistake is not automatically a learner weakness. Game-derived examples are personal/private by default, not silently promoted to the shared Knowledge Library.

## 14. Learner Model and Personalization

The local profile and evidence services are in `backend/app/learner/`; assessment and needs logic are in `backend/app/assessment/`. Puzzle outcomes, Training assessments, lesson behavior, and analyzed-game evidence feed different signals. The profile tracks per-skill evidence/status/confidence and can distinguish an observed weakness from a one-time error. Difficulty calibration uses prior/observed skill and current-session results to select practice in the learner's zone; results can adjust a running set.

Game-history weaknesses are evidence-backed and distinct from profile-derived puzzle/Training needs. The game endpoints scope history, weaknesses, and training to one selected learner; when no player is specified, current code selects a single default player rather than merging people. Identity ambiguity must be resolved, not guessed.

Personalization should choose useful focus, difficulty, and review using existing profile/assessment APIs. Do not add a second profile, infer a durable trait from one move, mix players' games, or expose personal game positions as shared training content. Persistence is local/runtime storage; do not describe it as account synchronization or cloud identity management.

## 15. Frontend Map

The UI is static, served from the FastAPI app; browser modules use relative `/api/...` URLs.

| File | Role |
| --- | --- |
| `frontend/index.html` | Tabs, panels, controls, board, and user-facing shell. |
| `frontend/app.js` | Main app state, API calls, lesson/board orchestration, chat routing and stream consumption. |
| `frontend/coach.js` | Coach message presentation and related interaction helpers. |
| `frontend/net.js` | Fetch/error handling and NDJSON stream parsing. |
| `frontend/board-state.js`, `position.js`, `board-nav.js` | Board model/render verification, safe position parsing/setup, and navigation. |
| `frontend/puzzles.js`, `puzzle-solver.js`, `training.js` | Puzzles dashboard/solver and separate Training flow. |
| `frontend/analysis.js` | Game import, analysis display, history/weakness views. |
| `frontend/speech.js` | Read-aloud controls, speech queue, browser/server voice fallback, and move/square highlighting. TTS is not implemented in a `frontend/tts.js` file. |

When adding UI, reuse the existing API wrapper, route/stream path, board verification, and component conventions. Do not call `localhost` from browser-facing code to reach another service; use the relative API and a backend proxy/server configuration if one is needed. Inspect the actual module list—file names in old notes may be stale.

## 16. Backend Map

| Area | Location | Role |
| --- | --- | --- |
| API/app entry | `backend/app/main.py` | FastAPI app, coach/plans/sessions and stream endpoints, app lifecycle and static serving. |
| Session/board | `backend/app/session.py`, `backend/app/chess_system.py` | In-memory live sessions, authoritative boards, step state, move orchestration and feedback. |
| Routing/planning | `backend/app/planner/intent/`, `backend/app/planner/intent/understand.py`, `backend/app/planner/`, `backend/app/lessons/` | Conversation semantics, chess-topic resolution, plan building, authored and knowledge lessons. |
| Teacher/model | `backend/app/teacher/`, `backend/app/config.py` | Qwen-compatible client, prompt building, fallbacks, consistency and configuration. |
| Engine | `backend/app/engine/` | UCI lifecycle, Stockfish search and centralized move classification. |
| Knowledge | `backend/app/knowledge/`, `backend/app/basic_explanations/` | Schema, stores, verified data, retrieval, facts, explanations and generation/verification. |
| Puzzle/Training | `backend/app/puzzle_api.py`, `backend/app/training_api.py`, `backend/app/puzzles/`, `backend/app/assessment/` | Puzzle set selection/results/adaptation, separate bot Training and assessment. |
| Game/analysis | `backend/app/game_api.py`, `backend/app/games/`, `backend/app/analysis/` | PGN/platform import, scoped game storage, engine analysis, review and weakness evidence. |
| Learner | `backend/app/learner/` | Profile, skills, calibration and local learner data. |

Keep primary API ownership in `backend/app/main.py` where the app currently defines it; do not create guessed `session_api.py`, `plan_api.py`, or `knowledge_api.py` modules without an intentional architecture change.

## 17. Tests and Validation

The current inventory is 59 backend `test_*.py` files and 17 frontend `*.test.mjs` files. `backend/tests/conftest.py` isolates test data/settings; API and domain tests use fake/scripted engines or teachers where possible. Qwen mock/fake coverage is not a real-Qwen benchmark. Board-state and position tests check frontend model/render correctness and recovery.

Linux/macOS commands from the repository root:

```bash
.venv/bin/python -m pytest backend/tests/ -q
node --test frontend/tests/*.test.mjs
.venv/bin/python -m compileall -q backend/app backend/tests
```

Windows PowerShell equivalents:

```powershell
.venv\Scripts\python.exe -m pytest backend\tests\ -q
node --test frontend\tests\*.test.mjs
.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
```

Run focused tests first, then the relevant backend and frontend suites, then broader checks where time/environment permit. Engine/host/model-dependent tests can have different prerequisites; record what was actually run. For the current integration snapshot, `python -m compileall -q backend/app backend/tests` passed; `node --test frontend/tests/*.test.mjs` completed with 118 tests (108 passed, 10 skipped, 0 failed); all 74 tracked JSON files parsed; and `git diff --check` passed. A later Arena sandbox run **did complete the backend suite** (2026-10-08, commit `cc71992`, Python 3.11.2, pytest 9.1.1, `chess` 1.11.2, fastapi 0.142.4): `python -m pytest backend/tests/ -q` → **2 failed, 1725 passed, 104 skipped in 156 s**, with `compileall`, all 74 tracked JSON files, `git diff --check` and the 118-test frontend run re-confirmed in the same environment. **This was not a full-engine pass.** No working Stockfish was available there, so the run used `ENGINE_CMD=/nonexistent/engine` to make engine startup fail fast, and it ran against a **copy of the tree with no `.git` directory**. The skips are overwhelmingly engine-dependent tests, plus one optional `kokoro_onnx` test and four `test_repo_hygiene.py` tests that skip *only* because that copy was not a git checkout; those four would execute in a real clone, so a re-run there should expect slightly different skip and pass counts. Both failures are in `backend/tests/test_custom_plans.py` and both follow from the absent engine: `test_plan_for_goal_asks_then_builds_the_chosen_reading` receives the *intended* honest `PlanError` (exact-material positions cannot be verified without Stockfish), and `test_qwen_proposals_are_validated_and_retried_with_feedback` trips its own assertion at line 530 because `"fen" not in prompt` is a bare substring test and a candidate description contains “de**fen**der” — a latent test brittleness worth tightening to a word-boundary or FEN-shaped check. Re-run with a working engine before treating any of this as a clean pass.

A separate lightweight documentation-workflow check in this Arena checkout (2026-10-17; Python 3.11.2, Node v22.22.3) ran `node --test frontend/tests/*.test.mjs` → **118 tests: 108 passed, 10 skipped, 0 failed** (the skips are optional jsdom-dependent onboarding/TTS cases); a standard-library AST parse of `backend/app` and `backend/tests` → **201 Python files parsed**; and JSON parsing → **74 tracked/worktree JSON files parsed**. `git diff --check` passed before final commit. No backend pytest run was attempted in this turn because `pytest`, `python-chess`, FastAPI and `httpx` are absent; no packages were installed. This is not a backend test result or an engine/model runtime check. Node emitted `MODULE_TYPELESS_PACKAGE_JSON` warnings from ES-module files under the package's current module configuration; tests still completed successfully.

For a local app launch, port **8000 is explicitly rejected by the user**. Do not launch on 8000. The last chosen port was 8080; confirm the current run configuration and bind address before starting a server. Keep the app's API/UI same-origin and the preview host/origin allowed.

## 18. Verified Bug / Regression History

The repository contains explicit audit/regression tests for some prior defects and regression guards for multi-turn behavior. The entries below distinguish those sources. GitHub commit/PR history was inspected on 2026-10-17, but it does not establish the production root cause or incident date for every test; where code/tests do not prove those details, the uncertainty is stated. Selective milestone history is in [CHESSAI_DESIGN_CONTEXT.md](CHESSAI_DESIGN_CONTEXT.md); the current branch/publishing snapshot is in Section 24.

| Problem/failure mode | What the evidence says about how it arose | Current protection / design |
| --- | --- | --- |
| **Active lesson context could override a clear new topic or corrupt a multi-turn flow.** | `backend/tests/test_coach_conversation.py` is explicitly titled “Multi-turn regressions”; its test says the old topic is temporary context, never an override. It covers greeting → lesson → temporary definition → topic switch → puzzle inheritance → clarification → board reference → another switch → return. The exact production incident/date is not recorded. | Current-message semantic route, explicit `preserve_context`/`topic_change`, confidence gate, narrow fast/offline paths, and end-to-end route tests. A greeting/discovery is explicitly not a lesson request. The offline regression test asserts greetings never become plans. |
| **Ambiguity could guess between explanation and practice, or an old topic could leak through a switch.** | The multi-turn regression suite specifically checks “work on pins” asks which mode, puzzle inheritance uses the active London topic, and repeated switches use the newly requested topic. The tests document the failure mode but not a commit-level timeline. | Clarification carries explicit options and preserves context; only a clear topic request supersedes it. Semantic intent plus planner topic resolution remains the design—do not replace it with more keyword rules. |
| **A generic concept lookup could be mistaken for a named opening.** | `backend/app/planner/intent/conversation.py` documents that searching “explain a pin” can match the Sicilian Pin Variation by the word “pin” alone, which is not evidence that the learner asked about an opening. | `QuestionIntent` classifies the requested task first; opening lookup requires opening context/name/style signal. `backend/tests/test_knowledge_tutor.py` and the intent/knowledge tests cover routing/retrieval boundaries. |
| **Weakness/history/training results mixed games belonging to different players (audit B4).** | `backend/tests/test_audit_fixes.py` explicitly records this audit finding and constructs two players with similar mistakes. | Aggregations, history and game-derived training are scoped to one selected player; default is a single player (currently the one with most games), never a blend. Regression tests assert each player's evidence stays separate. |
| **Learner identity could be guessed incorrectly; deleted games surfaced as raw errors (audit B5).** | `backend/tests/test_audit_fixes.py` names same-name-on-both-sides, placeholder `?` chosen as learner, and a game deleted during batch analysis. | Importer asks/refuses when identity is ambiguous or placeholder-only. Batch analysis emits a clean skipped event for deleted games and does not write stale results. Tests cover all three. |
| **A move that stalemated the opponent while mate was available was described as an ordinary missed mate (audit B12).** | The audit test records that the old headline hid the draw. | `backend/app/analysis/review.py` checks the resulting board and uses a draw/stalemate headline and tip; unit and end-to-end tests verify the facts. |
| **Stockfish could keep the server hanging on shutdown (audit B17).** | `backend/tests/test_api_robustness.py` records that python-chess's non-daemon engine thread could leave Ctrl+C hanging after engine startup. | App lifespan closes the engine; shutdown test verifies close and reference reset. Preserve lifecycle handling. |
| **FastAPI validation/404 responses did not match the UI's `{error: ...}` contract.** | `backend/tests/test_api_robustness.py` records that raw `detail` produced an unhelpful “Request failed (422)”. | App handlers normalize validation/not-found errors to a readable `{error: ...}` shape; regression tests protect it. |
| **Missing/invalid FEN could silently appear as a different board.** | `frontend/position.js` documents that `new Chess(undefined)` silently constructs the standard start and that this chess.js build can return an empty board for an invalid FEN; either could hide an absent/bad API position. The exact original incident/date is not recorded. | `parsePosition()` rejects missing, invalid, and empty FENs; `parsePuzzlePosition()` also rejects the standard start as a puzzle. `frontend/tests/position.test.mjs` covers missing/invalid FEN, exact supplied position, and puzzle start-position rejection. |
| **Board setup/render mismatch could otherwise be mistaken for a successful position.** | Current board-state tests explicitly simulate a transient missing piece and persistent/wrong piece states; they establish protected failure modes, but do not establish when a production incident happened. The actual renderer is vendored `cm-chessboard`, not Chessground. | `VerifiedBoard` compares expected FEN, board model/accessor and rendered SVG, serializes setup, retries without animation, restores a known position or fails safely, and prevents moves when verification fails. Use mismatch-layer details to isolate parser/model vs renderer vs session/API. Do not weaken verification. |
| **A model could guess board facts without authoritative position/engine evidence.** | `backend/tests/test_coach_conversation.py` verifies the semantic route gets only opaque `board_state_ref`, never browser FEN. This is a regression guard; a historical incorrect-answer incident is not established by that test. | Resolve board questions through `SessionManager`; Python-chess answers legality/turn questions, Stockfish answers strength/evaluation, and tests distinguish the `requires_engine` cases. |
| **Lesson result, engine verdict, and teacher prose could contradict one another.** | `backend/tests/test_verdict_consistency.py` and `backend/app/teacher/consistency.py` document and test the invariant; the code is the evidence for the protection, not proof of a particular historic incident. | One centralized classifier, deterministic facts, prompt constraints, and reply conflict checks keep accepted/rejected moves consistent. |
| **Redundant routing/model round trips could add latency.** | `docs/LATENCY_AUDIT.md` documents the old frontend request topology and subsequent consolidation; the audit explicitly warns that mock timing is not a real model comparison. | Fold local knowledge lookup into normal routing and reuse `QuestionIntent`; closed greeting/discovery/status routes avoid unnecessary setup/model calls. Required engine, board and validation work is retained. |

Other safety guards include capped chat/plan input, stale-analysis schema checks, engine-independent cached responses when no new analysis is required, and tests distinguishing legality questions from engine-strength questions. Treat unlisted concerns as risks to verify, not established historical bugs.

## 19. Current Limitations and Uncertainties

**Confirmed limits in the code/repo:**

- Live lesson sessions and browser-parked lesson state are not durable across backend restarts; this is not a multi-user authenticated service.
- The authored checked-in lesson course is limited (currently Italian Game); planner-generated/knowledge-backed lessons expand coverage but are not the same as a broad authored curriculum.
- Decision Puzzles are not implemented (see Section 12). Existing Puzzles and bot Training are real but different features.
- Confirmed game import is PGN and Chess.com public fetch; other platform support was not established.
- Game-analysis moments are capped for display; small inaccuracies are summarized rather than all individually narrated. There is no current “brilliant” category.
- Stockfish availability depends on `ENGINE_CMD`, bundled runtime, or host installation. Qwen-dependent features may fall back or be unavailable when no compatible model is configured.
- The single-user/local learner profile is not account/cloud synchronization. Library counts and generated/private content depend on data directory and runtime state.
- Documented custom-plan limits: generated material endgame exercises are single-move “find the decisive move” positions; multi-move technique lines are not generated yet. Text/fact checking covers move mentions and win/draw wording, not arbitrary prose. Ambiguity detection is grounded in the catalog, Knowledge Library, Glossary, and opening lexicon; unfamiliar wording uses the existing fallback and optional Qwen.
- Qwen plan proposals/intent suggestions have fake-teacher test coverage, not real-model quality certification. Full candidate validation remains separate from the proposer.

**Additional code-level risks and documentation drift recorded 2026-10-17:**

- `config.py` exposes `ENGINE_TIMEOUT`, but the engine service does not apply it as a per-search timeout; the UCI startup handshake can also block if an engine never responds. The 2026-10-08 sandbox observation below is environment-specific, while the missing timeout path is a code-level issue.
- Puzzle completion metrics (`solved`, `first_try`, `mistakes`, hints, etc.) arrive from the browser and are used to update usage/learner evidence; the result endpoint does not replay a submitted move history to independently verify those claims. Treat results as client-reported, not cryptographically or server-replay verified.
- `SessionManager` keeps sessions in a process-local dictionary without a TTL/size bound or per-session lock. Sessions are not restart-durable, and concurrent operations on one session need further lifecycle/concurrency review.
- Checked-in seed audit: **89 concepts, 617 verified Knowledge Library examples in 24 JSON files, and 575 global Puzzle Library entries/profile signatures**. These figures exclude runtime generated/personal data. The 10 opening examples are demonstrations/deep-dives without a key move, so they do not produce opening-concept puzzles; the checked-in Puzzle Library has no `openings`-category puzzle entries. Its few positions that happen to begin in an opening-like phase are not opening-concept coverage.
- Several focused docs are older snapshots and should not be mistaken for current measurements: `README.md` still reports 268 examples and undated historical test totals (653/1034/1161/1327); `KNOWLEDGE_LIBRARY.md` says 489 examples; `PUZZLE_LIBRARY.md` says ~320 puzzles and 302 lines. The current checked-in counts above supersede those figures. The README, `LATENCY_AUDIT.md`, and `TTS.md` also contain launch commands using port 8000, contrary to Section 17's project rule. These stale references are flagged here; they were not rewritten as part of this workflow-only task.

**Not verified for the user's current machine:**

- Actual `QWEN_MODEL`, active quantization, model-server reachability/loaded status, keep-alive, CPU/GPU placement, memory use, real TTFO and tokens/second.
- Whether Stockfish is installed/resolvable in the active environment, aside from code fallback order.
- Exact runtime counts in the user's `DATA_DIR` (generated/personal knowledge and puzzles, game history and analysis), because dynamic data may be outside checked-in seed files. The checked-in seed-only census is recorded above; older focused docs disagree.
- Whether a previously started preview/server process is still live. Preview process liveness has been unknown on later turns.

A previous environment audit found no `.env`, unset `QWEN_MODEL`, unavailable Ollama/cache, and a failed model download with TLS `SSL_ERROR_SYSCALL`. This is evidence only about that audit environment, not proof about another host or the user's current local machine. Do not claim real-Qwen performance or loaded state without checking it there.

A 2026-10-08 Arena sandbox found the **bundled Stockfish 19 WASM build emits nothing** under Node (`printf 'uci\nisready\nquit\n' | node node_modules/stockfish/bin/stockfish-19-lite-single.js` returns no output). Because `SimpleEngine.popen_uci` blocks on the UCI handshake, `engine_available()` then **hangs indefinitely instead of returning `False`**: `backend/app/engine/service.py` has no startup timeout. Its `chessai.performance` `stockfish_start` line is emitted from a `finally:` block, so it does record both `status=ready` and `status=failed` — but a handshake that never returns reaches neither branch, so a hung engine produces **no log line at all** and the instrumentation cannot distinguish “slow” from “hung”. That is evidence about the sandbox, not about the user's machine, but the missing handshake timeout is a code-level robustness gap wherever an engine binary starts and never responds. The workaround used for the completed test run above is `ENGINE_CMD=/nonexistent/engine`, which fails fast with `EngineUnavailable`.

## 20. Feature Status Summary

Status describes repository implementation, not whether the feature is configured or reachable on a particular machine.

| Feature | Status | Notes |
| --- | --- | --- |
| Semantic conversational routing and lesson continuity | **Implemented** | Semantic route, confidence-aware fallback, clarification and multi-turn regression tests. |
| Authored lessons and planner-built lessons | **Implemented / limited content** | Lesson/session engine exists; checked-in authored course coverage is limited. |
| Authoritative board, legality and verified browser setup | **Implemented** | Python-chess backend authority plus frontend verification/recovery. |
| Stockfish move feedback and game analysis | **Implemented, host-dependent** | Requires a working engine for fresh analysis. |
| Verified Knowledge Library and Basic Explanation Library | **Implemented** | Data totals can vary by seed/runtime store; do not use old counts as current. |
| Optional Qwen/Ollama-compatible teacher | **Implemented, opt-in/runtime-dependent** | No real model availability or performance is certified by this handoff. |
| Puzzles (library, personalized/practice sets, results/adaptation) | **Implemented** | Presented under the Practice area; separate from the Training mode. |
| Bot Training and learner assessment | **Implemented** | Presented as a submode of Practice; own API/assessment flow. |
| Game import, analysis, review and weakness evidence | **Implemented** | Presented under Review. Confirmed import support includes PGN and Chess.com public fetch. |
| Local learner personalization | **Implemented** | Not accounts or cloud synchronization. |
| TTS/speech integration | **Implemented, provider-dependent** | `backend/app/tts_api.py`, `backend/app/tts/`, and `frontend/speech.js`; inspect `docs/TTS.md`. `frontend/tts.js` does not exist. |
| **Decision Puzzles** | **PLANNED / NOT YET IMPLEMENTED** | No separate module/API/UI flow found. The planned version should assess a learner's explanation against verified position/engine facts; do not mistake ordinary puzzle solving or streamed Qwen explanations for this feature. |
| Unified app navigation | **Implemented** | The existing views are grouped as Learn (Lessons), Practice (Puzzles and Training), and Review (Game Analysis). This changes frontend information architecture only; underlying view/state owners remain separate. Board move-history controls are still distinct. |
| Timed games against configurable bots | **PLANNED / NOT FOUND** | Existing `backend/app/assessment/` Training is short bot-based assessment in four phases; no clock/time-control API or user-configurable bot strength flow was found. |
| Stronger game-analysis → learner-model → practice loop | **PARTIALLY IMPLEMENTED; further work planned** | Game-history evidence, local learner signals and personalized practice already connect. Extend those contracts rather than introducing another profile or duplicate recommendation path. |
| Expanded verified opening-puzzle coverage | **PLANNED; current content gap confirmed** | Checked-in puzzle data has 575 global puzzles but no opening-concept puzzle entries. The 10 checked-in opening examples have no `key_move` and are demonstration/deep-dive material only. Opening trees are not themselves puzzle coverage. |
| Engine timeout/resilience and AI response-time improvements | **PARTIAL / OPEN** | Stockfish and optional Qwen paths exist, but `ENGINE_TIMEOUT` is not applied to analysis; real Qwen latency/quality on the target host is unverified. See Sections 19 and 24. |
| Multi-user authentication/cloud profiles | **Not implemented / not found** | Do not imply tenant isolation beyond current player-scoped game data. |
| More authored courses / expanded curriculum | **Limited current coverage; expansion status unknown** | The checked-in authored course is Italian Game; no current commitment to a broader authored curriculum was confirmed. |

### 20.1 Implemented-feature source, API and test index

This is a cross-chat map of the significant shipped flows, not a second API specification. API owners and their tests are authoritative; `*` below denotes the route family actually registered by the owning module.

| Feature | Backend and API | Frontend and representative tests | Dependencies / completion notes |
|---|---|---|---|
| Coach, lesson plans and conversation continuity | `backend/app/main.py`, `backend/app/session.py`, `backend/app/planner/intent/`, `backend/app/planner/`, `backend/app/teacher/`; `/api/coach/route`, `/api/plans*`, `/api/lessons/{lesson_id}/start`, `/api/sessions/*`, `/api/knowledge/intent`, `/api/knowledge/answer` | `frontend/app.js`, `frontend/coach.js`, `frontend/plan-view.js`, `frontend/lesson-progression.js`, `frontend/net.js`; `backend/tests/test_coach_conversation.py`, `backend/tests/test_plans_api.py`, `backend/tests/test_adaptive_lessons.py`, `backend/tests/test_api.py` | Implemented. Python-chess owns board facts; Stockfish is needed for strength claims; Qwen is optional. Authored-course coverage is limited. |
| Board setup, move safety and move-history navigation | `backend/app/chess_system.py`, `backend/app/session.py`; session move/advance/reveal endpoints under `/api/sessions/*` | `frontend/board-state.js`, `frontend/position.js`, `frontend/board-nav.js`, `frontend/lines.js`; `frontend/tests/board-state.test.mjs`, `position.test.mjs`, `board-nav.test.mjs`, `lines.test.mjs`; `backend/tests/test_core.py`, `test_api.py` | Implemented board verification and board move-history navigation. Browser chess.js/rendering verifies presentation; backend Python-chess remains authoritative. |
| Unified app navigation | No API change; existing lesson, game-analysis and puzzle/training owners remain separate | `frontend/index.html`, `frontend/app.js`, `frontend/styles.css`; `frontend/tests/app-navigation.test.mjs`, `scripts/ui_e2e.mjs` | Implemented as Learn (Lessons), Practice (Puzzles and Training), and Review (Game Analysis). The app switcher preserves per-view state and sets `aria-current`; puzzle submodes and board move-history controls remain separate. |
| Knowledge, examples, opening trees and explanations | `backend/app/knowledge/`, `backend/app/basic_explanations/`, `backend/app/planner/knowledge_lessons.py`, `backend/app/lessons/`; `/api/knowledge/intent`, `/api/knowledge/answer`, `/api/courses`, `/api/topics`, plus plan/session routes | `frontend/app.js`; `backend/tests/test_knowledge.py`, `test_knowledge_engine.py`, `test_knowledge_tutor.py`, `test_basic_explanations.py`, `test_opening_trees.py`, `test_catalog_engine.py` | Implemented; checked-in seed has 89 concepts and 617 verified examples. Generated content must pass validators/engine checks. Qwen is not a verification authority. |
| Puzzle Library and adaptive solving | `backend/app/puzzle_api.py`, `backend/app/puzzles/`; `/api/puzzles`, `/api/puzzles/dashboard`, `/difficulty`, `/select`, `/set`, `/next`, `/adapt`, `/{puzzle_id}`, `/{puzzle_id}/result` | `frontend/puzzles.js`, `frontend/puzzle-solver.js`; `backend/tests/test_puzzles.py`, `test_puzzle_library.py`, `test_puzzle_profile.py`, `test_puzzle_sets.py`, `test_puzzle_skill.py`, `test_puzzle_tab_api.py`, `frontend/tests/puzzle-solver.test.mjs` | Implemented; 575 checked-in global entries have matching engine-profile signatures. Uses the Knowledge Library and local learner/usage data. Result metrics are currently client-reported (see Section 19); opening-concept coverage is absent. |
| Bot Training and learner assessment | `backend/app/training_api.py`, `backend/app/assessment/`; `/api/puzzles/training`, `/api/puzzles/training/profile`, `/start`, `/{segment_id}`, `/{segment_id}/move`, `/{segment_id}/finish` | `frontend/training.js`; `backend/tests/test_training_assessment.py`, `test_training_level.py`, `frontend/tests/training.test.mjs` | Implemented as four-phase, short bot assessment with persistent local evidence. It is not a timed game mode and has no confirmed user-configurable clock/opponent settings. Requires Stockfish for play/analysis. |
| Game import, analysis, review and history | `backend/app/game_api.py`, `backend/app/games/`, `backend/app/analysis/`; `/api/games/*` (import/fetch/list/analyze/history/explain/weaknesses/training/puzzles) | `frontend/analysis.js`, `frontend/game-picker.js`, `frontend/game-format.js`, `frontend/history-view.js`; `backend/tests/test_game_import.py`, `test_chesscom_fetch.py`, `test_game_analysis.py`, `test_game_review.py`, `test_game_history.py`, `test_game_training.py`, `test_game_api.py`; `frontend/tests/game-format.test.mjs`, `game-picker.test.mjs`, `history-view.test.mjs` | Implemented for PGN and Chess.com public fetch. Fresh evaluation needs Stockfish; external fetch depends on Chess.com availability and quotas. Game data is local and player-scoped. |
| Learner profile and personalization | `backend/app/learner_api.py`, `backend/app/learner/`, `backend/app/assessment/`; `/api/profile`, `/api/profile/onboarding`, `/api/profile/preferences`, `/api/profile/next`, `/api/profile/reset`, plus profile-aware puzzle, Training and game recommendations | `frontend/coach.js`, `frontend/puzzles.js`, `frontend/training.js`, `frontend/analysis.js`; `backend/tests/test_learner.py`, `test_training_level.py`, game-history and puzzle tests; `frontend/tests/coach.test.mjs` | Implemented locally, no cloud account/sync. Evidence comes from distinct lesson, puzzle, Training and analyzed-game paths; preserve learner/game scope. |
| Read-aloud / TTS | `backend/app/tts_api.py`, `backend/app/tts/`; `GET /api/tts`, `POST /api/tts/speak` | `frontend/speech.js`; `backend/tests/test_tts.py`, `frontend/tests/speech.test.mjs`, `frontend/tests/tts.test.mjs` | Implemented, provider/runtime-dependent; browser speech fallback is separate from optional server voices. |

## 21. Future Features and Roadmap

- **Decision Puzzles** are an explicit future concept, not a current mode. Design them as a verified position-and-decision loop that layers on top of existing board, puzzle/Training, assessment, learner-profile, and engine systems. Keep the board authoritative and Qwen explanatory; latency readiness is conditional until real Qwen and browser-paint behavior are measured on target hardware.
- `docs/ROADMAP_TRAINING_LOOP.md` is a roadmap snapshot, not guaranteed current backlog: it labels P1/P2 done and P3 started. Items it still lists include a fork-specific defensive “prevent it” puzzle constructor, Qwen intent/spec-driven puzzle generation, an identify → threat → prevent → respond defensive ladder, linking practice to the source game/move, adding `material_change` to per-game evidence, puzzle-set request-satisfaction checks, and per-candidate generation-rejection/progression visibility. Its P4 proposes deeper branching opening trees, typical plans/mistakes, and lessons that select a slice. P5 proposes richer per-skill profile/trend views, strategic/endgame detectors (examples: bad exchanges, passive pieces, conversion), and a tighter new-games → reanalysis → adaptation loop. Verify each against current code before treating it as unfinished; the roadmap's latency-audit item has since been documented separately. The roadmap also says payment processing is not implemented (tiers are configuration only).
- `docs/INTENT_AND_CUSTOM_PLANS.md` describes existing intent clarification and verified custom-plan systems, not merely a future proposal. Its documented gaps/limits include fake-teacher-only Qwen proposer tests, single-move generated material endgames, limited text-vs-fact checking, and lexicon-bounded ambiguity detection; see Section 19 and verify against current code before changing the design.
- `docs/PUZZLE_LIBRARY.md`, `docs/GAME_ANALYSIS.md`, `docs/PERSONALIZATION.md`, and `docs/KNOWLEDGE_LIBRARY.md` are focused design/status references for those areas. Their counts/metrics may be stale; current code/data wins.
- Do not infer a feature is planned merely because it would be useful. Add a roadmap item only when the user/product asks for it or a maintained project document names it, and label implementation status clearly.

## 22. Rules for AI Coding Agents

1. Work only in the branch/session assigned by Arena; do not switch branches. Before editing, record the branch, `HEAD`, `main`/`origin/main`, and working-tree status. **Never merge branches or pull requests, create merge commits, or push directly to `main` from an Arena task.** Do not force-push, reset, clean, or rebase. Preserve unrelated/uncommitted work. A separately authorized, non-merging integration path is documented in Section 24; until it is completed and verified, task-branch work is not on `main`.
2. Read this handoff and the focused documentation, then inspect the owning code and tests before editing.
3. Follow the existing architecture. No broad rewrite or new parallel router, board authority, session state, learner profile, or engine client without explicit approval.
4. Keep semantic routing semantic. Narrow closed-intent fast paths are fine; do not grow a brittle keyword decision tree in place of the existing interpreter/planner.
5. Preserve structured lesson state, temporary-question handling, clear topic switching, resume behavior, and ambiguity clarification.
6. The backend `python-chess` board remains authoritative. Never duplicate a live FEN in `LearningState`; never teach from an unverified frontend position.
7. Use Python-chess for legal/rule facts and Stockfish where chess strength/evaluation is required. Do not fabricate facts when the engine or model is unavailable.
8. Preserve Qwen reasoning filtering, validated prompts/responses, stream fallbacks, and engine-verdict consistency.
9. Do not weaken schema, provenance, deduplication, board rendering checks, analysis confirmation, or other validation just to gain speed.
10. Keep shared Knowledge Library examples separate from private/user-game examples and keep game-derived history scoped to the selected learner.
11. Check model configuration, process, and keep-alive on the target host. Never assume `.env.example` is active or report mock timings as real-model performance.
12. Reuse existing performance logging and debug instrumentation before adding timers or caches. Report cold/warm/cache state and whether timing is mock, local, or browser-observed.
13. Add focused regression tests for behavior changes; run and report only tests that actually completed.
14. **MANDATORY — update this file as part of every major change.** `docs/CHESSAI_PROJECT_CONTEXT.md` is updated in the *same task*, not afterwards and not in a separate cleanup pass. This is a project convention, not a suggestion: a major change shipped without its context update is an incomplete change.
    - **What counts as major:** new features; architectural changes; intent-routing changes; lesson-state changes; board/position verification; Stockfish or Qwen integration; Knowledge Library changes; Puzzle/Training changes; Game Analysis; personalization and the learner model; major bug fixes; significant performance work; new APIs or endpoints; and any change to what is implemented versus planned.
    - **How:** edit the relevant existing sections in place. Correct or remove outdated claims instead of appending text that contradicts them — contradictions must not accumulate. Keep updates focused and reference deeper subsystem docs rather than duplicating them.
    - **Status discipline:** keep implemented, planned, host-dependent, and unverified behavior clearly distinguished. Planned or partially verified work must never read as shipped.
    - **Record:** important architectural decisions, the systems affected, and the relevant files.
    - **Validation:** record only tests and checks that actually ran, with their environment and limits (engine available or not, mock versus real model, cold versus warm). Never record a partial, interrupted, or skipped run as a pass.
    - **Before finishing:** re-read the edited sections and compare them against the actual implementation. The code wins over any statement in this file.
    - **Same commit/PR:** include this file's update in the same commit or pull request as the major change whenever practical.
    - **Always report it:** every final report states explicitly whether `docs/CHESSAI_PROJECT_CONTEXT.md` was updated — and if it was not, why not.
    - **Minor changes are exempt:** edits that do not materially affect architecture, behavior, decisions, or future handoff needs should not generate documentation churn.
15. Use PowerShell commands for Windows workflows when relevant.
16. Port **8000 must not be used for launches** per the user's correction. Last selected port was 8080; confirm before launching.
17. Avoid committing generated datasets, runtime stores, model downloads, `.env` files, or other large/local artifacts unless explicitly required.

## 23. Safe Feature Development Workflow

1. Restate the request as observable user behavior; mark unknowns explicitly. Before editing, inspect `git status --short --branch`, `git branch --show-current`, `git rev-parse HEAD`, and how the assigned branch relates to local/remote `main`; preserve any existing changes.
2. Read this file and the narrow relevant references (for example `docs/ARCHITECTURE.md`, `docs/INTENT_AND_CUSTOM_PLANS.md`, `docs/KNOWLEDGE_LIBRARY.md`, `docs/GAME_ANALYSIS.md`, `docs/PERSONALIZATION.md`, `docs/PUZZLE_LIBRARY.md`, `docs/ROADMAP_TRAINING_LOOP.md`, `docs/LATENCY_AUDIT.md`, or `docs/TTS.md`).
3. Trace the actual frontend → API → planner/session/engine/knowledge/profile path. Search for the route and state owner; don't guess file names.
4. Identify authority and invariants before coding: current intent, `LearningState`, backend board, source-of-truth facts, model/engine prerequisites, and personal-data scope.
5. Choose the smallest compatible change. Reuse the existing data contract and telemetry; explain a proposed schema/API change before broadening it.
6. Add tests first or alongside the change for the reported failure and nearby regressions (topic preservation/switching, ambiguity, board verification, legality/engine requirement, or data scoping as applicable).
7. Implement without removing required analysis, validation, filtering, or fallback behavior. Keep UI/API changes consistent and same-origin.
8. Run focused tests, then relevant backend and frontend suites and compile checks where feasible. Record failures/interruption/environment gaps honestly; do not turn partial runs into “passed.”
9. Review the resulting source and user-visible behavior; verify no stale duplicated state, unsafe board path, accidental data leak, or unintended model/engine call remains.
10. Update focused documentation and feature-status notes when behavior changes — and for any major change, update `docs/CHESSAI_PROJECT_CONTEXT.md` in this same task, as Section 22 rule 14 requires. Distinguish implemented, planned, host-dependent, and unverified behavior.
11. Report changed files, behavior, exact validation run/results, and unresolved uncertainties, and state explicitly whether `docs/CHESSAI_PROJECT_CONTEXT.md` was updated. Do not claim tests, model loading, latency, or runtime state that were not observed.
12. For every substantial task, append/update its concise completion record in Section 24.2 in the same task. Record the actual commit(s), branch publish result, and `main` status only after verifying them; never leave a placeholder or imply integration from matching file paths.

## 24. Cross-Chat Handoff, Task Records and Non-Merging Publishing

### 24.1 Safe branch, publishing and integration workflow

Treat these as different states:

1. **Edited** means files are in the active working tree; they may be uncommitted and local only.
2. **Committed** means a commit exists on the active local task branch. Record its SHA; this does not publish it.
3. **Published** means that exact commit is verified on the matching remote task branch (for example, by comparing `git rev-parse HEAD` with `git ls-remote --heads origin <branch>`). This does not update `main`.
4. **Integrated into `main`** means the specific changes are confirmed present in the canonical `main` history/tree. Same-path edits, a task-branch push, a PR, or a local commit do not prove this.
5. **Documented** means this project context/task record names the files, actual checks, branch/commit and integration status. Documentation cannot substitute for a commit or publication.

At the start of every Arena task, inspect rather than infer the state. Safe read-only checks include:

```bash
git status --short --branch
git branch --show-current
git rev-parse HEAD
git rev-parse main origin/main
git branch -avv
git ls-remote --heads origin
```

If the tree is dirty or another branch contains task work, do not tidy it by resetting, cleaning, switching, rebasing or overwriting. Note the changes and identify overlap before editing. This checkout may be shallow, so use GitHub's commit/PR history when local ancestry is incomplete; do not infer that old task branches are lost or unpublished from a shallow `git log` alone.

**Arena publishing policy:** work and commit only on the branch assigned to the active session. After reviewing the diff and tests, stage only intended paths, commit on that branch, then publish only to that same named remote branch (for example, `git push -u origin arena/<the-verified-active-branch>`). Verify the remote ref and SHA after the push. Do not push this work to `main`, create merge commits, merge PRs, or open a PR as a substitute for integration. Do not delete task branches after publication. If the push fails, report it as unpublished and keep the local commit intact.

**Non-merging route to eventual `main` incorporation:** the active Arena session is constrained to its assigned task branch; in this environment the session is authorized to publish only to that branch. User policy also forbids branch/PR merges. Therefore this task cannot safely publish directly to `main` here. This is a session-policy limitation, not evidence that GitHub itself rejects a fast-forward push: `main` write permission was not probed, and no write/test to canonical `main` was attempted because it would be unauthorized and could disrupt the active workflow. Preserve the complete task branch remotely. Only after the active Arena task/session is finished and the user separately authorizes integration, a maintainer in a separate checkout/process may apply the reviewed, non-merge task commits to current `main` with `git cherry-pick` (in chronological order), run the relevant tests, and publish through that maintainer's authorized mainline process. If a conflict or unexpected `main` change appears, stop and coordinate; do not rewrite the task branch or force the integration. Cherry-picking creates new commit IDs, so record both original task-branch SHAs and resulting `main` SHAs. Until that process is verified, say **“published on task branch; not integrated into `main`.”** If separate integration is not authorized, leaving the remote task branch intact is the safe, recoverable alternative.

### 24.2 Task completion record (append one compact entry per substantial task)

Use this template in this section rather than creating a second changelog for each task. Keep the record factual and update it alongside the work; enter final SHA/publishing facts only after they are verified.

```text
Task and purpose:
Starting branch and commit; starting working-tree status:
Final active branch and task changeset commit(s):
Files created/modified/moved/deleted:
API, module, interface or data-contract changes:
Dependencies and prerequisite features:
Tests/checks actually run (environment and exact result):
Known issues, skipped work and remaining risks:
Publishing: local only / remote branch + verified SHA:
Integration: exact evidence on main, or “not integrated” + reason:
Next-chat instructions: what to read, preserve, verify, and avoid duplicating:
```

The final response must also give the final branch tip SHA; the task record lists the substantive task changeset(s). Keep the existing [CHESSAI_DESIGN_CONTEXT.md](CHESSAI_DESIGN_CONTEXT.md) as the selective commit-anchored architectural milestone history; do not copy the whole Git log into a new ledger.

### 24.3 Branch audit and workflow-setup task record (2026-10-17)

**Recent completed work already in `main` (history inspection only; no merge was performed in this task):**

| Completed task/milestone | Source/task commit SHA(s) | Important files/areas | Publishing/integration evidence |
|---|---|---|---|
| Training assessment core, Training UI, and end-to-end flow | `c415c4592dc8622322aaa7388aaf0cc6039d8735` (assessment), `878c148a0470238cb7dc2716b7c5573e2ddf4282` (UI), `eb7aaa150a09fd4d4f09ff7606310f7e1fcf76e8` (e2e) | `backend/app/assessment/`, `backend/app/training_api.py`, `frontend/training.js`, `backend/tests/test_training_assessment.py` | These refs are ancestors of `main`; remote `arena/01a0d715-chessai` compares `ahead_by=0` and is behind `main`. |
| Canonical project-state snapshot and design milestone record (PR #1) | `e7b7c571b71f661083e0b44d998418b57a9de88b` (state snapshot), `e79a31133c924ef9d2e7a47f3a6b9aa95198644c` (design context); merge commit `d0e28042f4491d37843933f17bd065922364c02b` | Project source/data/tests and `docs/CHESSAI_DESIGN_CONTEXT.md` / `docs/CHESSAI_PROJECT_CONTEXT.md` (100 PR files) | PR #1 is closed/merged; its merge is in current `main`. |
| Record migration-validation results (PR #2) | `df86fb7947336179126d807f0f74f2314d6d349d`; merge commit `cc719926a350dc5d018cc5f122c34a7aa99449a3` | `docs/CHESSAI_PROJECT_CONTEXT.md` | PR #2 is closed/merged; current `main` contains the validation record. |
| Record completed backend run, seed counts and Stockfish startup finding (PR #3) | `781e16e7c076601d8e1613fdfd6852979e2110c7`; merge commit `a4d80cc2ee7cb4cf7e7dfbb214672da7d0423b50` | `docs/CHESSAI_PROJECT_CONTEXT.md` | PR #3 is closed/merged; current `main` contains the findings. |
| Require same-task project-context updates (PR #4) | `be62877aec328a1bf972f903ead44dc54c2b6597`; merge commit `056d058ec82ba6b5cdea03dd4a48df8d90a309e2` | `docs/CHESSAI_PROJECT_CONTEXT.md` | PR #4 is closed/merged and is the current `main` tip at this audit's start. |

For earlier architectural milestones and design rationale, use [CHESSAI_DESIGN_CONTEXT.md](CHESSAI_DESIGN_CONTEXT.md). The rows above are selected recent task anchors, not a complete commit log.

- **Purpose:** establish a factual cross-chat handoff and preserve future work without merging or disturbing the active Arena branch.
- **Start:** active branch `arena/756ebf60-chessai` at `056d058ec82ba6b5cdea03dd4a48df8d90a309e2`; clean working tree. Local `main`, `origin/main` and active `HEAD` all pointed to that SHA. The clone is shallow. The active branch had no matching remote ref and no local commits ahead of `main` before this task.
- **Other refs:** GitHub reported remote heads `arena/01a0d715-chessai` at `eb7aaa150a09`, `arena/01a1041f-chessai` at `df86fb794733`, `arena/ff9247c3-chessai` at `781e16e7c076`, and `arena/2fbbd906-chessai` at `be62877aec32`. Each compared to `main` with `ahead_by=0`; PRs #1–#4 are closed/merged. No unpublished changes on those task heads were found by that comparison. The current `arena/756ebf60-chessai` branch was not yet on `origin` at audit start.
- **Changes in this task:** documentation only; this project context adds the feature/API/test index, known status and documentation drift, and no-merge publishing rules. No source code, APIs, runtime stores, or chess data were changed.
- **Final task branch:** `arena/756ebf60-chessai`. Substantive workflow/context commit: `de5ca45ac5254b4e412ea847690e1d912eeb52bd`. The exact final branch tip is independently verified and given in the completion report.
- **Validation/environment:** Python 3.11.2 and Node v22.22.3. `node --test frontend/tests/*.test.mjs` → **118 tests, 108 passed, 10 skipped, 0 failed**; AST syntax parse → **201 Python files**; JSON parse → **74 files**; `git diff --check` passed. `pytest`, `python-chess`, FastAPI and `httpx` are absent, so no backend pytest run was available and no dependencies were installed. No source-code tests, Stockfish search, or Qwen call were run.
- **Publishing/integration:** the substantive commit above was pushed to and verified on `origin/arena/756ebf60-chessai` at that SHA. At first-push verification, GitHub reported the task branch `ahead_by=1`, `behind_by=0` relative to `main`, with merge base `056d058ec82ba6b5cdea03dd4a48df8d90a309e2`; `main` was not changed. This task is **published on the task branch; not integrated into `main`**. No PR was opened and no merge was performed.
- **Next chat:** first read Sections 20–24 and [CHESSAI_DESIGN_CONTEXT.md](CHESSAI_DESIGN_CONTEXT.md), inspect the active branch/working tree, then follow the separately supplied navigation prompt. Do not begin the navigation redesign from this workflow task alone. Preserve this branch's documentation changes; do not duplicate existing board-move navigation as the unified app-navigation feature.

### 24.4 Project state and next-chat handoff

**Current feature and dependency map:** Section 20.1 points to source modules, API families, frontend entry points and representative tests. The core boundaries remain: Python-chess = chess rules/legal moves; backend session = live board authority; Stockfish = engine evaluation; Qwen = optional interpretation/explanations over supplied facts; learner stores/assessment = local evidence and personalization. Qwen, Stockfish, external game fetching and TTS providers are host-dependent, not guaranteed by the checked-in code.

**Requested future work (not yet implemented unless marked otherwise):** timed games against configurable bots; Decision Puzzles that grade explanations against verified chess analysis; deeper game-analysis → learner-model → personalized-practice connections; more verified opening puzzles; and engine timeout/resilience plus AI response-time improvements. Unified app navigation is implemented as Learn, Practice and Review (see Sections 20.1 and 24.5). The remaining building blocks and gaps are documented in Sections 19–21; do not rebuild functioning board, puzzle, Training, game-analysis, learner or routing systems.

**Contracts to preserve:** use same-origin relative `/api/...` routes; session board and move legality remain server-owned; puzzle/result and learner data stay scoped as implemented; do not promote personal game positions to shared knowledge; tests use the existing pytest/Node conventions; read `.env.example`, `config.py` and the focused subsystem doc before changing a dependency. Do not launch on port 8000; Section 17 says to confirm the run settings (last selected port 8080) first.

### 24.5 Unified app-navigation redesign task record (2026-10-17)

- **Purpose and behavior:** replace the three feature-name top-level tabs with one consistent learner-task navigation: Learn (Lessons), Practice (Puzzles and Training), and Review (Game Analysis). The existing view switcher and per-view state remain authoritative; the Puzzles dashboard's submodes stay nested under Practice, and board move-history navigation remains separate.
- **Starting state:** active branch `arena/756ebf60-chessai` was clean at `069ecdcdc7269a6c267ef49ac6467c869264b5ba`; its remote ref matched. Local/remote `main` remained at `056d058ec82ba6b5cdea03dd4a48df8d90a309e2`, with the task branch ahead by 3 and behind by 0. No existing work was overwritten.
- **Substantive task commit:** `1408fbfd8501d5baf77bc3c6976d5a32117977c2` (`Redesign primary app navigation`). The final active branch tip is reported in the completion report; this record is a follow-up documentation commit.
- **Files:** `frontend/index.html`, `frontend/app.js`, `frontend/styles.css`, new `frontend/tests/app-navigation.test.mjs`, `scripts/ui_e2e.mjs`, and this project context. No backend, API, chess-data, or dependency changes.
- **Interface and compatibility:** primary controls are grouped and labeled for Learn/Practice/Review; active-page semantics use `aria-current`. Existing view switching and state-preservation behavior are unchanged. The puzzle theme submode is labeled **By theme** to distinguish it from the top-level Practice destination. Existing Training and board-navigation controls remain distinct.
- **Validation:** Node v22.22.3; `node --test frontend/tests/*.test.mjs` → **121 tests, 111 passed, 10 skipped, 0 failed**. `node --check` passed for `frontend/app.js`, `frontend/tests/app-navigation.test.mjs`, and `scripts/ui_e2e.mjs`. A standard-library ID check passed for **83 HTML IDs** across **20 frontend scripts**; `git diff --check` passed. The live `scripts/ui_e2e.mjs` flow and backend tests were not run; no server was launched and no port was used.
- **Publishing and integration:** substantive commit `1408fbf…` was pushed to `origin/arena/756ebf60-chessai` and verified by `git ls-remote`. GitHub comparison reported the branch **ahead by 4, behind by 0**, based on `056d058ec82ba6b5cdea03dd4a48df8d90a309e2`; remote `main` remains unchanged. **Published on the task branch; not integrated into `main`**; no PR was opened and no merge was performed.
- **Next chat:** inspect the current branch and this navigation record. Preserve Learn/Practice/Review and the unchanged view-state contracts. Keep Decision Puzzles, timed configurable-bot games, and other remaining roadmap items planned unless separately authorized and implemented; do not fold them into navigation work. Do not confuse board move-history navigation with app navigation.

## 25. Glossary

| Term | Meaning in this codebase |
| --- | --- |
| **Session / `SessionManager`** | Backend-owned in-memory lesson conversation and authoritative live board for a session. |
| **`LearningState`** | Small structured lesson context for routing/coaching (`topic`, mode, objective, stage/progress, difficulty, relevant context, opaque `board_state_ref`); never a second board. |
| **`board_state_ref`** | Opaque pointer/identifier for the server's authoritative board/session; not a FEN or board snapshot. |
| **Semantic route / conversation intent** | Classification of what the learner wants to do now (greet, ask, switch, practice, clarify, etc.). |
| **Planner intent / `understand()`** | Resolution of the chess topic, side/variation, ambiguities, and learning plan after the request is understood. |
| **`IntentMemory`** | Reusable answers to keyed ambiguity questions, so the planner can apply a prior clarification or ask the learner to revisit it. |
| **`QuestionIntent`** | Local task classifier for a short question (definition, example, opening, personal coaching, position, or tutor path), used before choosing a knowledge source. |
| **`LessonContext`** | Structured lesson/board facts assembled for a teacher response; distinct from `LearningState` and not an independent board authority. |
| **`MoveFeedback`** | Facts-only verdict on a move: before/after FEN, SAN/UCI, category/loss, best move, evaluations, principal variations, notes, and search depth. |
| **Temporary question** | A short or unrelated question answered without replacing the active lesson; state is preserved. |
| **Topic switch** | A clear new learning topic that supersedes the active topic while the prior session may remain parked in the UI/process. |
| **Clarification** | A structured question/options response used when intent is ambiguous or confidence is too low; it should not mutate lesson state. |
| **Authoritative board** | The session's backend `python-chess` board; the source of legal moves, turn, and server-side position facts. |
| **Verified board / `VerifiedBoard`** | Frontend check that the local chess model and rendered board match the intended position before interaction. |
| **chess.js** | Vendored browser-side chess model used to parse FENs and simulate moves for UI verification; it is not the server's rules authority. |
| **cm-chessboard** | Vendored browser board renderer/interaction library used by this repo; Chessground is not used here. |
| **FEN** | Forsyth–Edwards Notation, a serialized chess position. Browser FEN is not sent to the semantic router as board truth. |
| **UCI** | Universal Chess Interface; engine protocol and common coordinate move format used by the backend. |
| **SAN** | Standard Algebraic Notation, human-readable move notation such as `Nf3` or `Qxf7+`. |
| **Stockfish** | UCI chess engine used for evaluations, best lines, move grading, and engine-backed verification. |
| **Centipawn / cp** | Evaluation unit; 100 centipawns is conventionally about one pawn of engine evaluation, not a guaranteed game result. |
| **PV / principal variation** | A candidate best line returned by an engine search. |
| **Knowledge Library** | Structured chess concepts/examples with source, replay, verification, and retrieval metadata. |
| **Candidate plan** | Proposed lesson/plan content that is not safe to show until validation passes or it is explicitly marked for review. |
| **Plan Library** | Reusable plans stored with verification/provenance metadata; global shared-content plans are kept separate from learner-personal plans and revalidated on reuse. |
| **Basic Explanation Library** | Curated multi-level chess explanations and misconception/key-idea text linked to knowledge/glossary data. |
| **Glossary** | Concise term definitions/aliases; a reference source, not the full tutoring system. |
| **Verified example** | A knowledge position/line that passed the repository's applicable schema, legality, engine, and explanation checks. |
| **Personal example** | Example derived from an individual learner/game; private/opt-in and not shared by default. |
| **Puzzle Library** | Standalone curated/verified positions served through `/api/puzzles` for selection and solving. |
| **Training mode** | Existing bot/position practice and assessment flow shown as a submode under the top-level Practice area, with its own API. |
| **Decision Puzzles** | Proposed future interactive decision-and-coaching feature; **planned, not implemented**. |
| **Learner profile** | Local skill/evidence/calibration state used for personalization; not an account or cloud identity. |
| **Weakness** | Recurring, evidence-backed learner pattern; different from a single mistake or puzzle need. |
| **Qwen teacher** | Optional OpenAI-compatible LLM client for routing/planning/explanations; not the source of chess legality or engine truth. |
| **Ollama** | One possible local model server implementing the compatible API; its model lifecycle/keep-alive is host configuration. |
| **TTFO / TTFT** | Time to first visible output / first token. Server stream TTFO differs from browser-paint latency. |
| **NDJSON** | Newline-delimited JSON used by the app-facing streaming endpoints. |
| **SSE** | Server-Sent Events; protocol consumed from the upstream OpenAI-compatible streaming model endpoint. |
