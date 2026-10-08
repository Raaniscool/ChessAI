// Training (Puzzles tab): the info line while playing and the report after a segment.
import test from "node:test"
import assert from "node:assert/strict"
import {MODE_TEXT, STATUS_WORD, reportSections, segmentLine} from "../training.js"

const SEG = {id: "s1", mode: "middlegame", phase: "middlegame", side: "black", learner_moves: 0, min_moves: 5,
  max_moves: 10, bot_rating: 950, fen: "x", start_fen: "x", moves: [], moves_uci: []}

const RESULT = {
  segment: {...SEG, learner_moves: 6, finished: true},
  analysis: {learner_moves: 6, accuracy: 81.4, avg_loss_cp: 74, best_move_rate: 0.5,
    important_mistakes: [{label: "24...Qb6", san: "Qb6", category: "blunder", best_move: "hxg2#"}],
    missed_opportunities: [{label: "24...Qb6", best_move: "hxg2#"}]},
  feedback: {headline: "Accuracy 81% over 6 moves", ended: null,
    went_well: ["3 of your 6 moves matched Stockfish's top choice."],
    work_on: ["24...Qb6 was a blunder: hxg2# was better."],
    concepts: [{concept: "mate_in_one", name: "Mate in one", result: "missed", kind: "missed", line: "Mate in one: missed"}],
    reveal: {source: "Real game · Lichess puzzle database (CC0)", url: "https://lichess.org/training/x", line: null,
      idea: "Mate in one", key_move: "hxg2#"}},
}

test("the info line names phase, colour and progress — never the idea", () => {
  const line = segmentLine(SEG, "Middlegame")
  assert.match(line, /Middlegame · You play Black · Your move \(about 5–10 moves\) · bot ~950/)
  assert.match(segmentLine({...SEG, learner_moves: 3}, "Mixed Games"), /3 played/)
  assert.equal(segmentLine(null, "x"), "")
})

test("the report has every section in order, with the idea revealed only there", () => {
  const sections = reportSections(RESULT)
  assert.deepEqual(sections.map(s => s.kind), ["head", "mistakes", "missed", "concepts", "good", "work", "reveal"])
  assert.match(sections[0].lines[0], /Accuracy 81% · 6 moves · average loss 74 cp · 50% top moves/)
  assert.deepEqual(sections[1].lines, ["24...Qb6: blunder — hxg2# was better"])
  assert.deepEqual(sections[2].lines, ["24...Qb6: hxg2# was the chance"])
  assert.deepEqual(sections[3].lines, ["Mate in one: missed"])
  const reveal = sections.at(-1)
  assert.match(reveal.lines[0], /The idea in this position: Mate in one \(hxg2#\)/)
  assert.equal(reveal.url, "https://lichess.org/training/x")
})

test("a clean segment has no mistake sections and a position without an idea shows only its source", () => {
  const clean = {analysis: {learner_moves: 5, accuracy: 96, avg_loss_cp: 8, best_move_rate: 0.8,
    important_mistakes: [], missed_opportunities: []},
  feedback: {headline: "Accuracy 96% over 5 moves", went_well: ["No mistakes or blunders in 5 moves."],
    work_on: ["Nothing stood out this time. Keep going!"], concepts: [],
    reveal: {source: "Italian Game: Giuoco Piano", line: ["e4", "e5", "Nf3"], idea: null, key_move: null}}}
  const sections = reportSections(clean)
  assert.deepEqual(sections.map(s => s.kind), ["head", "good", "work", "reveal"])
  assert.deepEqual(sections.at(-1).lines, ["From: Italian Game: Giuoco Piano — e4 e5 Nf3"])
})

test("every mode has a description and every status a word", () => {
  for (const m of ["opening", "middlegame", "endgame", "mixed"]) assert.ok(MODE_TEXT[m])
  for (const s of ["needs_work", "improving", "solid", "ok", "insufficient"]) assert.ok(STATUS_WORD[s])
})
