import {test} from "node:test"
import assert from "node:assert/strict"
import {analysisLine, evalForLearner, gameMeta, matchup, progressText, resultLabel, reviewItems,
  uciSquares, weaknessLine} from "../game-format.js"

const summary = {learner_result: "loss", result: "0-1", player_color: "white", opponent: "knightmare42",
  opening: "Italian Game", time_label: "10 min", date: "2026-09-20"}

test("game summary lines", () => {
  assert.equal(resultLabel(summary), "Lost")
  assert.equal(matchup(summary), "You (White) vs knightmare42")
  assert.equal(gameMeta(summary), "Lost · Italian Game · 10 min · 2026-09-20")
  assert.equal(gameMeta({learner_result: "draw"}), "Draw")
})

test("analysis line speaks in beginner words", () => {
  assert.equal(analysisLine(null), "not analyzed yet")
  assert.equal(analysisLine({blunders: 0, mistakes: 0, moments: 0, habits: 0}), "no big mistakes 🎉")
  assert.match(analysisLine({blunders: 0, mistakes: 0, moments: 0, habits: 1}), /opening habit/)
  assert.equal(analysisLine({blunders: 1, mistakes: 0, moments: 1}), "1 big mistake")
  assert.equal(analysisLine({blunders: 6, mistakes: 4, moments: 8}), "10 big mistakes · 8 worth reviewing")
})

test("evaluations are shown from the learner's side, mates as words", () => {
  assert.equal(evalForLearner({kind: "cp", value: 850}, "white"), "you are clearly better (+8.5)")
  assert.equal(evalForLearner({kind: "cp", value: 850}, "black"), "your opponent is clearly better (-8.5)")
  assert.equal(evalForLearner({kind: "cp", value: 12}, "white"), "about equal")
  assert.equal(evalForLearner({kind: "cp", value: -60}, "white"), "your opponent is slightly better (-0.6)")
  assert.equal(evalForLearner({kind: "cp", value: 150}, "white"), "you are better (+1.5)")
  assert.equal(evalForLearner({kind: "mate", value: 2}, "white"), "you can force mate in 2")
  assert.equal(evalForLearner({kind: "mate", value: 2}, "black"), "your opponent can force mate in 2")
  assert.equal(evalForLearner({kind: "checkmate", value: -1}, "black"), "you gave checkmate")
  assert.equal(evalForLearner(null, "white"), "?")
})

test("progress text", () => {
  assert.deepEqual(progressText({done: 5, total: 10, index: 2, count: 4}),
    {text: "Stockfish is analyzing game 2 of 4… 50%", pct: 50})
  assert.equal(progressText({done: 1, total: 4, index: 1, count: 1}).text, "Stockfish is analyzing your game… 25%")
})

test("review items: mistakes first, then habits", () => {
  assert.deepEqual(reviewItems({moments: [{id: "a"}], habits: [{id: "h"}]}).map(m => m.id), ["a", "h"])
  assert.deepEqual(reviewItems(null), [])
  assert.deepEqual(uciSquares("e7e8q"), ["e7", "e8"])
  assert.deepEqual(uciSquares(null), [])
})

test("weakness line doesn't repeat the title", () => {
  assert.equal(weaknessLine({title: "Missed knight fork", description: "Missed knight fork: in 2 of your 4 games."}),
    "In 2 of your 4 games.")
  assert.equal(weaknessLine({title: "Hanging a piece"}), "Hanging a piece")
})
