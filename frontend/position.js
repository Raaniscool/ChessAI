// Strict position loading shared by the boards. chess.js treats `new Chess(undefined)` as a
// request for the standard starting position, which can hide a missing FEN in an API response.
// Reject absent/invalid FENs instead of silently showing the start (or an empty board).
const EMPTY_PLACEMENT = "8/8/8/8/8/8/8/8"

export function parsePosition(Chess, fen, label = "Position") {
  if (typeof fen !== "string" || !fen.trim()) {
    throw new Error(`${label} is missing its FEN position.`)
  }
  let chess
  try {
    chess = new Chess(fen.trim())
  } catch (_) {
    throw new Error(`${label} has an invalid FEN position.`)
  }
  // chess.js returns an empty board instead of throwing when a FEN is invalid.
  if (!chess || chess.fen().split(/\s+/)[0] === EMPTY_PLACEMENT) {
    throw new Error(`${label} has an invalid FEN position.`)
  }
  return chess
}

export function isStartingPlacement(chess, Chess) {
  if (!chess || typeof chess.fen !== "function") return false
  return chess.fen().split(/\s+/)[0] === new Chess().fen().split(/\s+/)[0]
}

export function parsePuzzlePosition(Chess, fen, label = "Puzzle position") {
  const chess = parsePosition(Chess, fen, label)
  if (isStartingPlacement(chess, Chess)) {
    throw new Error(`${label} is the standard starting board, not a puzzle position.`)
  }
  return chess
}
