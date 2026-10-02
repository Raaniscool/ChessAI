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
| `difficulty` (1–5), `rating` | from the puzzle's decisions (see *Puzzle profile* below) |
| `fen`, `side_to_move` | the board at the key move, replayed with python-chess |
| `solution` | SAN from the key move up to where the objective is met (trimmed; see below) |
| `steps` | per learner move: `kind` (critical / forced / open), accepted and "good" moves, the opponent's reply |
| `critical_moves`, `critical_decision_points`, `forced_moves`, `meaningful_moves` | the real decisions (SAN, 1-based learner move numbers), the forced/obvious moves, how many decisions |
| `primary_concept`, `objective`, `expected_solution_length` | the concept; `mate` / `material` / `defense` / `idea`; learner moves in the trimmed solution |
| `accepted_first` | the key move + every accepted alternative |
| `uniqueness` | `unique`: Stockfish found no equally good alternative at any learner move. `multiple`: it did, and those moves are accepted. `unchecked`: no engine record |
| `main_idea`, `tags`, `source` (type, id, licence, url) | entry |
| `verification_state`, `tier`, `weakness` | entry status; global/generated/personal; the weakness a personal puzzle was made for |

The index (`PuzzleLibrary`) rebuilds itself when the Knowledge Library changes (new
generated/personal entries, status changes). That takes ~0.2 s for ~320 puzzles, then
it's cached.

## Puzzle profile (`puzzles/profile.py`)

Puzzles work like Lichess puzzles: find the important idea, see the opponent's reply, and
stop once the idea has worked. Nothing is lengthened, and there is no fixed move count.

**Each learner move** is classified from Stockfish's top 5 moves (multipv 5, depth 12):

| Kind | When |
|---|---|
| `critical` | a clear best move (the next-best move is ≥150 cp worse), or a checkmate |
| `forced` | the only legal move, or an obvious follow-up after the idea: taking the target, escaping a check with ≤3 moves, a recapture. A move that gives material away is never obvious |
| `open` | another move is about as good (within 120 cp, or a mate that's just as fast) |

- The verified key move is always the decision; only follow-ups can be forced.
- Any checkmate solves a puzzle, so other mating moves are accepted.
- Moves within 150 cp, or a slower mate, are kept as "good": the solver says "good, but there's
  a stronger move" and lets you try again without a penalty.
- Without engine data, python-chess heuristics classify the moves, and moves after the
  objective never extend the puzzle.

**Where it ends:**

| Objective | Ends |
|---|---|
| `mate` | at mate. Mate-family puzzles only, so mate is what's being tested |
| `material` | at the first learner move after which the material gain is secured. A fork ends fork → reply → take the target |
| `defense` | right after the move that meets the threat |
| `idea` | at the last decision |

A later critical decision always stays in the puzzle. After trimming, 4 of the 302 library
lines are shorter.

**Difficulty** comes from the decisions, never from the move count:

- Each decision is rated from:
  - quiet move vs check/capture
  - sacrifice
  - backward move
  - free capture
  - number of plausible candidates (checks, captures, and engine moves within 300 cp)
  - how far ahead the payoff is
  - how crowded the position is
- The puzzle rating is the hardest decision plus 30% of the others. Forced moves and filler
  add nothing.
- The learner side of this (results, time, hints, whether the critical move was found first)
  moves the learner's level through `learner.record_attempt` and the selection level nudge.

**Engine data:**

- Library puzzles: precomputed in `puzzles/data/engine_profiles.json`
  (`python scripts/build_puzzle_profiles.py`, about 1 minute; it only updates missing or stale
  entries, matched by a line signature).
- Generated puzzles: stored at generation time in `DATA_DIR/puzzles/engine_profiles.json`.

**Not served:**

- Rule drills (`basics`): they're lessons, not puzzles.
- Puzzles whose first move isn't a clear decision (`clear_start: false`; currently 3).

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

## Puzzles tab

The Puzzles tab is a top-level tab next to Lessons and Game Analysis. It works without any
analysis or lesson.

**Personalized** (`GET /api/puzzles/dashboard`, `puzzles/dashboard.py`). Weakness cards with
their evidence:

- **from games:** recurring/occasional patterns in the learner profile, e.g. "Missed knight
  fork: 6 times in 4 of your last 10 analyzed games".
- **from puzzles:** "You solved 1 of your last 4 pin puzzles" (at least 3 recent attempts with an
  average score below 0.5).

How cards are ranked:

- Priority = share of games × tier weight (recurring 1, occasional 0.6) × a factor from recent
  puzzle scores. All recent puzzles failed gives ×1.35; all clean gives ×0.55 and the card reads
  "improving". Every result changes what's recommended next.
- The top card is the profile box (main weakness, evidence, "5 puzzles on …, easy to hard").

**Practice.** Themes are concepts from the concept graph: Forks, Pins, Skewers, Discovered
attacks, Checkmates, Hanging pieces, Endgames and others. A theme is listed only if the library
has at least 3 puzzles for it.

**Sets** (`POST /api/puzzles/set`):

- Selection is `select.py`: level fit, novelty, no repeats.
- Personalized sets use the library first. When fewer than 3 match, generation runs through the
  full pipeline (at most 3 puzzles), and only `verified` results with a clear first move are
  served.
- Positions from the learner's games are excluded.
- Practice themes serve puzzles *about* the theme (exact or "trains" matches). Puzzles that only
  use the idea (a smothered mate that relies on a pin) come in only when those run out.

**Skill targeting** (`puzzles/skill.py`; also used by "Start training"). A specific weakness is
served only by puzzles of exactly that skill:

| Match | Fills a weakness slot? |
|---|---|
| the weakness concept itself, the generator's "trains" mapping, a personal puzzle built for it | yes |
| a broader concept ("fork") whose **verified facts** prove the skill (the forking piece is a knight) | yes |
| a related concept, a puzzle that only also uses the idea, a broader concept without proof, a label the facts contradict | no (counted as `partial_not_used` in the debug block) |

The shortfall goes to constrained generation for the exact concept, never to a generic stand-in.

**Solver** (`frontend/puzzle-solver.js` rules, `frontend/puzzles.js` UI):

- The header shows the side to move, the objective ("Mate in 2", "Win material"), the concept,
  the difficulty and progress (2/5).
- The solution move animates the opponent's reply.
- A wrong move gets ✗ and is taken back. The puzzle counts as failed, and you can still finish
  it or retry.
- Hints: 1 = the idea, 2 = which piece. Show solution plays the rest of the line.
- After a solve or fail it shows the solution line, a short verified explanation and the source.
- Results go to `POST /api/puzzles/{id}/result`, which updates the per-puzzle stats and the
  learner model.
- Each tab keeps its state: a half-solved puzzle is still there after visiting Lessons or
  Analysis.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/puzzles/dashboard` | `personalized` (cards, main, profile box), `practice` themes with counts |
| `POST /api/puzzles/set {mode, concept?, weakness?, count}` | solver-ready puzzles, easy → hard (+ `debug` for weakness sets) |
| `POST /api/puzzles/{id}/result {solved, first_try, critical_first_try, mistakes, hints, seconds, revealed}` | records the outcome; returns the concept rating change and stats |
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
