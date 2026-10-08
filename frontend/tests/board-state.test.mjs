import test from "node:test"
import assert from "node:assert/strict"
import {Chess} from "../vendor/chess.mjs/Chess.js"
import {BoardStateError, VerifiedBoard, assertBoardPosition, computePositionAfterMove} from "../board-state.js"

const START = new Chess().fen()
const POSITION = "k7/8/5n2/1B6/8/8/8/3QK3 b - - 0 1"
const WITHOUT_KNIGHT = "k7/8/8/1B6/8/8/8/3QK3 b - - 0 1"
const SAME_PIECES_WHITE_TO_MOVE = "k7/8/5n2/1B6/8/8/8/3QK3 w - - 0 1"

function expand(fen) {
  const placement = String(fen).trim().split(/\s+/)[0]
  const map = {}
  const files = "abcdefgh"
  const rows = placement.split("/")
  for (let i = 0; i < rows.length; i++) {
    let file = 0
    for (const ch of rows[i]) {
      if (/^[1-8]$/.test(ch)) file += Number(ch)
      else {
        map[`${files[file]}${8 - i}`] = `${ch === ch.toUpperCase() ? "w" : "b"}${ch.toLowerCase()}`
        file++
      }
    }
  }
  return map
}

function placement(map) {
  const files = "abcdefgh"
  const rows = []
  for (let rank = 8; rank >= 1; rank--) {
    let row = "", empty = 0
    for (const file of files) {
      const piece = map[`${file}${rank}`]
      if (!piece) empty++
      else {
        if (empty) row += empty
        empty = 0
        const letter = piece[1]
        row += piece[0] === "w" ? letter.toUpperCase() : letter
      }
    }
    if (empty) row += empty
    rows.push(row)
  }
  return rows.join("/")
}

class FakeBoard {
  constructor(fen = START) {
    this.pieces = expand(fen)
    this.position = placement(this.pieces)
    this.markers = []
    this.orientation = "white"
    this.calls = []
    this.dropSquare = null
    this.dropCount = 0
    this.holdNext = null
    this.view = {piecesGroup: {querySelectorAll: () => Object.entries(this.pieces).map(([square, piece]) => ({
      getAttribute(name) { return name === "data-square" ? square : name === "data-piece" ? piece : null },
      style: {opacity: ""},
    }))}}
  }
  getPosition() { return this.position }
  getPiece(square) { return this.pieces[square] || null }
  getOrientation() { return this.orientation }
  async setOrientation(value) { this.orientation = value }
  setPosition(fen) {
    this.calls.push(fen)
    const apply = () => {
      this.pieces = expand(fen)
      if (this.dropSquare && (this.dropCount === Infinity || this.dropCount > 0)) {
        delete this.pieces[this.dropSquare]
        if (this.dropCount !== Infinity) this.dropCount--
      }
      this.position = placement(this.pieces)
    }
    if (this.holdNext) {
      const wait = this.holdNext
      this.holdNext = null
      return wait.then(() => { apply(); return undefined })
    }
    apply()
    return Promise.resolve()
  }
  addMarker(marker, square) { this.markers.push({marker, square}) }
  removeMarkers(marker = undefined) {
    this.markers = marker === undefined ? [] : this.markers.filter(m => m.marker !== marker)
  }
  removeLegalMovesMarkers() {}
}

function marker(name = "piece") { return {name} }

test("successful setup verifies all pieces in the board model and rendered squares", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  assertBoardPosition(board, Chess, POSITION)
  assert.equal(board.getPiece("e1"), "wk")
  assert.equal(board.getPiece("d1"), "wq")
  assert.equal(board.getPiece("b5"), "wb")
  assert.equal(board.getPiece("a8"), "bk")
  assert.equal(board.getPiece("f6"), "bn")
})

test("a transient missing piece is detected and a full-position retry repairs it", async () => {
  const board = new FakeBoard()
  board.dropSquare = "f6"
  board.dropCount = 1
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  const result = await verified.setPosition(POSITION)
  assert.equal(result.recovered, true)
  assertBoardPosition(board, Chess, POSITION)
  assert.ok(board.calls.length >= 2)
})

test("a persistently incomplete setup fails and restores the last verified board", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  board.dropSquare = "f6"
  board.dropCount = Infinity
  await assert.rejects(verified.setPosition(POSITION), /could not be applied and verified/)
  assertBoardPosition(board, Chess, START, "recovered board")
  assert.equal(verified.currentFen(), START)
})

test("a piece move requires the expected source piece and preserves every unrelated square", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  const transition = await verified.move(POSITION, "f6g4", {
    expectedPiece: {color: "b", type: "n"}, animated: false,
  })
  assert.equal(transition.move.san, "Ng4")
  assertBoardPosition(board, Chess, transition.afterFen)
  assert.equal(board.getPiece("f6"), null)
  assert.equal(board.getPiece("g4"), "bn")
  assert.equal(board.getPiece("b5"), "wb")
  assert.equal(board.getPiece("d1"), "wq")
  assert.equal(board.getPiece("a8"), "bk")
})

test("a wrong or absent source piece prevents movement", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  assert.throws(() => verified.move(POSITION, "f6g4", {
    expectedPiece: {color: "w", type: "n"},
  }), /not the expected/)
  assert.throws(() => computePositionAfterMove(Chess, WITHOUT_KNIGHT, "f6g4"), /no piece on f6/)
  assertBoardPosition(board, Chess, POSITION)
})

test("the tracked FEN state includes side to move, not only identical piece placement", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  assert.equal(verified.matches(POSITION), true)
  assert.equal(verified.matches(SAME_PIECES_WHITE_TO_MOVE), false)
  const calls = board.calls.length
  await assert.rejects(verified.move(SAME_PIECES_WHITE_TO_MOVE, "d1d2", {
    expectedPiece: {color: "w", type: "q"},
  }), /last verified starting position/)
  assert.equal(board.calls.length, calls)
  assertBoardPosition(board, Chess, POSITION)
})

test("an external drag from the same pieces but a stale FEN state is rolled back", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  const stale = new Chess(SAME_PIECES_WHITE_TO_MOVE)
  stale.move({from: "d1", to: "d2"})
  board.pieces = expand(stale.fen())
  board.position = placement(board.pieces)
  await assert.rejects(verified.acknowledgeExternalMove(SAME_PIECES_WHITE_TO_MOVE, "d1d2", {
    expectedPiece: {color: "w", type: "q"},
  }), /not based on the last verified position/)
  assertBoardPosition(board, Chess, POSITION)
  assert.equal(verified.currentFen(), new Chess(POSITION).fen())
})

test("external board moves are accepted only after the entire result position is confirmed", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  const trial = new Chess(POSITION)
  trial.move({from: "f6", to: "g4"})
  board.pieces = expand(trial.fen())
  board.position = placement(board.pieces)
  const result = await verified.acknowledgeExternalMove(POSITION, "f6g4", {
    expectedPiece: {color: "b", type: "n"},
  })
  assertBoardPosition(board, Chess, result.afterFen)
  assert.equal(verified.currentFen(), result.afterFen)
})

test("piece highlights are gated by the actual piece and verified full position", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  await verified.addPieceMarker(marker(), "f6", {color: "b", type: "n"}, POSITION)
  assert.deepEqual(board.markers.map(m => m.square), ["f6"])

  await verified.setPosition(WITHOUT_KNIGHT)
  const markerCount = board.markers.length
  await assert.rejects(verified.addPieceMarker(marker(), "f6", {color: "b", type: "n"}, WITHOUT_KNIGHT),
    /expected piece is not on f6/)
  assert.equal(board.markers.length, markerCount)
})

test("stale highlights are rejected instead of landing on a later board", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  await verified.setPosition(POSITION)
  await assert.rejects(verified.addMarkers([{marker: marker(), square: "f6"}], START), /does not match/)
  assert.deepEqual(board.markers, [])
})

test("concurrent setup requests are serialized and each resolves only after its own verification", async () => {
  const board = new FakeBoard()
  const verified = new VerifiedBoard(board, Chess, {fallbackFen: START})
  let release
  board.holdNext = new Promise(resolve => { release = resolve })
  const first = verified.setPosition(POSITION)
  const second = verified.setPosition(WITHOUT_KNIGHT)
  await Promise.resolve()
  assert.equal(board.calls.length, 1)
  release()
  const [firstResult, secondResult] = await Promise.all([first, second])
  assert.equal(firstResult.fen, new Chess(POSITION).fen())
  assert.equal(secondResult.fen, new Chess(WITHOUT_KNIGHT).fen())
  assertBoardPosition(board, Chess, WITHOUT_KNIGHT)
  assert.equal(verified.currentFen(), new Chess(WITHOUT_KNIGHT).fen())
})
