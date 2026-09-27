// Talking to the tutor server: readable errors when it is down, and NDJSON streams that
// notice when they were cut off. No DOM here, so node tests can run it.

export const OFFLINE_MESSAGE = "Can't reach the tutor server. Is it still running? (Start it again, then retry.)"
export const CUT_OFF_MESSAGE = "The connection to the tutor server was lost before this finished. " +
  "Anything already finished (like analyzed games) is saved. Try again."

// fetch() rejects with a bare "Failed to fetch" TypeError when the server is down.
export async function reach(path, opts, fetchImpl = globalThis.fetch) {
  try {
    return await fetchImpl(path, opts)
  } catch (err) {
    if (err && err.name === "AbortError") throw err
    throw new Error(OFFLINE_MESSAGE)
  }
}

// The server's error text: {"error": ...} (every API route), else the status code.
export async function errorMessage(res) {
  let msg = `Request failed (${res.status})`
  try {
    const data = await res.json()
    if (data && typeof data.error === "string" && data.error) msg = data.error
  } catch (_) { /* not JSON */ }
  return msg
}

// Read newline-delimited JSON events from a fetch body. Every server stream ends with a
// {"type": "done"} event; a stream that stops before it (server crash, restart, dropped
// connection) throws CUT_OFF_MESSAGE instead of leaving a progress bar frozen forever.
export async function readEvents(body, onEvent, signal = null) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let finished = false
  const handle = line => {
    const event = JSON.parse(line)
    if (event.type === "done") finished = true
    onEvent(event)
  }
  for (;;) {
    let chunk
    try {
      chunk = await reader.read()
    } catch (err) {
      if (err && err.name === "AbortError") throw err
      break  // dropped mid-stream: reported below
    }
    if (chunk.done) break
    buffer += decoder.decode(chunk.value, {stream: true})
    let nl
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim()
      buffer = buffer.slice(nl + 1)
      if (line) handle(line)
    }
  }
  if (buffer.trim()) handle(buffer)
  if (!finished && !(signal && signal.aborted)) throw new Error(CUT_OFF_MESSAGE)
}
