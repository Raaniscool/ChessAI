// Onboarding form, "what your coach knows" lines, when to offer onboarding.
import test from "node:test"
import assert from "node:assert/strict"
import {onboardingBody, profileLines, wantsOnboarding} from "../coach.js"

test("onboarding: a rating, or a description, and nothing else is required", () => {
  assert.deepEqual(onboardingBody({rating: "1250", platform: "lichess", goals: ["tactics"]}).body,
    {explanation: "balanced", goals: ["tactics"], rating: 1250, platform: "lichess"})
  assert.deepEqual(onboardingBody({experience: "casual", explanation: "brief"}).body,
    {explanation: "brief", goals: [], experience: "casual"})
  assert.equal(onboardingBody({rating: "1250"}).body.platform, "chesscom")
  assert.match(onboardingBody({}).error, /rating|description/)
  assert.match(onboardingBody({rating: "12"}).error, /rating/)
  assert.match(onboardingBody({rating: "abc"}).error, /rating/)
})

test("onboarding: the Chess.com username is optional but checked when given", () => {
  assert.equal("username" in onboardingBody({experience: "new", username: "  "}).body, false)
  assert.equal(onboardingBody({experience: "new", username: " hikaru "}).body.username, "hikaru")
  assert.match(onboardingBody({experience: "new", username: "a b"}).error, /username/i)
})

const profile = {
  new: false, level: "intermediate", rating: 1180, rating_source: "games",
  concepts: {fork: {name: "Fork"}, pin: {name: "Pin"}, back_rank_mate: {name: "Back-rank mate"}},
  mastered: ["fork"], practicing: ["pin"], learned: [], weak: ["back_rank_mate"], needs_review: [],
  weaknesses: [{title: "Hanging pieces", tier: "recurring", game_count: 4, total_games: 10},
    {title: "Early queen", tier: "occasional", game_count: 1, total_games: 10}],
  stats: {puzzles: {attempts: 8, solved: 7, first_try: 6}, lessons: {completed: 2}, hints_used: 1, first_try_rate: 0.75},
}

test("coach panel lines say what the coach knows, in words", () => {
  const lines = Object.fromEntries(profileLines(profile).map(l => [l.label, l.text]))
  assert.equal(lines.Level, "Intermediate — about 1180 (from your Chess.com games)")
  assert.equal(lines["Strong at"], "Fork")
  assert.equal(lines.Practising, "Pin")
  assert.equal(lines["Needs work"], "Back-rank mate")
  assert.equal(lines["From your games"], "Hanging pieces (4 of 10 games)")  // one game isn't a pattern
  assert.equal(lines.Practice, "8 positions tried, 75% solved first try, 1 hint used")
  assert.equal(lines.Lessons, "2 completed")
  assert.equal("Due for review" in lines, false)
  const fresh = profileLines({level: "beginner", rating: 800, rating_source: "default", concepts: {}, stats: {}})
  assert.deepEqual(fresh.map(l => l.label), ["Level"])
})

test("onboarding is offered once, only to someone new", () => {
  assert.equal(wantsOnboarding({new: true, onboarding: {done: false}}), true)
  assert.equal(wantsOnboarding({new: true, onboarding: {done: true, skipped: true}}), false)
  assert.equal(wantsOnboarding({new: false, onboarding: {}}), false)  // already learning: don't interrupt
  assert.equal(wantsOnboarding(null), false)
})

// --- the onboarding card in a DOM (jsdom, optional dev dependency) ---------------------------
let JSDOM = null
try { ({JSDOM} = await import("jsdom")) } catch (_) { /* optional */ }

async function onboardingDom() {
  const dom = new JSDOM(`<!doctype html><body><div id="messages"><div id="welcome"></div></div>
    <aside id="coach-panel" hidden><div class="panel-body"></div></aside>
    <aside id="settings-panel" hidden><div class="panel-body"></div></aside>
    <input id="ga-username"></body>`, {url: "http://localhost/"})
  for (const k of ["window", "document", "localStorage"]) globalThis[k] = dom.window[k]
  const {setupCoach} = await import("../coach.js")
  const calls = [], analyzed = [], msgs = []
  const profile = {new: true, onboarding: {done: false}, rating: 800, level: "beginner", concepts: {},
    preferences: {explanation: "balanced", tts: {}}}
  const api = async (url, method, body) => {
    calls.push({url, method, body})
    if (!method) return {profile, suggestions: []}  // GET /api/profile: someone new
    return {profile: {...profile, new: false, onboarding: {done: true}, rating: body.rating || 800}, suggestions: []}
  }
  const addMsg = text => { const d = dom.window.document.createElement("div"); d.textContent = text
    dom.window.document.getElementById("messages").appendChild(d); msgs.push(d); return d }
  const coach = setupCoach({api, narrator: {auto() {}}, requestPlan() {}, addMsg, escapeHtml: s => s,
    messagesEl: dom.window.document.getElementById("messages"), analyzeGames: u => analyzed.push(u)})
  return {dom, coach, calls, analyzed, msgs}
}

test("onboarding with a Chess.com username offers to analyze the games in one click", {skip: !JSDOM}, async () => {
  const {dom, coach, calls, analyzed, msgs} = await onboardingDom()
  assert.equal(await coach.offerOnboarding(), true)
  const card = dom.window.document.querySelector(".onboarding-card")
  card.querySelector(".onb-rating").value = "1300"
  card.querySelector(".onb-username").value = "magnus_fan"
  card.querySelector(".onb-save").click()
  await new Promise(r => setTimeout(r, 10))
  assert.equal(calls.at(-1).url, "/api/profile/onboarding")
  assert.equal(calls.at(-1).body.username, "magnus_fan")
  assert.equal(dom.window.document.getElementById("ga-username").value, "magnus_fan")
  const go = msgs.at(-1).querySelector(".onb-analyze")
  assert.ok(go, "a one-click offer to analyze the games")
  go.click()
  assert.deepEqual(analyzed, ["magnus_fan"])
  assert.equal(go.disabled, true)
})

test("onboarding without a username works and offers no game analysis", {skip: !JSDOM}, async () => {
  const {dom, coach, calls, msgs} = await onboardingDom()
  await coach.offerOnboarding()
  const card = dom.window.document.querySelector(".onboarding-card")
  card.querySelector(".onb-rating").value = "900"
  card.querySelector(".onb-save").click()
  await new Promise(r => setTimeout(r, 10))
  assert.equal(calls.at(-1).body.username, undefined)
  assert.equal(msgs.at(-1).querySelector(".onb-analyze"), null)
  assert.match(msgs.at(-1).textContent, /start around/)
})
