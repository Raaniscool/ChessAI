// Puzzle solving rules (Lichess-style), independent of the board and the DOM.
//
// A puzzle (from POST /api/puzzles/set) has steps, one per learner move:
//   {uci, san, kind: "critical"|"forced"|"open", accepted: [uci], good: [uci], reply_uci, reply_san}
// Judging a learner move:
//   - any checkmate                      -> solved (a mate is never wrong)
//   - the solution move                  -> correct; the opponent's reply follows, or solved at the end
//   - a verified accepted alternative    -> solved (the stored line no longer applies, so it ends here)
//   - a "good" move (engine: about as good / a slightly slower win)
//       open step and no decision left   -> solved
//       otherwise                        -> "good, but there's a stronger move": take it back, no penalty
//   - anything else                      -> wrong: take it back; the puzzle counts as failed, retry allowed

// "e7e8" from the board equals "e7e8q" (promotion is to a queen unless stated otherwise).
export function sameMove(a, b) {
  if (!a || !b) return false
  const norm = m => (m.length === 5 && m[4].toLowerCase() === "q" ? m.slice(0, 4) : m).toLowerCase()
  return norm(a) === norm(b)
}

export function newAttempt(puzzle, now = Date.now()) {
  return {id: puzzle.id, index: 0, mistakes: 0, hints: 0, hintLevel: 0, failed: false, revealed: false,
    done: false, solved: false, criticalFirstTry: true, started: now, played: [], recorded: false}
}

function decisionsLeft(puzzle, fromIndex) {
  return puzzle.steps.slice(fromIndex).some(s => s.kind === "critical")
}

// -> {verdict, done, reply, message}; mutates `attempt`.
export function judge(puzzle, attempt, uci, {mate = false} = {}) {
  if (attempt.done) return {verdict: "done", done: true, reply: null, message: ""}
  const step = puzzle.steps[attempt.index]
  const last = attempt.index === puzzle.steps.length - 1
  const finish = (verdict, message) => {
    attempt.done = true
    attempt.solved = !attempt.failed && !attempt.revealed
    attempt.played.push(uci)
    return {verdict, done: true, reply: null, message}
  }
  if (mate) return finish("mate", "Checkmate!")
  if (sameMove(uci, step.uci)) {
    if (last) return finish("correct", "Solved!")
    attempt.played.push(uci)
    attempt.hintLevel = 0
    attempt.index += 1
    return {verdict: "correct", done: false, reply: step.reply_uci, message: "Best move!"}
  }
  if ((step.accepted || []).some(m => sameMove(m, uci))) return finish("accepted", "That works too — solved!")
  if ((step.good || []).some(m => sameMove(m, uci))) {
    if (step.kind === "open" && !decisionsLeft(puzzle, attempt.index + 1)) return finish("accepted", "Good move — solved!")
    return {verdict: "good", done: false, reply: null, message: "Good move, but there's a stronger one. Try again."}
  }
  attempt.mistakes += 1
  attempt.failed = true
  if (step.kind === "critical") attempt.criticalFirstTry = false
  return {verdict: "wrong", done: false, reply: null, message: "✗ Not the move. Try again."}
}

// Hint ladder: 1 = the idea (concept hint), 2 = which piece to move (from-square).
export function nextHint(puzzle, attempt) {
  if (attempt.done) return null
  const step = puzzle.steps[attempt.index]
  if (attempt.hintLevel >= 2) return {level: 2, text: "Move the highlighted piece.", square: step.uci.slice(0, 2)}
  attempt.hintLevel += 1
  attempt.hints += 1
  if (attempt.hintLevel === 1) return {level: 1, text: puzzle.hint || `Look for a ${String(puzzle.concept_name || "").toLowerCase()}.`}
  return {level: 2, text: "Move the highlighted piece.", square: step.uci.slice(0, 2)}
}

// The moves still to be shown when the learner asks for the solution (learner + replies, as uci).
export function remainingLine(puzzle, attempt) {
  const out = []
  puzzle.steps.slice(attempt.index).forEach((s, i, rest) => {
    out.push(s.uci)
    if (s.reply_uci && i < rest.length - 1) out.push(s.reply_uci)
  })
  return out
}

export function reveal(puzzle, attempt) {
  const line = remainingLine(puzzle, attempt)
  attempt.revealed = true
  attempt.failed = true
  attempt.done = true
  attempt.solved = false
  return line
}

export function retry(puzzle, attempt) {
  // back to the start; the result already counts as failed if a mistake was made
  attempt.index = 0
  attempt.done = false
  attempt.hintLevel = 0
  attempt.played = []
  return attempt
}

// Whether there is something worth recording (an untouched skipped puzzle isn't a result).
export function shouldRecord(attempt) {
  return !attempt.recorded && (attempt.done || attempt.mistakes > 0 || attempt.hints > 0)
}

export function resultBody(attempt, now = Date.now()) {
  return {solved: Boolean(attempt.solved && !attempt.failed && !attempt.revealed),
    first_try: attempt.mistakes === 0, critical_first_try: attempt.criticalFirstTry,
    mistakes: attempt.mistakes, hints: attempt.hints, revealed: attempt.revealed,
    seconds: Math.max(0, Math.round((now - attempt.started) / 100) / 10)}
}

export const DIFFICULTY_LABEL = {1: "Easy", 2: "Moderate", 3: "Challenging", 4: "Hard", 5: "Very hard"}

// What the solver shows above the board. Every set names its idea as before — the weakness in a
// personalized set, the chosen theme in Practice — except Mixed practice (`mixed`), whose point
// is to recognise the idea yourself: there, before the puzzle is over, only neutral information
// (side to move, "find the best move", difficulty, progress) is shown, and the concept ("Queen
// fork"), the real objective ("Mate in 2") and the named "why" (puzzle.reveal) appear once the
// puzzle is solved, failed or revealed (`done`).
// `continuous`: an open-ended session (no total): "Puzzle 7" instead of "7/10".
export function header(puzzle, position, total, done = false, {mixed = false, continuous = false} = {}) {
  const open = done || !mixed
  const reveal = (open && puzzle.reveal) || {}
  return {
    side: puzzle.side === "black" ? "Black to move" : "White to move",
    objective: reveal.objective_text || (open && puzzle.objective_text) || "Find the best move",
    concept: open ? reveal.concept_name || puzzle.concept_name || "" : "",
    difficulty: DIFFICULTY_LABEL[puzzle.difficulty] || "",
    progress: continuous ? `Puzzle ${position + 1}` : total > 1 ? `${position + 1}/${total}` : "",
    role: reveal.role_label || puzzle.role_label || "",
    why: reveal.why || puzzle.why || "",
  }
}

// The set list names an item by what it is for ("Your game", "Harder") or by its concept; in
// Mixed practice the concept only once that puzzle has a result.
export function itemLabel(puzzle, result = null, {mixed = false} = {}) {
  const reveal = puzzle.reveal || {}
  if (result || !mixed) return reveal.role_label || puzzle.role_label || reveal.concept_name || puzzle.concept_name || "Puzzle"
  return puzzle.role_label || "Puzzle"
}

// Continuous sessions: after a puzzle the next one loads by itself — a short pause after a clean
// solve, a longer one to look at the solution after a miss (any interaction cancels it).
export const AUTO_NEXT_SOLVED = 2500
export const AUTO_NEXT_MISSED = 6000
export function autoNextDelay(attempt) {
  return attempt.solved && !attempt.failed && !attempt.revealed ? AUTO_NEXT_SOLVED : AUTO_NEXT_MISSED
}

// Fetch more puzzles while fewer than this many unopened ones are left in the session.
export const PREFETCH_BELOW = 2
export function needsMore(set, results, pos) {
  return pendingIds(set, results, pos).length < PREFETCH_BELOW
}

// One line under a weakness card: the next recognition stage (puzzles.progression), if any.
export function focusLine(card) {
  const f = card && card.focus
  return f && f.note ? `Next: ${f.label} — ${f.note}` : ""
}

// Dashboard text helpers.
export function cardLine(card) {
  return [card.evidence, card.progress].filter(Boolean).join(" · ")
}

// Session adaptation (POST /api/puzzles/adapt): the puzzles still ahead, and the set after the
// server swapped some of them. Only unopened puzzles after the current one are ever replaced.
export function pendingIds(set, results, pos) {
  return set.slice(pos + 1).filter(p => !results[p.id]).map(p => p.id)
}

export function applyAdapt(set, results, pos, replace) {
  const ahead = new Set(pendingIds(set, results, pos))
  let changed = 0
  const next = set.map(p => {
    const fresh = replace && replace[p.id]
    if (!fresh || !ahead.has(p.id)) return p
    changed++
    return fresh
  })
  return {set: next, changed}
}
