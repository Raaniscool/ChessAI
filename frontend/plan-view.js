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
  // exact-material requests say where the positions came from — never "from the library"
  // when they were generated for this request
  const cov = c.coverage || []
  const generated = cov.reduce((n, x) => n + (x.generated || 0), 0)
  let who = c.proposer === "qwen" ? "Proposed by the AI tutor" : "Built from the verified library"
  if (cov.length && generated) {
    who = cov.map(x => `${x.label}: ${x.library ? `${x.library} verified library position${x.library === 1 ? "" : "s"} + ` : "no verified library positions yet, so "}${x.generated} generated for this request`).join("; ")
  } else if (cov.length) {
    who = cov.map(x => `${x.label}: ${x.library} verified position${x.library === 1 ? "" : "s"} with exactly this material`).join("; ")
  }
  if (c.status === "verified" || c.status === "reused") {
    const how = cov.length ? "python-chess checked every position (legal, exactly the requested pieces for each side) and Stockfish checked every solution"
      : c.engine ? "python-chess and Stockfish checked every position and move"
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

// Developer view (?debug=1 or localStorage "chessai.debug"): how the request became this plan.
export function debugEnabled(win = globalThis.window) {
  try {
    if (win && new URLSearchParams(win.location.search).get("debug") === "1") return true
    return !!(win && win.localStorage && win.localStorage.getItem("chessai.debug"))
  } catch { return false }
}

export function debugLines(debug) {
  if (!debug) return []
  const out = []
  const intent = debug.interpreted_intent || {}
  const reading = intent.reading || {}
  out.push(["USER REQUEST", debug.user_request || ""])
  const mats = (intent.material || []).map(m => `${m.id} (${m.short}; owner: ${m.owner || "either side"})`)
  out.push(["INTERPRETED INTENT", (mats.join(", ") || (intent.components || []).join(", ") || "—") +
    (intent.objective ? ` · focus: ${intent.objective}` : "") +
    (reading.agreement ? ` · read by: ${reading.agreement}` : "") +
    (reading.qwen && reading.qwen.status && reading.qwen.status !== "ok" ? ` · Qwen: ${reading.qwen.status}` : "")])
  for (const c of debug.library_coverage || []) {
    out.push(["LIBRARY COVERAGE", `${c.material}: ${c.coverage} (${c.verified_matches} exact verified matches, ${c.needed} needed)`])
  }
  for (const g of debug.generation || []) {
    const d = g.details || {}
    const why = d.rejected ? Object.entries(d.rejected).slice(0, 3).map(([k, v]) => `${k} ×${v}`).join("; ") : ""
    out.push(["GENERATION", `${g.material}: ${g.generated} accepted` + (d.attempts ? ` of ${d.attempts} candidates` : "") +
      (d.stopped ? ` · stopped: ${d.stopped}` : "") + (why ? ` · rejected: ${why}` : "")])
  }
  for (const c of debug.position_constraints || []) out.push(["POSITION CONSTRAINTS", c.join(" · ")])
  const v = debug.validation || {}
  for (const k of ["python-chess", "stockfish", "material constraints", "educational validation", "request satisfaction"]) {
    if (v[k]) out.push([`VALIDATION · ${k}`, v[k] + (v[`${k} (why)`] ? ` — ${v[`${k} (why)`].join("; ")}` : "")])
  }
  return out
}
