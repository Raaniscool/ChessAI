import test from "node:test"
import assert from "node:assert/strict"

import {checkedKings, parsePlacement} from "../check-highlight.js"
import {Chess} from "../vendor/chess.mjs/Chess.js"

test("finds the king in check, whoever gives it", () => {
  assert.deepEqual(checkedKings("4k3/8/8/8/8/8/8/4R1K1 b - - 0 1"), ["e8"])           // rook on the file
  assert.deepEqual(checkedKings("4k3/8/8/1B6/8/8/8/6K1 b - - 0 1"), ["e8"])           // bishop diagonal
  assert.deepEqual(checkedKings("4k3/2N5/8/8/8/8/8/6K1 b - - 0 1"), ["e8"])           // knight
  assert.deepEqual(checkedKings("4k3/3P4/8/8/8/8/8/6K1 b - - 0 1"), ["e8"])           // white pawn below
  assert.deepEqual(checkedKings("6k1/8/8/8/8/8/5p2/6K1 w - - 0 1"), ["g1"])           // black pawn above
  assert.deepEqual(checkedKings("6k1/8/8/8/8/8/8/q5K1 w - - 0 1"), ["g1"])            // queen on the rank
  assert.deepEqual(checkedKings("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"), ["e1"])  // fool's mate
})

test("no highlight without a check: blocked lines, own pieces, pawns that only push", () => {
  assert.deepEqual(checkedKings("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"), [])
  assert.deepEqual(checkedKings("4k3/4p3/8/8/8/8/8/4R1K1 b - - 0 1"), [])              // blocked by its own pawn
  assert.deepEqual(checkedKings("4k3/4P3/8/8/8/8/8/6K1 b - - 0 1"), [])                // a pawn straight ahead
  assert.deepEqual(checkedKings("4k3/8/8/8/8/8/3p4/4K3 w - - 0 1"), ["e1"])            // ... but diagonally it checks
  assert.deepEqual(checkedKings("4k3/3P4/8/8/8/8/8/6K1"), ["e8"])                      // placement only (board.getPosition)
  assert.deepEqual(checkedKings(""), [])
  assert.equal(parsePlacement("8/8/8/8/8/8/8/K7")[0][0], "K")
})

test("agrees with chess.js on positions from real play", () => {
  // walk many pseudo-random games and compare with chess.js's own check detection
  let seed = 7
  const rand = n => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n }
  let checks = 0
  for (let g = 0; g < 40; g++) {
    const chess = new Chess()
    for (let ply = 0; ply < 80 && !chess.game_over(); ply++) {
      const moves = chess.moves()
      chess.move(moves[rand(moves.length)])
      const fen = chess.fen()
      const expected = []
      if (chess.in_check()) {
        const turn = chess.turn()
        const square = chess.SQUARES.find(sq => { const p = chess.get(sq); return p && p.type === "k" && p.color === turn })
        expected.push(square)
        checks++
      }
      assert.deepEqual(checkedKings(fen), expected, fen)
    }
  }
  assert.ok(checks > 20, `only ${checks} checks met`)
})
