import {test} from "node:test"
import assert from "node:assert/strict"
import {CUT_OFF_MESSAGE, OFFLINE_MESSAGE, errorMessage, readEvents, reach} from "../net.js"

function bodyOf(chunks, {failAfter = false} = {}) {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c))
      if (failAfter) controller.error(new TypeError("network error"))
      else controller.close()
    },
  })
}

test("a complete stream delivers every event, even split across chunks", async () => {
  const seen = []
  await readEvents(bodyOf(['{"type":"start"}\n{"type":"pro', 'gress","done":1}\n', '{"type":"done"}']),
    e => seen.push(e.type))
  assert.deepEqual(seen, ["start", "progress", "done"])
})

test("a stream that stops before 'done' is reported, not left hanging", async () => {
  const seen = []
  await assert.rejects(readEvents(bodyOf(['{"type":"progress"}\n']), e => seen.push(e.type)),
    {message: CUT_OFF_MESSAGE})
  assert.deepEqual(seen, ["progress"])  // what arrived is still shown
})

test("a connection dropped mid-stream is reported the same way", async () => {
  await assert.rejects(readEvents(bodyOf(['{"type":"progress"}\n'], {failAfter: true}), () => {}),
    {message: CUT_OFF_MESSAGE})
})

test("a stream cancelled on purpose is not an error", async () => {
  const controller = new AbortController()
  controller.abort()
  await readEvents(bodyOf(['{"type":"delta"}\n']), () => {}, controller.signal)
})

test("server down: a readable message instead of 'Failed to fetch'", async () => {
  const down = async () => { throw new TypeError("Failed to fetch") }
  await assert.rejects(reach("/api/x", {}, down), {message: OFFLINE_MESSAGE})
  const aborted = async () => { const e = new Error("aborted"); e.name = "AbortError"; throw e }
  await assert.rejects(reach("/api/x", {}, aborted), {name: "AbortError"})
})

test("error bodies: the server's message, else the status", async () => {
  const res = (status, body) => ({status, json: async () => (body === undefined ? Promise.reject(new Error()) : body)})
  assert.equal(await errorMessage(res(422, {error: "Invalid request — goal: Field required"})),
    "Invalid request — goal: Field required")
  assert.equal(await errorMessage(res(500)), "Request failed (500)")
  assert.equal(await errorMessage(res(404, {detail: "Not Found"})), "Request failed (404)")
})
