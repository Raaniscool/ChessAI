// Pure helpers for learning-plan requests: clarification questions and plan badges.
// No DOM here, so they can be unit-tested with node.

export const OTHER = "other"

// The body re-sent after the learner answers a clarification question.
// Returns {body} or {error} (e.g. "Something else" with nothing typed).
export function clarifyBody(goal, question, choice, text = "") {
  if (!question || !question.key) return {error: "That question has expired — please ask again."}
  const known = (question.options || []).some(o => o.id === choice)
  if (!known) return {error: "Please pick one of the options."}
  const clarification = {key: question.key, choice}
  if (choice === OTHER) {
    const said = (text || "").trim()
    if (!said) return {error: "Tell me in a few words what you'd like to learn."}
    clarification.text = said
  }
  return {body: {goal, library: true, clarification}}
}

// "♞ Knight endgames and ♝ bishop endgames separately" — the icon is kept apart from the words.
export function optionLabel(option) {
  return option.icon ? `${option.icon} ${option.label}` : option.label
}

// "Understood as …" is shown only when the plan follows a reading the learner chose (or that
// was remembered from an earlier answer / picked among Qwen's suggestions), so they can change it.
export function understoodLine(plan) {
  const intent = plan && plan.intent
  if (!intent || !intent.interpretation) return null
  const how = {clarified: "as you chose", remembered: "from your earlier answer", qwen: "as you chose"}[intent.source]
  if (!how) return null
  return `Understood as: ${intent.interpretation} (${how}).`
}

// Custom plans are built when the library has no ready-made plan; say how they were checked.
export function verificationBadge(plan) {
  const c = plan && plan.custom
  if (!c) return null
  const who = c.proposer === "qwen" ? "Proposed by the AI tutor" : "Built from the verified library"
  if (c.status === "verified" || c.status === "reused") {
    const how = c.engine ? "python-chess and Stockfish checked every position and move"
      : "every position comes from verified library content"
    return {kind: "verified", text: "✔ Custom plan — verified",
      detail: `${who}; ${how}, plus checks on order, difficulty and topic.` +
        (c.status === "reused" ? " (Verified earlier for the same request.)" : "")}
  }
  if (c.status === "fallback") {
    return {kind: "fallback", text: "⚠ Broader verified plan",
      detail: "I couldn't verify a plan for exactly what you asked, so this one covers the broader idea."}
  }
  return null
}
