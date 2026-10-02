# Training loop roadmap: Play → Analyze → Diagnose → Teach → Practice → Improve

This maps the product-expansion request (30 points) onto what already exists and lists the
phases that close the gaps. Each phase is small enough to review, keeps the app working, and
comes with tests. Rules that hold everywhere:

- python-chess decides legality, Stockfish decides chess facts, and Qwen interprets and
  explains. Qwen never declares content correct.
- Only `verified` content is trusted, and nothing generated becomes trusted without the
  full pipeline.
- Learner data stays separate from global content and never enters the global library.
- The free version is a complete training loop. Payments are **not** implemented; tiers are
  a configuration value.

## Status by request item

| # | Item | Existing | Gap → phase |
|---|---|---|---|
| 1 | Onboarding | `coach.js` onboarding (experience, goals, style, optional username) | Username first and prominent, with a "no Chess.com account" path kept → P1 |
| 2 | Retrieve 100 games, cache, choose, filter | `chesscom_api.fetch_recent_games` (max 100), game store caches games + analyses | Default 100, game picker with filters and checkboxes, analyze the selection → **P1** |
| 3 | Analysis limits | none | 25 first games, then 10/day (free) or 20/day (paid); counted per new game; visible → **P1** |
| 4 | Per-game analysis evidence | `analysis/analyzer.py` (Stockfish, classification, motifs, facts) | Moment records already hold FEN/SAN/UCI/evals/concept/severity. Add `material_change` to evidence → P2 |
| 5 | Recurring weaknesses | `analysis/history.py` (frequency × severity, thresholds by sample size: recurring ≥3 games and ≥15%, occasional 2, one-time 1) | More strategic/endgame detectors (bad exchanges, passive pieces, conversion) → P5 |
| 6 | Learner profile | `learner/profile.py` (skills, weaknesses, rating, trends) | Profile page with per-skill state and improvement arrows → P5 |
| 7–8 | Puzzle Library | Lichess puzzles live inside the Knowledge Library as `tactics` | Separate `puzzles/` package: puzzle index with type/skill/solution/uniqueness metadata, its own attempt stats (time, hints, success), import pipeline → **P2** |
| 9–10 | Personalized selection | `personal_puzzles.unseen_for`, `learner/adapt.py` | Scored selection (weakness, rating, recent success, time, hints, recency, repetition) → P2 |
| 11–12 | Strict custom puzzle generation | `knowledge/generation` (constructors + verification pipeline), `material_positions.py` | Qwen puzzle spec → generator; uniqueness/alternatives, personalization and difficulty validators → P3 |
| 13 | Puzzles from actual mistakes | personal puzzles target the weakness concept | Link each puzzle to the source game/move it addresses (transfer, never a copy) → P3 |
| 14 | 5-level progression per weakness | single level | Identify → threat → prevent → respond → realistic → P3 |
| 15 | Libraries communicate | training plans combine library concepts + puzzles | Concept explanation + puzzle set in one training session → P3 |
| 16 | Training page | history report + training/puzzle buttons | "Your Training" card: main weakness (found in X of Y games), 5 puzzles, other weaknesses, recommended lesson → **P1** |
| 17 | Continuous loop | re-analysis updates the profile | "New games since last analysis" prompt, adaptation summary → P5 |
| 18–20 | Deep Knowledge Library | 341 verified entries (10 openings) | Opening trees from the Lichess opening database (CC0), verified move by move, importance-scaled targets → P4 |
| 21 | Verified pipeline for everything | `knowledge/pipeline.py` | Reused by P2–P4 |
| 22–24 | Qwen role, exact intent, request satisfaction | done for lessons (`planner/intent`, `custom/satisfy.py`, `request_satisfied`) | Same request check for puzzle sets (concept + weakness) → P3 |
| 25 | Debug visibility | plan debug panel (`?debug=1`) | Puzzle debug: weakness, source game/move, library match, generation, validation → P3 |
| 26 | Performance | earlier latency pass | Audit of the new paths after P3 → P5 |
| 27 | TTS | modular providers, voices, speed, auto-read, categories | keep |
| 28 | Premium | none | Tier setting only (no payments) → P1 |

## Phases

- **P1: game library and analysis limits. Done.** Fetch up to 100, a picker with filters, "Analyze
  last 25", analyze a chosen set, quota accounting and display, and the "Your Training" card.
- **P2: Puzzle Library.** A separate package and index, puzzle metadata, attempt statistics,
  scored personalized selection, and verified imports.
- **P3: personalized puzzle generation.** Spec → generator → strict verification, the 5-level
  progression, links to source mistakes, and puzzle debug.
- **P4: deep openings.** Branching opening trees, typical plans and mistakes, and lessons that
  select a slice.
- **P5: profile page and continuous loop.** Skill states, trends, more detectors, and a
  latency audit.
