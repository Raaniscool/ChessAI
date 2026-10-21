// node --test frontend/tests
import test from "node:test"
import assert from "node:assert/strict"
import {readFileSync} from "node:fs"
import {fileURLToPath} from "node:url"

const here = new URL("../index.html", import.meta.url)
const html = readFileSync(fileURLToPath(here), "utf8")
const app = readFileSync(fileURLToPath(new URL("../app.js", import.meta.url)), "utf8")
const puzzles = readFileSync(fileURLToPath(new URL("../puzzles.js", import.meta.url)), "utf8")
const brilliant = readFileSync(fileURLToPath(new URL("../brilliant-finder.js", import.meta.url)), "utf8")

function buttonMarkup(id) {
  const match = html.match(new RegExp(`<button\\b(?=[^>]*\\bid="${id}")[\\s\\S]*?<\\/button>`))
  assert.ok(match, `expected a button with id ${id}`)
  return match[0]
}

test("primary navigation groups the three existing views as Learn, Practice, and Review", () => {
  const start = html.indexOf('<nav class="tabs primary-nav"')
  const end = html.indexOf("</nav>", start)
  assert.notEqual(start, -1, "primary navigation exists")
  assert.notEqual(end, -1, "primary navigation closes")
  const nav = html.slice(start, end)
  assert.match(nav, /aria-label="Primary navigation"/)
  assert.doesNotMatch(nav, /role="tablist"|role="tab"|aria-selected=/,
    "top-level destinations are navigation links, not a tab widget")
  assert.match(nav, /id="tab-lessons"[\s\S]*?<span class="tab-title">Learn<\/span>[\s\S]*?<span class="tab-description">Lessons<\/span>/)
  assert.match(nav, /id="tab-puzzles"[\s\S]*?<span class="tab-title">Practice<\/span>[\s\S]*?Puzzles &amp; Training/)
  assert.match(nav, /id="tab-games"[\s\S]*?<span class="tab-title">Review<\/span>[\s\S]*?<span class="tab-description">Game Analysis<\/span>/)
  assert.match(buttonMarkup("tab-lessons"), /aria-current="page"/, "the initial view is announced")
  assert.match(buttonMarkup("tab-lessons"), /aria-label="Learn, lessons"/)
  assert.match(buttonMarkup("tab-puzzles"), /aria-label="Practice, puzzles and training"/)
  assert.match(buttonMarkup("tab-games"), /aria-label="Review, game analysis"/)
  for (const id of ["tab-games", "tab-puzzles"]) {
    assert.doesNotMatch(buttonMarkup(id), /aria-current=/, `${id} is not initially current`)
  }
})

test("switching top-level views updates the current-page accessibility state", () => {
  const start = app.indexOf("async function switchView(name)")
  const end = app.indexOf("// ---------- wire up ----------", start)
  assert.notEqual(start, -1, "view switcher exists")
  assert.notEqual(end, -1, "view wiring follows the switcher")
  const switcher = app.slice(start, end)
  assert.match(switcher, /tab\.classList\.toggle\("active", on\)/)
  assert.match(switcher, /if \(on\) tab\.setAttribute\("aria-current", "page"\)\s+else tab\.removeAttribute\("aria-current"\)/)
  assert.doesNotMatch(switcher, /aria-selected=/)
})

test("Brilliant Finder is immediate guided board play with explicit reveal and no submit button", () => {
  assert.match(html, /Play every required move yourself on the board/)
  assert.match(html, /id="pz-brilliant-reveal"[^>]*>Reveal solution/)
  assert.match(html, /id="pz-brilliant-retry"[^>]*>Retry this position/)
  assert.doesNotMatch(html, /pz-brilliant-submit|Submit move|Submit this move/)
  assert.match(brilliant, /api\(apiPath\(task, "move"\), "POST"/)
  assert.match(brilliant, /verifiedMove\(player\.after_fen, response\.opponent_move\.uci/)
  assert.match(brilliant, /response\.outcome === "incorrect"|\["incorrect", "playable", "unverified_alternative"\]/)
  assert.match(brilliant, /apiPath\(task, "reveal"\)/)
  assert.match(puzzles, /createBrilliantFinder\(\{api, board, showPosition, verifiedMove, isPositionVerified/)
})

test("puzzle modes and board move-history navigation remain distinct from app navigation", () => {
  assert.match(html, /<div class="pz-modes" role="tablist" aria-label="Puzzle mode">/)
  for (const id of ["pz-mode-personal", "pz-mode-practice", "pz-mode-training", "pz-mode-decision"]) {
    assert.match(html, new RegExp(`id="${id}"`))
  }
  assert.match(html, /id="pz-decision" class="pz-section hidden"/)
  assert.match(html, /id="pz-decision-answer"[\s\S]*?maxlength="2000"/)
  for (const id of ["pz-decision-yes", "pz-decision-no", "pz-decision-yes-side", "pz-decision-no-side"]) {
    assert.match(html, new RegExp(`id="${id}"`), `${id} is part of the objective choice controls`)
  }
  assert.match(puzzles, /choiceYes: el\.decisionYes, choiceNo: el\.decisionNo/,
    "the mutually exclusive choice controls are wired into the Decision Puzzle controller")
  assert.match(puzzles, /choiceYesSide: el\.decisionYesSide, choiceNoSide: el\.decisionNoSide/)
  assert.match(puzzles, /if \(view\.screen === "decision" && view\.mode === "decision"\) \{\s*await decision\.enter\(\)\s*return/,
    "returning to Practice restores the in-progress Decision Puzzle instead of resetting the shared board")
  assert.match(puzzles, /decision\.leave\(\)/, "leaving Practice invalidates pending requests without deleting the attempt")
  assert.match(html, /id="board-nav" class="board-nav" role="group" aria-label="Move navigation"/)
  assert.match(html, /id="nav-prev"/)
  assert.match(html, /id="nav-next"/)
})
