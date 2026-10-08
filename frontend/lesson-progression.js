// Small, deterministic chat-intent helpers for continuing or explicitly restarting
// the active lesson plan. Plan order and completion still come from the server.
const CONTINUATION_REQUESTS = new Set([
  "another round",
  "do another round",
  "keep going",
  "continue",
  "continue the lesson",
  "continue with the lesson",
  "next",
  "next lesson",
  "i'm ready",
  "more",
  "what's next",
  "what is next",
  "do it",
])

function normalized(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[’‘]/g, "'")
    .replace(/[!?.,;:]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
}

export function isContinueRequest(text) {
  let value = normalized(text)
    .replace(/^(?:please|okay|ok|sure|great|yes|alright|all right)\s+/, "")
    .replace(/^(?:let's|lets)\s+/, "")
    .trim()
  if (value === "i am ready") value = "i'm ready"
  return CONTINUATION_REQUESTS.has(value)
}

export function isRestartRequest(text) {
  const value = normalized(text)
  if (/\b(?:don't|do not|never|not)\b/.test(value)) return false
  return /\b(?:restart|start over|start again|from (?:the )?beginning|relearn|redo|repeat)\b/.test(value)
}
