# Puzzle Library

Practice positions, optimized for training, with per-puzzle stats and a "most useful now"
selection. Code: `backend/app/puzzles/`.

## What a puzzle is

A puzzle is derived from a **verified** Knowledge Library entry that has a key move. There
is no second store and nothing is decided by Qwen. Global, generated and personal entries
all qualify. Personal entries copied from the learner's own games never appear as puzzles
for them.

| Field | Where it comes from |
|---|---|
| `id` | the Knowledge Library entry id |
| `type` | `tactic`, `checkmate`, `calculation` (3+ learner moves), `defense` (stop a threat / save a piece), `endgame`, `opening`, `mistake_correction` |
| `concept`, `concepts`, `required_skill` | entry concepts + related concepts |
| `difficulty` (1–5), `rating` | entry label; `knowledge.difficulty.puzzle_rating` |
| `fen`, `side_to_move` | the board at the key move, replayed with python-chess |
| `solution` | SAN from the key move on (learner moves and the replies) |
| `accepted_first` | the key move + every accepted alternative |
| `uniqueness` | `unique`: Stockfish found no equally good alternative at any learner move. `multiple`: it did, and those moves are accepted. `unchecked`: no engine record |
| `main_idea`, `tags`, `source` (type, id, licence, url) | entry |
| `verification_state`, `tier`, `weakness` | entry status; global/generated/personal; the weakness a personal puzzle was made for |

The index (`PuzzleLibrary`) rebuilds itself when the Knowledge Library changes (new
generated/personal entries, status changes). That takes ~0.2 s for ~320 puzzles, then
it's cached.

## Stats per puzzle

`knowledge/usage.py` (the existing usage tracker) records each finished puzzle:
`record_resolved(id, solved, first_try, hints, seconds, revealed)`. `puzzle_stats(id)`
returns:

- attempts (whole puzzles; the per-move `attempts` counter stays separate)
- success rate and first-try rate
- average seconds (capped at 10 minutes per attempt, so an idle tab doesn't skew it)
- hints used
- seen before
- last result (`solved_first_try` / `solved` / `failed` / `revealed`) and when

Lessons record this automatically. The clock starts when the example's first exercise step
is shown and stops when its last move is solved or revealed.

## Selection (`puzzles/select.py`)

Deterministic, so every puzzle has a reason the learner can read:

1. **Relevance:**
   - the concept or a sub-concept (1.0)
   - a personal puzzle made for this weakness (1.0)
   - a concept the generator trains for it, e.g. walked into forks → knight forks (0.95)
   - a related concept (0.5)
   - ×0.7 when it's only a secondary concept of the puzzle

   Below 0.5 is never used.
2. **Level:** the learner's rating for the concept (`learner.views.target_rating`,
   "practice"). Recent results on these puzzles nudge it:
   - missed half → −100
   - slow (>150 s average) → −50
   - 80% clean first-try solves → +80

   The set spans target −150 … +150, one slot per puzzle, so it runs **easy → hard**. A
   puzzle more than 600 points from its slot is never used.
3. **Novelty / spaced retry:**
   - New puzzles come first.
   - A puzzle missed at least a day ago comes back as a retry ("Retry: you missed this
     4 days ago").
   - **Never repeated:** puzzles shown in the last day, solved in the last 3 days, or
     missed earlier the same day.
4. **Quality:** a single clear solution ranks highest, then several accepted moves, then
   entries with no engine record.
5. **Variety:** never the same position twice; a small penalty for repeating a sub-concept.

`slot score = relevance × (0.50·fit + 0.35·novelty + 0.15·quality) − variety`, with
`fit = exp(−((rating − slot)/250)²)`.

When too few puzzles qualify, the selection reports a **shortfall**. It never pads the
set with unrelated filler.

## Targeted training for a weakness ("Start training")

`POST /api/games/puzzles {key, count}` (Your Training → Start training, and "New puzzles
for this"):

1. **Library first.** Pick the best verified puzzles for the weakness. The learner's own
   positions (the weakness's evidence boards) are excluded.
2. **Generation only for the shortfall.** The generator runs with full python-chess and
   Stockfish verification (personal tier). Without an engine, the library puzzles are
   still served.
3. Order everything easy → hard and build one lesson:
   - The intro says what was found and where ("in 6 of your 25 analyzed games").
   - Each puzzle shows "Why this puzzle: …".
   - The plan card lists the reasons.
4. `fresh: true` asks for newly generated positions only.

The plan carries a **debug** block, shown with `?debug=1`:

- USER WEAKNESS
- SOURCE (game, move, severity)
- LIBRARY MATCH (candidates, chosen, target rating, per-puzzle match and score)
- CUSTOM GENERATION (needed, accepted, candidates, rejected, status)
- PUZZLE VALIDATION (per puzzle: status, uniqueness, accepted moves, length, rating)

## API

| Endpoint | Returns |
|---|---|
| `GET /api/puzzles` | counts by type, tier and uniqueness |
| `GET /api/puzzles/select?concept=&count=5` | the selection for a concept: puzzles with `reasons`, `stats`, `slot_rating`, `match`, plus `target_rating`, `shortfall` |
| `GET /api/puzzles/{id}` | one puzzle + the learner's stats |

## Adding puzzles

New puzzles enter through the existing Knowledge Library pipeline. A puzzle becomes
`verified` only after:

- source and licence checks
- python-chess legality
- Stockfish verification of the key move and alternatives
- concept and duplicate checks

Only then does it appear here. Bulk-dumping database puzzles is not supported, by design.
