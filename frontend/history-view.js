// Pure helpers for the Game History panel (no DOM): what the words and numbers say.
// The server decides every pattern, tier and score; this only formats them.

export const PRESET_COUNTS = [10, 25, 50, 100]
export const FIRST_ANALYSIS = 25  // the suggested first analysis: "Analyze my last 25 games"
export const MIN_COUNT = 10
export const MAX_COUNT = 100

// "custom" + a typed number -> {count} or {error}
export function parseCount(choice, custom) {
  const raw = choice === "custom" ? String(custom ?? "").trim() : String(choice ?? "")
  if (!/^\d+$/.test(raw)) return {error: "Type a whole number of games."}
  const count = Number(raw)
  if (count < MIN_COUNT) return {error: `Choose at least ${MIN_COUNT} games: fewer isn't enough to find patterns.`}
  if (count > MAX_COUNT) return {error: `Choose at most ${MAX_COUNT} games.`}
  return {count}
}

export const TIER_HEADINGS = {
  recurring: "Patterns that keep showing up",
  occasional: "Seen more than once (not a pattern yet)",
  one_time: "One-off mistakes (happened in only one game)",
}

// A small picture per concept family; plain text only, so it reads well aloud (icons are aria-hidden).
const ICONS = [
  [/knight_fork/, "♞"], [/fork/, "⑂"], [/pin|skewer|discovered/, "📌"], [/mate|checkmate/, "♚"],
  [/hung_piece|hanging|trapped|poisoned/, "⚠"], [/king_safety|castl/, "🛡"], [/early_queen/, "♛"],
  [/development|repeated_moves/, "⏱"], [/endgame/, "♙"], [/threat|check/, "⚡"],
]

export function patternIcon(p) {
  const key = `${p.key || ""} ${p.concept || ""} ${(p.motifs || []).join(" ")}`
  for (const [re, icon] of ICONS) if (re.test(key)) return icon
  return "•"
}

// "Found in 4 of 10 games" / "Found in all 10 games" / "Found in 1 game"
export function foundIn(p) {
  if (p.game_count === 1) return "Found in 1 game"
  if (p.game_count === p.total_games) return p.total_games === 2 ? "Found in both games" : `Found in all ${p.total_games} games`
  return `Found in ${p.game_count} of ${p.total_games} games`
}

const SEVERITY_WORDS = {blunder: ["blunder", "blunders"], mistake: ["mistake", "mistakes"],
  inaccurate: ["inaccuracy", "inaccuracies"], habit: ["opening habit", "opening habits"]}

// "4 blunders, 1 mistake · 6 times"
export function patternDetail(p) {
  const parts = Object.entries(p.severity_counts || {})
    .map(([s, n]) => `${n} ${(SEVERITY_WORDS[s] || [s, s])[n === 1 ? 0 : 1]}`)
  let text = parts.join(", ")
  if (p.occurrences > p.game_count) text += `${text ? " · " : ""}${p.occurrences} times`
  if (p.distinct_positions > 1 && p.game_count > 1) text += ` · ${p.distinct_positions} different positions`
  return text
}

export function practiceLabel(p) {
  const name = p.concept_name || p.title.replace(/^Missed /, "")
  return `▶ Practice ${name[0].toUpperCase()}${name.slice(1)}`
}

// "3 won · 5 lost · 2 drawn"
export function resultsLine(results) {
  const parts = [["win", "won"], ["loss", "lost"], ["draw", "drawn"]]
    .filter(([k]) => results[k]).map(([k, w]) => `${results[k]} ${w}`)
  if (results.unfinished) parts.push(`${results.unfinished} unfinished`)
  return parts.join(" · ") || "no results"
}

// One evidence row: "vs knightmare42 · move 12: you played Kd2, Stockfish's move Nc7+"
export function evidenceLine(e) {
  const who = e.opponent ? `vs ${e.opponent}` : "a game"
  const date = e.date ? ` (${e.date})` : ""
  const best = e.best_move ? `, Stockfish's move ${e.best_move}` : ""
  return `${who}${date} · move ${e.move_number}: you played ${e.san}${best}`
}

// Progress of a history batch: the batch (index/count) and the game being analyzed (done/total).
export function historyProgress(ev) {
  const count = ev.count || 1
  const inGame = ev.total ? ev.done / ev.total : 0
  const pct = Math.round((100 * ((ev.index || 1) - 1 + inGame)) / count)
  return {text: `Stockfish is analyzing game ${ev.index || 1} of ${count}… ${pct}%`, pct}
}

// What the "select" event means for the learner.
export function selectionText(ev) {
  if (!ev.selected.length) return "No games to analyze yet: import your Chess.com games first."
  const using = ev.selected.length < ev.requested
    ? `Only ${ev.available} game${ev.available === 1 ? " is" : "s are"} imported, so I'm using ${ev.selected.length === 1 ? "it" : "those"}.`
    : `Your last ${ev.selected.length} games.`
  if (!ev.to_analyze) return `${using} All of them are already analyzed.`
  const cached = ev.cached ? ` (${ev.cached} already done)` : ""
  return `${using} Analyzing ${ev.to_analyze} game${ev.to_analyze === 1 ? "" : "s"}${cached}…`
}

// Status line while new puzzles are generated (POST /api/games/puzzles, NDJSON events).
export function puzzleStatus(ev, made = 0) {
  if (ev.type === "status") return ev.text
  if (ev.type === "puzzle") return `${made} new puzzle${made === 1 ? "" : "s"} checked by Stockfish…`
  if (ev.type === "done") {
    const lib = ev.library || 0
    const n = lib + ev.new + ev.reused
    const parts = []
    if (lib) parts.push(`${lib} from the puzzle library`)
    if (ev.reused) parts.push(`${ev.reused} made for you earlier`)
    if (ev.new) parts.push(`${ev.new} built just now`)
    const ready = `${n} puzzle${n === 1 ? "" : "s"} ready`
    if (!lib) return `${ready} (new positions, not from your games).`
    return `${ready}${parts.length > 1 ? `: ${parts.join(", ")}` : " from the puzzle library"} — none of them from your games.`
  }
  if (ev.type === "error") return ev.error
  return ""
}

// "Your Training": turn the report into one clear next step. The main weakness is the
// highest-ranked pattern the server found in more than one game (the server ranks by tier, then
// frequency × severity); a pattern seen in only 2 games is offered as "possible", never as a verdict.
export const TRAINING_PUZZLES = 5

export function trainingPlan(r) {
  if (!r || !r.games_analyzed) return null
  const ranked = (r.patterns || []).filter(p => p.tier === "recurring" || p.tier === "occasional")
  const n = r.games_analyzed
  if (!ranked.length) {
    return {main: null, text: `No mistake came back in more than one of your ${n} analyzed game${n === 1 ? "" : "s"}, ` +
      "so there's nothing specific to fix yet. Analyze more games and I'll look again."}
  }
  const [main, ...others] = ranked
  return {
    main,
    heading: main.tier === "recurring" ? "Main weakness" : "Possible pattern (not confirmed yet)",
    found: `Found in ${main.game_count} of your ${n} analyzed game${n === 1 ? "" : "s"}.`,
    puzzles: main.new_puzzles ? TRAINING_PUZZLES : 0,
    lesson: `${main.title}: verified examples first, then positions from your own games`,
    others: others.slice(0, 3).map(p => ({key: p.key, title: p.title, found: `${p.game_count} of ${n} games`, tier: p.tier})),
  }
}
