# ChessAI Design Context and Milestone History

This document complements [CHESSAI_PROJECT_CONTEXT.md](CHESSAI_PROJECT_CONTEXT.md): it explains the durable design boundaries and gives a selective, commit-anchored history of how the current product took shape. It is not an exhaustive changelog or a substitute for the owning code, tests, or focused subsystem documents. When they disagree, the current implementation and tests win.

## Product shape

ChessAI is a local-first, interactive chess tutor. It combines conversational lesson planning, authored and generated lessons, verified chess examples, board interaction, puzzles and bot-based Training, game analysis, and learner-specific practice. The web UI is static JavaScript served by the FastAPI application; Qwen-compatible inference and Stockfish are optional host services, not built-in guarantees.

The product's defining design choice is to combine flexible language interaction with deterministic chess truth. A model can help understand or explain a request, but it does not become the chess rules engine, board owner, or move-quality authority.

## Durable design boundaries

### 1. Separate language, chess truth, and presentation

- **Python-chess** parses positions and games, enforces legal moves, and supplies rules facts.
- **Stockfish** supplies evaluations, best-move comparisons, and chess-strength facts when needed. Required searches and verification are not optional just because a response could be faster without them.
- **Qwen or another compatible model** may classify a learner's intent, organize selected plans, or explain supplied facts. It must not invent legality, engine verdicts, or unsupported board state. Fallback and consistency checks are part of the design, not merely error handling.
- **The frontend** presents and interacts with API state; a rendered board is not the backend's source of truth.

See [ARCHITECTURE.md](ARCHITECTURE.md), [LATENCY_AUDIT.md](LATENCY_AUDIT.md), and the detailed authority notes in [CHESSAI_PROJECT_CONTEXT.md](CHESSAI_PROJECT_CONTEXT.md#7-board-and-position-authority).

### 2. Keep one authoritative live board

A backend session owns its live `python-chess` board. Structured `LearningState` can carry an opaque `board_state_ref`, but not a copied FEN/board. Browser-side `chess.js`, `VerifiedBoard`, `position.js`, `board-nav.js`, and `cm-chessboard` support parsing, safe interaction, rendering verification, and view history; they do not supersede server legality. A failed or stale position check stops the dependent teaching action instead of silently substituting a different position.

This also keeps move history distinct from game state. Lesson, puzzle, and review navigation is view-level history; it must not create a second mutable authority or change the server's lesson/puzzle result.

### 3. Understand the action before resolving the chess topic

Conversation routing and planner/topic interpretation are separate stages. The conversation layer decides whether the learner is greeting, asking a temporary question, continuing, switching topics, requesting practice, or needing clarification. The planner then resolves the chess subject, side, variation, and suitable lesson material. This avoids treating every chess noun as a new lesson and lets a short question preserve an active lesson.

`LearningState` is structured context, not a transcript-derived command. Temporary questions preserve lesson progress; a clear topic switch is explicit; ambiguity asks instead of guessing. Do not replace the semantic route and shared planner with an expanding keyword-to-action list.

### 4. Validate teaching material before trusting it

Authored lessons are data validated at load. Planner output and model-proposed content use the same schemas and are screened before presentation. The Knowledge Library tracks provenance and verification; eligible examples are replayed/checked with Python-chess and the applicable validators/engine checks. Basic explanations, the glossary, authored lessons, generated positions, and private game-derived examples are distinct sources with different contracts.

A model proposal is a candidate, not a verified lesson. A user's game position remains personal by default and is not silently promoted into shared teaching data. See [KNOWLEDGE_LIBRARY.md](KNOWLEDGE_LIBRARY.md) and [INTENT_AND_CUSTOM_PLANS.md](INTENT_AND_CUSTOM_PLANS.md).

### 5. Personalize from evidence without conflating flows

Game-analysis evidence, puzzle outcomes, Training assessments, lesson behavior, and profile calibration contribute related but distinct signals. Personalized recommendations should reuse the existing learner/assessment services and keep the selected player's game history scoped. A single mistake is not automatically a durable weakness.

Ordinary Puzzles and bot-based Training are separate product flows even though they share a tab and learner signals. Game analysis, library practice, lesson progression, and Training should not be collapsed into a parallel profile or state machine. See [PERSONALIZATION.md](PERSONALIZATION.md), [GAME_ANALYSIS.md](GAME_ANALYSIS.md), and [PUZZLE_LIBRARY.md](PUZZLE_LIBRARY.md).

### 6. Treat optional services and measurements honestly

The app can provide deterministic/local behavior for supported tasks when Qwen is absent. Engine-dependent claims still require Stockfish. A configured endpoint, warm-up request, or mock-model test does not prove that a real model is loaded or warm. Latency reports must name the measured path, host conditions, and whether timings came from a mock model, Stockfish, server stream, or browser-visible output.

## Milestone history

The rows below select architectural milestones from the repository's project history. Hashes are anchors for further inspection (`git show <hash>`); they are not a complete release log. Commit subjects describe recorded changes, not proof of an unrecorded production incident.

| Milestone | History anchors | Design significance |
| --- | --- | --- |
| MVP and interactive lessons | `ccdaadb` — MVP foundation; `97f3833` — session API and web UI | Established the chess system, engine/teacher split, lesson data, tests, session API, and browser loop. |
| Model-backed planning with constraints | `ef8bd65` — Qwen3 support; `b8b35b8` — verified learning plans; `e88089e` — no topic substitution | Added optional semantic/model assistance while keeping requested topics and verified lesson plans bounded. |
| Verified Knowledge Library | `af60950` — library core; `2103cea` — trusted sources; `877d1ca` — reproducible seed; `02f71b4` — verified facts in lessons | Made provenance, schemas, retrieval, replay, validation, and verified teaching facts first-class rather than relying on free-form model knowledge. |
| Game review and learner-specific practice | `51470fb` — Chess.com PGN import; `0c72fda` — Stockfish game analysis; `0010e05` — weakness-based plans; `b4cc450` — Game Analysis screen | Connected imported games to validated analysis, review cards, recurring evidence, and personalized training while keeping game-derived material scoped. |
| Clarification and custom-plan validation | `9c5de6c` — structured intent/clarification; `413ea95` — validated custom-plan pipeline; `5672ecc` — validation tests | Separated intent understanding from plan generation and placed legality, correctness, provenance, and learner checks around generated plans. |
| Learner model and explicit library flows | `2cbf9c4` — learner profile/skills; `6e96ae6` — game library; `2fb7e6c` — Puzzle Library | Added evidence-backed skills and calibration, selectable game sets, and library-first puzzle selection/adaptation. |
| Verified content and subject coverage | `d519ade` — verified opening trees; `f4c5272` / `4e066d5` — concept validators and library expansion | Expanded openings and tactical/endgame examples through validators and engine checks instead of treating raw imported positions as teachable by default. |
| Board navigation and Training assessment | `6af8f1a` — board move history/navigation; `c415c45` — Training assessment core; `878c148` — Training UI; `eb7aaa1` — end-to-end Training flow | Added reviewable board lines and a distinct bot-based Training loop with hidden ideas, reports, continuation, and profile evidence. |
| Current worktree snapshot integrated | `e7b7c57` — current-state integration snapshot | Captured the later working-tree state on top of the preceding project history, including conversation/session updates, basic explanations and knowledge data, verified frontend board/position modules, Training changes, and corresponding tests/docs. Inspect the commit diff and present files for exact scope. |

## How to use this document

- For the current file map, runtime assumptions, feature status, regression evidence, and agent workflow, start with [CHESSAI_PROJECT_CONTEXT.md](CHESSAI_PROJECT_CONTEXT.md).
- For the core teaching loop and planner/engine behavior, see [ARCHITECTURE.md](ARCHITECTURE.md).
- For subsystem contracts, read the focused documents linked above before changing the owning code and tests.
- Treat roadmap statements and historical counts as snapshots. Verify current status, data totals, host configuration, model availability, and engine availability before making claims about them.
