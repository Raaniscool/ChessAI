# AI Chess Tutor (ChessAI)

A chess-learning application where **the AI teaches through the board itself**: it demonstrates concepts on a real chessboard, gives the learner positions, validates their moves with a chess library, analyzes them with Stockfish, and has a local Qwen model explain the result — learning by doing, not by reading.

## Architecture (short version)

```
Frontend (cm-chessboard) ──move──► Application (FastAPI)
        ▲                              │
        └──────── board state ◄────────┤── validates every move/command (python-chess)
                                       ├── analyzes positions (Stockfish UCI)
                                       └── explains facts (Qwen, or offline fallback)
```

- **Chess system** — `python-chess` is the only authority on legality, FEN, and history.
- **Engine** — Stockfish 19 (WASM via Node by default; any UCI engine via `ENGINE_CMD`) decides *what happened*.
- **Teacher** — a local Qwen model (OpenAI-compatible endpoint) *explains what happened*, from structured engine facts only. A deterministic fallback teacher keeps the app honest and usable when Qwen isn't running.
- **Application** — owns the authoritative board state and makes lessons interactive. No AI output ever reaches the board unvalidated.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## "I want to learn ___" — personal learning plans

Type what you want to learn in the chat box (*"I want to learn the Sicilian"*, *"teach me knight forks"*, *"I want to get better at endgames"*). The tutor builds a plan of lessons and adds it to the sidebar ("Your lessons"). The plan contains only what you asked for, in steps: first the idea (a verified example, then one you finish yourself), then easier and harder practice, then a mixed review. Openings go ideas → verified lines → the whole line from memory. Foundations such as opening principles are suggested next to the plan, not added to it, and *"what should I play as Black against e4?"* gets one main defence to learn, with the other good answers offered. Built-in material such as the Italian Game lessons isn't advertised there — ask for it (*"I want to learn the Italian Game"*) and the tutor builds the lesson.

- **Verified examples first:** if the [Knowledge Library](docs/KNOWLEDGE_LIBRARY.md) covers the subject (*"Show me checkmates"*, *"Teach me forks"*, *"Give me an endgame lesson"*), the plan starts with a short lesson built from verified examples: watch one, find the key move in a slightly harder one, then solve one on your own. A 🧠 *Explain this example* button has the AI explain it from the example's verified facts. Otherwise the catalog planner below runs as before.
- Plans are built from a **verified topic catalog** (`backend/app/planner/data/topics.json`, 57 topics): 15 openings, opening principles, and 40+ tactics, checkmate patterns and endgames — e.g. *smothered mate*, *Anastasia's mate*, *Boden's mate*, *deflection*, *zwischenzug*, *x-ray*, *zugzwang*, *rook endgames*. Each opening becomes two lessons (watch + first moves, then the whole line from memory); each tactic/endgame topic becomes "learn the pattern" + "practice" lessons.
- **Puzzles come from real games**: `backend/app/planner/data/puzzles.json` holds 7 puzzles per theme from the [Lichess puzzle database](https://database.lichess.org/#puzzles) (public domain, CC0). `scripts/build_puzzle_library.py` picks the simplest ones and keeps a puzzle only if Stockfish confirms every one of your moves is the best move *and* clearly better than any alternative (on a final mating move every mate is accepted).
- **A plan only contains what you asked for.** If there are no verified lessons on the subject, the tutor says so and offers related topics as one-click suggestions — it never quietly swaps in something else.
- **Correctness:** every catalog line and position is audited by Stockfish in the test suite (`test_catalog_engine.py`). Add a topic to the JSON and it's audited automatically.
- **With Qwen connected** the planner also *organizes* the plan (order, title, why each unit matters) and can add an opening that isn't in the catalog. Qwen's move sequence is only used after python-chess confirms it's legal **and** Stockfish checks it move by move; the line is cut at the first mistake, and anything unverifiable is listed as "left out" instead of being taught.
- Without Qwen the planner works from the catalog alone (keyword + typo-tolerant matching).
- Plans are saved to `data/plans/` (gitignored) and survive restarts. Delete one with the ✕ on its card.

**Lesson flow:** demonstrations play by themselves (↻ *Watch again* replays them), then the button says **"Your turn — practise it →"**. Pieces can only be moved in an exercise; clicking the board anywhere else tells you what to press instead of silently doing nothing.

**🔊 Read aloud:** turn it on in the header (speed and voice next to it) and the tutor's messages are read to you; every message also has its own 🔊 button. Chess notation is spoken as words (*Nxg5* → "knight takes G5", *O-O* → "castles kingside"), and as each move or square is spoken it lights up on the board and in the text — including Black's moves written *...e6*, routes like *the h4-e1 diagonal* and alternatives like *e4/d4*. Point at any move or square in the text to light it up without reading aloud. Game Analysis is read aloud too (each mistake's explanation, and the AI's answer). With read-aloud on, a demonstration waits for each comment to be read before playing the next move. It uses the browser's built-in voices (Edge and Chrome on Windows work offline; Edge's "Natural" voices sound best) — nothing to install.

## 🔍 Game Analysis — learn from your own Chess.com games

Type your Chess.com username in the **Game Analysis** tab and press **Fetch my last 10 games**: your most recent games are downloaded from Chess.com's official public API and analyzed together (no copying PGNs; pasting still works as a fallback).
- **Checking the games:** python-chess validates every move.
- **Stockfish analysis:** Stockfish finds the moments that mattered, and the Knowledge Library's validators name them (missed forks, hung pieces, ignored threats, missed mates, opening habits…).
- **Review:** step through each moment: the position before, your move, Stockfish's move and line, and a plain-language explanation. The optional 🧠 AI explanation is built only from verified facts. When the text mentions a line of moves, the board plays it (read aloud, pointed at, or clicked); only single squares are highlighted.
- **Game history:** your last 10 (or 20, 30, 50, custom) games analyzed together. It shows which mistakes keep showing up ("♞ Missed knight fork: found in 4 of 10 games"), ranked by how often *and* how costly they were. A mistake from one game is never called a pattern, and recurring patterns need at least 10 games. Click a pattern to see every game and position; **Practice** turns it into a lesson plan: verified library examples first, then positions from your own games.

Your games stay in `data/games/` and never enter the shared library. Details: [docs/GAME_ANALYSIS.md](docs/GAME_ANALYSIS.md).

## Repository layout

```
backend/app/
  chess_system.py      # strict python-chess helpers (FEN, moves, board-command validation)
  config.py            # env-driven settings (engine cmd, Qwen endpoint, port)
  engine/              # Stockfish service + position-aware move classification
  teacher/             # Qwen client, prompts, deterministic fallback teacher
  lessons/             # lesson schema + loader + JSON course data
  lessons/data/italian_game/   # first course (structured lesson JSON)
  planner/             # "I want to learn ___": catalog, plan builder, lesson generator, storage
  planner/data/topics.json     # verified topic catalog (openings, tactics, endgames, strategy)
  planner/data/puzzles.json    # Stockfish-verified Lichess puzzles per theme (built by scripts/build_puzzle_library.py)
  games/               # PGN import (python-chess validation), platform-neutral GameRecord, importers/chesscom.py, storage
  analysis/            # game analyzer (Stockfish), motif + habit detection, recurring weaknesses, review cards, training plans
  game_api.py          # /api/games endpoints
backend/tests/         # pytest suite (chess, classification, lessons, engine, teacher)
frontend/              # web UI (vendored cm-chessboard + chess.mjs, no CDNs)
frontend/speech.js     # read-aloud: notation → words, move/square highlighting (tests: frontend/tests)
frontend/analysis.js   # Game Analysis screen (game-format.js, history-view.js: pure display helpers)
scripts/ui_e2e.mjs     # browser-level UI smoke test (jsdom) against a running server
docs/ARCHITECTURE.md, docs/KNOWLEDGE_LIBRARY.md, docs/GAME_ANALYSIS.md
requirements.txt
package.json           # Stockfish WASM engine dependency
```

## Quick start

```bash
# 1. Python deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Engine (Stockfish WASM over Node)
npm install

# 3. Tests
.venv/bin/python -m pytest backend/tests/ -q

# 4. Run the server (API + web UI)
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
# 1. Python deps (venv scripts live in Scripts\ on Windows)
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2. Engine (Stockfish WASM over Node — needs Node.js installed)
npm install

# 3. Tests
.\.venv\Scripts\python -m pytest backend/tests/ -q

# 4. Run the server
.\.venv\Scripts\uvicorn backend.app.main:app --port 8000
```

Then open http://localhost:8000
</details>

### Connecting your local Qwen

The teacher speaks the OpenAI protocol, so Ollama, LM Studio, vLLM, or llama.cpp all work. The app only uses Qwen when `QWEN_MODEL` is set — otherwise it runs the built-in fallback teacher (engine facts only — it never invents analysis).

**Windows / PowerShell (Ollama):**

```powershell
ollama pull qwen3:4b-instruct                 # recommended: answers directly, no thinking phase
Set-Content .env "QWEN_MODEL=qwen3:4b-instruct"
.\.venv\Scripts\python -m backend.app.check_qwen   # diagnoses the connection + speed step by step
.\.venv\Scripts\uvicorn backend.app.main:app --port 8000
```

The **`.env` file** in the project folder is git-ignored and read at every start (see `.env.example`), so the setting survives new windows. Variables set in the shell (`$env:QWEN_MODEL = "..."`) override it.

> **Use `qwen3:4b-instruct`, not `qwen3:4b`.** Ollama's `qwen3:4b` is now *Qwen3-4B-Thinking-2507*: it
> reasons silently before every answer (tens of seconds on a CPU) and ignores `/no_think`, so the
> tutor feels frozen. `check_qwen` and the server log both detect this and say so.

**macOS / Linux:**

```bash
echo "QWEN_MODEL=qwen3:4b-instruct" > .env
.venv/bin/python -m backend.app.check_qwen
```

| Variable | Default | Notes |
|---|---|---|
| `QWEN_MODEL` | *(empty → offline fallback)* | exact model name from `ollama list` |
| `QWEN_BASE_URL` | `http://localhost:11434/v1` | LM Studio: `http://localhost:1234/v1` |
| `QWEN_API_KEY` | `ollama` | ignored by local servers |
| `QWEN_TIMEOUT` | `120` | seconds; first reply is slow while the model loads |
| `QWEN_THINKING` | `auto` | `auto` = sends `/no_think` to hybrid Qwen3 builds (not to `-instruct`, which never thinks, or `-thinking`, which ignores it). `on` / `off` to force |
| `QWEN_MAX_TOKENS` | `300` | cap on explanation length (shorter = faster on local hardware) |
| `QWEN_PLANNER` | `auto` | `auto` = ask Qwen only when the catalog can't answer (instant plans), `always`, `never` |
| `QWEN_WARMUP` | `1` | load the model when the server starts so the first reply isn't the slowest |

Qwen3 `<think>…</think>` reasoning is always stripped before it reaches the student (also mid-stream).

#### Speed with a local model

The app never makes you wait for the language model to see a result:

- **Move feedback is instant**: Stockfish's verdict plus a built-in explanation appear right away (~0.1 s), then Qwen's explanation **streams in word by word** (`POST /api/sessions/{id}/explain`, NDJSON events). Chat replies stream the same way.
- **Small prompts**: on a CPU the model must read the whole prompt before its first word, so move prompts carry only compact engine facts (no FENs/JSON, ~165 tokens), and the fixed system prompt is pre-loaded at startup so Ollama reuses it from its prompt cache.
- **Thinking is switched off** on Ollama (`reasoning_effort: "none"`), so models with a thinking mode answer directly.
- **Plans for known topics don't use Qwen at all** (`QWEN_PLANNER=auto`), so they're instant.
- `python -m backend.app.check_qwen` runs a **speed test** (time to first word, tokens/s) and tells you what to change. Typical fixes, all free and unlimited:
  - Keep the model loaded: `setx OLLAMA_KEEP_ALIVE "2h"`, then restart Ollama (otherwise it unloads after 5 idle minutes and the next reply has to reload it).
  - Check `ollama ps`: `100% GPU` is fast; `CPU` is slow.
  - Make sure it isn't a thinking model (see above); `check_qwen` flags it.
  - Still slow on a CPU? Try a smaller model: `ollama pull qwen3:1.7b` (about 2× faster than 4b), then re-run `check_qwen`. The chess facts come from Stockfish either way. The status bar shows `teacher: qwen (model)` when connected.

## Status

**Phase 1 — MVP: core loop complete.** ✅ chess system, engine integration + position-aware move classification, teacher layer (Qwen + facts-only fallback), lesson schema/loader, Italian Game lesson 1, session API, web UI (board + demonstrations + exercises + hints + chat), test suite.

**Learning plans:** ✅ "I want to learn ___" planner (catalog + optional Qwen organization), verified topic catalog with Stockfish audit, generated lessons, persistent plans. Streaming AI text + instant move verdicts + speed diagnostics. Themed puzzle lessons (40+ tactics, mating patterns and endgames). Verified Knowledge Library (268 examples) wired into plans, lessons, chat and the AI teacher. Test suite: **653 passing** (+ `scripts/ui_e2e.mjs` UI smoke test).

**Game analysis:** ✅ Chess.com PGN import, Stockfish game analysis with verified motifs, game history analysis across your last N games (tiered, scored patterns), interactive review, and personalized training plans ([docs/GAME_ANALYSIS.md](docs/GAME_ANALYSIS.md)). Chess.com games are fetched by username. Plans contain only what you asked for, in stages. Personalized, Stockfish-verified puzzles for your recurring weaknesses, and a fallback that never leaves a lesson request empty. Test suite: **1034 passing** (+ 34 frontend). The original proposal is in [docs/PERSONALIZATION.md](docs/PERSONALIZATION.md).

**Understanding requests + custom plans:** ✅ Ambiguous requests ("knight and bishop endgames", "the Philidor", "the Sicilian as White", …) get a short question with genuinely different choices plus "Something else"; answers are remembered, and clear requests are never questioned. When the library has no ready-made plan, a custom plan is built from verified content (new positions are generated and checked by Stockfish when needed), validated for legality, engine correctness, teaching order, topic consistency, personalization and duplication, and only then shown; verified plans are stored with provenance (shared, or kept with the learner when personalized). See [docs/INTENT_AND_CUSTOM_PLANS.md](docs/INTENT_AND_CUSTOM_PLANS.md). Test suite: **1161 passing** (+ 38 frontend, 88 UI e2e checks).

The proven MVP loop:

```
User chooses lesson → AI teaches concept → board demonstrates it → user gets a
position → user moves → python-chess validates → Stockfish classifies → Qwen
explains → next teaching position → lesson completion ✓
```
