import {test} from "node:test"
import assert from "node:assert/strict"
import {evidenceLine, foundIn, historyProgress, parseCount, patternDetail, patternIcon, practiceLabel, resultsLine,
  selectionText} from "../history-view.js"

test("count choice: presets, custom, and what's refused", () => {
  assert.deepEqual(parseCount("10"), {count: 10})
  assert.deepEqual(parseCount("50"), {count: 50})
  assert.deepEqual(parseCount("custom", "15"), {count: 15})
  assert.deepEqual(parseCount("custom", " 100 "), {count: 100})
  assert.match(parseCount("custom", "9").error, /at least 10/)
  assert.match(parseCount("custom", "101").error, /at most 100/)
  assert.match(parseCount("custom", "").error, /whole number/)
  assert.match(parseCount("custom", "12.5").error, /whole number/)
})

const fork = {key: "knight_fork", concept: "knight_fork", concept_name: "Knight fork", title: "Missed knight fork",
  game_count: 4, total_games: 10, occurrences: 5, distinct_positions: 3, severity_counts: {blunder: 4, mistake: 1},
  motifs: ["missed_fork"]}

test("pattern wording", () => {
  assert.equal(foundIn(fork), "Found in 4 of 10 games")
  assert.equal(foundIn({...fork, game_count: 10}), "Found in all 10 games")
  assert.equal(foundIn({...fork, game_count: 1}), "Found in 1 game")
  assert.equal(foundIn({...fork, game_count: 2, total_games: 2}), "Found in both games")
  assert.equal(patternDetail(fork), "4 blunders, 1 mistake · 5 times · 3 different positions")
  assert.equal(patternDetail({...fork, occurrences: 4, distinct_positions: 1, severity_counts: {habit: 1}}),
    "1 opening habit")
  assert.equal(practiceLabel(fork), "▶ Practice Knight fork")
  assert.equal(practiceLabel({title: "Missed pin"}), "▶ Practice Pin")
  assert.equal(patternIcon(fork), "♞")
  assert.equal(patternIcon({key: "back_rank_mate"}), "♚")
  assert.equal(patternIcon({key: "something_new"}), "•")
})

test("results and evidence lines", () => {
  assert.equal(resultsLine({win: 3, loss: 5, draw: 2, unfinished: 0}), "3 won · 5 lost · 2 drawn")
  assert.equal(resultsLine({win: 0, loss: 0, draw: 0, unfinished: 2}), "2 unfinished")
  assert.equal(evidenceLine({opponent: "opp", date: "2026-09-01", move_number: 12, san: "Kd2", best_move: "Nc7+"}),
    "vs opp (2026-09-01) · move 12: you played Kd2, Stockfish's move Nc7+")
})

test("batch progress counts finished games plus the current one", () => {
  assert.deepEqual(historyProgress({index: 1, count: 10, done: 0, total: 40}),
    {text: "Stockfish is analyzing game 1 of 10… 0%", pct: 0})
  assert.equal(historyProgress({index: 6, count: 10, done: 20, total: 40}).pct, 55)
})

test("selection message", () => {
  assert.equal(selectionText({requested: 10, available: 12, selected: Array(10).fill("g"), to_analyze: 3, cached: 7}),
    "Your last 10 games. Analyzing 3 games (7 already done)…")
  assert.equal(selectionText({requested: 20, available: 12, selected: Array(12).fill("g"), to_analyze: 0, cached: 12}),
    "Only 12 games are imported, so I'm using those. All of them are already analyzed.")
  assert.match(selectionText({requested: 10, available: 0, selected: [], to_analyze: 0}), /import your Chess.com/)
})

test("new-puzzle progress wording", async () => {
  const {puzzleStatus} = await import("../history-view.js")
  assert.equal(puzzleStatus({type: "status", text: "Stockfish is checking a position…"}), "Stockfish is checking a position…")
  assert.equal(puzzleStatus({type: "puzzle"}, 1), "1 new puzzle checked by Stockfish…")
  assert.equal(puzzleStatus({type: "puzzle"}, 2), "2 new puzzles checked by Stockfish…")
  assert.equal(puzzleStatus({type: "done", new: 2, reused: 1}), "3 puzzles ready (new positions, not from your games).")
  assert.equal(puzzleStatus({type: "done", new: 0, reused: 1}), "1 puzzle ready (new positions, not from your games).")
  assert.equal(puzzleStatus({type: "done", library: 5, new: 0, reused: 0}),
    "5 puzzles ready from the puzzle library — none of them from your games.")
  assert.equal(puzzleStatus({type: "done", library: 3, new: 2, reused: 0}),
    "5 puzzles ready: 3 from the puzzle library, 2 built just now — none of them from your games.")
  assert.equal(puzzleStatus({type: "error", error: "No engine"}), "No engine")
  assert.equal(puzzleStatus({type: "other"}), "")
})

import {trainingPlan} from "../history-view.js"

test("Your Training: the top pattern becomes the main weakness, with a clear reason", () => {
  const p = (key, tier, game_count, o = {}) => ({key, tier, game_count, title: key, ...o})
  const r = {games_analyzed: 25, game_ids: [], patterns: [
    p("hanging", "recurring", 6, {new_puzzles: true}), p("fork", "recurring", 4), p("queen", "occasional", 2),
    p("castle", "occasional", 2), p("pin", "occasional", 2), p("once", "one_time", 1)]}
  const t = trainingPlan(r)
  assert.equal(t.main.key, "hanging")
  assert.equal(t.heading, "Main weakness")
  assert.equal(t.found, "Found in 6 of your 25 analyzed games.")
  assert.equal(t.puzzles, 5)
  assert.deepEqual(t.others.map(o => o.key), ["fork", "queen", "castle"])  // at most 3, never one-off mistakes
  const possible = trainingPlan({games_analyzed: 10, patterns: [p("fork", "occasional", 2)]})
  assert.match(possible.heading, /Possible pattern/)
  assert.equal(possible.puzzles, 0)
  assert.match(trainingPlan({games_analyzed: 3, patterns: [p("once", "one_time", 1)]}).text, /nothing specific to fix yet/)
  assert.equal(trainingPlan({games_analyzed: 0, patterns: []}), null)
})
