// Pure helpers for the Game Analysis screen (no DOM): easy to unit-test.
// Everything shown here comes from the server's analysis; nothing is computed
// about the chess itself in the browser.

const RESULT_WORDS = {win: "Won", loss: "Lost", draw: "Draw", unfinished: "Unfinished"}

export function resultLabel(summary) {
  if (RESULT_WORDS[summary.learner_result]) return RESULT_WORDS[summary.learner_result]
  return summary.result === "*" ? RESULT_WORDS.unfinished : summary.result || ""
}

export function colorWord(color) {
  return color === "black" ? "Black" : "White"
}

// "You (White) vs knightmare42"
export function matchup(summary) {
  return `You (${colorWord(summary.player_color)}) vs ${summary.opponent || "?"}`
}

// "Lost · Italian Game · 10 min · 2026-09-20"
export function gameMeta(summary) {
  return [resultLabel(summary), summary.opening, summary.time_label, summary.date].filter(Boolean).join(" · ")
}

// How the analysis went, in beginner words: "2 big mistakes", "no big mistakes".
export function analysisLine(analysis) {
  if (!analysis) return "not analyzed yet"
  const big = (analysis.blunders || 0) + (analysis.mistakes || 0)
  if (!big) return analysis.habits ? "no big mistakes · an opening habit to fix" : "no big mistakes 🎉"
  const parts = [`${big} big mistake${big === 1 ? "" : "s"}`]
  if (analysis.moments < big) parts.push(`${analysis.moments} worth reviewing`)
  return parts.join(" · ")
}

// Engine score (White's view, {kind, value}) -> the learner's view as text.
// Mates are words, never huge pawn numbers.
export function evalForLearner(score, learnerColor) {
  if (!score) return "?"
  const sign = learnerColor === "black" ? -1 : 1
  if (score.kind === "checkmate") return score.value * sign > 0 ? "you gave checkmate" : "you were checkmated"
  if (score.kind === "mate") {
    const n = score.value * sign
    return n > 0 ? `you can force mate in ${n}` : `your opponent can force mate in ${-n}`
  }
  const pawns = (score.value * sign) / 100
  if (Math.abs(pawns) < 0.3) return "about equal"
  const size = Math.abs(pawns) >= 3 ? "clearly" : Math.abs(pawns) >= 1 ? "" : "slightly"
  const who = pawns > 0 ? "you are" : "your opponent is"
  return `${who} ${size ? size + " " : ""}better (${pawns > 0 ? "+" : ""}${pawns.toFixed(1)})`
}

export function progressText(ev) {
  const pct = ev.total ? Math.round((100 * ev.done) / ev.total) : 0
  const which = ev.count > 1 ? `game ${ev.index} of ${ev.count}` : "your game"
  return {text: `Stockfish is analyzing ${which}… ${pct}%`, pct}
}

// Moments to step through: mistakes in game order, then opening habits.
export function reviewItems(analysis) {
  if (!analysis) return []
  return [...(analysis.moments || []), ...(analysis.habits || [])]
}

// "Missed knight fork — in 2 of your 4 games (3 times)"
export function weaknessLine(w) {
  const text = w.description || ""
  const prefix = `${w.title}: `
  const rest = text.startsWith(prefix) ? text.slice(prefix.length) : text
  return rest ? rest[0].toUpperCase() + rest.slice(1) : w.title
}

export function uciSquares(uci) {
  return uci && uci.length >= 4 ? [uci.slice(0, 2), uci.slice(2, 4)] : []
}
