// Pure helpers for the game picker (no DOM): filtering the downloaded games, describing the
// analysis allowance, and checking a selection against it. The server enforces the allowance;
// this only explains it before the learner presses Analyze.

const RESULTS = {win: "win", loss: "loss", draw: "draw"}

export function ownRating(g) {
  return g.player_color === "white" ? g.white_elo : g.player_color === "black" ? g.black_elo : null
}

export function opponentRating(g) {
  return g.player_color === "white" ? g.black_elo : g.player_color === "black" ? g.white_elo : null
}

// "2026.03.14" / "2026-03-14" -> "2026-03-14" ("" when unknown)
export function isoDate(value) {
  const m = /^(\d{4})[.\-/](\d{2})[.\-/](\d{2})/.exec(String(value || ""))
  return m ? `${m[1]}-${m[2]}-${m[3]}` : ""
}

export const EMPTY_FILTER = Object.freeze({
  text: "", result: "", color: "", timeClass: "", from: "", to: "",
  minRating: "", maxRating: "", minMoves: "", maxMoves: "", analyzed: "",
})

const num = v => (v === "" || v === null || v === undefined || Number.isNaN(Number(v))) ? null : Number(v)

// Every filter is optional; text matches the opponent or the opening; rating = the opponent's.
export function filterGames(games, filter = EMPTY_FILTER) {
  const f = {...EMPTY_FILTER, ...filter}
  const text = f.text.trim().toLowerCase()
  const [minR, maxR, minM, maxM] = [num(f.minRating), num(f.maxRating), num(f.minMoves), num(f.maxMoves)]
  return games.filter(g => {
    if (text && !`${g.opponent || ""} ${g.opening || ""} ${g.eco || ""}`.toLowerCase().includes(text)) return false
    if (f.result && (g.learner_result || "unfinished") !== RESULTS[f.result]) return false
    if (f.color && g.player_color !== f.color) return false
    if (f.timeClass && (g.time_class || "") !== f.timeClass) return false
    const day = isoDate(g.date)
    if (f.from && (!day || day < f.from)) return false
    if (f.to && (!day || day > f.to)) return false
    const opp = opponentRating(g)
    if (minR !== null && !(opp >= minR)) return false
    if (maxR !== null && !(opp <= maxR)) return false
    if (minM !== null && !(g.moves >= minM)) return false
    if (maxM !== null && !(g.moves <= maxM)) return false
    if (f.analyzed === "yes" && !g.analysis) return false
    if (f.analyzed === "no" && g.analysis) return false
    return true
  })
}

export function timeClasses(games) {
  return [...new Set(games.map(g => g.time_class).filter(Boolean))].sort()
}

// What the allowance means right now, in one sentence.
export function quotaLine(q) {
  if (!q) return ""
  if (!q.enabled) return "Game analysis is unlimited on this installation."
  const parts = []
  if (q.initial.left) parts.push(`${q.initial.left} left from your first ${q.initial.allowance}`)
  parts.push(q.daily.left ? `${q.daily.left} of today's ${q.daily.allowance}` : `today's ${q.daily.allowance} used`)
  if (!q.left) return `You've used today's game analyses${q.initial.left ? "" : ` (and your first ${q.initial.allowance})`}. ` +
    "More are available tomorrow. Games you've already analyzed stay available, and lessons and puzzles never run out."
  return `You can analyze ${q.left} more game${q.left === 1 ? "" : "s"} now (${parts.join(" + ")}). ` +
    "Games already analyzed are free to open again."
}

// Only games never analyzed cost allowance (the server applies the same rule).
export function newGames(ids, games) {
  const byId = new Map(games.map(g => [g.id, g]))
  return ids.filter(id => byId.has(id) && !byId.get(id).analysis)
}

export function selectionSummary(ids, games, q) {
  const fresh = newGames(ids, games).length
  const n = ids.length
  if (!n) return {ok: false, fresh, text: "Tick the games you want analyzed."}
  const base = `${n} game${n === 1 ? "" : "s"} selected` + (fresh < n ? ` (${n - fresh} already analyzed)` : "")
  if (q && q.enabled && fresh > q.left) {
    return {ok: false, fresh, text: `${base}. That's ${fresh} new, but ${q.left} ${q.left === 1 ? "is" : "are"} available right now: untick ${fresh - q.left}.`}
  }
  if (!fresh) return {ok: true, fresh, text: `${base}. Nothing new to analyze: the report is free.`}
  return {ok: true, fresh, text: q && q.enabled ? `${base}. Uses ${fresh} of the ${q.left} analyses available now.`
    : `${base}. ${fresh} new to analyze.`}
}

// The most recent `count` games (the list is newest first) that the learner played.
export function lastGames(games, count) {
  return games.filter(g => g.player_color).slice(0, count).map(g => g.id)
}
