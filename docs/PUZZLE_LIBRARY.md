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
2. **Level:** the calibrated target (see [Difficulty calibration](#difficulty-calibration)).
   The set spans target −100 … +150, one slot per puzzle, so it runs **warm-up → moderate →
   harder → harder variation → challenge**. Puzzles below the learner's floor (target −250)
   are left out while enough others exist. A puzzle more than 600 points from its slot is never
   used.

   Callers that pass no calibration (none in the app today) keep the old rule: the concept
   rating (`learner.views.target_rating`, "practice") nudged by recent results (missed half
   −100, slow −50, 80% clean +80), spanning target −150 … +150.
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
- Personalized sets are built by `puzzles/sets.py` (below). Practice sets are the selection,
  easy → hard.
- Library copies of positions from the learner's games are excluded; the learner's own moment
  appears only as the labelled "Your game" item.
- Practice themes serve puzzles *about* the theme (exact or "trains" matches). Puzzles that only
  use the idea (a smothered mate that relies on a pin) come in only when those run out.

**Personalized set composition** (`puzzles/sets.py`, 5 puzzles):

| # | Role | Source | Notes |
|---|---|---|---|
| 1 | **Your game** | A: the learner's own mistake (`puzzles/from_game.py`) | re-verified by Stockfish, see below |
| 2–4 | **Same pattern / Easier / Harder** | B: library puzzles of the exact skill; C: generation for the shortfall | role = rating vs the learner's target (±75) |
| 5 | **Defend** | library defence puzzles, else one generated "stop the threat" puzzle | only for weaknesses the learner *walked into* (forks, hung pieces) |

Each item carries one line for the solver, built from the evidence (no AI text), e.g.
"Missed knight fork — you missed this 4 times in 3 recent games (a step harder)." or
"Your own game vs alice — you played 11.Kd2 here. Find what you missed."

**Your game** (`puzzles/from_game.py`). The weakness evidence holds the position before the
learner's move, the move played and the engine's best move. Before it's served:

1. python-chess: legal position, the learner to move, the played move legal.
2. Stockfish, fresh (depth 14, multipv 5): the played move must still be ≥150cp worse than the
   best; at most 3 moves may solve it (all accepted) and only if every other candidate is
   clearly worse — otherwise it's rejected as "several moves are about as good".
3. `puzzles/profile.py` with that engine data: critical/forced moves, the objective (the line is
   trimmed there), the rating. A first move that is "open" is rejected.

Verdicts are cached in `DATA_DIR/puzzles/game_puzzles.json` (accepted and rejected), so each
moment costs Stockfish time once. Game puzzles are private: source type `user_game`, never
in Practice, never written to the Knowledge Library. The same novelty rules apply (not shown
today, not solved in the last 3 days, a miss returns the next day), and a repeated mistake in
another game with the same position isn't served as "new". Results are recorded like any
puzzle and feed the learner model.

**Progression** (`puzzles/progression.py`). Each puzzle's critical decision puts it in a
recognition stage:

| Stage | Key move |
|---|---|
| spot | a check or capture with fewer than 4 plausible candidates |
| choose | 4–6 plausible candidates, only one works |
| deep | a quiet move, 7+ candidates, or more than one real decision |

Focus = the first stage the learner hasn't mastered (≥3 puzzles, ≥75% first try). Selection
gets +0.12 for the focus stage, +0.04 for the next one and −0.06 for mastered stages below. So
someone who solves easy knight forks cleanly is moved to forks with several candidates, not
just to higher-rated easy forks; missing those keeps the focus there. Weakness cards show
"Next: Choose among candidates — You solve the ones where the key move is a check or capture
(3/3 first try) …"; the ladder is in the debug block.

**Skill targeting** (`puzzles/skill.py`; also used by "Start training"). A specific weakness is
served only by puzzles of exactly that skill:

| Match | Fills a weakness slot? |
|---|---|
| the weakness concept itself, the generator's "trains" mapping, a personal puzzle built for it | yes |
| a broader concept ("fork") whose **verified facts** prove the skill (the forking piece is a knight) | yes |
| a related concept, a puzzle that only also uses the idea, a broader concept without proof, a label the facts contradict | no (counted as `partial_not_used` in the debug block) |

The shortfall goes to constrained generation for the exact concept, never to a generic stand-in.

**Solver** (`frontend/puzzle-solver.js` rules, `frontend/puzzles.js` UI):

- The header shows the side to move, the objective ("Mate in 2", "Win material"), the role
  (Your game / Same pattern / Easier / Harder / Defend), the concept, the difficulty, progress
  ("Puzzle 7") and one "Why this puzzle" line.
- **Mixed practice is the exception** (`MIXED_THEMES` in `puzzles/dashboard.py`, the "Mixed
  tactics" theme). Its point is to recognise the idea yourself, so before a puzzle is over the
  solver shows only the side to move, "Find the best move", the difficulty and progress. The set
  list shows "Puzzle", and the heading is "Mixed practice". The concept, the real objective and
  the named reason appear once the puzzle is solved, failed or revealed. Personalized sets and
  single-theme Practice keep showing the type throughout. The concept is always in the payload
  (`reveal`), for stats and selection.
- The solution move animates the opponent's reply.
- A wrong move gets ✗ and is taken back. The puzzle counts as failed, and you can still finish
  it or retry.
- Hints: 1 = the idea, 2 = which piece. Show solution plays the rest of the line.
- After a solve or fail it shows the solution line, a short verified explanation and the source.
- Results go to `POST /api/puzzles/{id}/result`, which updates the per-puzzle stats and the
  learner model.
- Each tab keeps its state: a half-solved puzzle is still there after visiting Lessons or
  Analysis.
- ← → under the board step through the moves played. While solving, earlier positions are
  look-only. After the puzzle, you can try other moves (not graded). Right-drag draws
  calculation arrows. See "Board" in `ARCHITECTURE.md`.

## Difficulty calibration

*What* to train (the weakness) and *how hard* (the learner's skill) are decided separately.
Being weak at knight forks means knight-fork puzzles, **not** the easiest puzzles in the
library. There are no rating bands: the Chess.com rating is one input among several, and
evidence from games and puzzles can move the target well away from it in either direction.

Code: `learner/difficulty.py` (profile, targets, session step), `analysis/skill_evidence.py`
(game evidence), `puzzles/select.py` (`calibration=`).

### Evidence

Everything is on the puzzle-rating scale (the scale of `puzzles.profile.decision_rating`). Each
skill estimate is a **weighted mean of its evidence items**. Every item is stored with its source,
value, weight and a plain-language detail, so a target can always be explained.

| Source | Value | Weight |
|---|---|---|
| Rating prior | `520 + 0.6 × game rating` (400 → 760, 800 → 1000, 1200 → 1240). The game rating is the first available of: median of analyzed games, onboarding rating (normalized), onboarding experience, profile rating. | 2 |
| Game errors (≥ 3 analyzed games) | Error rate (mistakes + blunders per learner move) → game rating `400 − 1600·log10(rate / 0.12)`, then the same scale map. "Allowed" tactics use base 0.05 for defense; per-phase rates (≥ 30 moves in the phase) feed opening / endgame. | min(3, moves / 80) |
| Game chances (≥ 3 games, ≥ 3 chances) | Positions where the eval swung ≥ 200 cp to the learner, who is ≥ +150: found (loss ≤ 100) or missed (loss ≥ 200). Each chance is rated like a puzzle (`decision_rating` of the engine's best move) and is check/capture ("pattern") or quiet ("calculation"). Value = Elo performance + 100, because finding it in a real game, unprompted, is harder than in a puzzle. | min(3, n / 4) |
| Puzzle results (≥ 2 puzzles) | Elo performance on resolved puzzles. Score = first-try rate + 0.6 × (later solves) − 0.1 × hints per attempt. A clean solve in ≤ 20 s counts the puzzle as +100 (it was too easy to measure); a solve slower than 150 s scores × 0.85. Results older than 30 days count half. | 0.4 per puzzle, max 6 |

Skills are overall, tactics, pattern_recognition, calculation, defense, endgame and opening.
Puzzles count toward the skills that match their type and recognition stage (spot →
pattern recognition, deep → calculation, defense puzzles → defense, …). The level words
(beginner … expert) are for display only.

### Concept level and target

- **Concept estimate.** Start from the matching skill estimate, minus 50 if the concept is one of
  the profile's weaknesses. That anchor gets weight 2. It is then moved by Elo performance on
  puzzles that strictly match the concept (≥ 0.95). So "easy forks 10/10, harder forks 3/5" lands
  between the two groups, and the easy forks stop being served.
- **Target** = concept estimate − 60. That gives about 58% expected success: challenging but
  realistic.
  - Zone: target −120 … +150.
  - Floor: target −250 (trivial for this learner).
- **In a session** (`POST /api/puzzles/adapt`), after each finished puzzle:
  - The last two were instant clean solves (≤ 20 s, first try, no hint) → +100. Remaining
    puzzles below the new zone are swapped for harder ones.
  - The last two were missed, revealed or needed hints → −100, and the reverse swap.
  - Otherwise nothing changes. The set does **not** step up automatically after every puzzle.
  - Your-game items and the defensive item keep their place.
  - The results are also recorded, so the next set starts from the updated estimate.
- **Intrinsic difficulty is unchanged.** The puzzle rating still comes from the structure of the
  critical decision (quiet move, plausible candidates, payoff depth), not from move count.
  Calibration only decides which ratings fit this learner.

The UI shows one line on the first card, for example *"Aimed at your tactics level (improving),
from 12 analyzed games and 9 puzzles; 3/5 of these solved cleanly so far"*. When the set
adapts, it shows a short note. The numbers and evidence are in `/api/puzzles/difficulty`
and in the set's `debug.difficulty`.

**Continuous sessions.** A training session doesn't end after the first batch.

- After each puzzle, the result and explanation are shown. The next puzzle then loads by itself:
  2.5 s after a clean solve, 6 s after a miss (time to look at the solution). A countdown with
  "Stay here" is shown. Any interaction cancels it: ← →, a move or arrow on the board, or Retry.
  Next → always works.
- The flow is: record the result → the difficulty profile updates → select the next puzzle.
- When fewer than 2 unopened puzzles are left, `POST /api/puzzles/next` fetches 3 more. It uses
  exactly the selection of `/set`: the weakness or theme, concept skill, calibration, novelty,
  progression, and library first. It never serves a puzzle already in the session. It adds this
  session's results (`session_shift`: two instant solves +100, two tough ones −100).
- When a theme's fresh puzzles are used up, it tries verified generation: the existing
  shortfall path for weaknesses, and the same generator for a Practice theme when one exists.
  Then earlier puzzles come back as "Review", least recently played first and never one of the
  last 12. The session never runs dry.
- "Your game" opens a personalized session only once. Only "← Back to Puzzles" ends the
  session, and the dashboard then shows "Session: N of M solved".

## API

| Endpoint | Returns |
|---|---|
| `GET /api/puzzles/dashboard` | `personalized` (cards, main, profile box), `practice` themes with counts |
| `POST /api/puzzles/set {mode, concept?, weakness?, count}` | solver-ready puzzles with `role`, `role_label`, `why`; `ladder`; `difficulty` (target, zone, summary); `debug` for weakness sets (incl. `your_game` trail, `defend`, `roles`) |
| `POST /api/puzzles/next {mode, concept?, weakness?, exclude[], done[], count}` | the next puzzles of a continuous session (same shape as `/set`, plus `session{shift, reason}`); never one in `exclude`; generation, then review, when fresh ones run out |
| `POST /api/puzzles/adapt {mode, concept?, weakness?, done[], remaining[], set_ids[]}` | `{shift, reason, replace{old_id: puzzle}, difficulty}`: swaps remaining puzzles that left the zone |
| `GET /api/puzzles/difficulty?concept=` | the difficulty profile (skills with evidence, prior) and, for a concept, the target with its rule |
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
