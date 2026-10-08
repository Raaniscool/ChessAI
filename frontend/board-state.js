// Verified operations for cm-chessboard. A local chess.js position describes the intent; the
// board's own model and rendered SVG must agree before a move/highlight is allowed to continue.
import {parsePosition} from "./position.js"

const FILES = "abcdefgh"
const RANKS = "12345678"
const PIECES = new Set("pnbrqkPNBRQK")
const EMPTY_FEN = "8/8/8/8/8/8/8/8"
const UCI_RE = /^[a-h][1-8][a-h][1-8][qrbn]?$/i

export class BoardStateError extends Error {
  constructor(message, details = {}) {
    super(message)
    this.name = "BoardStateError"
    this.details = details
  }
}

function squares() {
  return [...RANKS].flatMap(rank => [...FILES].map(file => `${file}${rank}`))
}

function positionIdentity(Chess, fen, label = "Board position") {
  return parsePosition(Chess, fen, label).fen().split(" ").slice(0, 4).join(" ")
}

function pieceMapFromPlacement(placement) {
  if (typeof placement !== "string") throw new Error("Board returned no piece placement.")
  const rows = placement.trim().split(/\s+/)[0].split("/")
  if (rows.length !== 8) throw new Error("Board returned an invalid piece placement.")
  const result = {}
  for (let row = 0; row < 8; row++) {
    let file = 0
    for (const ch of rows[row]) {
      if (/^[1-8]$/.test(ch)) {
        file += Number(ch)
      } else if (PIECES.has(ch)) {
        if (file >= 8) throw new Error("Board returned an invalid piece placement.")
        result[`${FILES[file]}${8 - row}`] = `${ch === ch.toUpperCase() ? "w" : "b"}${ch.toLowerCase()}`
        file++
      } else {
        throw new Error("Board returned an invalid piece placement.")
      }
    }
    if (file !== 8) throw new Error("Board returned an invalid piece placement.")
  }
  return result
}

function expectedPieceMap(chess) {
  const result = {}
  for (const square of squares()) {
    const piece = chess.get(square)
    if (piece) result[square] = `${piece.color}${piece.type}`
  }
  return result
}

function normalizePiece(piece) {
  if (typeof piece === "string") return piece.toLowerCase()
  if (piece && typeof piece === "object" && piece.color && piece.type) {
    return `${piece.color}${piece.type}`.toLowerCase()
  }
  return null
}

function differences(expected, actual, layer) {
  const result = []
  for (const square of squares()) {
    const want = expected[square] || null
    const got = actual[square] || null
    if (want !== got) result.push({square, expected: want, actual: got, layer})
  }
  return result
}

function renderedPieceMap(board) {
  const group = board && board.view && board.view.piecesGroup
  if (!group || typeof group.querySelectorAll !== "function") return null
  const result = {}
  for (const node of group.querySelectorAll("[data-square][data-piece]")) {
    const square = node.getAttribute("data-square")
    const piece = normalizePiece(node.getAttribute("data-piece"))
    const hidden = node.getAttribute("visibility") === "hidden" ||
      (node.style && node.style.opacity !== "" && Number(node.style.opacity) < 0.99)
    if (!square || hidden) continue
    if (Object.hasOwn(result, square)) result[square] = "<duplicate>"
    else result[square] = piece
  }
  return result
}

/** Compare all 64 squares in the requested FEN with the board model and, when available, SVG. */
export function inspectBoardPosition(board, Chess, fen, label = "Board position") {
  const expectedChess = parsePosition(Chess, fen, label)
  const expectedFen = expectedChess.fen()
  const expected = expectedPieceMap(expectedChess)
  let actualPlacement
  let actualModel
  try {
    actualPlacement = board.getPosition()
    actualModel = pieceMapFromPlacement(actualPlacement)
  } catch (err) {
    return {matches: false, expectedFen, actualFen: actualPlacement || null,
      mismatches: [{layer: "model", error: err.message}]}
  }

  const mismatches = differences(expected, actualModel, "position")
  if (board && typeof board.getPiece === "function") {
    const accessors = {}
    try {
      for (const square of squares()) {
        const piece = normalizePiece(board.getPiece(square))
        if (piece) accessors[square] = piece
      }
      mismatches.push(...differences(expected, accessors, "piece-accessor"))
    } catch (err) {
      mismatches.push({layer: "piece-accessor", error: err.message})
    }
  }
  const rendered = renderedPieceMap(board)
  if (rendered) mismatches.push(...differences(expected, rendered, "rendered"))
  return {matches: mismatches.length === 0, expectedFen, actualFen: String(actualPlacement), mismatches}
}

export function assertBoardPosition(board, Chess, fen, label = "Board position") {
  const result = inspectBoardPosition(board, Chess, fen, label)
  if (!result.matches) {
    const first = result.mismatches[0]
    const where = first && first.square ? ` at ${first.square}` : ""
    throw new BoardStateError(`${label} does not match the verified position${where}.`, result)
  }
  return result
}

function matchesPiece(actual, expected) {
  if (!expected) return true
  const normalized = normalizePiece(expected)
  return actual === normalized
}

function toUci(chess, uci) {
  if (typeof uci !== "string" || !UCI_RE.test(uci)) {
    throw new BoardStateError(`Invalid move coordinate: ${String(uci)}.`)
  }
  const normalized = uci.toLowerCase()
  const from = normalized.slice(0, 2)
  const to = normalized.slice(2, 4)
  const piece = chess.get(from)
  if (!piece) throw new BoardStateError(`There is no piece on ${from}; the move was not played.`)
  const move = chess.move({from, to, promotion: normalized[4] || "q"})
  if (!move) throw new BoardStateError(`The move ${normalized} is not legal from the verified position.`)
  return {move, from, to, piece: `${piece.color}${piece.type}`, uci: normalized}
}

/** Compute and validate a legal move without changing the supplied position or the board. */
export function computePositionAfterMove(Chess, fen, uci, {expectedPiece = null, label = "Move"} = {}) {
  const before = parsePosition(Chess, fen, `${label} starting position`)
  const beforeFen = before.fen()
  const transition = toUci(before, uci)
  if (!matchesPiece(transition.piece, expectedPiece)) {
    throw new BoardStateError(`The piece on ${transition.from} is ${transition.piece}, not the expected ${normalizePiece(expectedPiece)}.`)
  }
  return {beforeFen, afterFen: before.fen(), move: transition.move,
    uci: transition.uci, from: transition.from, to: transition.to, piece: transition.piece}
}

export class VerifiedBoard {
  constructor(board, Chess, {fallbackFen = null} = {}) {
    this.board = board
    this.Chess = Chess
    this.queue = Promise.resolve()
    this.fallbackFen = fallbackFen ? parsePosition(Chess, fallbackFen, "Fallback board position").fen() : null
    this.lastVerifiedFen = null
    if (this.fallbackFen) {
      try {
        assertBoardPosition(board, Chess, this.fallbackFen, "Initial board position")
        this.lastVerifiedFen = this.fallbackFen
      } catch (_) { /* the first explicit setup will repair or fail safely */ }
    }
  }

  currentFen() {
    if (!this.lastVerifiedFen) return null
    try {
      assertBoardPosition(this.board, this.Chess, this.lastVerifiedFen, "Current board position")
      return this.lastVerifiedFen
    } catch (_) {
      return null
    }
  }

  matches(fen) {
    try {
      if (!this.lastVerifiedFen || positionIdentity(this.Chess, this.lastVerifiedFen) !== positionIdentity(this.Chess, fen)) {
        return false
      }
      return assertBoardPosition(this.board, this.Chess, fen).matches
    } catch (_) { return false }
  }

  pieceAt(square) {
    if (!this.board || typeof this.board.getPiece !== "function") return null
    return normalizePiece(this.board.getPiece(square))
  }

  isPieceAt(square, expectedPiece, fen = this.currentFen()) {
    if (!fen || !this.matches(fen)) return false
    const chess = parsePosition(this.Chess, fen)
    const expected = chess.get(square)
    const expectedCode = expected ? `${expected.color}${expected.type}` : null
    return Boolean(expectedCode && matchesPiece(this.pieceAt(square), expectedPiece || expectedCode))
  }

  _enqueue(run) {
    const task = this.queue.then(run, run)
    this.queue = task.catch(() => {})
    return task
  }

  _clearMarkersDirect(marker = undefined) {
    if (this.board.removeMarkers) this.board.removeMarkers(marker)
    if (this.board.removeLegalMovesMarkers) this.board.removeLegalMovesMarkers()
  }

  _applyMarkersDirect(markers, expectedFen) {
    const expected = parsePosition(this.Chess, expectedFen, "Highlight position")
    for (const item of markers || []) {
      const square = item.square
      if (typeof square !== "string" || !/^[a-h][1-8]$/.test(square)) {
        throw new BoardStateError(`Refused an invalid highlight square: ${String(square)}.`)
      }
      if (item.piece && !matchesPiece(this.pieceAt(square), item.piece)) {
        throw new BoardStateError(`Refused to highlight ${square}: the expected piece is not there.`)
      }
      // All pieces are verified against the entire FEN immediately before this call. For a
      // piece-target highlight, `piece` also asserts the exact piece/color on its square.
      if (!expected) throw new BoardStateError("Highlight position is unavailable.")
      this.board.addMarker(item.marker, square)
    }
  }

  async _setAndVerify(fen, {orientation, animated = false, label = "Board position"} = {}) {
    if (orientation !== undefined && this.board.getOrientation && this.board.getOrientation() !== orientation) {
      await this.board.setOrientation(orientation)
    }
    await this.board.setPosition(fen, animated)
    assertBoardPosition(this.board, this.Chess, fen, label)
  }

  async _restoreAfterFailure(candidates) {
    for (const fen of [...new Set(candidates.filter(Boolean))]) {
      try {
        await this._setAndVerify(fen, {animated: false, label: "Recovered board position"})
        this.lastVerifiedFen = parsePosition(this.Chess, fen).fen()
        this._clearMarkersDirect()
        return this.lastVerifiedFen
      } catch (_) { /* try the next known-safe position */ }
    }
    // A blank board is a safer visible failure state than leaving a half-applied position.
    try {
      await this.board.setPosition(EMPTY_FEN, false)
      const blank = pieceMapFromPlacement(this.board.getPosition())
      if (!Object.keys(blank).length) {
        this.lastVerifiedFen = null
        this._clearMarkersDirect()
      }
    } catch (_) { /* the original BoardStateError remains the useful failure */ }
    return null
  }

  setPosition(fen, {orientation, highlights = [], animated = false, label = "Board position"} = {}) {
    const targetFen = parsePosition(this.Chess, fen, label).fen()
    return this._enqueue(async () => {
      const previousFen = this.lastVerifiedFen
      this._clearMarkersDirect()
      let recovered = false
      let firstError
      try {
        await this._setAndVerify(targetFen, {orientation, animated, label})
      } catch (err) {
        firstError = err
        try {
          // Retry without animation; the board model is reset as one complete FEN, then checked.
          await this._setAndVerify(targetFen, {orientation, animated: false, label})
          recovered = true
        } catch (retryError) {
          await this._restoreAfterFailure([previousFen, this.fallbackFen])
          throw new BoardStateError(`${label} could not be applied and verified. ${retryError.message || firstError.message}`,
            {firstError, retryError, targetFen, restoredFen: this.lastVerifiedFen})
        }
      }
      this.lastVerifiedFen = targetFen
      try {
        this._applyMarkersDirect(highlights, targetFen)
        assertBoardPosition(this.board, this.Chess, targetFen, label)
      } catch (err) {
        this._clearMarkersDirect()
        await this._restoreAfterFailure([previousFen, this.fallbackFen])
        throw new BoardStateError(`${label} was set, but its highlights could not be safely applied.`,
          {cause: err, targetFen, restoredFen: this.lastVerifiedFen})
      }
      return {fen: targetFen, recovered}
    })
  }

  async _settleCurrentAnimation() {
    if (!this.board || typeof this.board.setPosition !== "function") return
    const placement = this.board.getPosition()
    if (typeof placement === "string" && placement.trim()) {
      // cm-chessboard queues a no-op position change behind any in-flight piece animation.
      await this.board.setPosition(placement, false)
    }
  }

  addMarkers(markers, expectedFen = this.currentFen(), label = "Board highlight") {
    if (!expectedFen) return Promise.reject(new BoardStateError(`${label} has no verified position.`))
    const fen = parsePosition(this.Chess, expectedFen, label).fen()
    return this._enqueue(async () => {
      await this._settleCurrentAnimation()
      if (!this.matches(fen)) throw new BoardStateError(`${label} does not match the last verified position.`)
      this._applyMarkersDirect(markers, fen)
      return true
    })
  }

  addPieceMarker(marker, square, expectedPiece, expectedFen = this.currentFen(), label = "Piece highlight") {
    if (!expectedFen) return Promise.reject(new BoardStateError(`${label} has no verified position.`))
    const fen = parsePosition(this.Chess, expectedFen, label).fen()
    return this._enqueue(async () => {
      await this._settleCurrentAnimation()
      assertBoardPosition(this.board, this.Chess, fen, label)
      if (!this.isPieceAt(square, expectedPiece, fen)) {
        throw new BoardStateError(`${label} skipped: the expected piece is not on ${square}.`, {square, expectedPiece})
      }
      this._applyMarkersDirect([{marker, square, piece: expectedPiece}], fen)
      return true
    })
  }

  clearMarkers(expectedFen = null) {
    return this._enqueue(async () => {
      await this._settleCurrentAnimation()
      if (expectedFen && !this.matches(expectedFen)) {
        throw new BoardStateError("Clear board markers does not match the last verified position.")
      }
      this._clearMarkersDirect()
      return true
    })
  }

  removeMarkers(marker, expectedFen = null) {
    return this._enqueue(async () => {
      await this._settleCurrentAnimation()
      if (expectedFen && !this.matches(expectedFen)) {
        throw new BoardStateError("Remove board markers does not match the last verified position.")
      }
      if (this.board.removeMarkers) this.board.removeMarkers(marker)
      return true
    })
  }

  move(fenBefore, uci, {expectedPiece = null, animated = true, label = "Demonstrated move"} = {}) {
    const transition = computePositionAfterMove(this.Chess, fenBefore, uci, {expectedPiece, label})
    return this._enqueue(async () => {
      if (!this.matches(transition.beforeFen)) {
        throw new BoardStateError(`${label} refused: the board does not match the last verified starting position.`)
      }
      const actualSource = this.pieceAt(transition.from)
      if (actualSource !== transition.piece || !matchesPiece(actualSource, expectedPiece)) {
        throw new BoardStateError(`${label} refused: the expected piece is not on ${transition.from}.`,
          {square: transition.from, expectedPiece: transition.piece, actualPiece: actualSource})
      }
      const previousFen = this.lastVerifiedFen || transition.beforeFen
      let firstError
      try {
        await this._setAndVerify(transition.afterFen, {animated, label: `${label} result`})
      } catch (err) {
        firstError = err
        try {
          await this._setAndVerify(transition.afterFen, {animated: false, label: `${label} result`})
        } catch (retryError) {
          await this._restoreAfterFailure([transition.beforeFen, previousFen, this.fallbackFen])
          throw new BoardStateError(`${label} could not be completed and verified. ${retryError.message || firstError.message}`,
            {firstError, retryError, transition})
        }
      }
      this.lastVerifiedFen = transition.afterFen
      return {...transition, recovered: Boolean(firstError)}
    })
  }

  acknowledgeExternalMove(fenBefore, uci, {expectedPiece = null, label = "Board move"} = {}) {
    const transition = computePositionAfterMove(this.Chess, fenBefore, uci, {expectedPiece, label})
    return this._enqueue(async () => {
      // The board widget has already performed the drag, so its rendered position is expected to
      // differ from the last verified FEN. Still require that the drag began from that verified
      // legal state before acknowledging the result.
      if (!this.lastVerifiedFen ||
          positionIdentity(this.Chess, this.lastVerifiedFen) !== positionIdentity(this.Chess, transition.beforeFen)) {
        const restoredFen = await this._restoreAfterFailure([this.lastVerifiedFen, this.fallbackFen])
        throw new BoardStateError(`${label} was not based on the last verified position; the board was restored.`,
          {transition, restoredFen})
      }
      // Confirm the complete resulting position before the application sends the move to the backend
      // or shows feedback.
      let modelMatches = false
      try {
        const actual = pieceMapFromPlacement(this.board.getPosition())
        const expected = pieceMapFromPlacement(transition.afterFen)
        modelMatches = differences(expected, actual, "position").length === 0
      } catch (_) { modelMatches = false }
      if (!modelMatches) {
        await this._restoreAfterFailure([transition.beforeFen, this.fallbackFen])
        throw new BoardStateError(`${label} was not applied to the board; it has been reset to the verified position.`,
          {transition, restoredFen: this.lastVerifiedFen})
      }
      try {
        await this._settleCurrentAnimation()
        assertBoardPosition(this.board, this.Chess, transition.afterFen, `${label} result`)
      } catch (err) {
        await this._restoreAfterFailure([transition.beforeFen, this.fallbackFen])
        throw new BoardStateError(`${label} result did not match the expected position and was reset.`,
          {cause: err, transition, restoredFen: this.lastVerifiedFen})
      }
      this.lastVerifiedFen = transition.afterFen
      return transition
    })
  }
}
