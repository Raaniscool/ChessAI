import {test} from "node:test"
import assert from "node:assert/strict"
import {isContinueRequest, isRestartRequest} from "../lesson-progression.js"

test("known continuation phrases are recognized without a model", () => {
  for (const phrase of [
    "another round",
    "let's do another round",
    "keep going",
    "continue",
    "next",
    "let's continue",
    "I'm ready",
    "more",
    "what's next?",
    "let's do it",
    "Okay, let's continue!",
  ]) assert.equal(isContinueRequest(phrase), true, phrase)
})

test("continuation matching does not swallow new-topic or restart requests", () => {
  assert.equal(isContinueRequest("I want to learn forks"), false)
  assert.equal(isContinueRequest("Let's learn pins"), false)
  assert.equal(isContinueRequest("another round of pins"), false)
  assert.equal(isRestartRequest("restart the lesson"), true)
  assert.equal(isRestartRequest("I want to start over from the beginning"), true)
  assert.equal(isRestartRequest("I do not want to restart"), false)
})
