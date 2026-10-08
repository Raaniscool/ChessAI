// The coach side of the tutor: a short optional onboarding, "what your coach knows about you",
// and settings (read-aloud voice and speed, what's read automatically, explanation length).
// Pure helpers are exported for tests; setupCoach() wires them to the page.

import {READ_KINDS, SPEEDS, voiceGroups} from "./speech.js"

export const PLATFORMS = [
  ["chesscom", "Chess.com"], ["lichess", "Lichess"], ["fide", "FIDE / national"], ["other", "Other"]]
export const EXPERIENCE = [
  ["new", "I'm new to chess"], ["rules", "I know how the pieces move"], ["casual", "I play casually"],
  ["club", "I play regularly / at a club"], ["strong", "Experienced tournament player"]]
export const GOALS = [
  ["tactics", "Tactics"], ["checkmates", "Checkmates"], ["openings", "Openings"], ["endgames", "Endgames"],
  ["stop_blundering", "Stop blundering"], ["calculation", "Calculation"], ["strategy", "Strategy"],
  ["rating", "Gain rating"]]
export const STYLES = [
  ["brief", "Brief", "Short and to the point"],
  ["balanced", "Balanced", "The why, without the essay"],
  ["detailed", "Detailed", "More background and reasoning"]]
const SPEED_LABELS = {slow: "Slow", normal: "Normal", fast: "Fast"}
const USERNAME_RE = /^[A-Za-z0-9_-]{3,25}$/

/** Onboarding form values → the /api/profile/onboarding body, or a friendly error. */
export function onboardingBody(v) {
  const body = {explanation: v.explanation || "balanced", goals: v.goals || []}
  const raw = String(v.rating ?? "").trim()
  if (raw) {
    const rating = Number(raw)
    if (!Number.isInteger(rating) || rating < 100 || rating > 3500) {
      return {error: "That rating looks off — enter a number like 800 or 1500, or leave it empty."}
    }
    body.rating = rating
    body.platform = v.platform || "chesscom"
  } else if (v.experience) {
    body.experience = v.experience
  } else {
    return {error: "Enter your rating, or pick the description that fits you best."}
  }
  const user = String(v.username || "").trim()
  if (user) {
    if (!USERNAME_RE.test(user)) return {error: "Chess.com usernames are 3–25 letters, digits, _ or -."}
    body.username = user
  }
  return {body}
}

const LEVEL_NAMES = {beginner: "Beginner", intermediate: "Intermediate", advanced: "Advanced"}
const SOURCE_NAMES = {
  onboarding: "from the rating you gave", experience: "estimated from what you told me",
  games: "from your Chess.com games", puzzles: "from your results here", default: "a starting guess",
}
const names = (profile, ids) => ids.map(id => (profile.concepts[id] && profile.concepts[id].name) || id.replace(/_/g, " "))

/** What the coach knows, as short labelled lines (empty sections left out). */
export function profileLines(profile) {
  if (!profile) return []
  const lines = []
  const source = SOURCE_NAMES[profile.rating_source] || ""
  lines.push({label: "Level", text: `${LEVEL_NAMES[profile.level] || profile.level} — about ${profile.rating}` +
    (source ? ` (${source})` : "")})
  const list = (label, ids) => { if (ids && ids.length) lines.push({label, text: names(profile, ids).join(", ")}) }
  list("Strong at", profile.mastered)
  list("Practising", [...(profile.practicing || []), ...(profile.learned || [])].slice(0, 6))
  list("Needs work", profile.weak)
  list("Due for review", profile.needs_review)
  const fromGames = (profile.weaknesses || []).filter(w => w.tier === "recurring")
  if (fromGames.length) {
    lines.push({label: "From your games", text: fromGames.slice(0, 3)
      .map(w => `${w.title} (${w.game_count} of ${w.total_games} games)`).join("; ")})
  }
  const s = profile.stats || {}
  const p = s.puzzles || {}
  if (p.attempts) {
    const rate = Math.round((s.first_try_rate || 0) * 100)
    lines.push({label: "Practice", text: `${p.attempts} position${p.attempts === 1 ? "" : "s"} tried, ` +
      `${rate}% solved first try, ${s.hints_used || 0} hint${s.hints_used === 1 ? "" : "s"} used`})
  }
  const sp = profile.skill_profile
  if (sp && sp.skills && (sp.games || sp.puzzles)) {
    // the skill profile behind Puzzles and Training difficulty (only once there is real evidence)
    const show = [["tactical_skill", "Tactics"], ["calculation_skill", "Calculation"], ["defensive_skill", "Defence"],
      ["endgame_skill", "Endgames"], ["opening_skill", "Openings"]]
    const parts = show.filter(([k]) => sp.skills[k]).map(([k, label]) => `${label}: ${sp.skills[k].level}`)
    const basis = [sp.games ? `${sp.games} analyzed game${sp.games === 1 ? "" : "s"}` : "",
      sp.puzzles ? `${sp.puzzles} solved position${sp.puzzles === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ")
    lines.push({label: "Skills", text: `${parts.join(" · ")} (from ${basis})`})
  }
  const lessons = (s.lessons || {}).completed || 0
  if (lessons) lines.push({label: "Lessons", text: `${lessons} completed`})
  return lines
}

/** Should the onboarding card be offered? Only to someone new who hasn't answered or skipped. */
export function wantsOnboarding(profile) {
  if (!profile) return false
  const ob = profile.onboarding || {}
  return !ob.done && !ob.skipped && !!profile.new
}

// ---------------------------------------------------------------- page wiring

function el(tag, cls, text) {
  const e = document.createElement(tag)
  if (cls) e.className = cls
  if (text !== undefined) e.textContent = text
  return e
}

function chipGroup(options, {multi = false, selected = []} = {}) {
  const row = el("div", "chip-group no-speech")
  const chosen = new Set(selected)
  for (const [id, label, title] of options) {
    const b = el("button", "chip toggle-chip", label)
    b.type = "button"
    b.dataset.value = id
    if (title) b.title = title
    b.setAttribute("aria-pressed", String(chosen.has(id)))
    b.addEventListener("click", () => {
      if (!multi) {
        const was = chosen.has(id)
        chosen.clear()
        row.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", "false"))
        if (was && row.dataset.optional) return
      }
      if (multi && chosen.has(id)) { chosen.delete(id); b.setAttribute("aria-pressed", "false"); return }
      chosen.add(id)
      b.setAttribute("aria-pressed", "true")
      row.dispatchEvent(new Event("change"))
    })
    row.appendChild(b)
  }
  row.values = () => [...chosen]
  row.value = () => [...chosen][0] || ""
  return row
}

/**
 * @param deps {api, narrator, requestPlan, addMsg, messagesEl, escapeHtml}
 */
export function setupCoach(deps) {
  const {api, narrator} = deps
  const state = {profile: null, suggestions: [], tts: null}

  // ----- profile -----
  async function refresh() {
    try {
      const res = await api("/api/profile")
      state.profile = res.profile
      state.suggestions = res.suggestions || []
    } catch (_) { /* the tutor works without it */ }
    return state.profile
  }

  function level() {
    return (state.profile && state.profile.level) || "beginner"
  }

  // ----- onboarding (a card in the conversation, never a wall) -----
  function onboardingCard({editing = false} = {}) {
    const ob = (state.profile && state.profile.onboarding) || {}
    const card = el("div", "msg assistant onboarding-card")
    card.appendChild(el("div", "onb-title", editing ? "Your chess, so I can pitch lessons right"
      : "Let's start with your games: what's your Chess.com username?"))
    // the username comes first: your own games are the best guide to what to practise. It stays
    // optional, and the tutor works fully without it (lessons, puzzles, pasted PGNs).
    const userRow = el("div", "onb-row onb-user-row no-speech")
    const user = el("input")
    user.type = "text"; user.placeholder = "e.g. magnus_fan"; user.className = "onb-username"; user.autocomplete = "off"
    user.setAttribute("aria-label", "Chess.com username")
    user.value = ob.chesscom_username || ""
    userRow.append(el("label", "", "Chess.com username"), user)
    card.appendChild(userRow)
    card.appendChild(el("div", "muted onb-sub",
      "I'll load your recent games (up to 100) and find what's worth practising. No Chess.com account? " +
      "Leave it empty: everything else works without it."))
    card.appendChild(el("div", "muted onb-label", "Your rating, if you know it (optional):"))

    const ratingRow = el("div", "onb-row no-speech")
    const rating = el("input")
    rating.type = "number"; rating.min = "100"; rating.max = "3500"; rating.placeholder = "e.g. 900"
    rating.className = "onb-rating"; rating.setAttribute("aria-label", "Your rating")
    if (ob.rating) rating.value = String(ob.rating)
    const platform = el("select", "onb-platform")
    platform.setAttribute("aria-label", "Rating from")
    for (const [id, label] of PLATFORMS) {
      const o = el("option", "", label); o.value = id; platform.appendChild(o)
    }
    if (ob.platform) platform.value = ob.platform
    ratingRow.append(el("label", "", "Rating"), rating, el("span", "muted", "on"), platform)
    card.appendChild(ratingRow)

    card.appendChild(el("div", "muted onb-label", "No rating? Pick what fits best:"))
    const experience = chipGroup(EXPERIENCE, {selected: ob.experience ? [ob.experience] : []})
    experience.dataset.optional = "1"
    experience.classList.add("onb-experience")
    card.appendChild(experience)
    experience.addEventListener("change", () => { if (experience.value()) rating.value = "" })
    rating.addEventListener("input", () => {
      if (rating.value) experience.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false"))
    })

    card.appendChild(el("div", "muted onb-label", "What would you like to work on? (optional)"))
    const goals = chipGroup(GOALS, {multi: true, selected: ob.goals || []})
    goals.classList.add("onb-goals")
    card.appendChild(goals)

    card.appendChild(el("div", "muted onb-label", "How much explanation do you like?"))
    const style = chipGroup(STYLES, {selected: [(state.profile && state.profile.preferences.explanation) || "balanced"]})
    style.classList.add("onb-style")
    card.appendChild(style)

    const note = el("div", "clarify-note onb-error")
    const actions = el("div", "onb-actions no-speech")
    const save = el("button", "btn primary onb-save", editing ? "Save" : "Save and continue")
    const skip = el("button", "btn onb-skip", editing ? "Cancel" : "Skip for now")
    actions.append(save, skip)
    card.append(note, actions)

    save.addEventListener("click", async () => {
      const got = onboardingBody({rating: rating.value, platform: platform.value, experience: experience.value(),
        goals: goals.values(), explanation: style.value(), username: user.value})
      if (got.error) { note.textContent = got.error; return }
      save.disabled = true
      try {
        const res = await api("/api/profile/onboarding", "PUT", got.body)
        state.profile = res.profile
        state.suggestions = res.suggestions || []
        if (got.body.username) {
          try { localStorage.setItem("chessai.chesscomUsername", got.body.username) } catch (_) { /* ignore */ }
          const input = document.getElementById("ga-username")
          if (input && !input.value) input.value = got.body.username
        }
        card.remove()
        thanks(got.body)
        renderCoach()
      } catch (err) {
        note.textContent = err.message
        save.disabled = false
      }
    })
    skip.addEventListener("click", async () => {
      card.remove()
      if (!editing) {
        deps.addMsg("No problem — I'll work it out from how you do. You can tell me later under 👤 Coach.", "system")
        api("/api/profile/onboarding", "PUT", {skipped: true}).catch(() => {})
      }
    })
    return card
  }

  function thanks(body) {
    const p = state.profile
    let text = `Thanks! I'll start around ${p.rating} and adjust as I see how you do.`
    if (body.username) text += " Your recent games will show me what to work on: load them and pick which to analyze."
    const msg = deps.addMsg(text, "assistant")
    if (body.username && deps.analyzeGames) {
      const row = el("div", "suggestions coach-suggestions no-speech")
      const go = el("button", "chip onb-analyze", "🔍 Load my games")
      go.type = "button"
      go.addEventListener("click", () => { go.disabled = true; deps.analyzeGames(body.username) })
      row.appendChild(go)
      msg.appendChild(row)
    }
    if (state.suggestions.length) msg.appendChild(suggestionRow(state.suggestions))
  }

  function suggestionRow(list) {
    const row = el("div", "suggestions coach-suggestions no-speech")
    for (const s of list) {
      const b = el("button", "chip", s.title)
      b.title = s.reason
      b.addEventListener("click", () => { closePanels(); deps.requestPlan(s.goal) })
      row.appendChild(b)
    }
    return row
  }

  async function offerOnboarding() {
    const profile = await refresh()
    if (!wantsOnboarding(profile)) return false
    const welcome = document.getElementById("welcome")
    welcome.after(onboardingCard())
    return true
  }

  // ----- panels -----
  const coachPanel = document.getElementById("coach-panel")
  const settingsPanel = document.getElementById("settings-panel")

  function closePanels() {
    for (const p of [coachPanel, settingsPanel]) p.classList.add("hidden")
    for (const id of ["btn-coach", "btn-settings"]) document.getElementById(id).setAttribute("aria-expanded", "false")
  }

  function toggle(panel, button) {
    const open = panel.classList.contains("hidden")
    closePanels()
    if (open) {
      panel.classList.remove("hidden")
      document.getElementById(button).setAttribute("aria-expanded", "true")
    }
    return open
  }

  function renderCoach() {
    const body = coachPanel.querySelector(".panel-body")
    body.innerHTML = ""
    const p = state.profile
    if (!p) { body.appendChild(el("p", "muted", "I couldn't load your profile just now.")); return }
    if (p.new && !(p.onboarding || {}).done) {
      body.appendChild(el("p", "muted", "I don't know much about you yet. Tell me your level, or just start a " +
        "lesson — I learn from how you do."))
    }
    const dl = el("dl", "coach-facts")
    for (const line of profileLines(p)) dl.append(el("dt", "", line.label), el("dd", "", line.text))
    body.appendChild(dl)
    if (state.suggestions.length) {
      body.appendChild(el("h3", "", "Suggested next"))
      for (const s of state.suggestions) {
        const item = el("div", "coach-suggestion")
        const go = el("button", "btn primary", s.title)
        go.addEventListener("click", () => { closePanels(); deps.requestPlan(s.goal) })
        item.append(go, el("div", "muted", s.reason))
        body.appendChild(item)
      }
    }
    const actions = el("div", "panel-actions")
    const edit = el("button", "btn", "✎ My details")
    edit.addEventListener("click", () => {
      closePanels()
      const card = onboardingCard({editing: true})
      deps.messagesEl.appendChild(card)
      if (card.scrollIntoView) card.scrollIntoView({block: "nearest"})
    })
    const reset = el("button", "btn subtle", "Start over")
    reset.title = "Forget everything the coach has learned about you"
    reset.addEventListener("click", async () => {
      if (!confirm("Forget your level, progress notes and preferences, and start fresh?")) return
      const res = await api("/api/profile/reset", "POST")
      state.profile = res.profile
      state.suggestions = res.suggestions || []
      renderCoach()
    })
    actions.append(edit, reset)
    body.appendChild(actions)
  }

  // ----- settings -----
  let savePending = null
  function persist() {
    narrator.save()
    clearTimeout(savePending)
    // also on the server: the same settings on the next visit, even from a fresh browser profile
    savePending = setTimeout(() => {
      const t = narrator.prefs
      api("/api/profile/preferences", "PUT", {tts: {enabled: t.enabled, provider: t.provider, voice: t.voice,
        browser_voice: t.browserVoice, speed: t.speed, read: t.read}}).catch(() => {})
    }, 400)
  }

  async function loadVoices() {
    try {
      state.tts = await api("/api/tts")
      narrator.configure(state.tts)
    } catch (_) {
      state.tts = null
      narrator.configure(null)
    }
  }

  /** Server-saved settings win when this browser has none of its own. */
  function adoptServerPrefs() {
    let local = null
    try { local = localStorage.getItem("chess-tutor-speech") } catch (_) { /* ignore */ }
    const server = state.profile && state.profile.preferences && state.profile.preferences.tts
    if (local || !server) return
    Object.assign(narrator.prefs, {
      enabled: !!server.enabled, provider: server.provider || "auto", voice: server.voice || "",
      browserVoice: server.browser_voice || "", speed: SPEEDS[server.speed] ? server.speed : "normal",
      read: {...narrator.prefs.read, ...(server.read || {})}})
    narrator.save()
  }

  /** Board sounds: the server's setting is used when this browser has never chosen. */
  function adoptServerSounds() {
    let local = null
    try { local = localStorage.getItem("chessai.boardSounds") } catch (_) { /* ignore */ }
    const server = state.profile && state.profile.preferences && state.profile.preferences.board_sounds
    if (deps.sounds && local === null && typeof server === "boolean") deps.sounds.setEnabled(server)
  }

  function voiceOptions(select) {
    select.innerHTML = ""
    const usable = ((state.tts && state.tts.providers) || []).filter(p => p.available && p.voices.length)
    for (const provider of usable) {
      for (const group of voiceGroups(provider.voices)) {
        const og = el("optgroup")
        og.label = `${provider.name} · ${group.label.toLowerCase()}`
        for (const v of group.voices) {
          const o = el("option", "", `${v.name} (${v.accent})`)
          o.value = `natural:${provider.id}:${v.id}`
          o.title = v.description || ""
          og.appendChild(o)
        }
        select.appendChild(og)
      }
    }
    const browserVoices = narrator.voices()
    if (browserVoices.length) {
      const og = el("optgroup")
      og.label = "Your browser's voices"
      for (const v of browserVoices) {
        const o = el("option", "", v.name.replace(/^Microsoft\s+/, "").replace(/\s+-\s+.*$/, "").replace(/\s*\(.*\)$/, ""))
        o.value = `browser:${v.name}`
        og.appendChild(o)
      }
      select.appendChild(og)
    }
    const engine = narrator.engine()
    const provider = narrator.naturalProvider()
    if (engine === "natural" && provider) {
      const ids = provider.voices.map(v => v.id)
      select.value = `natural:${provider.id}:${ids.includes(narrator.prefs.voice) ? narrator.prefs.voice : ids[0]}`
    } else if (engine === "browser") {
      const v = narrator.voice()
      if (v) select.value = `browser:${v.name}`
    }
    return usable
  }

  function voiceStatus(usable) {
    if (narrator.engine() === "natural") {
      return `Natural voice: ${narrator.naturalProvider().name}. Sentences are made on your computer and ` +
        "remembered, so repeats start instantly."
    }
    const why = ((state.tts && state.tts.providers) || []).map(p => p.reason).filter(Boolean)[0]
    const base = narrator.browserSupported ? "Using your browser's built-in voice." : "No voice is available in this browser; the text stays on screen."
    return usable.length ? base : `${base} For a more natural voice, set up Kokoro (free, runs offline) — ` +
      `see docs/TTS.md.${why ? " (" + why + ")" : ""}`
  }

  function renderSettings() {
    const body = settingsPanel.querySelector(".panel-body")
    body.innerHTML = ""

    body.appendChild(el("h3", "", "Read aloud"))
    const onRow = el("label", "setting-row")
    const on = el("input"); on.type = "checkbox"; on.id = "set-read-aloud"; on.checked = narrator.enabled
    onRow.append(on, el("span", "", "Read the coach's messages aloud automatically"))
    body.appendChild(onRow)

    const voiceRow = el("div", "setting-row")
    const select = document.getElementById("speech-voice")
    select.classList.remove("hidden")
    const preview = el("button", "btn", "▶ Preview")
    preview.id = "btn-voice-preview"
    voiceRow.append(el("span", "setting-label", "Voice"), select, preview)
    body.appendChild(voiceRow)
    const usable = voiceOptions(select)
    const status = el("div", "muted setting-note", voiceStatus(usable))
    status.id = "voice-status"
    body.appendChild(status)

    const speedRow = el("div", "setting-row")
    const speed = document.getElementById("speech-rate")
    speed.value = narrator.prefs.speed
    speedRow.append(el("span", "setting-label", "Speed"), speed)
    body.appendChild(speedRow)

    body.appendChild(el("div", "muted setting-note", "Read automatically (the rest has a 🔊 button):"))
    const kinds = el("div", "setting-kinds")
    for (const [kind, label] of Object.entries(READ_KINDS)) {
      const row = el("label", "setting-row small")
      const box = el("input"); box.type = "checkbox"; box.dataset.kind = kind
      box.checked = narrator.prefs.read[kind] !== false
      box.addEventListener("change", () => { narrator.prefs.read[kind] = box.checked; persist() })
      row.append(box, el("span", "", label))
      kinds.appendChild(row)
    }
    body.appendChild(kinds)
    body.appendChild(el("div", "muted setting-note",
      "Move explanations are only read out when the move matters — not for every recapture."))

    if (deps.sounds) {
      body.appendChild(el("h3", "", "Board"))
      const soundRow = el("label", "setting-row")
      const sound = el("input"); sound.type = "checkbox"; sound.id = "set-board-sounds"; sound.checked = deps.sounds.enabled
      soundRow.append(sound, el("span", "", "Move, capture and check sounds"))
      sound.addEventListener("change", () => {
        deps.sounds.setEnabled(sound.checked)
        if (sound.checked) deps.sounds.play("move")
        api("/api/profile/preferences", "PUT", {board_sounds: sound.checked}).catch(() => {})
      })
      body.appendChild(soundRow)
    }

    body.appendChild(el("h3", "", "Explanations"))
    const current = (state.profile && state.profile.preferences.explanation) || "balanced"
    const style = chipGroup(STYLES, {selected: [current]})
    style.id = "set-explanation"
    style.addEventListener("change", async () => {
      try {
        const res = await api("/api/profile/preferences", "PUT", {explanation: style.value()})
        state.profile = res.profile
      } catch (_) { /* keep going */ }
    })
    body.appendChild(style)

    on.addEventListener("change", () => {
      narrator.enabled = on.checked
      syncToggle()
      persist()
      if (!on.checked) narrator.stop()
    })
    select.onchange = () => {
      const [kind, a, ...rest] = select.value.split(":")
      if (kind === "natural") { narrator.prefs.provider = a; narrator.prefs.voice = rest.join(":") }
      else { narrator.prefs.provider = "browser"; narrator.prefs.browserVoice = [a, ...rest].join(":") }
      status.textContent = voiceStatus(usable)
      persist()
    }
    speed.onchange = () => { narrator.prefs.speed = speed.value; persist() }
    preview.onclick = () => {
      const [kind, a, ...rest] = select.value.split(":")
      const choice = kind === "natural" ? {provider: a, voice: rest.join(":"), speed: speed.value}
        : {provider: "browser", browserVoice: [a, ...rest].join(":"), speed: speed.value}
      narrator.preview(choice)
    }
  }

  function syncToggle() {
    const toggleBtn = document.getElementById("btn-read-aloud")
    toggleBtn.classList.toggle("on", narrator.enabled)
    toggleBtn.setAttribute("aria-pressed", String(narrator.enabled))
    const box = settingsPanel.querySelector("#set-read-aloud")
    if (box) box.checked = narrator.enabled
  }

  function setupSpeedOptions() {
    const speed = document.getElementById("speech-rate")
    speed.innerHTML = ""
    for (const key of Object.keys(SPEEDS)) {
      const o = el("option", "", SPEED_LABELS[key]); o.value = key; speed.appendChild(o)
    }
  }

  async function init({readCurrent}) {
    setupSpeedOptions()
    document.getElementById("btn-coach").addEventListener("click", async () => {
      if (toggle(coachPanel, "btn-coach")) { await refresh(); renderCoach() }
    })
    document.getElementById("btn-settings").addEventListener("click", () => {
      if (toggle(settingsPanel, "btn-settings")) renderSettings()
    })
    for (const btn of document.querySelectorAll(".panel-close")) btn.addEventListener("click", closePanels)
    document.addEventListener("keydown", e => { if (e.key === "Escape") closePanels() })
    document.getElementById("btn-read-aloud").addEventListener("click", () => {
      narrator.enabled = !narrator.enabled
      persist()
      syncToggle()
      if (!narrator.enabled) narrator.stop()
      else readCurrent()  // start with what's on screen, so the learner hears it works
    })
    document.getElementById("btn-stop-speech").addEventListener("click", () => narrator.stop())
    if (typeof window !== "undefined" && window.speechSynthesis && window.speechSynthesis.addEventListener) {
      window.speechSynthesis.addEventListener("voiceschanged", () => {
        if (!settingsPanel.classList.contains("hidden")) renderSettings()
      })
    }
    await Promise.all([loadVoices(), refresh()])
    adoptServerPrefs()
    adoptServerSounds()
    syncToggle()
    document.getElementById("speech-controls").classList.toggle("hidden", !narrator.supported)
  }

  return {init, refresh, offerOnboarding, level, closePanels, get profile() { return state.profile }}
}
