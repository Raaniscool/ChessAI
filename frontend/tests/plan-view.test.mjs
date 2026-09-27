import {test} from "node:test"
import assert from "node:assert/strict"
import {clarifyBody, optionLabel, understoodLine, verificationBadge} from "../plan-view.js"

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
