# Game Analysis (Chess.com games)

Import your own Chess.com games, let Stockfish find the moments that mattered,
see which mistakes **keep showing up across your last 10, 25, 50 or 100 games (or games you pick)**,
and train them with verified lessons plus positions from your own games.

The rules are the same as everywhere else in the tutor:

| Question | Who answers it |
|---|---|
| Is this PGN a real, legal game? | **python-chess**. Every move is replayed, and any illegal or unreadable game is rejected |
| How good was each move, and what was better? | **Stockfish**, through the tutor's existing move classification |
| What went wrong (fork, pin, hung piece, mate…)? | The **Knowledge Library validators** check it on the board |
| How do I explain it to a beginner? | **Qwen**, using only the verified facts above. Its reply is checked and replaced by a template if it contradicts them |

## Using it

1. Click **🔍 Game Analysis** in the header.
2. Type your Chess.com username and press **Load my last 100 games** (or Enter).
   The app downloads your most recent finished games from Chess.com's official
   public API and imports them. **Nothing is analyzed yet.** Then either press
   **▶ Analyze** with "my last 25 games" (the default first analysis), or open
   **Choose your own games**. There you can filter by opponent or opening, result, colour,
   time control, dates, opponent rating, number of moves or analyzed/not, tick games,
   and press **Analyze selected games**. The report then covers exactly those games. The
   username is remembered. Loading again later picks up new games, and games you already
   have aren't imported twice.
   - **Analysis allowance** (`games/quota.py`): the first 25 games, then 10 new
     games a day (free) or 20 (paid, `CHESSAI_TIER=paid`; no payments are handled).
     A game is charged once, only when its analysis is saved. Re-analysis, downloads,
     reports, lessons and puzzles are free, and games analyzed before limits existed
     are never charged. A selection that doesn't fit is refused before any engine work,
     with "choose N or fewer". `ANALYSIS_LIMITS=0` turns limits off for development.
   - The **learner profile** always learns from all of your analyzed games (the last
     100), so a report on a few hand-picked games never erases a weakness found
     across many.
   - **Your Training** sits above the report. It shows the main weakness ("found in X of
     your Y analyzed games"), one clear next step (5 verified puzzles when the weakness
     supports them, otherwise the training plan), a recommended lesson, and up to 3 other
     weaknesses.
3. No internet, or want a specific game? Open **Or paste games yourself (PGN)**:
   on Chess.com open a game, click **Share → PGN**, copy the text and paste one or
   several games, then **Import & analyze**. If the app can't tell which player you
   are, it asks you to pick one.
4. Stockfish's progress is shown game by game. A 40-move game takes roughly
   30–60 s on a laptop; games analyzed before are reused.
5. The overview is your **game history**: your last N games analyzed together
   (see [Game history](#game-history-your-last-n-games-together) below). Pick
   10 (default), 20, 30, 50 or a custom number (10–100) and press **▶ Analyze**.
   Games that are already analyzed are reused, so only new games cost engine
   time.
6. Click a pattern's **Show the games** to see every game and move where it
   happened; click one to open that position. Or click a game on the left to step
   through its key moments:
   - **Position before**, **Your move**, **Best move** and **▶ Best line**
     (Stockfish's line played out on the board).
   - A short explanation in plain words. **🧠 Explain** asks the AI teacher, and you
     can type a question first.
   - **Engine details** (evaluations, lines, equally good moves) stay folded away
     until you open them.
7. **▶ Practice …** (on a pattern or a moment) builds a normal lesson plan in the
   Lessons tab. **💬 Explain my patterns** has the AI tutor explain the findings.

**Lines move the pieces.** When the review (or the AI's answer) mentions a move or a line
of moves ("Stockfish prefers Nc7+ (the line goes Nc7+ Kd8 Nxa7)"), the board *plays* it
instead of highlighting squares:
- Read aloud: as each move is spoken, the piece moves; afterwards the board goes back to the
  position you were reviewing.
- Point at a move: the board shows the line up to that move, and goes back when you move away.
- Click a move of a line (dotted underline): the whole line plays from the start.

Only single squares ("the f7 square") are highlighted. A run of moves counts as a line when
the moves are next to each other in the text and chess.js can play
them, in order, from one of the positions the moment is about (`frontend/lines.js`).

**Read aloud:** with 🔊 *Read aloud* on (header), each moment's explanation is read when
you open it, and so is the AI's answer. The 🔊 button on the card reads it on demand.

### Fetching games from Chess.com

`games/importers/chesscom_api.py` uses Chess.com's **Published-Data API** (read-only,
public, no login; nothing is scraped from web pages):

1. `GET https://api.chess.com/pub/player/{username}/games/archives`: the list of monthly archives.
2. The newest months first (`…/games/YYYY/MM`), until there are enough games; at most 24 months back.
3. Only standard chess with a PGN is kept (no Chess960, Bughouse, …), newest first by `end_time`.
4. The PGNs then go through the normal importer, so fetched games are validated by
   python-chess exactly like pasted ones.

Chess.com asks API users to make requests one at a time and to identify their app, so
requests are strictly serial and send a `User-Agent`. Clear messages for an unknown player
(404/410), rate limiting (429), time-outs and no connection. The history report is limited to
the fetched player's games, so a friend's game you pasted doesn't end up in your patterns.
Pasting PGNs still works (up to 100 games per paste; a game pasted twice is imported once).

## What gets detected

Each of the learner's moves is graded by the existing classifier (blunder,
mistake, inaccuracy…). Only moves that cost enough are reviewed, and each one is
re-checked at full depth, so quick-pass false alarms are dropped. A game shows
at most 8 moments. The rest are counted but not shown, so beginners aren't
overwhelmed. Small slips in a position that stays clearly won (or lost) are
skipped.

Every reviewed move is then explained with **motifs**. A motif is only reported
when a validator proves it on the board and the tactic actually wins something
in the engine's line:

| Motif | Concept id (existing) |
|---|---|
| missed checkmate / allowed checkmate | `checkmate`, `mate_in_one`, `back_rank_mate`, `smothered_mate`, … |
| hung piece | `hung_piece`, `hanging_queen` |
| walked into a fork | `walked_into_fork` |
| missed fork | `knight_fork`, `pawn_fork`, `queen_fork`, `fork` |
| missed pin / skewer / discovered attack / double check | `pin`, `skewer`, `discovered_attack`, `double_check` |
| allowed pin / skewer / discovered attack | same ids |
| ignored threat | `missed_threat` (only if the threat was real before the move) |
| king weakened | `king_safety_mistake` |
| poisoned pawn, bad trade, missed free piece, missed check | `poisoned_pawn`, `hung_piece`, `hanging_piece`, `check` |
| opening habits: early queen, repeated moves, poor development, king left in the centre | `early_queen`, `repeated_moves`, `ignoring_development`, `castling` |

- **Habits** are reported only when Stockfish confirms that the learner came out of
  the opening worse. A queen that comes out early without costing anything is not
  a lesson.
- **Next to a checkmate**, only the mate, the ignored threat and king safety are
  kept. Other tactics would just be noise.
- **Material** is described by value ("6 pawns' worth"), never by naming a piece
  that wasn't actually lost.

No second taxonomy: all concept ids come from `knowledge/data/concepts.json`.

## Evidence stored per moment

Game id, move number, SAN and UCI, FEN before and after, evaluations before and
after, best move and line, the opponent's likely continuation, equally good
alternatives, material change, motifs with their verified facts, concept,
severity, and a source reference (players, colour, date, link to the game).

## Game history: your last N games together

Analyzing one game says what went wrong in it. The history report answers a
different question: **what keeps showing up in my games, and what should I
practise?** It adds no chess judgement of its own: every occurrence it counts is
a moment Stockfish already confirmed and the validators already proved on the
board. It only selects, groups, counts and ranks (`analysis/history.py`).

**Selection.** The N most recent games you played, newest first (by Chess.com's
end date/time, then the game number in the link, then the import time). If
fewer are imported, the report says so and uses what's there.

**Grouping.** Findings are grouped by Knowledge Library concept (knight fork,
pin, hanging a piece, back-rank mate, early queen, king safety…). Specific
concepts roll up one level when that's what repeats: a knight fork in 2 games
and a pawn fork in 2 others are *forks in 4 games*.

**Tiers.** A pattern's label depends only on how many *different games* it
appears in:

| Tier | Rule | Shown as |
|---|---|---|
| one-time | 1 game | "One-off mistakes": never called a weakness |
| occasional | 2+ games, below the recurring bar | "Seen more than once (not a pattern yet)" |
| recurring | at least `max(3, ceil(15% of N))` games, **and N ≥ 10** | "Patterns that keep showing up" |

So: 3 of 10, 3 of 20, 5 of 30, 8 of 50. With fewer than 10 analyzed games the
report explains that there isn't enough history and never uses the word
*recurring*.

**Ranking (significance).** How often is not the same as how important. A small
inaccuracy in 8 games must not automatically outrank a blunder in 3:

```
impact(occurrence) = severity weight × (1 + min(loss in centipawns, 1000) / 1000)
                     blunder 3, mistake 2, inaccuracy 1, habit 1   → 1.0 … 6.0
same position again (e.g. the same opening trap)  → × 0.5
per game  = biggest impact in that game + 0.25 × the others
score     = sum over games (rounded to 2 decimals)
```

Patterns are listed recurring → occasional → one-time, each by score, then by
name. It's deterministic: the same games always give the same report. For
example, 3 blunders that each lost a piece (3 × 3 × 1.9 = 17.1) clearly beat 8
inaccuracies (8 × 1 × 1.06 ≈ 8.5).

**Evidence per pattern:** concept id and title, games count and total
occurrences, game ids, and every occurrence's game, move number, move played,
Stockfish's move, FEN, severity and centipawn loss. Plus severity counts,
average and maximum cost, distinct positions, phases, the tier, the score and
how many verified library examples exist.

**The overview** also shows the games analyzed, won/lost/drawn, the number of
big mistakes (and per game), the biggest single mistakes, and opening and
endgame observations (e.g. "Most of your big mistakes (5 of 8) came in the
endgame", "You played the Italian Game in 4 games (2 won, 2 lost)").

**Performance.** Each game is analyzed once and cached with the analyzer's
version. A new analyzer version triggers re-analysis. Progress streams game by
game, each game is saved as soon as it's done, and one broken game is reported
and left out without stopping the batch.

**The AI tutor** (💬 Explain my patterns) gets the finished report as structured
facts: tier, concept, "4 of your 10 games", severity, example moves with
Stockfish's move, library examples, one-offs marked *NOT patterns*, and the
learner level. It explains; it doesn't decide. A reply that claims different
numbers, names a weakness the analysis didn't find, or calls something a pattern
without enough games is replaced by the verified summary.

## Personalized training

"Practice …" builds an ordinary plan (same lesson schema, same player). For
each chosen pattern (from the history report, using the same games):

1. **Learn the pattern:** an explanation, then verified Knowledge Library
   examples (watch one, find the key move, solve one).
2. **Your own games:** positions from your games. *"Your game against X, move 17.
   You played Nf3 here. Find a better move."* Stockfish's move and any equally
   good alternatives are accepted. Afterwards the engine line is shown, then the
   explanation.
3. **More practice** from the library when there are enough examples. Their
   difficulty follows your demonstrated level (from your earlier attempts), once
   there are enough attempts to judge.
4. **New puzzles for you** (when some exist): personalized positions, below.
5. A related **catalog lesson** (e.g. Forks, Rook endgames) as a follow-up.

### Targeted puzzles (Start training)

**Start training** on the Your Training card asks for 5 puzzles for the main weakness. They
come from the [Puzzle Library](PUZZLE_LIBRARY.md) first: verified puzzles that train the
weakness, at your level, new or due for a retry, ordered easy → hard, each with a "why this
puzzle" line. New positions are generated (below) only when the library doesn't have
enough. Your own positions are never served back as puzzles.

### Personalized puzzles (new positions, not copies of your games)

Each pattern card with `new_puzzles: true` has a **New puzzles for this** button
(`POST /api/games/puzzles`). The weakness is mapped to a concept (walked into forks →
knight forks, hung pieces → spotting threats and hanging pieces, missed mates → mate in
one / back-rank mates, endgames → opposition and queen mates), and positions are
generated by the Knowledge Library generator: python-chess legality, the concept
validator, Stockfish multipv (every good move must use the idea), then the full library
pipeline. Only `verified` positions are shown.

- Stored in the **personal** tier (`mygen_…`), never in the shared library, with metadata:
  target weakness and concept, difficulty, FEN, side to move, solution, alternatives,
  Stockfish eval, motif, provenance, verification state, and the evidence that triggered
  it (games, moves, severity).
- Already-shown positions are remembered and never generated or offered again; unseen ones
  are reused before new ones are made.
- Not generated (the button is hidden): early queen moves, king safety and other
  weaknesses without a verified constructor. Their training still uses the library and
  your own positions.
- No engine: unseen puzzles are still offered; with nothing to reuse the request fails
  with a clear message (`503`) rather than showing unchecked positions.

## Privacy: your games are yours

- Imported games and analyses live in `DATA_DIR/games/` (default `data/games/`,
  gitignored), next to your plans. They never go into the Knowledge Library.
- The library refuses any entry whose source is a user game unless it is in the
  **personal** tier (`knowledge/schema.py`). Training plans use your positions
  directly and never write library entries.

## Architecture

```
Chess.com PubAPI ─► games/importers/chesscom_api.py (username → recent PGNs)
                              │
Chess.com PGN ─► games/importers/chesscom.py ─► games/model.GameRecord (platform-neutral)
                       (games/pgn.py validates)            │
                                                           ▼
                           analysis/analyzer.py  (Stockfish, existing classification)
                                                           │
                    analysis/motifs.py + habits.py  (Knowledge Library validators)
                                                           │
             analysis/weaknesses.py  (group by concept)
                                                           │
             analysis/history.py  (last N games: tiers, scores) ─► library retrieval (verified only)
                                                           │
     analysis/review.py (cards, Qwen facts, checks)   analysis/training.py (lesson plan)
                                                           │
                                  game_api.py  (/api/games/…)  ─►  frontend/analysis.js
```

**Adding Lichess later** only needs `games/importers/lichess.py` implementing
`parse(text, username)` → `ImportResult`, registered in `IMPORTERS` (`games/importers/__init__.py`).
Everything after `GameRecord` is platform-neutral.

## API

| Endpoint | What it does |
|---|---|
| `GET /api/games/quota` | The analysis allowance: `{tier, enabled, initial {allowance, used, left}, daily {allowance, used, left, resets_on}, left}` (also in `GET /api/games` and every `done` event) |
| `POST /api/games/fetch {username, count=100}` | Download the player's last `count` (1–100) standard games from Chess.com's public API and import them. Returns `fetched`, `new`, `imported`, `errors`. `404` unknown player / no games, `429` rate limited, `502/504` Chess.com unreachable |
| `POST /api/games/import {pgn, username?}` | Validate and store games. Returns `imported`, `new`, and per-game `errors`. `422 {needs_player, players}` when the side can't be told |
| `GET /api/games` | Your games, with a summary of each analysis |
| `POST /api/games/analyze {game_ids?, reanalyze?}` | NDJSON stream: `start`, `progress`, `game_done`, `error`, `skipped` (game deleted meanwhile), then `done {weaknesses}` (across the learner's analyzed games) |
| `GET /api/games/{id}` | Game plus analysis, with a review card per moment |
| `POST /api/games/{id}/moments/{ply}/explain {question?, level?}` | Streamed explanation (Qwen, checked against the facts; template fallback) |
| `GET /api/games/history?count=10&username=&ids=` | The history report for the last `count` games (10–100), or exactly the games in `ids` (comma-separated), from cached analyses. No engine needed. `username` limits it to that player's games |
| `POST /api/games/history/analyze {count \| game_ids, reanalyze?, username?}` | `429 {error, quota, needed}` if the new games don't fit the allowance. NDJSON: `select {requested, available, selected, cached, to_analyze}`, `start`, `progress`, `game_done`, `error` (per game), then `done {report}` |
| `POST /api/games/history/explain {count, level?, username?}` | Streamed explanation of the report (Qwen, checked; template fallback) |
| `GET /api/games/weaknesses?ids=&username=` | Concepts seen in 2+ games (low-level; the history report builds on it). Each has `new_puzzles` |
| `POST /api/games/training {keys, game_ids?, username?, level?}` | Build a training plan; returns `course_id` and `first_lesson_id` |
| `POST /api/games/puzzles {key, count 1–5, game_ids?, username?, fresh?}` | Targeted training: Puzzle Library first (easy → hard, at your level), only the shortfall generated. NDJSON: `status`, `puzzle` (per generated position), then `done {plan, course_id, first_lesson_id, library, reused, new, rejected, attempts}` (the plan has `puzzles` with reasons and a `debug` block) or `error`. `422` when there are no library puzzles and nothing can be generated, `503` with no engine and no library puzzles. `fresh: true`: generated positions only |
| `DELETE /api/games/{id}` | Delete a game and its analysis |

**One learner at a time.** Weaknesses, history and training never mix players. `username`
picks whose games; without it the player with the most imported games is used (the
report's `player` says who). A game with the same name on both sides, or with placeholder
names like `?`, is not assigned to anyone.

## Settings

`GAME_ANALYSIS_DEPTH` (default 12) sets the depth of the quick pass over every
position. Candidate mistakes are re-checked at `ENGINE_DEPTH` (default 14).

## Tests

`backend/tests/test_game_*.py`:
- **Import**: parsing, metadata, malformed and illegal PGNs, several games at once.
- **Fetch** (`test_chesscom_fetch.py`, mocked HTTP): newest games first, only the months
  needed, variants skipped, missing `[Link]` added, unknown player / rate limit / network
  errors, bad usernames, then fetch → history for that player only.
- **Analysis**: engine use, false alarms, motifs, edge cases.
- **Review**: Qwen gets the verified facts, and contradictions are caught.
- **Training and privacy**.
- **History** (`test_game_history.py`): 10/20/30/50/custom counts, fewer than 10,
  duplicates, malformed games, a failing game in a batch, a lost engine mid-batch,
  tiers, the significance formula, evidence, concept roll-up, caching and stale
  analyses, progress, library links, training, privacy, and the Qwen facts and
  checks.
- **Personalized puzzles** (`test_personal_puzzles.py`, real Stockfish): recurring weakness →
  new verified positions → plan, personal tier kept out of the global library, no copies of
  your games, no repeats, unsupported weaknesses, no engine, partial failures.
- **Audit regressions** (`test_import_edge_cases.py`, `test_api_robustness.py`,
  `test_audit_fixes.py`): odd PGNs, ids, one player at a time, same name on both sides,
  games deleted mid-batch, stalemate instead of mate.
- **API**: end to end, plus a real-Stockfish test that is skipped when no engine is
  installed.

The UI flow is covered by `scripts/ui_e2e.mjs`; line detection by `frontend/tests/lines.test.mjs`.
