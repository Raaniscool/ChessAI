# Chess Knowledge Library

A verified, growing library of teaching examples: rules, tactics, checkmates,
openings, endgames and typical mistakes. The tutor retrieves examples from it
instead of asking Qwen to invent positions.

> **Qwen can propose chess knowledge, but it cannot declare that knowledge correct.**

| Authority | Decides |
|-----------|---------|
| python-chess (rules) | legality of positions and moves |
| Stockfish | evaluation, best moves, alternatives |
| Library | which examples are verified teaching material |
| Qwen | explanations (from verified facts), later: candidate examples |
| Tutor | lesson structure (demonstration, interactive, hints, practice) |

Lookup order: **verified library → deterministic rules → Stockfish → Qwen**.

## Status

| Part | State |
|------|-------|
| Schema, concept graph (62 concepts), validators, verification pipeline | done |
| Source importers: curated records, Lichess opening DB, Lichess puzzles | done |
| Seed library: 107 verified examples, reproducible build | done |
| Runtime tiers (generated / personal), review states, usage metadata | done (data model + library API) |
| Lesson API, chat/planner integration | next |
| Qwen candidates on a library miss (verified before saving) | after that |
| Admin dashboard | not planned yet (the review API/data model is ready for it) |

## Layout (`backend/app/knowledge/`)

| File | Role |
|------|------|
| `data/concepts.json` | the concept graph: parents, related, prerequisites, validator, engine profile |
| `data/examples/<category>/<source>.json` | the global library, **verified entries only** (written by the seed builder) |
| `data/seed_report.json` | the last seed build: counts, `needs_review` records, rejections with reasons |
| `schema.py` | entry schema, statuses, categories, presentation modes, parsing |
| `positions.py` | replaying a line: positions, SAN, move labels, material |
| `validators.py` | concept validators (registry): fork, pin, skewer, mates, stalemate, castling, mistakes... |
| `engine_check.py` | Stockfish profiles: tactic, mate, opening, mistake, principle, endgame_win, endgame_draw |
| `facts.py` | verified facts for Qwen + the check that an explanation doesn't contradict the board |
| `dedupe.py` | exact / near-duplicate detection |
| `pipeline.py` | `verify_candidate()`: the whole verification pipeline |
| `library.py` | loading, indexing, search, selection (variety, level, freshness, mode) |
| `store.py`, `usage.py` | runtime tiers in `DATA_DIR/knowledge/`, usage statistics |
| `teaching.py` | deterministic teaching text from validator facts |
| `sources/` | importers and their source data (kept separate from the teaching data) |

## Entry schema

`id, status, title, concept, concepts (extra), category, subcategory, difficulty (1-5),
description, start_fen, moves (SAN), final_fen, key_move, mistake_move, accepted,
prompt, hints, notes (per move), explanation, highlights, tags, related,
prerequisites, presentation_modes, variations, concept_params, teaching_purpose,
facts (validator output), source{}, verification{}`

`source`: `source_type, source_id, source_license, reference/url, import_date`.
`verification`: `status, pipeline_version, method, checked, stages, engine, alternatives, verified_at`.

Statuses: `candidate → verifying → verified | rejected | needs_review`, plus `deprecated`.
**Only `verified` entries are ever retrieved.** A reviewer may approve a
`needs_review` entry, but approval re-runs the rules and concept checks: broken
chess can't be approved.

## Verification pipeline

1. **FEN** parses, **position** is legal (python-chess status).
2. **Moves**: every move is legal in sequence.
3. **Structure**: schema, known concepts, key/mistake move in the line, notes on real moves.
4. **Concept**: every claimed concept's validator must find it on the board
   (a fork really attacks two pieces, a mate has no legal reply, a stalemate is not check...).
   Its findings become the entry's `facts`.
5. **Engine**: the concept's Stockfish profile. Engine state is reset for every
   analysis, so verification is reproducible.
   - tactic/mate: the key move and later learner moves must be good and must actually win/mate.
     A slightly better engine move does **not** reject an example; it's recorded as an alternative.
   - mistake: the mistake must lose ≥150 cp (80-150 → review); the punishment must be good.
   - principle (early queen, development...): the position must get ≥80 cp worse by the end.
   - opening: no move loses more than 120 cp (120-250 → review, e.g. gambits).
   - endgame_win: winning at the start, and no learner move throws the win away.
6. **Solution**: the key move and any `accepted` moves are good moves.
7. **Explanation**: pieces on named squares exist, SAN moves are legal, "X attacks Y" is true,
   the forking/pinning piece matches the verified tactic, mate/stalemate claims are true.
8. **Duplicates**: exact duplicates are rejected; near-duplicates need a teaching reason
   (different difficulty, variation or purpose).

Any fail → `rejected`; any uncertain (e.g. no engine) → `needs_review`; else `verified`.

## Sources (never Qwen)

| Source | License | Used for |
|--------|---------|----------|
| FIDE Laws of Chess (rules as facts) | facts | rules examples (`curated/basics.json`) |
| Standard patterns, classic traps, textbook endgames | public-domain facts, original text | `curated/{checkmates,endgames,mistakes}.json` |
| [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings) | CC0 | opening lines; the moves come from the database, max. 6 extra moves |
| Lichess puzzle database (via `planner/data/puzzles.json`) | CC0 | tactics/mates from real games, and "walked into a fork / hung a piece" mistakes |

## Rebuilding the seed library

After editing a curated record, a validator or a threshold:

```powershell
.\.venv\Scripts\python scripts\build_knowledge_seed.py
.\.venv\Scripts\python -m pytest backend/tests/ -q
```

The build takes about a minute and is deterministic: rebuilding without changes
produces identical files. Check `seed_report.json` for anything that landed in
`needs_review` or `rejected`. Fix the source record, or leave it out if Stockfish
disagrees with the idea.
