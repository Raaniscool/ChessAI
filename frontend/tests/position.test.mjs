import test from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {isStartingPlacement, parsePosition, parsePuzzlePosition} from "../position.js"
import {MoveHistory} from "../board-nav.js"

const ITALIAN = "r1bqkbnr/pppp1ppp/2n5/8/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"
const EMPTY = "8/8/8/8/8/8/8/8 w - - 0 1"

test("a position must include a FEN; undefined never becomes chess.js's default start", () => {
  for (const fen of [undefined, null, "", "   "]) {
    assert.throws(() => parsePosition(Chess, fen, "Puzzle position"), /missing its FEN/)
  }
})

test("invalid FENs are rejected instead of leaving an empty board", () => {
  assert.throws(() => parsePosition(Chess, "not a FEN", "Puzzle position"), /invalid FEN/)
  assert.throws(() => parsePosition(Chess, EMPTY, "Puzzle position"), /invalid FEN/)
})

test("a supplied non-starting puzzle position is preserved exactly", () => {
  const chess = parsePosition(Chess, ITALIAN, "Puzzle position")
  assert.equal(chess.fen(), ITALIAN)
  assert.equal(isStartingPlacement(chess, Chess), false)
})

test("move histories reject missing starting positions instead of defaulting to chess.js's start", () => {
  assert.throws(() => MoveHistory.fromMoves(undefined, ["e2e4"], Chess), /missing its FEN/)
  assert.throws(() => MoveHistory.fromPositions([undefined], Chess), /missing its FEN/)
})

test("a puzzle response that contains the valid starting board is rejected", () => {
  const start = new Chess().fen()
  assert.equal(isStartingPlacement(parsePosition(Chess, start), Chess), true)
  assert.throws(() => parsePuzzlePosition(Chess, start, "Puzzle position"), /standard starting board/)
})
