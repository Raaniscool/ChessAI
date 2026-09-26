# Game Analysis (Chess.com games)

Import your own Chess.com games, let Stockfish find the moments that mattered,
see which mistakes **keep showing up across your last 10, 20, 30 or 50 games**,
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
2. On Chess.com open a game, click **Share → PGN** and copy the text. Paste one
   or several games (blank lines between games are optional).
3. Type your Chess.com username, or leave it empty. If it's empty and the app
   can't tell which player you are, it asks you to pick one.
4. **Import & analyze.** Stockfish's progress is shown game by game. A
   40-move game takes roughly 30–60 s on a laptop.
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

**Read aloud:** with 🔊 *Read aloud* on (header), each moment's explanation is read when
you open it, and so is the AI's answer. The 🔊 button on the card reads it on demand. As a
move is spoken, its squares light up. Moves from Stockfish's line light up in the position
where they're played. Pointing at a move in the text lights it up too.

Only Chess.com PGNs are supported in this milestone. Nothing is scraped from
Chess.com: you paste the text yourself (up to 100 games per paste; a game pasted
twice is imported once). The app can't fetch your recent games by itself yet.
Chess.com's official public API (monthly game archives per username) would be
the way to add that later; everything after the importer would stay the same.

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
4. A related **catalog lesson** (e.g. Forks, Rook endgames) as a follow-up.

## Privacy: your games are yours

- Imported games and analyses live in `DATA_DIR/games/` (default `data/games/`,
  gitignored), next to your plans. They never go into the Knowledge Library.
- The library refuses any entry whose source is a user game unless it is in the
  **personal** tier (`knowledge/schema.py`). Training plans use your positions
  directly and never write library entries.

## Architecture

```
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
| `POST /api/games/import {pgn, username?}` | Validate and store games. Returns `imported`, `new`, and per-game `errors`. `422 {needs_player, players}` when the side can't be told |
| `GET /api/games` | Your games, with a summary of each analysis |
| `POST /api/games/analyze {game_ids?, reanalyze?}` | NDJSON stream: `start`, `progress`, `game_done`, `error`, then `done {weaknesses}` (across all analyzed games) |
| `GET /api/games/{id}` | Game plus analysis, with a review card per moment |
| `POST /api/games/{id}/moments/{ply}/explain {question?, level?}` | Streamed explanation (Qwen, checked against the facts; template fallback) |
| `GET /api/games/history?count=10` | The history report for the last `count` games (10–100), from cached analyses. No engine needed |
| `POST /api/games/history/analyze {count, reanalyze?}` | NDJSON: `select {requested, available, selected, cached, to_analyze}`, `start`, `progress`, `game_done`, `error` (per game), then `done {report}` |
| `POST /api/games/history/explain {count, level?}` | Streamed explanation of the report (Qwen, checked; template fallback) |
| `GET /api/games/weaknesses?ids=` | Concepts seen in 2+ games (low-level; the history report builds on it) |
| `POST /api/games/training {keys, game_ids?, level?}` | Build a training plan; returns `course_id` and `first_lesson_id` |
| `DELETE /api/games/{id}` | Delete a game and its analysis |

## Settings

`GAME_ANALYSIS_DEPTH` (default 12) sets the depth of the quick pass over every
position. Candidate mistakes are re-checked at `ENGINE_DEPTH` (default 14).

## Tests

`backend/tests/test_game_*.py`:
- **Import**: parsing, metadata, malformed and illegal PGNs, several games at once.
- **Analysis**: engine use, false alarms, motifs, edge cases.
- **Review**: Qwen gets the verified facts, and contradictions are caught.
- **Training and privacy**.
- **History** (`test_game_history.py`): 10/20/30/50/custom counts, fewer than 10,
  duplicates, malformed games, a failing game in a batch, a lost engine mid-batch,
  tiers, the significance formula, evidence, concept roll-up, caching and stale
  analyses, progress, library links, training, privacy, and the Qwen facts and
  checks.
- **API**: end to end, plus a real-Stockfish test that is skipped when no engine is
  installed.

The UI flow is covered by `scripts/ui_e2e.mjs`.
