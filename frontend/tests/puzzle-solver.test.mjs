// Puzzle solving rules: solution + reply, mates, accepted/good moves, mistakes, hints, results.
import test from "node:test"
import assert from "node:assert/strict"
import {AUTO_NEXT_MISSED, AUTO_NEXT_SOLVED, applyAdapt, autoNextDelay, needsMore, pendingIds, cardLine, focusLine, header, itemLabel, judge, newAttempt, nextHint, remainingLine, resultBody, retry, reveal, sameMove, shouldRecord}
  from "../puzzle-solver.js"

// 1.Nc7+ (critical) Kd7 2.Nxa8 (forced)
const FORK = {id: "f", side: "white", concept_name: "Knight fork", hint: "Your knight can give check.",
  objective_text: "Find the best move", difficulty: 2,
  reveal: {concept_name: "Knight fork", objective_text: "Win material", role_label: null, why: null},
  steps: [{uci: "b5c7", san: "Nc7+", kind: "critical", accepted: [], good: ["b5d6"], reply_uci: "e8d7", reply_san: "Kd7"},
    {uci: "c7a8", san: "Nxa8", kind: "forced", accepted: [], good: [], reply_uci: null, reply_san: null}]}

test("the solution move gets the opponent's reply, then the puzzle ends once the target is won", () => {
  const a = newAttempt(FORK, 0)
  const r1 = judge(FORK, a, "b5c7")
  assert.equal(r1.verdict, "correct"); assert.equal(r1.reply, "e8d7"); assert.equal(r1.done, false)
  const r2 = judge(FORK, a, "c7a8")
  assert.equal(r2.verdict, "correct"); assert.equal(r2.done, true); assert.equal(a.solved, true)
  assert.deepEqual(resultBody(a, 12000), {solved: true, first_try: true, critical_first_try: true, mistakes: 0,
    hints: 0, revealed: false, seconds: 12})
})

test("a wrong move is taken back, fails the puzzle and can be retried", () => {
  const a = newAttempt(FORK, 0)
  const r = judge(FORK, a, "e1d2")
  assert.equal(r.verdict, "wrong"); assert.match(r.message, /✗/); assert.equal(a.index, 0)
  assert.equal(a.failed, true); assert.equal(a.criticalFirstTry, false)
  judge(FORK, a, "b5c7"); judge(FORK, a, "c7a8")
  assert.equal(a.done, true); assert.equal(a.solved, false)  // finished, but not solved
  assert.equal(resultBody(a).solved, false); assert.equal(resultBody(a).mistakes, 1)
})

test("a good-but-weaker move at a decision is a free retry", () => {
  const a = newAttempt(FORK, 0)
  const r = judge(FORK, a, "b5d6")
  assert.equal(r.verdict, "good"); assert.match(r.message, /stronger/)
  assert.equal(a.failed, false); assert.equal(a.mistakes, 0); assert.equal(a.index, 0)
})

test("any checkmate solves, and verified alternatives solve", () => {
  const a = newAttempt(FORK, 0)
  assert.equal(judge(FORK, a, "h1h8", {mate: true}).verdict, "mate"); assert.equal(a.solved, true)
  const alt = {...FORK, steps: [{...FORK.steps[0], accepted: ["b5d6"], good: []}, FORK.steps[1]]}
  const b = newAttempt(alt, 0)
  assert.equal(judge(alt, b, "b5d6").verdict, "accepted"); assert.equal(b.done, true); assert.equal(b.solved, true)
})

test("an equally good move at an open step solves when no decision is left", () => {
  const open = {...FORK, steps: [{...FORK.steps[0]}, {...FORK.steps[1], kind: "open", good: ["c7e6"]}]}
  const a = newAttempt(open, 0)
  judge(open, a, "b5c7")
  assert.equal(judge(open, a, "c7e6").verdict, "accepted"); assert.equal(a.solved, true)
  // ... but not while a real decision still comes later
  const early = {...FORK, steps: [{...FORK.steps[0], kind: "open", good: ["b5d6"]},
    {...FORK.steps[1], kind: "critical"}]}
  assert.equal(judge(early, newAttempt(early, 0), "b5d6").verdict, "good")
})

test("hints: the idea first, then the piece; they count", () => {
  const a = newAttempt(FORK, 0)
  assert.deepEqual(nextHint(FORK, a), {level: 1, text: "Your knight can give check."})
  assert.deepEqual(nextHint(FORK, a), {level: 2, text: "Move the highlighted piece.", square: "b5"})
  nextHint(FORK, a)
  assert.equal(a.hints, 2)  // asking again doesn't count twice
  assert.equal(resultBody(a).hints, 2)
})

test("show solution plays the rest of the line and counts as revealed", () => {
  const a = newAttempt(FORK, 0)
  assert.deepEqual(remainingLine(FORK, a), ["b5c7", "e8d7", "c7a8"])
  judge(FORK, a, "b5c7")
  assert.deepEqual(reveal(FORK, a), ["c7a8"])
  assert.equal(a.revealed, true); assert.equal(resultBody(a).solved, false); assert.equal(resultBody(a).revealed, true)
  retry(FORK, a)
  assert.equal(a.index, 0); assert.equal(a.done, false)
})

test("what gets recorded", () => {
  const a = newAttempt(FORK, 0)
  assert.equal(shouldRecord(a), false)      // skipped without trying
  judge(FORK, a, "e1d2")
  assert.equal(shouldRecord(a), true)       // a mistake is a result
  a.recorded = true
  assert.equal(shouldRecord(a), false)      // once
})

test("promotion and header text", () => {
  assert.ok(sameMove("e7e8", "e7e8q")); assert.ok(!sameMove("e7e8n", "e7e8q")); assert.ok(sameMove("e7e8N", "e7e8n"))
  // a theme set names its idea from the start (the learner chose it)
  assert.deepEqual(header(FORK, 1, 5), {side: "White to move", objective: "Win material", concept: "Knight fork",
    difficulty: "Moderate", progress: "2/5", role: "", why: ""})
  // Mixed practice, while solving: nothing that names the idea
  assert.deepEqual(header(FORK, 1, 5, false, {mixed: true}), {side: "White to move", objective: "Find the best move",
    concept: "", difficulty: "Moderate", progress: "2/5", role: "", why: ""})
  assert.equal(header(FORK, 6, 7, false, {continuous: true}).progress, "Puzzle 7")   // an open-ended session
  // once a Mixed puzzle is over: the theme and the real objective
  assert.deepEqual(header(FORK, 1, 5, true, {mixed: true}), {side: "White to move", objective: "Win material", concept: "Knight fork",
    difficulty: "Moderate", progress: "2/5", role: "", why: ""})
  assert.equal(cardLine({evidence: "In 3 of your last 10 analyzed games", progress: null}),
    "In 3 of your last 10 analyzed games")
})

test("personalized items say what they are for and why, in one line", () => {
  const mine = {...FORK, role: "your_game", role_label: "Your game",
    why: "Your own game vs alice — you played 30.Kd2 here. Find what you missed."}
  const h = header(mine, 0, 5)
  assert.equal(h.role, "Your game")
  assert.match(h.why, /Your own game vs alice/)
  assert.equal(itemLabel(mine), "Your game")
  assert.equal(itemLabel(FORK), "Knight fork")                        // theme sets: the concept
  assert.equal(itemLabel(FORK, null, {mixed: true}), "Puzzle")       // Mixed: no concept before solving...
  assert.equal(itemLabel(FORK, "solved", {mixed: true}), "Knight fork")  // ...the concept once it has a result
  assert.equal(itemLabel(FORK, "failed", {mixed: true}), "Knight fork")
  assert.equal(focusLine({focus: {label: "Choose among candidates", note: "You solve the obvious ones (3/3 first try)"}}),
    "Next: Choose among candidates — You solve the obvious ones (3/3 first try)")
  assert.equal(focusLine({focus: {label: "Spot it", note: null}}), "")
  assert.equal(focusLine({}), "")
})

test("session adaptation only swaps unopened puzzles after the current one", () => {
  const set = ["a", "b", "c", "d", "e"].map(id => ({id}))
  const results = {a: "solved", b: "solved", d: "failed"}   // d was opened out of order
  assert.deepEqual(pendingIds(set, results, 1), ["c", "e"])
  const {set: next, changed} = applyAdapt(set, results, 1, {a: {id: "x"}, c: {id: "y"}, d: {id: "z"}, e: {id: "w"}})
  assert.equal(changed, 2)
  assert.deepEqual(next.map(p => p.id), ["a", "b", "y", "d", "w"])
  assert.equal(applyAdapt(set, results, 1, {}).changed, 0)
  assert.equal(applyAdapt(set, results, 4, {e: {id: "w"}}).changed, 0)   // nothing after the last
})

test("personalized sets keep naming the identified type before solving", () => {
  const defend = {...FORK, role: "defend", role_label: "Bonus", why: "Chosen for you — it cost you material in 3 recent games.",
    reveal: {concept_name: "Spotting threats", objective_text: "Defend against the threat", role_label: "Defend",
      why: "Walked into a fork: the defensive side — spot the opponent's threat and stop it."}}
  const h = header(defend, 4, 5)
  assert.equal(h.concept, "Spotting threats"); assert.equal(h.role, "Defend")
  assert.equal(h.objective, "Defend against the threat"); assert.match(h.why, /defensive side/)
  assert.equal(itemLabel(defend), "Defend")
})

test("continuous sessions: next puzzle by itself, more fetched before the queue runs out", () => {
  const clean = {...newAttempt(FORK, 0), done: true, solved: true}
  const missed = {...newAttempt(FORK, 0), done: true, solved: false, failed: true}
  const revealed = {...newAttempt(FORK, 0), done: true, revealed: true, failed: true}
  assert.equal(autoNextDelay(clean), AUTO_NEXT_SOLVED)
  assert.equal(autoNextDelay(missed), AUTO_NEXT_MISSED); assert.equal(autoNextDelay(revealed), AUTO_NEXT_MISSED)
  assert.ok(AUTO_NEXT_MISSED > AUTO_NEXT_SOLVED)   // time to look at the solution after a miss
  const set = ["a", "b", "c", "d"].map(id => ({id}))
  assert.equal(needsMore(set, {a: "solved"}, 0), false)            // b, c, d still ahead
  assert.equal(needsMore(set, {a: "solved", b: "solved"}, 2), true)  // only d left: fetch more now
  assert.equal(needsMore(set, {}, 3), true)                          // at the end: nothing queued
})

test("Mixed practice: a defensive item and its set name nothing until solved", () => {
  const defend = {...FORK, role: "defend", role_label: "Bonus", why: "Chosen for you — it cost you material in 3 recent games.",
    reveal: {concept_name: "Spotting threats", objective_text: "Defend against the threat", role_label: "Defend",
      why: "Walked into a fork: the defensive side — spot the opponent's threat and stop it."}}
  const before = header(defend, 4, 5, false, {mixed: true})
  const shown = Object.values(before).join(" ")
  for (const word of ["fork", "Fork", "Defend", "threat", "Spotting"]) assert.ok(!shown.includes(word), word)
  assert.equal(itemLabel(defend, null, {mixed: true}), "Bonus")
  const after = header(defend, 4, 5, true, {mixed: true})
  assert.equal(after.role, "Defend"); assert.equal(after.concept, "Spotting threats")
  assert.equal(after.objective, "Defend against the threat"); assert.match(after.why, /defensive side/)
  assert.equal(itemLabel(defend, "solved", {mixed: true}), "Defend")
  // the hint (asked for) may name the idea: that is what a hint is for
  assert.equal(nextHint({...defend, hint: null}, newAttempt(defend)).text, "Look for a knight fork.")
})
