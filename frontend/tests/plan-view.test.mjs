import {test} from "node:test"
import assert from "node:assert/strict"
import {clarifyBody, debugEnabled, debugLines, optionLabel, puzzleReasons, understoodLine, verificationBadge} from "../plan-view.js"

const question = {key: "coordination:B+N:endgame", question: "What do you mean by “knight and bishop endgames”?",
  options: [{id: "separate", label: "Knight endgames and bishop endgames separately", icon: "♞ ♝"},
    {id: "together", label: "Endgames where one side has both a bishop and a knight", icon: "♝♞"},
    {id: "other", label: "Something else — let me explain", icon: "✏️"}]}

test("answering a clarification question", () => {
  assert.deepEqual(clarifyBody("knight and bishop endgames", question, "together"), {body: {
    goal: "knight and bishop endgames", library: true,
    clarification: {key: "coordination:B+N:endgame", choice: "together"}}})
  assert.deepEqual(clarifyBody("g", question, "other", "  smothered mates ").body.clarification,
    {key: "coordination:B+N:endgame", choice: "other", text: "smothered mates"})
  assert.match(clarifyBody("g", question, "other", "   ").error, /few words/)
  assert.match(clarifyBody("g", question, "made_up").error, /pick one/)
  assert.match(clarifyBody("g", null, "together").error, /ask again/)
})

test("option labels", () => {
  assert.equal(optionLabel(question.options[0]), "♞ ♝ Knight endgames and bishop endgames separately")
  assert.equal(optionLabel({label: "Plain"}), "Plain")
})

test("understood-as line only for readings the learner chose", () => {
  const plan = {intent: {interpretation: "Knight against bishop endgames", source: "clarified"}}
  assert.equal(understoodLine(plan), "Understood as: Knight against bishop endgames (as you chose).")
  assert.match(understoodLine({intent: {...plan.intent, source: "remembered"}}), /earlier answer/)
  assert.equal(understoodLine({intent: {...plan.intent, source: "parsed"}}), null)  // clear request: nothing to change
  assert.equal(understoodLine({}), null)
})

test("custom plan badges", () => {
  const v = verificationBadge({custom: {status: "verified", proposer: "composer", engine: "Stockfish"}})
  assert.equal(v.kind, "verified")
  assert.match(v.detail, /Stockfish/)
  assert.match(verificationBadge({custom: {status: "reused", proposer: "qwen", engine: null}}).detail,
    /AI tutor.*verified library content.*earlier/)
  assert.equal(verificationBadge({custom: {status: "fallback"}}).kind, "fallback")
  assert.equal(verificationBadge({custom: {status: "failed"}}), null)  // never shown as correct
  assert.equal(verificationBadge({}), null)
})

test("the badge never says 'from the library' for positions generated for the request", () => {
  const gen = verificationBadge({custom: {status: "verified", proposer: "composer", engine: "Stockfish",
    coverage: [{material: "two_rooks_vs_queen", label: "Two rooks against a queen", library: 0, generated: 5}]}})
  assert.doesNotMatch(gen.detail, /Built from the verified library/)
  assert.match(gen.detail, /Two rooks against a queen: no verified library positions yet, so 5 generated for this request/)
  assert.match(gen.detail, /exactly the requested pieces for each side/)
  const lib = verificationBadge({custom: {status: "verified", proposer: "composer",
    coverage: [{material: "two_rooks_vs_queen", label: "Two rooks against a queen", library: 5, generated: 0}]}})
  assert.match(lib.detail, /5 verified positions with exactly this material/)
})

test("the debug panel lists every stage, and is opt-in", () => {
  const lines = debugLines({
    user_request: "2 rooks vs a queen",
    interpreted_intent: {material: [{id: "two_rooks_vs_queen", short: "2R vs Q", owner: "learner"}], objective: "general",
      reading: {agreement: "parser"}},
    library_coverage: [{material: "two_rooks_vs_queen", coverage: "NONE", verified_matches: 0, needed: 5}],
    generation: [{material: "two_rooks_vs_queen", generated: 5, details: {attempts: 35, rejected: {"no single best move": 20}}}],
    position_constraints: [["learner: exactly 2 rooks", "opponent: exactly 1 queen"]],
    validation: {"python-chess": "PASS", "stockfish": "PASS", "material constraints": "PASS",
      "educational validation": "PASS", "request satisfaction": "FAIL", "request satisfaction (why)": ["title"]},
  })
  const labels = lines.map(l => l[0])
  for (const l of ["USER REQUEST", "INTERPRETED INTENT", "LIBRARY COVERAGE", "GENERATION", "POSITION CONSTRAINTS",
    "VALIDATION · request satisfaction"]) assert.ok(labels.includes(l), l)
  assert.match(Object.fromEntries(lines)["LIBRARY COVERAGE"], /NONE \(0 exact verified matches, 5 needed\)/)
  assert.equal(Object.fromEntries(lines)["VALIDATION · request satisfaction"], "FAIL — title")
  assert.deepEqual(debugLines(null), [])
  assert.equal(debugEnabled({location: {search: "?debug=1"}, localStorage: {getItem: () => null}}), true)
  assert.equal(debugEnabled({location: {search: ""}, localStorage: {getItem: () => null}}), false)
})

test("targeted puzzles: the debug panel shows weakness, source, library match, generation and validation", () => {
  const lines = debugLines({
    kind: "puzzles",
    user_weakness: {key: "hung_piece", title: "Hanging pieces", concept: "hung_piece", games: 6, of: 25, tier: "recurring"},
    source: [{game_id: "chesscom-1", move: "14...Nxe4", severity: "blunder"}],
    library_match: {considered: 22, chosen: 3, target_rating: 720, puzzles: [{id: "p1", type: "tactic", rating: 650, match: "trains", score: 0.81}]},
    custom_generation: {status: "ran", needed: 2, generated: 2, attempts: 9, rejected: 7},
    puzzle_validation: [{id: "p1", origin: "library", status: "verified", uniqueness: "unique", accepted: ["Nxe4"], moves: 1, rating: 650},
      {id: "g1", origin: "generated", status: "candidate", uniqueness: "unchecked", accepted: []}],
  })
  const keys = lines.map(([k]) => k)
  for (const k of ["USER WEAKNESS", "SOURCE", "LIBRARY MATCH", "CUSTOM GENERATION", "PUZZLE VALIDATION"]) assert.ok(keys.includes(k), k)
  const get = k => lines.filter(([kk]) => kk === k).map(([, v]) => v)
  assert.match(get("USER WEAKNESS")[0], /6 of 25 games · recurring/)
  assert.match(get("SOURCE")[0], /chesscom-1 move 14\.\.\.Nxe4 \(blunder\)/)
  assert.match(get("LIBRARY MATCH")[0], /3 chosen of 22 candidates · target ≈720/)
  assert.match(get("CUSTOM GENERATION")[0], /ran · needed 2, accepted 2 of 9 candidates · rejected 7/)
  const v = get("PUZZLE VALIDATION")
  assert.ok(v[0].startsWith("PASS p1 (library) · unique · accepts Nxe4"))
  assert.ok(v[1].startsWith("FAIL g1"))
})

test("why-this-puzzle lines", () => {
  assert.deepEqual(puzzleReasons({}), [])
  assert.deepEqual(puzzleReasons({puzzles: [{title: "Knight fork", origin: "library", reasons: ["Trains forks", "New to you"]}]}),
    [{n: 1, title: "Knight fork", origin: "library", why: "Trains forks · New to you"}])
})
