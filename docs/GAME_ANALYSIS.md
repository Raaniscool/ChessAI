# Game Analysis (Chess.com games)

Import your own Chess.com games, let Stockfish find the moments that mattered,
see which mistakes **repeat across games**, and train them with verified
lessons plus positions from your own games.

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
5. The overview lists **recurring weaknesses**. Click a game on the left to step
   through its key moments:
   - **Position before**, **Your move**, **Best move** and **▶ Best line**
     (Stockfish's line played out on the board).
   - A short explanation in plain words. **🧠 Explain** asks the AI teacher, and you
     can type a question first.
   - **Engine details** (evaluations, lines, equally good moves) stay folded away
     until you open them.
6. **▶ Start training** (on a weakness or a moment) builds a normal lesson plan in
   the Lessons tab.

Only Chess.com PGNs are supported in this milestone. Nothing is scraped from
Chess.com: you paste the text yourself.

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

## Recurring weaknesses

A weakness is **never** named from one game: it needs evidence from at least
**two different games**. Repeating a mistake twice inside one game is still
"seen once". Each weakness records:
- the games and moves where it happened;
- the positions;
- how severe it was (blunder 3, mistake 2, inaccuracy 1, habit 1);
- how often it happened;
- how many verified library examples exist for it.

## Personalized training

"Start training" builds an ordinary plan (same lesson schema, same player). For
each chosen weakness:

1. **Learn the pattern:** an explanation, then verified Knowledge Library
   examples (watch one, find the key move, solve one).
2. **Your own games:** positions from your games. *"Your game against X, move 17.
   You played Nf3 here. Find a better move."* Stockfish's move and any equally
   good alternatives are accepted. Afterwards the engine line is shown, then the
   explanation.
3. **More practice** from the library when there are enough examples.
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
             analysis/weaknesses.py  (2+ games) ─► library retrieval (verified only)
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
| `GET /api/games/weaknesses?ids=` | Recurring weaknesses, optionally for some games only |
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
- **API**: end to end, plus a real-Stockfish test that is skipped when no engine is
  installed.

The UI flow is covered by `scripts/ui_e2e.mjs`.
