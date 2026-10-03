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
| Schema, concept graph (89 concepts), validators, verification pipeline | done |
| Source importers: curated records, Lichess opening DB, Lichess puzzles | done |
| Seed library: 489 verified examples (about 10 per tactic and mating pattern), reproducible build | done |
| Library expansion: new tactics, mates, endgame technique and beginner mistakes (`--expand`) | done (see *Library expansion*) |
| Runtime tiers (generated / personal), review states, usage metadata | done (data model + library API) |
| Tutor integration: retrieval layer, lessons from examples, chat/planner, teacher facts | done (see *Using the library in the tutor*) |
| New positions on a library miss: constructors + Qwen proposals, verified before saving | done (see *Generated positions*) |
| Missing-content fallback (never a dead end) | done (see *When the library has nothing*) |
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
| `retrieval.py` | the tutor's way in: concept resolution, teaching sequences, personalization, teacher facts |
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
| Lichess puzzle database (via `planner/data/puzzles.json` and the larger `sources/data/lichess_puzzles/pool.json`) | CC0 | tactics/mates from real games, and "walked into a fork / hung a piece" mistakes |
| Lichess puzzle database, expansion pools (`pool_expansion.json`, `pool_ideas.json`) | CC0 | decoys, clearance, interference, underpromotion, mate in two, hook/dovetail mates, pawn endings, back-rank and missed-threat mistakes |
| Textbook endgames and one historical game (Alburt–Kasparov 1978) | public-domain facts, original text | `curated/{endgames,mistakes}_expansion.json` |

## Using the library in the tutor

The tutor never reads the library directly: it goes through `retrieval.py`, and the
examples are presented by the **existing lesson system** (same steps, same session
engine, same move validation).

```
"Teach me checkmates"                       (sidebar box or chat)
  -> POST /api/plans {goal, library: true}
  -> retrieval.resolve_concepts   "checkmates" -> checkmate (aliases via the concept graph)
  -> retrieval.retrieve           3 verified examples, easiest first, varied patterns,
                                  not seen recently, at the learner's level
  -> planner/knowledge_lessons    demonstration -> guided example -> practice
                                  as teach / demonstrate / exercise steps (parse_lesson-validated)
  -> plan record                  unit 1: the examples (+ a harder practice lesson);
                                  units 2-3: linked catalog topics for more practice
  -> session                      learner moves: python-chess (legality) -> Stockfish (grading)
                                  teacher: the example's verified facts -> Qwen explains
```

**Retrieval** (`RetrievalRequest`): concept (text or ids), category, subcategory,
difficulty band, learner level, presentation mode, tags, count, exclusions, seen
history, prerequisites and personalization signals. Only `verified` entries are ever
returned, never `candidate`, `verifying`, `needs_review`, `rejected`, `deprecated` or
personal-tier entries.

- *Confidence:* a request whose only match is a broad root concept must not contain
  other content words. "opening principles" is not a request for the `openings`
  concept, so it goes to the catalog's *Opening principles* topic.
- *Level:* taken from the request, else the text ("really simple", "hard"), else the
  learner's history (success rate on this concept's examples).
- *Seen history:* every example put in a plan is recorded as used. The next "teach
  me forks" picks fresh ones.
- *Prerequisites:* beginners who haven't met a prerequisite concept (e.g. `check`
  before `checkmate`) get one easy demonstration of it first.
- *Weak spots:* a sub-concept the learner keeps failing (under 50% over 2+ attempts)
  gets one example in the sequence.
- *Related concepts* are offered as suggestions, never slipped into the lesson.

**Sequence** (don't dump the library): the easiest example is **demonstrated** (whole
line with its verified move notes, then the explanation). The next is **guided**
(demonstrated up to the key moment, then the learner finds the key move with the
library's hints). The last is **practice** (the learner plays every move of their side;
the replies are demonstrated). "Quiz me" skips the demonstration. Examples without a key
move (opening lines) are always demonstrated. At the key move the exercise accepts the
line's move and the curator's accepted moves. On the learner's *last* move it also
accepts Stockfish's verified alternatives, where no continuation depends on it.

**Teacher facts** (`retrieval.teaching_facts`), per step, for the one example being
taught (never the library):
- the example's FEN and moves
- the side to move and the pieces
- the key move
- the validator facts (motif, targets, escape squares)
- Stockfish's verdict, eval (mates as mates) and good alternatives
- the concept summary and the verified explanation
- the position on the board and its legal moves
- the learner's level

They go into the move-feedback prompt, the chat prompt and the *Explain this example*
prompt. The prompts say the facts are verified and that Qwen must not judge
correctness or add moves or evaluations.

*No spoilers:* while an exercise of the example is still ahead, chat gets
position-only facts plus "don't reveal the solution", and *Explain this example* is
refused (409).

*Contradiction check:* a streamed explanation that contradicts the example
(`facts.check_explanation`) is replaced by the library's own verified explanation.
Offline, the learner gets that verified explanation directly.

**Fallback:** if the library has no suitable verified example (e.g. "London System",
"opening principles") or retrieval fails, the planner runs exactly as before: catalog
topics, then Qwen-organized plans with Stockfish-screened lines.
`POST /api/plans` without `library: true` is the catalog planner alone.

**Chat:** "Teach me forks", "I want to learn pins" and "learn ..." are lesson requests,
as before. "Show me checkmates", "Give me an endgame lesson" and "quiz me on forks" are
checked with `POST /api/knowledge/intent`. They become lessons only if the library
covers them; "show me why this move is bad" stays a chat question.

**Usage statistics:** used (on plan creation), attempts, successes, hints (per
exercise move) and completions (per finished lesson) go to
`DATA_DIR/knowledge/usage.json`. They feed freshness and personalization, and they
never change an example's status.

API: `POST /api/plans {goal, library, level?}`, `POST /api/knowledge/intent {message}`,
`POST /api/sessions/{id}/example/explain` (NDJSON stream). `/api/health` reports
`knowledge_examples`.

## Generated positions (`knowledge/generation/`)

When the library has too few verified examples, new positions can be generated. **Nothing
generated is trusted until it passes the same checks as every library entry.**

```
proposal  ← a constructor (python-chess builds a position with the idea)  or  Qwen (FEN + intended move)
  → position        python-chess: legal position, side to move, legal intended move
  → concept         the concept's validator must find the idea after the intended move
  → discrimination  Stockfish multipv: every move about as good as the intended one must use the
                    same idea (otherwise the puzzle doesn't test the concept)
  → line            Stockfish's continuation must show the gain (tactic ≥150 cp, mate, endgame win)
  → words           title / explanation / hints written from the validator's facts, not by Qwen
  → pipeline        verify_candidate(): rules, concept, engine profile, solution, explanation, duplicates
  → saved           verified → used · Qwen proposal that is only uncertain → needs_review (never shown)
                    anything else → rejected, with the stage and reason
```

- Qwen only **proposes** (every third attempt, when connected). Its intended move is checked like
  any other; a wrong tactic is rejected at the concept or discrimination stage.
- Ids are `gen_<concept>_<hash>` (global generated tier) or `mygen_…` (personal tier). Duplicates
  (same position, or a position already in the library or already shown) are skipped.
- Provenance on every entry: `source.kind = "generated"`, the proposer (constructor or Qwen),
  `verified_by`, the Stockfish eval, the key move and the alternatives Stockfish accepts.
- Time and attempt budgets (default 25 s / 40 attempts). If Stockfish dies, generation stops at once
  with `stopped = "Stockfish stopped working"` instead of retrying.
- Constructors: knight/queen/pawn fork, hanging piece, threat, back-rank mate, supported mate, bare
  queen mate, skewer, absolute pin, opposition. **Not generated:** relative pins (they rarely win by
  force; the constructor failed the discrimination check 40 of 40 times and was removed), openings
  (only the Lichess opening database is used), and concepts without a validator.

## Opening trees (`knowledge/opening_trees.py`)

25 openings are stored as **trees**, not lines: Italian, Ruy Lopez, Scotch, Four Knights, Vienna,
King's Gambit, Petrov, Philidor, Sicilian, French, Caro-Kann, Scandinavian, Pirc, Alekhine, QGD,
QGA, Slav, Catalan, London, King's Indian, Nimzo-Indian, Queen's Indian, Grünfeld, English, Dutch.
(Benoni and Réti are not in this set yet.)

| | |
|---|---|
| Spec (hand-written, reviewed) | `knowledge/sources/data/curated/opening_trees.json`: names, aliases, main line, summary, plans, traps, model games |
| Build | `scripts/build_opening_trees.py` (Stockfish depth 14; resumable eval cache `sources/data/opening_evals.json`) |
| Output | `knowledge/data/opening_trees/<id>.json` + `_report.json` |
| Last build | 6,768 positions (6,138 unique), 2,733 named lines; 100–873 positions per opening |

**Where the positions come from.** Every move is from the Lichess opening database (CC0, the same
TSVs as `lichess_openings.py`): all named lines of the opening and every prefix of them. Hand-picked
main lines must be *exact* database lines, or the build stops. Only a thin tree (under 100 database
positions: London, Pirc, Scandinavian, Petrov) is extended along Stockfish's best moves, at most six
plies past a named leaf. Those positions are marked `src: "engine"`, and lessons say when a line ends
in engine moves.

**What is verified.** Every position gets a Stockfish evaluation. Every move is labelled sound
(loses ≤120 cp), dubious (≤250) or mistake. A learner's move in a taught branch must be sound.
Traps and model games are hand-written candidates, and the build keeps them only if they hold up:

- **Traps:** the moves before the mistake are reasonable (≤250 cp). The mistake loses ≥150 cp. The
  punishment is accurate (≤80 cp per move) and ends in mate or ≥ +150.
- **Games:** each one replays legally, stays in the tree for ≥6 plies, and its result matches the
  final position.

Last build: 13 of 20 traps and 8 of 9 games were kept. Rejections are listed in `_report.json`, e.g.
"Fried Liver Attack: the mistake Nxd5 only loses 105 cp". The library's own Fried Liver example is
separate and verified on its own.

**Teaching a branch, not the tree** (`planner/opening_tree_plans.py`). `OpeningTrees.find(goal)`
matches the opening and, optionally, a named variation ("Sicilian Dragon", "Marshall Attack",
"Grünfeld"). A variation name with no opening named is used only when it is unambiguous, so "exchange
variation" matches nothing. `OpeningTree.branch(node, level, side)` picks the line to teach:

| Level | Branch |
|---|---|
| beginner | to the first named position ≥8 plies deep |
| intermediate | to the first named position ≥14 plies deep |
| advanced | the deepest continuation; if it ends before 18 plies, the deepest named theory that leaves it as late as possible |

So the length varies by opening and level: Scandinavian 8/14/17 plies, QGD 8/15/28. A request
for a variation always stays inside it.

The plan has three parts:

1. The branch: moves and ideas, then the whole branch from memory. The drill covers every learner
   move however long the line is. A "where this branch fits" step lists the alternatives at each
   move ("Your opponent can also play 3...a6 (Morphy Defense)…").
2. Up to two traps that share the branch's moves.
3. A model game that reaches it.

Other branches are offered as chips (`plan.branches`), never added to the plan.

**Who answers.** When the catalog or a verified library example already teaches exactly the request
(the Italian Game, the Caro-Kann Advance, the Najdorf on the catalog's Sicilian line, the Evans
Gambit), that planner answers as before. The tree only adds `plan.branches` suggestions. The tree
planner answers everything else that names one of the 25 openings. Tree failures are logged and
never break planning.

Rebuild after editing the spec (about 20 minutes cold; cached evaluations make reruns fast):

```powershell
.\.venv\Scripts\python scripts\build_opening_trees.py            # all 25
.\.venv\Scripts\python scripts\build_opening_trees.py --only dutch_defense,petrovs_defense
```

## When the library has nothing (`planner/missing.py`)

A lesson request never fails just because there is no verified example:

1. The verified library and the catalog are searched first.
2. The request is resolved to a concept id or a glossary term (`knowledge/data/glossary.json`). With
   Qwen connected, Qwen may map the words to one of *our* ids (a language task only).
3. New positions are generated for the concept (above, ~20 s budget, progress streamed).
4. Otherwise verified examples of a **broader** concept, labelled as broader.
5. Otherwise the hand-written definition, labelled *"not engine-checked"*, plus related verified material.

The plan carries `plan.fallback = {term, concept, understood_via, text_source, broader, generated,
verified_examples}` so the UI can say exactly what was checked and by what.

## Library expansion (`build_knowledge_seed.py --expand`)

The original lists for this step weren't available, so the expansion follows the standard
beginner-to-club syllabus (the Lichess puzzle themes, and the usual first endgame and
beginner-mistake chapters of the classic textbooks). Anything already in the library was left
alone. New concepts, each with its own validator in `validators.py` (the *library expansion*
section):

| Category | New concepts | Examples | Where they come from |
|----------|--------------|---------:|----------------------|
| Tactics | decoy (`attraction`), clearance, interference, underpromotion | 6 / 4 / 4 / 6 | Lichess (real games) |
| Checkmates | mate in two, hook mate, dovetail mate | 6 / 6 / 6 | Lichess |
| Endgames | rule of the square, key squares, triangulation, Philidor position, wrong bishop, pawn breakthrough, outside passed pawn | 2 / 2 / 2 / 1 / 1 / 2 / 6 | curated textbook positions (triangulation also Alburt–Kasparov 1978); Lichess for the outside passed pawn |
| Mistakes | ignoring the back rank, avoiding stalemate; more *missing a threat* | 6 / 2 / +6 | Lichess; curated for stalemate |

**Finishing pass** (the remaining standard ideas, then topping up thin concepts):

| Category | New concepts / top-ups | Examples | Where they come from |
|----------|------------------------|---------:|----------------------|
| Tactics | x-ray, Greek gift, windmill, desperado | 5 / 3 / 2 / 6 | Lichess |
| Checkmates | epaulette mate, double-bishop mate; ladder mate topped up | 6 / 6 / 2 | Lichess; ladder curated |
| Endgames | zugzwang (own concept), perpetual check, stalemate tricks; Lucena and Philidor topped up | 5 / 2 / 2 / 2 / 2 | Lichess for zugzwang; composed positions for the rest |
| Mistakes / threats | early queen, repeated moves, ignoring development topped up; more double attack, spotting threats, relative pin, pawn fork, hanging queen, poisoned pawn | 2 / 2 / 2 / +6 / 3 / +6 each | curated miniatures (Stockfish-checked drops); Lichess |

The composed positions (`endgames_expansion.json` (the ladder mate is in it too, with category
`checkmates`) and `mistakes_expansion.json` in `sources/data/curated/`) carry a reference and the Stockfish
fact that makes them a lesson (for example "only 1.Qg5+ draws, other moves lose").

What each validator demands, so that a similar-looking position doesn't pass:

- **Decoy:** the offered piece is captured on its square, and the next learner move uses it there
  (a check, an attack or a capture on that square, or mate). Then the line must end in mate or a material gain.
- **Clearance:** a later move by *another* piece uses the vacated square. If you put the
  first piece back, that move would be impossible.
- **Interference:** a piece lands between an enemy long-range piece and a square it guarded. Then
  another learner piece uses that square (the interposed piece moving on would reopen the line).
- **Underpromotion:** a knight, bishop or rook promotion. It is refused if a queen mates too. The new
  `underpromotion` engine profile also demands that a queen be at least 150 cp worse, or stalemate.
- **Mate in two / hook / dovetail:** exactly two learner moves to mate. A rook mating from the
  next square, guarded by a knight that a pawn guards. A queen mating diagonally next to the king,
  with the two squares behind it blocked by the king's own pieces.
- **Rule of the square:** a lone king outside the pawn's square. The pawn's double step and the
  defender's tempo are counted, and the line must promote. **Key squares:** K+P vs K, the king
  steps onto a key square it wasn't on (rook pawns: b7/b8 or g7/g8).
- **Triangulation:** the same piece placement returns with the other side to move after
  three or more learner king moves. **Wrong bishop:** bishop + rook pawn against a lone king,
  the bishop can't cover the corner, and the defender reaches it (Stockfish: a draw).
- **Philidor:** rook against rook and pawn. The defending rook holds its third rank and, once the pawn
  reaches it, checks from at least three ranks behind (Stockfish: a draw).
- **Breakthrough / outside passer:** pawn endings only. A pawn sacrifice and a promotion. A
  passed pawn two or more files away from every other pawn, after which the learner's king takes a pawn
  three or more files away.
- **Ignoring the back rank:** a real mistake (Stockfish) followed by a back-rank mate.
  **Avoiding stalemate:** some legal move would stalemate, the key move doesn't, and the line mates.
- **X-ray:** a long-range piece hits a square *through* an enemy piece, and that piece is
  exchanged or moves before the capture lands. **Epaulette / double-bishop:** the queen mates
  from two squares straight in front of a king on the edge, both side squares blocked by the
  king's own pieces; or a bishop mates while the other bishop covers at least one of the king's squares.
- **Greek gift:** Bxh7+ (Bxh2+), the king takes, then a knight check on g5 (g4). **Windmill:**
  the same piece gives discovered check at least twice. **Desperado:** an attacked piece
  captures before it is lost, and the learner is *not* in check (escaping check isn't a choice).
- **Zugzwang:** a quiet key move (no capture, no check), the learner not in check before it, and
  the engine shows that having to move costs the opponent at least 200 cp.
- **Perpetual check:** every learner move is a check, the learner is at least 3 points behind
  at the start *and still at the end* (winning the material back isn't a perpetual), and
  Stockfish calls the final position drawn. **Stalemate tricks:** the learner is behind,
  and the line ends in stalemate.
- **Missing a threat (imports):** on top of the validator, the whole punishment must have worked
  without the mistake, too (the same mate, or the same material). Otherwise the mistake created
  the threat rather than ignoring it.

**Spotting threats (imports):** the defence may not check and the line may not end in mate,
otherwise it is an attack, not a defence.

**How the Lichess part was built.** The CC0 per-theme samples in github.com/pwenker/chessli2 were
verified move by move with `scripts/build_puzzle_library.py` into `pool_expansion.json` (themes
mateIn2, hookMate, dovetailMate, underPromotion, attraction, clearance, interference, pawnEndgame,
zugzwang). Lichess tags some themes loosely: only 1 of 24 "interference" puzzles really is one. So
the CSVs were first filtered with the concept validators (rules only), and only the matches
were verified with Stockfish into `pool_ideas.json`. Both files exclude every puzzle already in
`puzzles.json`, `pool.json` and `pool_harder.json`.

The finishing pass used `scripts/prefilter_lichess_csv.py SRC DST [themes]` the same way: rules
only, before Stockfish. `TAG_SUBSETS` builds stricter sub-themes from a tag *and* the puzzle's
other tags (`zugzwangWin`: zugzwang with crushing/advantage/mate; `perpetualDraw`: perpetual
without mate or material win). The Stockfish-verified results are in `pool_finish.json`.

```powershell
.\.venv\Scripts\python scripts\build_knowledge_seed.py --expand
```

This verifies the candidates against the library as it is (every pipeline stage, nothing relaxed).
It writes only `examples/<category>/lichess_expansion.json` and `curated_expansion.json`, plus the
`expansion` section of `seed_report.json`. Up to 6 examples per concept (`--expand-per-concept`).

**What was refused (and stays out):**
- Four Lichess tactics where Stockfish sees the learner only 0–61 cp better (`needs_review`).
- The wrong-bishop a-pawn example: at depth 14 Stockfish shows −84 to −103 cp, not "clearly drawn".
- Two missed-threat candidates whose "mistake" lost only 10–20 cp.
- All Lichess *exposedKing / attackingF2F7* puzzles as "weakening the king". Their setup moves were
  king walks into skewers or moves elsewhere on the board, so they went to *missing a threat*,
  where they passed the stricter check.
- Lichess "opposition" from pawn endings: the kings facing each other was incidental there.

**Refused in the finishing pass:**
- Six Lichess "perpetual" puzzles: their checks end in mate (Stockfish ±mate), not a draw.
  Plus every perpetual where the checks win the material back. Real perpetuals from a lost
  position are rare in the sample (5 of 120 tagged rows), so the concept has 2 examples.
- Draw-saving zugzwangs (a zugzwang that only holds a draw), and one where having to move cost
  the opponent only 99 cp.
- Lichess *earlyQueen*: a mid-game position can't prove the queen came out early. Those
  examples are curated whole games instead.
- An x-ray and a windmill where Stockfish sees the learner only 8 cp and 52 cp better.
- Lichess has no stalemate-trick puzzles in the sample, and only 3 Greek gifts and 3 windmills
  that pass the rules.

**Still thin, honestly:** wrong bishop (1: the lite engine can't confirm a second one is
drawn), opposition, key squares, rule of the square, triangulation and pawn breakthrough
(2–3 each, textbook positions only). Nothing was relaxed to grow these.

The glossary no longer lists the ideas that became concepts (decoy, interference, Philidor,
triangulation, x-ray, Greek gift, windmill, desperado, epaulette mate, perpetual check,
stalemate tricks). Its remaining entries (outposts, pawn structure, isolated/doubled/backward
pawns, open files, fianchetto, calculation) keep the honest fallback plans.

## Rebuilding the seed library

After editing a curated record, a validator or a threshold:

```powershell
.\.venv\Scripts\python scripts\build_knowledge_seed.py --puzzles-per-concept 10 --mistakes-per-concept 6
.\.venv\Scripts\python -m pytest backend/tests/ -q
```

**The puzzle pool.** Lichess puzzles reach the library through two files, both verified move by
move by `scripts/build_puzzle_library.py` (Stockfish: the solution must be the unique best move,
by 150 cp, at every learner move): the planner's `planner/data/puzzles.json` (read first, so
existing entries stay stable) and the larger `knowledge/sources/data/lichess_puzzles/pool.json`
(30 per theme, 18 themes, none repeated from puzzles.json). The pool was built from the per-theme
CC0 samples in github.com/pwenker/chessli2 (`puzzles/<theme>.csv`):

```powershell
.\.venv\Scripts\python scripts\build_puzzle_library.py <csv-dir> --out backend/app/knowledge/sources/data/lichess_puzzles/pool.json `
  --themes fork,pin,skewer,discoveredAttack,doubleCheck,deflection,capturingDefender,hangingPiece,trappedPiece,sacrifice,intermezzo,backRankMate,smotheredMate,anastasiaMate,arabianMate,bodenMate,mateIn1,promotion `
  --per-theme 30 --max-candidates 120 --exclude backend/app/planner/data/puzzles.json
```

Every pool puzzle still goes through the full library pipeline (concept validator, Stockfish,
explanation checks, near-duplicate filter). In the last build, 35 landed in `needs_review` (e.g. the
tactic only wins 141 cp) and 20 were rejected: bigger, not looser.

The seed build takes a few minutes and is deterministic: rebuilding without changes
produces identical files. Check `seed_report.json` for anything that landed in
`needs_review` or `rejected`. Fix the source record, or leave it out if Stockfish
disagrees with the idea.
