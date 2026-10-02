import {test} from "node:test"
import assert from "node:assert/strict"
import {filterGames, isoDate, lastGames, newGames, opponentRating, quotaLine, selectionSummary, timeClasses}
  from "../game-picker.js"

const g = (id, o = {}) => ({id, opponent: "bob", opening: "Italian Game", eco: "C50", learner_result: "win",
  player_color: "white", white_elo: 900, black_elo: 950, time_class: "blitz", date: "2026.09.10", moves: 30,
  analysis: null, ...o})
const GAMES = [
  g("a"), g("b", {opponent: "Carla", learner_result: "loss", player_color: "black", black_elo: 880, white_elo: 1200,
    time_class: "rapid", date: "2026.08.01", moves: 61, opening: "Sicilian Defense", analysis: {moments: 3}}),
  g("c", {learner_result: "draw", date: "2026.09.20", moves: 12}),
  g("d", {player_color: null}),
]

test("filters combine: text, result, colour, time, dates, opponent rating, moves, analyzed", () => {
  const ids = f => filterGames(GAMES, f).map(x => x.id)
  assert.deepEqual(ids({text: "carla"}), ["b"])
  assert.deepEqual(ids({text: "sicilian"}), ["b"])
  assert.deepEqual(ids({result: "draw"}), ["c"])
  assert.deepEqual(ids({color: "black"}), ["b"])
  assert.deepEqual(ids({timeClass: "rapid"}), ["b"])
  assert.deepEqual(ids({from: "2026-09-15"}), ["c"])
  assert.deepEqual(ids({to: "2026-08-31"}), ["b"])
  assert.deepEqual(ids({minRating: "1000"}), ["b"])             // the opponent's rating (1200 as White)
  assert.deepEqual(ids({maxMoves: "20"}), ["c"])
  assert.deepEqual(ids({analyzed: "yes"}), ["b"])
  assert.deepEqual(ids({analyzed: "no", result: "win", color: "white"}), ["a"])
  assert.deepEqual(ids({}), ["a", "b", "c", "d"])
})

test("small helpers", () => {
  assert.equal(isoDate("2026.03.04"), "2026-03-04")
  assert.equal(isoDate("????.??.??"), "")
  assert.equal(opponentRating(GAMES[1]), 1200)
  assert.deepEqual(timeClasses(GAMES), ["blitz", "rapid"])
  assert.deepEqual(lastGames(GAMES, 2), ["a", "b"])          // only games the learner played, newest first
  assert.deepEqual(newGames(["a", "b", "zzz"], GAMES), ["a"])  // analyzed and unknown games cost nothing
})

const Q = (left, initialLeft, dailyLeft, enabled = true) => ({enabled, left,
  initial: {allowance: 25, left: initialLeft}, daily: {allowance: 10, left: dailyLeft}})

test("the allowance is explained in one sentence", () => {
  assert.match(quotaLine(Q(35, 25, 10)), /You can analyze 35 more games now \(25 left from your first 25 \+ 10 of today's 10\)/)
  assert.match(quotaLine(Q(4, 0, 4)), /4 more games now \(4 of today's 10\)/)
  assert.match(quotaLine(Q(0, 0, 0)), /More are available tomorrow.*lessons and puzzles never run out/)
  assert.match(quotaLine(Q(null, 0, 0, false)), /unlimited/)
  assert.equal(quotaLine(null), "")
})

test("a selection is checked against the allowance before pressing Analyze", () => {
  assert.equal(selectionSummary([], GAMES, Q(35, 25, 10)).ok, false)
  const fits = selectionSummary(["a", "b", "c"], GAMES, Q(35, 25, 10))
  assert.equal(fits.ok, true)
  assert.match(fits.text, /3 games selected \(1 already analyzed\)\. Uses 2 of the 35 analyses available now/)
  const over = selectionSummary(["a", "c"], GAMES, Q(1, 0, 1))
  assert.equal(over.ok, false)
  assert.match(over.text, /That's 2 new, but 1 is available right now: untick 1/)
  assert.match(selectionSummary(["b"], GAMES, Q(0, 0, 0)).text, /Nothing new to analyze/)  // reports stay free
  assert.equal(selectionSummary(["b"], GAMES, Q(0, 0, 0)).ok, true)
})
