// bt-call-agent web UI. No framework, no build step.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) if (k != null && k !== false) n.append(k);
  return n;
};
const SVG = "http://www.w3.org/2000/svg";
function icon(name) {
  const s = document.createElementNS(SVG, "svg");
  const u = document.createElementNS(SVG, "use");
  u.setAttribute("href", `#i-${name}`);
  s.append(u);
  s.setAttribute("aria-hidden", "true");
  return s;
}
function setIcon(svg, name) { svg.querySelector("use").setAttribute("href", `#i-${name}`); }

// ------------------------------------------------------------------ api

async function api(method, path, data) {
  const res = await fetch(path, {
    method,
    headers: data === undefined ? {} : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
    credentials: "same-origin",
  });
  if (res.status === 401) { location.reload(); throw new Error("Signed out"); }
  let out;
  try { out = await res.json(); } catch { throw new Error(`The agent answered ${res.status}`); }
  if (!out.ok) throw new Error(out.error || `The agent answered ${res.status}`);
  return out;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("toast");
  $("toast-text").textContent = msg;
  setIcon($("toast-icon"), isError ? "alert" : "check");
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 3000);
}

async function act(button, fn) {
  if (button) button.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { if (button) button.disabled = false; }
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { toast("Copying isn't allowed here: select the text instead", true); }
}

// ------------------------------------------------------------------ routing

const TABS = ["call", "task", "settings", "history", "system"];
const onShow = {};
let currentTab = null;

function route() {
  const [tab, arg] = (location.hash.slice(1) || "call").split("/");
  const name = TABS.includes(tab) ? tab : "call";
  currentTab = name;
  for (const t of TABS) {
    $(`view-${t}`).hidden = t !== name;
    const link = document.querySelector(`.nav [data-tab="${t}"]`);
    if (t === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  onShow[name]?.(arg && decodeURIComponent(arg));
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ formatting

function fmtDuration(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const ss = String(sec).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function fmtClock(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
function fileName(path) { return (path || "").split("/").pop(); }
function promptLabel(path) {
  const f = fileName(path);
  return !f ? "" : f === "default-prompt.txt" ? "Bundled test prompt" : f;
}

// ------------------------------------------------------------------ live call

const live = { status: "offline", call: null, partial: {}, logs: [], spokeAt: {} };
let state = null;   // /api/state

const STATUS = {
  offline: { badge: "Offline", title: "Can't reach the agent", detail: "Check that bt-call-agent is still running. Reconnecting…", icon: "alert", side: "Offline", sideDetail: "Reconnecting" },
  no_key: { badge: "Needs a key", title: "Add a Gemini API key", detail: "Calls are ignored until the agent has a key.", icon: "key", side: "No API key", sideDetail: "Calls are ignored" },
  waiting: { badge: "Waiting", title: "Waiting for a call", detail: "Make or answer a call with the phone's audio on this Bluetooth device. The agent takes it from there.", icon: "phone", side: "Waiting for a call", sideDetail: "The agent answers on its own" },
  connecting: { badge: "Connecting", title: "Joining the call", detail: "Opening the Gemini session…", icon: "phone", side: "Connecting", sideDetail: "Joining the call" },
  in_call: { badge: "On a call", title: "", detail: "", icon: "phone", side: "On a call", sideDetail: "" },
  agent_stopped: { badge: "Agent stopped", title: "You have the call", detail: "The agent left; the call is still up. Switch the phone's audio to the handset to talk.", icon: "user", side: "Agent stopped", sideDetail: "Call still up" },
};

function elapsed() { return live.call ? Date.now() / 1000 - live.call.started : 0; }

function renderStatus() {
  const s = live.status;
  const st = STATUS[s] || STATUS.offline;
  const card = $("call-card");
  card.dataset.state = s;
  $("side-status").dataset.state = s;
  $("call-badge").textContent = st.badge;
  const inCall = s === "in_call";
  const timer = inCall && live.call ? fmtDuration(elapsed()) : "";
  $("call-timer").textContent = timer;
  $("call-title").textContent = inCall ? speakingTitle() : st.title;
  $("call-detail").textContent = inCall ? "Both sides appear in the conversation as they speak." : st.detail;
  setIcon($("idle-svg"), st.icon);
  $("side-status-title").textContent = inCall ? `On a call ${timer}` : st.side;
  $("side-status-detail").textContent = inCall ? (live.call?.model || "") : st.sideDetail;
  const showMeta = (inCall || s === "connecting") && live.call;
  $("call-meta").hidden = !showMeta;
  if (showMeta) {
    $("meta-model").textContent = live.call.model;
    $("meta-task").textContent = promptLabel(live.call.prompt);
  }
  $("stop-agent").hidden = !(inCall || s === "connecting");
  $("add-key").hidden = s !== "no_key";
  $("live-tag").hidden = !inCall;
  document.title = inCall ? `● ${timer}: bt-call-agent` : "bt-call-agent";
  renderHealthLink();
  renderSpeaking();
}
setInterval(() => { if (live.status === "in_call") renderStatus(); }, 1000);

function speaking(who) { return live.status === "in_call" && Date.now() - (live.spokeAt[who] || 0) < 1400; }
function speakingTitle() {
  return speaking("agent") ? "The agent is speaking" : speaking("them") ? "The other side is speaking" : "On the line";
}

function renderSpeaking() {
  if (live.status === "in_call") $("call-title").textContent = speakingTitle();
  const now = Date.now();
  for (const party of document.querySelectorAll(".party")) {
    const who = party.dataset.who;
    party.classList.toggle("speaking", live.status === "in_call" && now - (live.spokeAt[who] || 0) < 1400);
  }
}
setInterval(renderSpeaking, 400);

const WHO = { them: "Them", agent: "Agent" };

function message(who, text, { at = null, partial = false } = {}) {
  const meta = el("div", { className: "msg-meta" }, icon(who === "agent" ? "bot" : "user"),
    el("b", { textContent: WHO[who] || who }), at != null ? el("span", { textContent: fmtDuration(at) }) : null);
  const bubble = el("div", { className: "bubble", textContent: text });
  if (partial) {
    const dots = el("span", { className: "typing" }, el("i"), el("i"), el("i"));
    bubble.append(dots);
  }
  return el("li", { className: `msg ${who}${partial ? " partial" : ""}` }, meta, bubble);
}

function renderConvo() {
  const scroller = $("convo-scroll");
  const mobile = matchMedia("(max-width: 860px)").matches;
  const box = mobile ? document.scrollingElement : scroller;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const call = live.call;
  const kids = (call?.lines || []).map((l) => message(l.who, l.text, { at: l.t - call.started }));
  for (const who of ["them", "agent"]) {
    if (live.partial?.[who]) kids.push(message(who, live.partial[who], { partial: true }));
  }
  if (call?.ended) kids.push(el("li", { className: "note", textContent: `Call ended after ${fmtDuration(call.ended - call.started)}` }));
  $("convo").replaceChildren(...kids);
  $("convo-empty").hidden = kids.length > 0;
  if (nearBottom && kids.length) box.scrollTop = box.scrollHeight;
}

function renderActivity() {
  const items = live.logs.filter((l) => !/^(👤|🤖)/.test(l.text)).slice(-40).reverse();
  $("activity").replaceChildren(...(items.length ? items.map((l) =>
    el("li", {}, el("time", { textContent: fmtClock(l.t) }), el("span", { textContent: l.text })))
    : [el("li")]));
}

function onEvent(ev) {
  switch (ev.type) {
    case "snapshot":
      Object.assign(live, { status: ev.status, call: ev.call, partial: ev.partial || {}, logs: ev.logs || [] });
      renderStatus(); renderConvo(); renderActivity(); renderPairing(ev.pairing);
      break;
    case "status":
      live.status = ev.status;
      if (ev.call) live.call = ev.call;
      if (ev.status === "connecting") { live.partial = {}; live.spokeAt = {}; }
      if (ev.status === "waiting" || ev.status === "no_key") loadHealth();
      renderStatus(); renderConvo();
      break;
    case "partial":
      live.partial[ev.who] = ev.text;
      live.spokeAt[ev.who] = Date.now();
      renderConvo(); renderSpeaking();
      break;
    case "line":
      delete live.partial[ev.who];
      live.call?.lines.push({ t: ev.t, who: ev.who, text: ev.text });
      renderConvo();
      break;
    case "call_ended":
      live.call = ev.call; live.partial = {};
      renderConvo();
      if (currentTab === "history") loadCalls();
      break;
    case "log":
      live.logs.push({ t: ev.t, text: ev.text });
      if (live.logs.length > 300) live.logs.shift();
      renderActivity();
      break;
    case "pairing":
      renderPairing(ev.pairing);
      break;
  }
}

function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  es.onerror = () => {
    live.status = "offline"; renderStatus();
    if (es.readyState === EventSource.CLOSED) {
      setTimeout(async () => {
        try { await api("GET", "/api/state"); } catch { /* api() reloads on 401 */ }
        connect();
      }, 3000);
    }
  };
}

$("stop-agent").addEventListener("click", (e) => act(e.currentTarget, async () => {
  if (!confirm("Take the agent out of this call? The call itself stays up, and you can continue it on the phone.")) return;
  await api("POST", "/api/agent/stop");
  toast("Agent stopped. The call is still up.");
}));

// ------------------------------------------------------------------ health (doctor)

let health = null, healthAt = 0;

async function loadHealth(force = false) {
  if (!force && health && Date.now() - healthAt < 30000) { renderHealthLink(); return health; }
  health = (await api("GET", "/api/doctor")).data;
  healthAt = Date.now();
  renderHealthLink();
  return health;
}

function renderHealthLink() {
  const a = $("health");
  const show = health && (live.status === "waiting" || live.status === "no_key");
  a.hidden = !show;
  if (!show) return;
  const bad = health.filter((c) => c.ok === false).length;
  a.classList.toggle("bad", bad > 0);
  a.replaceChildren(icon(bad ? "alert" : "check"),
    el("b", { textContent: bad ? `${bad} setup ${bad === 1 ? "problem" : "problems"}` : "Setup checks passed" }),
    el("span", { textContent: bad ? "Fix in System" : "Details" }));
}

// ------------------------------------------------------------------ state

async function loadState() {
  state = (await api("GET", "/api/state")).data;
  $("version").textContent = `Version ${state.version}`;
  $("calls-path").textContent = `Saved in ${state.transcripts_dir}`;
  return state;
}

// ------------------------------------------------------------------ task

let presets = [];
let promptDirty = false;

function setDirty(v) {
  promptDirty = v;
  $("prompt-dirty").hidden = !v;
  $("prompt-save").disabled = !v || state?.prompt.locked;
}

function renderPrompt() {
  const p = state.prompt;
  const box = $("prompt");
  if (!promptDirty) box.value = p.text;
  box.disabled = p.locked;
  $("prompt-source").textContent = p.bundled ? "Bundled test prompt" : fileName(p.path);
  $("prompt-source").title = p.path;
  const lock = $("prompt-locked");
  lock.hidden = !p.locked;
  lock.querySelector("span").textContent = p.locked
    ? `The agent was started with --prompt ${p.path}, so the task can't be changed here. Edit that file, or restart the agent without --prompt.`
    : "";
  $("preset-name").disabled = false;
  renderCount();
  setDirty(promptDirty);
  renderPresets();
}

function renderCount() {
  const n = $("prompt").value.trim().split(/\s+/).filter(Boolean).length;
  $("prompt-count").textContent = `${n} ${n === 1 ? "word" : "words"}`;
}

const BUILTIN_NAMES = { default: "Test call", "test-call-uk": "Test call in Ukrainian", "restaurant-booking": "Restaurant booking" };
function presetLabel(p) {
  if (!p.builtin) return p.name;
  return BUILTIN_NAMES[p.name] || p.name.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function renderPresets() {
  const current = $("prompt").value.trim();
  const sorted = [...presets.filter((p) => !p.builtin), ...presets.filter((p) => p.builtin)];
  $("presets").replaceChildren(...sorted.map((p) => {
    const btn = el("button", { type: "button", className: `preset${p.text.trim() === current ? " current" : ""}` },
      el("span", { className: "preset-name" }, presetLabel(p), p.builtin ? el("span", { className: "pill", textContent: "Example" }) : null),
      el("span", { className: "preset-text", textContent: p.text.trim() }));
    btn.disabled = !!state?.prompt.locked;
    btn.addEventListener("click", () => loadPreset(p));
    const wrap = el("li", { className: "preset-wrap" }, btn);
    if (!p.builtin) {
      const del = el("button", { type: "button", className: "btn icon ghost preset-del", title: "Delete", ariaLabel: `Delete ${p.name}` }, icon("trash"));
      del.addEventListener("click", (e) => act(e.currentTarget, () => deletePreset(p)));
      wrap.append(del);
    }
    return wrap;
  }));
}

function loadPreset(p) {
  const box = $("prompt");
  if (promptDirty && box.value !== p.text && !confirm("Replace the text in the editor? Your unsaved changes are lost.")) return;
  box.value = p.text;
  setDirty(box.value.trim() !== state.prompt.text.trim());
  renderCount(); renderPresets();
  if (promptDirty) toast(`Loaded “${presetLabel(p)}”. Save the task to use it.`);
}

async function deletePreset(p) {
  if (!confirm(`Delete the saved prompt “${p.name}”?`)) return;
  presets = (await api("DELETE", `/api/presets/${encodeURIComponent(p.name)}`)).data;
  renderPresets();
  toast(`Deleted “${p.name}”`);
}

async function savePrompt() {
  state.prompt = (await api("PUT", "/api/prompt", { text: $("prompt").value })).data;
  setDirty(false);
  renderPrompt();
  toast("Task saved. It applies from the next call.");
}

$("prompt").addEventListener("input", () => {
  setDirty($("prompt").value !== state.prompt.text);
  renderCount();
});
$("prompt").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); if (promptDirty) act($("prompt-save"), savePrompt); }
});
$("prompt-save").addEventListener("click", (e) => act(e.currentTarget, savePrompt));
$("save-as").addEventListener("submit", (ev) => {
  ev.preventDefault();
  act(ev.submitter, async () => {
    const name = $("preset-name").value.trim();
    if (!name) { $("preset-name").focus(); throw new Error("Give the prompt a name first"); }
    presets = (await api("POST", "/api/presets", { name, text: $("prompt").value })).data;
    $("preset-name").value = "";
    renderPresets();
    toast(`Saved “${name}” to the library`);
  });
});
window.addEventListener("beforeunload", (e) => { if (promptDirty) e.preventDefault(); });

onShow.task = () => act(null, async () => {
  await loadState();
  presets = (await api("GET", "/api/presets")).data;
  renderPrompt();
});

// ------------------------------------------------------------------ settings

const FLAGS = { model: "--model", voice: "--voice", language: "--language", greet: "--no-greet",
  greet_timeout: "--greet-timeout", out_volume: "--out-volume" };
const VOICE_STYLE = { Puck: "Upbeat", Charon: "Informative", Kore: "Firm", Fenrir: "Excitable",
  Aoede: "Breezy", Leda: "Youthful", Orus: "Firm", Zephyr: "Bright" };

function renderSettings() {
  const s = state.settings, e = s.effective;
  const locked = (k) => s.locked.includes(k);
  const voices = [null, ...s.voices];
  if (e.voice && !voices.includes(e.voice)) voices.push(e.voice);
  $("voices").replaceChildren(...voices.map((v) => {
    const b = el("button", { type: "button", className: "voice", role: "radio" },
      el("b", { textContent: v || "Default" }), el("span", { textContent: v ? (VOICE_STYLE[v] || "Custom") : "Model's choice" }));
    b.setAttribute("aria-checked", String((e.voice || null) === v));
    b.disabled = locked("voice");
    b.addEventListener("click", () => saveSetting({ voice: v }));
    return b;
  }));
  $("s-greet").checked = !!e.greet;
  $("s-greet_timeout").value = e.greet_timeout;
  $("s-language").value = e.language || "";
  $("s-out_volume").value = e.out_volume;
  $("s-out_volume-v").textContent = `${Math.round(e.out_volume * 100)}%`;
  $("s-model").value = e.model;
  for (const k of ["greet", "greet_timeout", "language", "out_volume", "model"]) $(`s-${k}`).disabled = locked(k);
  for (const note of document.querySelectorAll("[data-lock]")) {
    const k = note.dataset.lock;
    note.textContent = locked(k) ? `Set by ${FLAGS[k]} on the command line.` : "";
  }
  const err = $("settings-error");
  err.hidden = !s.error;
  err.querySelector("span").textContent = s.error ? `The settings file can't be read, so defaults are in use: ${s.error}` : "";
  renderKey();
}

let savedTimer;
async function saveSetting(changes) {
  await act(null, async () => {
    state.settings = (await api("PUT", "/api/settings", changes)).data;
    const tag = $("saved-tag");
    tag.hidden = false;
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => { tag.hidden = true; }, 2000);
  });
  renderSettings();
}

for (const input of document.querySelectorAll("[data-key]")) {
  input.addEventListener("change", () => {
    const k = input.dataset.key;
    let v = input.type === "checkbox" ? input.checked
      : input.type === "number" || input.type === "range" ? Number(input.value)
      : input.value.trim();
    if (k === "language" && !v) v = null;
    saveSetting({ [k]: v });
  });
}
$("s-out_volume").addEventListener("input", (e) => {
  $("s-out_volume-v").textContent = `${Math.round(e.target.value * 100)}%`;
});

$("settings-reset").addEventListener("click", (e) => act(e.currentTarget, async () => {
  if (!confirm("Put every setting back to its default?")) return;
  const changes = Object.fromEntries(Object.keys(state.settings.defaults).map((k) => [k, null]));
  state.settings = (await api("PUT", "/api/settings", changes)).data;
  renderSettings();
  toast("Defaults restored");
}));

const KEY_SOURCE = {
  file: { pill: ["Saved", "ok"], text: (k) => `Stored in ${k.file}, readable only by your user.` },
  env: { pill: ["From environment", "ok"], text: () => "Comes from GEMINI_API_KEY in the agent's environment, which wins over a saved key." },
  flag: { pill: ["From --key-file", "ok"], text: () => "Comes from --key-file on the command line, which wins over a saved key." },
  null: { pill: ["Missing", "warn"], text: () => "Get one at aistudio.google.com/apikey. The agent stays out of calls until it has one." },
};

function renderKey() {
  const k = KEY_SOURCE[state.key.source];
  $("key-pill").textContent = k.pill[0];
  $("key-pill").className = `pill ${k.pill[1]}`;
  $("key-status").textContent = k.text(state.key);
  $("key-save").textContent = state.key.source ? "Replace" : "Save";
}

$("key-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  act(ev.submitter, async () => {
    const key = $("key").value.trim();
    if (!key) { $("key").focus(); throw new Error("Paste the key first"); }
    const out = await api("PUT", "/api/key", { key });
    state.key = out.data;
    $("key").value = "";
    renderKey();
    toast(out.note || "Key saved. The next call uses it.");
  });
});

onShow.settings = () => act(null, async () => { await loadState(); renderSettings(); });

// ------------------------------------------------------------------ history

function callDate(name) {
  const m = /^call-(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)\.txt$/.exec(name);
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}
function dayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long",
    year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}
function timeLabel(d) { return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function lengthLabel(c) {
  const d = callDate(c.name);
  if (!d) return "";
  const s = c.mtime - d.getTime() / 1000;
  if (s < 1 || s > 6 * 3600) return "";
  return s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;
}
function previewText(raw) {
  return (raw || "").replace(/(?:👤 ?|🤖 ?)?\b(them|agent): /g, (_, who) => `${WHO[who]}: `).trim();
}
function parseTranscript(text) {
  const out = [];
  for (const raw of text.split("\n")) {
    const m = /^(?:👤 ?|🤖 ?)?(them|agent): (.*)$/.exec(raw);
    if (m) out.push({ who: m[1], text: m[2] });
    else if (raw.trim() && out.length) out[out.length - 1].text += "\n" + raw;
  }
  return out;
}

let calls = [];
let openName = null;

async function loadCalls() {
  calls = (await api("GET", "/api/transcripts")).data;
  renderCalls();
}

function renderCalls() {
  const kids = [];
  let lastDay = null;
  for (const c of calls) {
    const d = callDate(c.name);
    const day = d ? dayLabel(d) : "";
    if (day !== lastDay) { kids.push(el("div", { className: "day", textContent: day })); lastDay = day; }
    kids.push(el("a", { className: `call-item${c.name === openName ? " current" : ""}`, href: `#history/${encodeURIComponent(c.name)}` },
      el("span", { className: "call-item-top" }, el("b", { textContent: d ? timeLabel(d) : c.name }), el("span", { textContent: lengthLabel(c) })),
      el("p", { textContent: previewText(c.preview) || "No speech recorded" })));
  }
  $("calls").replaceChildren(...kids);
  $("calls-empty").hidden = calls.length > 0;
}

async function openCall(name) {
  const t = (await api("GET", `/api/transcripts/${encodeURIComponent(name)}`)).data;
  const d = callDate(t.name);
  const meta = calls.find((c) => c.name === name);
  $("reader-title").textContent = d ? `${dayLabel(d)}, ${timeLabel(d)}` : t.name;
  $("reader-sub").textContent = meta ? lengthLabel(meta) : "";
  const lines = parseTranscript(t.text);
  $("reader-convo").replaceChildren(...lines.map((l) => message(l.who, l.text)));
  $("reader-empty").hidden = lines.length > 0;
  if (!lines.length) {
    $("reader-empty").querySelector("b").textContent = "Nothing was said";
    $("reader-empty").querySelector("span:last-child").textContent = "The call ended before anyone spoke.";
  }
  $("reader-download").href = `/api/transcripts/${encodeURIComponent(name)}?download=1`;
}

function resetReader() {
  $("reader-title").textContent = "Pick a call";
  $("reader-sub").textContent = "";
  $("reader-convo").replaceChildren();
  $("reader-empty").hidden = false;
  $("reader-empty").querySelector("b").textContent = "Pick a call";
  $("reader-empty").querySelector("span:last-child").textContent = "Its conversation opens here.";
  for (const id of ["reader-download", "reader-delete"]) $(id).hidden = true;
}

onShow.history = (name) => act(null, async () => {
  if (!state) await loadState();
  openName = name || null;
  $("history-grid").classList.toggle("reading", !!name);
  await loadCalls();
  if (name) {
    for (const id of ["reader-download", "reader-delete"]) $(id).hidden = false;
    await openCall(name);
  } else {
    resetReader();
  }
});

$("reader-back").addEventListener("click", () => { location.hash = "#history"; });
$("reader-delete").addEventListener("click", (e) => act(e.currentTarget, async () => {
  const d = callDate(openName);
  if (!confirm(`Delete the transcript of the call on ${d ? `${dayLabel(d)}, ${timeLabel(d)}` : openName}? This can't be undone.`)) return;
  await api("DELETE", `/api/transcripts/${encodeURIComponent(openName)}`);
  toast("Transcript deleted");
  location.hash = "#history";
}));

// ------------------------------------------------------------------ system

async function runChecks() {
  $("health-summary").replaceChildren(el("b", { textContent: "Checking…" }));
  const found = await loadHealth(true);
  const bad = found.filter((c) => c.ok === false).length;
  const sum = $("health-summary");
  sum.classList.toggle("bad", bad > 0);
  sum.replaceChildren(el("span", { className: "sum-icon" }, icon(bad ? "alert" : "check")),
    el("div", {}, el("b", { textContent: bad ? `${bad} ${bad === 1 ? "problem" : "problems"} to fix` : "Everything looks right" }),
      el("small", { textContent: bad ? "Each one says how to fix it." : "The phone can use this machine as a headset." })));
  $("checks").replaceChildren(...found.map((c) => {
    const kind = c.ok === null ? "info" : c.ok ? "ok" : "bad";
    const fix = c.hint ? el("div", { className: "check-fix" }, el("code", { textContent: c.hint })) : null;
    if (fix) {
      const b = el("button", { type: "button", className: "btn icon ghost", title: "Copy", ariaLabel: "Copy" }, icon("copy"));
      b.addEventListener("click", () => copy(c.hint));
      fix.append(b);
    }
    return el("li", { className: kind },
      el("span", { className: "check-mark" }, icon({ ok: "check", bad: "x", info: "info" }[kind])),
      el("div", { className: "check-text" }, c.text.charAt(0).toUpperCase() + c.text.slice(1), fix));
  }));
}

let pairTimer = null;
function renderPairing(p) {
  const running = !!p?.running;
  $("pair-start").hidden = running;
  $("pair-stop").hidden = !running;
  $("pair-live").hidden = !running;
  $("pairing").replaceChildren(...(p?.messages || []).map((m) => el("li", { textContent: m })));
  clearInterval(pairTimer);
  if (running) {
    const until = (p.started || Date.now() / 1000) + (p.timeout || 120);
    const tick = () => { $("pair-count").textContent = `Visible, ${fmtDuration(until - Date.now() / 1000)} left`; };
    tick();
    pairTimer = setInterval(tick, 1000);
  }
  if (p && !running && p.result === 0 && currentTab === "system") runChecks().catch(() => {});
}

$("checks-run").addEventListener("click", (e) => act(e.currentTarget, runChecks));
$("pair-start").addEventListener("click", (e) => act(e.currentTarget, () => api("POST", "/api/pair", { timeout: 120 })));
$("pair-stop").addEventListener("click", (e) => act(e.currentTarget, () => api("DELETE", "/api/pair")));
for (const b of document.querySelectorAll("[data-copy]")) {
  b.addEventListener("click", () => copy($(b.dataset.copy).textContent));
}

onShow.system = () => act(null, runChecks);

// ------------------------------------------------------------------ start

connect();
act(null, async () => {
  await loadState();
  route();
  loadHealth().catch(() => {});
});
