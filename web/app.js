// Stream Clipper GUI.
// Safety: every value that comes from a stream or a remote service (titles, transcripts, channel names, usernames,
// Post for Me errors, URLs) goes into the page through textContent or setAttribute, never innerHTML, and only
// http(s) links from that data become links. Every request that isn't a GET carries X-Clipper: 1.

// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false || kid === "") continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function svg(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
const webUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u.trim()) ? u.trim() : null);
const setText = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };
function say(el, kind, text) {
  if (!el) return;
  el.classList.remove("ok", "err", "busy");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
  el.hidden = !text;
}
const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

const store = {
  get(key, area = "localStorage") { try { return window[area].getItem(key); } catch { return null; } },
  set(key, value, area = "localStorage") {
    try { if (value === null) window[area].removeItem(key); else window[area].setItem(key, value); } catch { /* storage blocked: work without it */ }
  },
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") init.headers["X-Clipper"] = "1";
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail ?? data.error;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || `Stream Clipper answered ${res.status}`);
  }
  return data;
}

// Reuses elements by key, so a refresh never swaps a row out from under the pointer, the focus or a playing video.
function keyed(box, items, keyOf, make, update) {
  const old = new Map();
  for (const el of box.children) if (el.dataset.key) old.set(el.dataset.key, el);
  let prev = null;
  for (const it of items) {
    const k = String(keyOf(it));
    let el = old.get(k);
    if (el) old.delete(k);
    else { el = make(it); el.dataset.key = k; }
    if (update) update(el, it);
    let want = prev ? prev.nextElementSibling : box.firstElementChild;
    while (want && !want.dataset.key) want = want.nextElementSibling;   // rows on their way out stay where they are
    if (el !== want) box.insertBefore(el, want);
    prev = el;
  }
  for (const el of old.values()) el.remove();
}

// ---------- formatting ----------
const PLATFORM = { twitch: "Twitch", kick: "Kick", youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook",
  x: "X", threads: "Threads", bluesky: "Bluesky", linkedin: "LinkedIn", pinterest: "Pinterest" };
const plat = (p) => PLATFORM[p] || p || "";
const n0 = (n) => Number(n || 0).toLocaleString("en-GB");
const plural = (n, one, many = `${one}s`) => `${n0(n)} ${n === 1 ? one : many}`;
const nowS = () => Date.now() / 1000 + S.skew;
function ago(ts) {
  const s = Math.max(0, Math.round(nowS() - ts));
  if (s < 10) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) { const hr = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return m ? `${hr} h ${m} min ago` : `${hr} h ago`; }
  const d = Math.floor(s / 86400);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}
function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  const hr = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return hr ? `${hr}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
const every = (sec) => (sec % 60 ? `${sec} s` : `${sec / 60} min`);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function when(ts) {
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? clock(ts)
    : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}
const score1 = (v) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(1) : "");
const listing = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

// ---------- state ----------
function readTicked() {
  try { const t = JSON.parse(store.get("ticked", "sessionStorage") || "{}"); return t && typeof t === "object" ? t : {}; } catch { return {}; }
}
const S = {
  state: null, system: null, settings: null, skew: 0, tab: null,
  clips: [], current: null, lastPending: undefined, clipSeq: 0, decided: new Map(), busy: false, note: null,
  accounts: null, accountsError: null, accountsLoading: false,
  drafts: {}, ticked: readTicked(), queueLimit: 50,
  streamerLimit: 100, streamerFilter: "",
  postedFilter: "done", postedSeq: 0, postedKey: "", postedWaiting: false,
  testMsg: {}, saveMsg: null, settingsSeq: 0, dirty: false,
};
const saveTicked = () => store.set("ticked", JSON.stringify(S.ticked), "sessionStorage");
const cfgOr = (key, fallback) => (S.settings && S.settings[key] !== undefined ? S.settings[key] : fallback);

// ---------- theme ----------
const root = document.documentElement, darkMQ = matchMedia("(prefers-color-scheme: dark)");
const systemTheme = () => (darkMQ.matches ? "dark" : "light");
const theme = () => root.dataset.theme || systemTheme();
function paintTheme() {
  const next = theme() === "dark" ? "light" : "dark", b = $("#theme");
  b.setAttribute("aria-label", `Switch to ${next} theme`);
  b.title = `Switch to ${next} theme`;
}
$("#theme").addEventListener("click", () => {
  const next = theme() === "dark" ? "light" : "dark";
  if (next === systemTheme()) { delete root.dataset.theme; store.set("theme", null); }  // back to following the system
  else { root.dataset.theme = next; store.set("theme", next); }
  paintTheme();
});
darkMQ.addEventListener("change", paintTheme);

// ---------- routing ----------
const VIEWS = ["setup", "review", "streamers", "posted", "activity", "settings"];
function readHash() {
  let raw = location.hash.slice(1);
  try { raw = decodeURIComponent(raw); } catch { /* keep it raw */ }
  let m;
  if ((m = raw.match(/^clip-(\d+)$/))) return { tab: "review", clip: Number(m[1]) };
  if ((m = raw.match(/^(settings|streamers)-([a-z]+)$/))) return { tab: m[1], target: m[2] };
  return { tab: VIEWS.includes(raw) ? raw : null };
}
function toolsMissing() { const y = S.system; return !!y && !(y.ffmpeg && y.ffprobe && y.streamlink); }
const firstRun = () => !!S.state && (!S.state.streamers.length || toolsMissing());

function route(e) {
  const r = readHash(), prev = S.tab;
  const tab = r.tab || (firstRun() ? "setup" : "review");
  if (r.clip) S.current = r.clip;
  S.tab = tab;
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== tab;
  for (const a of $$(".tabs a")) {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (tab !== "review") $("#stage video")?.pause();   // a clip you can't see shouldn't keep talking
  renderSetupLink();
  if (tab === "review") loadClips();
  else if (tab === "streamers") {
    renderStreamers();
    if (r.target === "add" && e) $("#add-url").focus();
  } else if (tab === "posted") { S.postedKey = ""; loadPosted(); }
  else if (tab === "activity") renderLog();
  else if (tab === "settings") {
    if (prev !== "settings") S.saveMsg = null;
    // keep unsaved edits when you come back to the page; a deep link to a section always lands on it
    if (!S.dirty || !$("#settings-form").children.length) renderSettings(r.target, !!e);
    else if (r.target) goToGroup(r.target, !!e);
  } else if (tab === "setup") renderSetup(true);
  // focus follows in-page navigation only, so a plain page load never draws a focus ring
  if (e && !r.target && tab !== prev) $("#main").focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

// ---------- polling ----------
async function refresh() {
  if (S.refreshing) return;
  S.refreshing = true;
  let st;
  try { st = await api("/api/state"); }
  catch {
    const p = $("#pulse");
    p.classList.add("lost"); p.dataset.key = "";
    p.textContent = "Can't reach Stream Clipper. Is its window still open?";
    return;
  } finally { S.refreshing = false; }
  S.state = st; S.skew = st.now - Date.now() / 1000;
  renderPulse(); renderCounts(); renderSetupLink();
  if (S.tab === "streamers") renderStreamers();
  else if (S.tab === "activity") renderLog();
  else if (S.tab === "setup") renderSetup();
  else if (S.tab === "posted") { renderPostedTabs(); maybeReloadPosted(); }
  const pending = st.counts.pending || 0;
  if (pending !== S.lastPending) { S.lastPending = pending; if (S.tab === "review") loadClips(); else renderCounts(); }
  else if (S.tab === "review" && !cur()) renderStage();
}

function tick() {
  const t = nowS();
  for (const el of document.querySelectorAll("[data-since]")) setText(el, dur(t - Number(el.dataset.since)));
  for (const el of document.querySelectorAll("[data-ago]")) setText(el, ago(Number(el.dataset.ago)));
  for (const el of document.querySelectorAll("[data-until]")) setText(el, dur(Math.max(0, Number(el.dataset.until) - t)));
  for (const el of document.querySelectorAll("[data-next-check]")) setText(el, checkWords(el.dataset.nextCheck));
}

function renderCounts() {
  const n = S.lastPending ?? S.state?.counts.pending ?? 0;
  setText($("#count-pending"), n ? n0(n) : "");
  document.title = n ? `(${n0(n)}) Stream Clipper` : "Stream Clipper";
}

function renderPulse() {
  const st = S.state, p = $("#pulse");
  const rec = st.streamers.filter((s) => s.session_id), live = st.streamers.filter((s) => s.live && !s.session_id);
  document.body.classList.toggle("recording", rec.length > 0);
  const key = JSON.stringify([rec.map((s) => [s.channel, s.recording_since]), live.length, st.streamers.length, st.clips_24h, st.posted_24h]);
  if (p.dataset.key === key) return;
  p.dataset.key = key; p.classList.remove("lost");
  const parts = [];
  if (!st.streamers.length) parts.push("No streamers yet");
  else if (rec.length === 1) parts.push(h("span", { class: "rec", text: "Recording" }), ` ${rec[0].channel} `,
    h("span", { class: "num", "data-since": rec[0].recording_since, text: dur(nowS() - rec[0].recording_since) }));
  else if (rec.length > 1) parts.push(h("span", { class: "rec", text: `Recording ${rec.length}` }), ` of ${n0(st.streamers.length)}`);
  else if (live.length) parts.push(h("span", { class: "rec", text: `${n0(live.length)} live` }), ` of ${n0(st.streamers.length)}`);
  else parts.push(`${plural(st.streamers.length, "streamer")}, none live`);
  parts.push(h("span", { class: "pulse-tail", text: ` · ${plural(st.clips_24h, "clip")} today · ${n0(st.posted_24h)} posted` }));
  p.replaceChildren(...parts);
}

// ---------- setup ----------
const REQUIRED = ["system", "streamer", "postforme"];
function setupSteps() {
  const st = S.state, y = S.system, set = st ? st.setup : {};
  const tools = y ? y.ffmpeg && y.ffprobe && y.streamlink : false;
  const pfm = !!set.postforme_api_key && !S.accountsError && (S.accounts === null || S.accounts.length > 0);
  return [
    { id: "system", done: !!tools },
    { id: "streamer", done: !!st && st.streamers.length > 0 },
    { id: "discord", done: !!set.discord_webhook_url, optional: true },
    { id: "postforme", done: pfm },
    { id: "chat", done: !!(set.twitch_user_token || set.youtube_api_key), optional: true },
  ];
}
function renderSetupLink() {
  const link = $("#setup-link");
  if (!S.state) { link.hidden = true; return; }
  const steps = setupSteps(), done = steps.filter((s) => s.done).length;
  link.hidden = S.tab === "setup" || steps.every((s) => s.optional || s.done);
  const text = `${done} of ${steps.length}`;
  if (link.dataset.text !== text) { link.dataset.text = text; link.replaceChildren("Setup ", h("span", { class: "num", text })); }
}

function fixFor(tool) {
  const os = S.system?.platform || "win32";
  if (tool === "ffmpeg") return os === "win32" ? "winget install Gyan.FFmpeg" : os === "darwin" ? "brew install ffmpeg" : "sudo apt install ffmpeg";
  return os === "win32" ? ".venv\\Scripts\\python.exe -m pip install streamlink" : "python3 -m pip install streamlink";
}
function cmdLine(cmd) {
  const code = h("code", { text: cmd });
  const btn = h("button", { class: "btn", type: "button" }, "Copy");
  btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(cmd); btn.textContent = "Copied"; }
    catch {
      const r = document.createRange(); r.selectNodeContents(code);
      getSelection().removeAllRanges(); getSelection().addRange(r);
      btn.textContent = S.system?.platform === "darwin" ? "Press Cmd+C" : "Press Ctrl+C";
    }
    setTimeout(() => (btn.textContent = "Copy"), 2500);
  });
  return h("div", { class: "cmd" }, code, btn);
}
const recordingRate = () => String(cfgOr("quality", "720p")).startsWith("720p");
function diskWords(gb) {
  const hours = Math.floor(gb / 1.5);   // README: about 1.5 GB per hour per streamer at 720p
  return recordingRate() ? `${gb} GB free on this drive: about ${plural(hours, "hour")} of recording for one streamer at 720p.` : `${gb} GB free on this drive.`;
}

async function recheckSystem(btn) {
  btn.disabled = true; btn.textContent = "Checking…";
  try { S.system = await api("/api/system"); } catch { /* keep the last answer */ }
  btn.disabled = false; btn.textContent = "Check again";
  renderSetup(true); renderSetupLink();
}

function systemStep(step) {
  const y = S.system, body = h("div", { class: "step-body" });
  if (!y) {
    body.append(h("p", { text: "Couldn't ask Stream Clipper about this computer yet." }));
    return { title: "Check this computer", state: ["Unknown", "st-warn"], body, actions: [h("button", { class: "btn", type: "button", onclick: (e) => recheckSystem(e.currentTarget) }, "Check again")] };
  }
  const checks = h("ul", { class: "checks" });
  if (y.ffmpeg && y.ffprobe) checks.append(h("li", { text: "ffmpeg and ffprobe found." }));
  else if (!y.ffmpeg) checks.append(h("li", {}, h("span", { class: "err", text: "ffmpeg not found." }), " Install it with:", cmdLine(fixFor("ffmpeg")),
    h("p", { text: "Then close Stream Clipper's window, start it again and press Check again." })));
  else checks.append(h("li", {}, h("span", { class: "err", text: "ffprobe not found." }), " It comes with ffmpeg. Reinstall ffmpeg with:", cmdLine(fixFor("ffmpeg"))));
  if (y.streamlink) checks.append(h("li", { text: "streamlink found." }));
  else checks.append(h("li", {}, h("span", { class: "err", text: "streamlink not found." }), " In the Stream Clipper folder, run:", cmdLine(fixFor("streamlink")),
    h("p", { text: "Then start Stream Clipper again." })));
  const low = y.disk_free_gb < 20;
  checks.append(h("li", { class: low ? "warn" : "" }, low
    ? `Only ${y.disk_free_gb} GB free on this drive. Recording takes about 1.5 GB an hour for each streamer at 720p, so free up space before a long stream.`
    : diskWords(y.disk_free_gb)));
  if (cfgOr("use_whisper", true)) {
    checks.append(h("li", { text: y.whisper_cached ? `Speech model (${y.whisper_model}) downloaded.`
      : `Speech model (${y.whisper_model}) downloads the first time a clip is made, up to about 500 MB.` }));
  }
  body.append(checks);
  const state = step.done ? (low ? ["Low on space", "st-warn"] : ["Done", "st-done"]) : ["Missing", "st-missing"];
  return { title: "Check this computer", state, body, actions: [h("button", { class: step.done ? "btn quiet" : "btn", type: "button", onclick: (e) => recheckSystem(e.currentTarget) }, "Check again")] };
}

function setupStepView(step) {
  const st = S.state, set = st.setup, mainCls = step.current ? "btn primary" : "btn";
  if (step.id === "system") return systemStep(step);
  if (step.id === "streamer") {
    const names = st.streamers.map((s) => s.channel);
    const shown = names.length > 3 ? [...names.slice(0, 3), `${n0(names.length - 3)} more`] : names;
    return {
      title: "Add your first streamer", state: step.done ? ["Done", "st-done"] : ["To do", "st-todo"],
      body: h("div", { class: "step-body" }, step.done
        ? h("p", { class: "fact", text: `Tracking ${n0(names.length)}: ${listing(shown)}.` })
        : h("p", { text: `Paste a Twitch, Kick or YouTube channel link. Stream Clipper checks every ${every(st.poll_seconds)} and starts recording the moment they go live.` })),
      actions: [h("a", { class: step.done ? "btn quiet" : mainCls, href: "#streamers-add", text: step.done ? "Add another streamer" : "Add a streamer" })],
    };
  }
  if (step.id === "discord") {
    return {
      title: "Connect Discord", optional: true, state: step.done ? ["Done", "st-done"] : ["Optional", "st-optional"],
      body: h("div", { class: "step-body" }, step.done
        ? h("p", { class: "fact", text: "Connected. Alerts and new clips go to your Discord channel." })
        : h("p", { text: "Get a message in your Discord channel when someone goes live and when a clip is ready, with the clip attached." })),
      actions: [h("a", { class: step.done ? "btn quiet" : "btn", href: "#settings-discord", text: step.done ? "Change Discord" : "Connect Discord" })],
    };
  }
  if (step.id === "postforme") {
    const body = h("div", { class: "step-body" });
    let state = step.done ? ["Done", "st-done"] : ["To do", "st-todo"];
    const actions = [];
    if (!set.postforme_api_key) {
      body.append(h("p", { text: "Needed to post. Approve a clip and it goes to your TikTok, YouTube or Instagram through Post for Me (a $10 a month plan at postforme.dev)." }));
      actions.push(h("a", { class: mainCls, href: "#settings-postforme", text: "Connect Post for Me" }));
    } else if (S.accountsError) {
      state = ["Problem", "st-missing"];
      body.append(h("p", { class: "err", text: `Your key is saved, but Post for Me said: ${S.accountsError}` }));
      actions.push(h("a", { class: mainCls, href: "#settings-postforme", text: "Check the key" }));
    } else if (S.accounts === null) {
      body.append(h("p", { text: "Key saved. Checking your Post for Me accounts…" }));
    } else if (!S.accounts.length) {
      body.append(h("p", { text: "Key saved, but no accounts are connected in Post for Me yet. Connect TikTok, YouTube or Instagram in your Post for Me dashboard, then check again." }));
      actions.push(h("button", { class: mainCls, type: "button", onclick: () => loadAccounts(true) }, "Check again"),
        h("a", { class: "link", href: "https://www.postforme.dev", target: "_blank", rel: "noopener noreferrer", text: "Open postforme.dev" }));
    } else {
      const who = S.accounts.map((a) => `@${a.username || a.id} on ${plat(a.platform)}`);
      body.append(h("p", { class: "fact", text: `Ready to post to ${plural(S.accounts.length, "account")}: ${listing(who)}.` }));
      actions.push(h("a", { class: "btn quiet", href: "#settings-postforme", text: "Change Post for Me" }));
    }
    return { title: "Connect Post for Me", state, body, actions };
  }
  // chat signals
  const tw = set.twitch_user_token, yt = set.youtube_api_key, login = cfgOr("twitch_login", "");
  return {
    title: "Add chat signals", optional: true, state: step.done ? ["Done", "st-done"] : ["Optional", "st-optional"],
    body: h("div", { class: "step-body" },
      h("p", { text: "Chat speed and hype emotes help pick the best moments on Twitch and YouTube. Without them, clips are picked from loudness and laughter." }),
      h("ul", { class: "checks" },
        h("li", { text: tw ? `Twitch chat: connected${login ? ` as ${login}` : ""}.` : "Twitch chat: not connected." }),
        h("li", { text: yt ? "YouTube chat: key saved." : "YouTube chat: no key yet." }),
        h("li", { text: "Kick chat isn't read, so Kick clips use loudness and laughter only." }))),
    actions: [h("a", { class: "btn", href: "#settings-twitch", text: tw ? "Change Twitch" : "Connect Twitch chat" }),
      h("a", { class: "btn quiet", href: "#settings-youtube", text: yt ? "Change YouTube key" : "Add a YouTube key" })],
  };
}

function renderSetup(force) {
  if (!S.state) return;
  if (S.state.setup.postforme_api_key && S.accounts === null) loadAccounts();
  const steps = setupSteps(), done = steps.filter((s) => s.done).length;
  const list = $("#steps");
  const sig = JSON.stringify([steps.map((s) => s.done), S.system, S.state.streamers.length, S.state.streamers.slice(0, 3).map((s) => s.channel),
    S.state.setup, S.state.poll_seconds, S.accounts && S.accounts.map((a) => [a.id, a.username]), S.accountsError, cfgOr("twitch_login", ""), cfgOr("use_whisper", true)]);
  if (!force && list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  const ready = REQUIRED.every((id) => steps.find((s) => s.id === id).done);
  $("#setup-progress").replaceChildren(
    h("div", { class: "segs", "aria-hidden": "true" }, ...steps.map((s) => h("i", { class: s.done ? "done" : "" }))),
    h("p", {}, h("b", { class: "num", text: `${done} of ${steps.length}` }), " done", ready ? ". Ready: clips will arrive in Review." : ""));
  const currentId = (steps.find((s) => !s.optional && !s.done) || steps.find((s) => !s.done))?.id;
  list.replaceChildren(...steps.map((s, i) => {
    const v = setupStepView({ ...s, current: s.id === currentId });
    return h("li", { class: `step${s.id === currentId ? " current" : ""}`, id: `step-${s.id}` },
      h("span", { class: "step-n", text: String(i + 1) }),
      h("h2", { text: v.title }),
      h("span", { class: `step-state ${v.state[1]}`, text: v.state[0] }),
      v.body,
      v.actions.length ? h("div", { class: "do step-body" }, ...v.actions) : "");
  }));
}

// ---------- review ----------
const cur = () => S.clips.find((c) => c.id === S.current);

async function loadClips() {
  const seq = ++S.clipSeq;
  let clips;
  try { clips = await api("/api/clips?status=pending&limit=500"); } catch { return; }
  if (seq !== S.clipSeq) return;
  const t = Date.now();
  for (const [id, at] of S.decided) if (t - at > 20000) S.decided.delete(id);
  clips = clips.filter((c) => !S.decided.has(c.id));   // a reply that left before your decision must not bring the clip back
  if (S.current && !clips.some((c) => c.id === S.current)) {
    // a link to a clip that is no longer pending (a failed post, say) still opens it
    try {
      const c = await api(`/api/clips/${S.current}`);
      if (seq !== S.clipSeq) return;
      if ((c.status === "pending" || c.status === "failed") && !S.decided.has(c.id)) clips.unshift(c); else S.current = null;
    } catch { S.current = null; }
  }
  S.clips = clips;
  if (!S.current && clips.length) S.current = clips[0].id;
  const i = clips.findIndex((c) => c.id === S.current);
  if (i >= S.queueLimit) S.queueLimit = Math.ceil((i + 1) / 50) * 50;
  renderCounts();
  renderReview();
  if (clips.length && S.accounts === null) loadAccounts();
}

async function loadAccounts(force) {
  if (!S.state) return;   // not loaded yet: stay null so the next call tries again
  if (!S.state.setup.postforme_api_key) { S.accounts = []; S.accountsError = null; renderAccounts(); return; }
  if (S.accountsLoading && !force) return;
  S.accountsLoading = true;
  try { S.accounts = await api(`/api/accounts${force ? "?refresh=true" : ""}`); S.accountsError = null; }
  catch (e) { S.accounts = []; S.accountsError = e.message; }
  finally { S.accountsLoading = false; }
  renderAccounts(); renderSetupLink();
  if (S.tab === "setup") renderSetup(true);
}

function renderReview() {
  renderQueue();
  renderStage();
  renderPanel();
}

function queueItem(c) {
  return h("li", { class: "q" }, h("button", { class: "qb", type: "button", onclick: () => select(c.id) },
    h("span", { class: "q-thumb" }, c.thumb_url ? h("img", { src: c.thumb_url, alt: "", loading: "lazy", decoding: "async", width: 40, height: 71 }) : h("span", { class: "noimg" })),
    h("span", { class: "q-text" }, h("span", { class: "q-name", text: c.streamer_name }),
      h("span", { class: "q-meta" }, `${plat(c.platform)} · `, h("span", { "data-ago": c.created_at, text: ago(c.created_at) }), ` · ${dur(c.duration)}`)),
    c.signals?.manual ? h("span", { class: "q-score hand", text: "by hand" }) : h("span", { class: "q-score", text: score1(c.score) })));
}
function updateQueueItem(li, c) {
  const b = li.firstElementChild, on = String(c.id === S.current);
  if (b.getAttribute("aria-current") !== on) b.setAttribute("aria-current", on);
}

function renderQueue() {
  performance.mark("queue-start");
  const shown = S.clips.slice(0, S.queueLimit), rest = S.clips.length - shown.length;
  $(".queue").hidden = !S.clips.length;
  $(".review").classList.toggle("no-clips", !S.clips.length);
  const waiting = Math.max(S.lastPending ?? 0, S.clips.length);
  const head = $("#queue-count"), headText = `${waiting}`;
  if (head.dataset.text !== headText) {
    head.dataset.text = headText;
    head.replaceChildren(h("b", { text: n0(waiting) }), waiting === 1 ? " waiting" : " waiting · best first");
  }
  keyed($("#queue"), shown, (c) => c.id, queueItem, updateQueueItem);
  const foot = $("#queue-foot"), capped = (S.lastPending || 0) > S.clips.length;
  foot.hidden = !rest && !capped;
  if (!foot.hidden) {
    const parts = [];
    if (rest) parts.push(h("button", { class: "btn quiet", id: "queue-more", type: "button", onclick: showMoreClips }, `Show ${Math.min(50, rest)} more`), " ",
      `${n0(shown.length)} of ${n0(S.clips.length)} shown.`);
    if (capped) parts.push(` The best ${n0(S.clips.length)} of ${n0(S.lastPending)} are loaded; the rest move up as you decide.`);
    const hadFocus = document.activeElement?.id === "queue-more";
    foot.replaceChildren(...parts);
    if (hadFocus) $("#queue-more")?.focus();
  }
  performance.measure("queue-render", "queue-start");
}
function showMoreClips() { S.queueLimit += 50; renderQueue(); }

function select(id) {
  if (S.current === id || S.busy) return;
  S.current = id;
  history.replaceState(null, "", `#clip-${id}`);
  const i = S.clips.findIndex((c) => c.id === id);
  if (i >= S.queueLimit) S.queueLimit = Math.ceil((i + 1) / 50) * 50;
  renderReview();
  $(`#queue li[data-key="${id}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderStage() {
  const stage = $("#stage"), c = cur();
  if (!c) {
    const key = emptyKey();
    if (stage.dataset.clip !== key) { stage.dataset.clip = key; stage.replaceChildren(emptyState()); }
    return;
  }
  if (stage.dataset.clip === String(c.id)) return;   // keep the video playing across refreshes
  stage.dataset.clip = String(c.id);
  const video = h("video", { src: c.video, controls: true, autoplay: true, playsinline: true, preload: "auto", poster: c.thumb_url || null });
  video.setAttribute("aria-label", `Clip of ${c.streamer_name}`);
  video.muted = store.get("muted") !== "0";
  video.addEventListener("volumechange", () => store.set("muted", video.muted ? "1" : "0"));
  stage.replaceChildren(h("div", { class: "player" }, video), signalStrip(c, video));
}

function emptyKey() {
  const st = S.state;
  if (!st) return "empty:loading";
  const rec = st.streamers.filter((s) => s.session_id).map((s) => `${s.id}:${s.chunk_count}`).join();
  return `empty:${st.streamers.length}:${rec}:${st.counts.failed || 0}:${S.note ? S.note.at : ""}:${S.settings ? 1 : 0}`;
}

function clipPlan() {
  const pre = cfgOr("preroll", null), post = cfgOr("postroll", null), chunk = cfgOr("chunk_seconds", null);
  return { pre, post, chunk, len: pre !== null && post !== null ? pre + post : null };
}
// pipeline: piece k (from 0) is checked once piece k+1 holds the post-roll, plus CHUNK_GRACE (5 s)
function nextCheckIn(since) {
  const { chunk, post } = clipPlan(), wait = post + 5, t = nowS() - since;
  return Math.max(1, Math.floor((t - wait) / chunk) + 1) * chunk + wait - t;
}
function checkWords(list) {
  const s = Math.min(...String(list).split(",").map((x) => nextCheckIn(Number(x))));
  return s < 60 ? "under a minute" : `about ${plural(Math.round(s / 60), "minute")}`;
}

function emptyState() {
  const st = S.state, box = h("div", { class: "empty" });
  if (!st) return h("div", { class: "empty" }, h("p", { text: "Loading…" }));
  if (S.note) box.append(noteLine());
  const plan = clipPlan();
  if (!st.streamers.length) {
    const piece = plan.chunk === null ? "" : plan.chunk % 60 ? ` in ${plan.chunk}-second pieces` : ` in ${plan.chunk / 60}-minute pieces`;
    const cut = plan.len === null ? "The best become vertical clips." :
      `The best become ${plan.len}-second ${cfgOr("vertical", true) ? "vertical " : ""}clips: ${plan.pre} s of build-up and ${plan.post} s after.`;
    box.append(h("h2", { text: "Add the streamers you want to clip" }),
      h("p", { text: "Paste their channel links on the Streamers page. From then on it runs by itself:" }),
      h("ol", {},
        h("li", {}, h("span", { class: "num", text: "1" }), h("span", { text: `Every ${every(st.poll_seconds)} it checks who is live on Twitch, Kick and YouTube.` })),
        h("li", {}, h("span", { class: "num", text: "2" }), h("span", { text: `Live streams are recorded${piece}.` })),
        h("li", {}, h("span", { class: "num", text: "3" }), h("span", { text: `Each piece is scored for loud moments, laughter and chat hype. ${cut}` })),
        h("li", {}, h("span", { class: "num", text: "4" }), h("span", { text: "They wait here for you. Approve one and it posts to your accounts." }))),
      h("a", { class: "btn primary big", href: "#streamers-add", text: "Add a streamer" }));
    return box;
  }
  const rec = st.streamers.filter((s) => s.session_id);
  if (!rec.length) {
    const checked = st.streamers.map((s) => s.checked_at).filter(Boolean), n = st.streamers.length;
    box.append(h("h2", { text: "Nothing to review" }),
      h("p", { text: n === 1 ? "Your streamer isn't live right now." : `None of your ${n0(n)} streamers is live right now.` }),
      checked.length
        ? h("p", {}, "Last checked ", h("span", { "data-ago": Math.max(...checked), text: ago(Math.max(...checked)) }), `. It checks again every ${every(st.poll_seconds)}.`)
        : h("p", { text: "The first live check is running." }),
      h("button", { class: "btn", type: "button", onclick: (e) => checkNow(e.currentTarget) }, "Check now"));
  } else {
    box.append(h("h2", { text: "Nothing to review yet" }));
    for (const s of rec) {
      box.append(h("p", { class: "rec-line" }, h("span", { class: "rec", text: "Recording " }), `${s.channel} for `,
        h("span", { class: "num", "data-since": s.recording_since, text: dur(nowS() - s.recording_since) }), `, ${plural(s.chunk_count, "piece")} so far.`));
    }
    if (plan.chunk !== null && plan.post !== null) {
      const since = rec.map((s) => s.recording_since).join();
      box.append(h("p", {}, "Each piece is checked for moments once the next one has started. The next check is in ",
        h("span", { "data-next-check": since, text: checkWords(since) }), "."));
    }
    box.append(h("p", {}, "Saw something good? ",
      h("a", { class: "link", href: "#streamers", text: "Clip the last minute" }), " from the Streamers page."));
  }
  const failed = st.counts.failed || 0;
  if (failed) box.append(h("p", {}, `${plural(failed, "clip")} didn't post. `, h("a", { class: "link", href: "#posted", text: "See why in Posted" }), "."));
  return box;
}

function noteLine() {
  const n = S.note, c = n.clip, label = c.signals?.manual ? c.streamer_name : `${c.streamer_name} (${score1(c.score)})`;
  if (n.kind === "approved") {
    return h("p", { class: "note", role: "status" }, h("b", { class: "ok", text: "Sent " }),
      `${label} to Post for Me for ${plural(n.count, "account")}. `, h("a", { class: "link", href: "#posted", text: "Results show in Posted" }), ".");
  }
  return h("p", { class: "note", role: "status" }, h("b", { text: "Rejected " }), `${label}. `,
    n.error ? h("span", { class: "err", text: n.error }) : h("button", { class: "btn", type: "button", onclick: () => undoReject(c.id) }, "Undo"));
}

function signalStrip(c, video) {
  const sig = c.signals || {}, curve = sig.curve || {};
  const a = (curve.audio_db || []).filter((v) => Number.isFinite(v)), chat = curve.chat || [];
  const total = Math.max(1, Number(c.duration) || a.length || chat.length || 1);
  const hasChat = chat.some((v) => v > 0);
  const peak = typeof sig.peak_offset === "number" ? Math.min(Math.max(sig.peak_offset, 0), total) : null;
  const W = 1000, H = 96, VOL_TOP = 4, VOL_BOT = hasChat ? 60 : 92, CHAT_TOP = 68;
  const X = (t) => (t / total) * W;

  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" });
  s.append(svg("line", { class: "s-base", x1: 0, x2: W, y1: VOL_BOT, y2: VOL_BOT }));
  if (hasChat) {
    const max = Math.max(1, ...chat), bw = W / total;
    s.append(svg("line", { class: "s-base", x1: 0, x2: W, y1: H - 0.5, y2: H - 0.5 }));
    chat.forEach((v, i) => {
      if (v > 0) {
        const bh = (v / max) * (H - CHAT_TOP);
        s.append(svg("rect", { class: "s-chat", x: (X(i) + bw * 0.17).toFixed(1), y: (H - bh).toFixed(1), width: (bw * 0.66).toFixed(1), height: bh.toFixed(1) }));
      }
    });
  }
  if (a.length) {
    const lo = Math.min(...a), hi = Math.max(...a), span = Math.max(6, hi - lo);
    const pts = a.map((v, i) => `${X(i + 0.5).toFixed(1)},${(VOL_BOT - 3 - ((v - lo) / span) * (VOL_BOT - VOL_TOP - 3)).toFixed(1)}`).join(" ");
    s.append(svg("polyline", { class: "s-vol", points: pts }));
  }
  if (peak !== null) s.append(svg("line", { class: "s-peak", x1: X(peak), x2: X(peak), y1: 0, y2: H }));

  const playhead = h("div", { class: "playhead" });
  const hover = h("div", { class: "hover-line", hidden: true });
  const graph = h("div", {
    class: "graph", role: "slider", tabindex: "0",
    "aria-label": "Clip timeline: loudness and chat across the clip. Click, drag or use the arrow keys to jump.",
    "aria-valuemin": "0", "aria-valuemax": String(Math.round(total)), "aria-valuenow": "0", "aria-valuetext": `0:00 of ${dur(total)}`,
  }, s, hover, playhead);
  if (!a.length && !hasChat) graph.append(h("span", { class: "lane-note", text: "No sound or chat data for this clip" }));

  const nowEl = h("span", { class: "strip-now", text: `0:00 / ${dur(total)}` });
  const tip = h("span", { class: "a-tip", hidden: true });
  const head = h("div", { class: "strip-head" },
    h("span", { class: "k" }, h("i", { class: "k-vol" }), "Loudness"),
    hasChat ? h("span", { class: "k" }, h("i", { class: "k-chat" }), "Chat messages") : h("span", { class: "k", text: "No chat data" }),
    peak !== null ? h("span", { class: "k" }, h("i", { class: "k-peak" }), h("span", { text: `Peak at ${dur(peak)}` })) : "",
    nowEl);
  const axis = h("div", { class: "axis", "aria-hidden": "true" }, h("span", { text: "0:00" }), h("span", { class: "a-end", text: dur(total) }), tip);

  let lastSec = -1, raf = 0, dragging = false;
  const paint = () => {
    const t = video.currentTime || 0;
    playhead.style.left = `${Math.min(100, (t / total) * 100)}%`;
    const sec = Math.floor(t);
    if (sec !== lastSec) {
      lastSec = sec;
      nowEl.textContent = `${dur(sec)} / ${dur(total)}`;
      graph.setAttribute("aria-valuenow", String(sec));
      graph.setAttribute("aria-valuetext", `${dur(sec)} of ${dur(total)}`);
    }
  };
  const loop = () => { paint(); if (!video.paused && video.isConnected) raf = requestAnimationFrame(loop); };
  video.addEventListener("play", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(loop); });
  for (const ev of ["pause", "seeked", "timeupdate", "loadedmetadata"]) video.addEventListener(ev, paint);

  const seek = (t) => {
    const end = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : total;
    video.currentTime = Math.min(Math.max(t, 0), Math.max(0, end - 0.05));
    paint();
  };
  const at = (e) => { const r = graph.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); };
  graph.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    dragging = true;
    graph.setPointerCapture(e.pointerId);
    seek(at(e) * total);
  });
  graph.addEventListener("pointermove", (e) => {
    const f = at(e);
    hover.hidden = false; hover.style.left = `${f * 100}%`;
    tip.hidden = false; tip.style.left = `${f * 100}%`; tip.textContent = `Jump to ${dur(f * total)}`;
    if (dragging) seek(f * total);
  });
  graph.addEventListener("pointerup", () => { if (!dragging) return; dragging = false; video.play().catch(() => {}); });
  graph.addEventListener("pointercancel", () => { dragging = false; });
  graph.addEventListener("pointerleave", () => { if (!dragging) { hover.hidden = true; tip.hidden = true; } });
  graph.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -5, PageUp: 5 }[e.key];
    if (step !== undefined) { e.preventDefault(); seek((video.currentTime || 0) + step); }
    else if (e.key === "Home") { e.preventDefault(); seek(0); }
    else if (e.key === "End") { e.preventDefault(); seek(total); }
  });
  return h("div", { class: "strip" }, head, graph, axis);
}

function why(c) {
  const s = c.signals || {}, hasChat = (s.curve?.chat || []).some((v) => v > 0) || s.chat > 0;
  // the score is the sum of these parts: loudness + chat + emotes + 2 x the share of speech that was laughter
  const rows = [
    ["Loudness", s.audio, false, null],
    ["Chat speed", s.chat, true, hasChat ? null : "no chat data"],
    ["Hype emotes", s.hype, true, hasChat ? null : "no chat data"],
    ["Laughter", typeof s.laugh === "number" ? s.laugh * 2 : undefined, false, null],
  ];
  const total = rows.reduce((sum, r) => sum + (r[1] > 0 ? r[1] : 0), 0) || 1;
  const dl = h("dl", { class: "why", "aria-label": "What the score is made of" });
  for (const [label, v, chatty, none] of rows) {
    dl.append(h("dt", { text: label }));
    if (none || !(v > 0)) dl.append(h("dd", { class: "none", text: none || "none" }), h("dd", { class: "val" }));
    else dl.append(h("dd", {}, h("div", { class: `bar${chatty ? " chat" : ""}`, style: `width:${Math.max(2, (v / total) * 100).toFixed(1)}%` })),
      h("dd", { class: "val", text: `+${v.toFixed(1)}` }));
  }
  return dl;
}

function failedLine(c) {
  const p = c.post || {}, bad = (p.results || []).filter((r) => r.success === false).map((r) => `${plat(r.platform)}: ${r.error || "no reason given"}`);
  const reason = p.error || bad.join("; ") || "no reason given";
  return h("p", { class: "failed", text: `It didn't post last time (${reason}). Approve to try again.` });
}

function renderPanel(force) {
  const p = $("#panel"), c = cur();
  if (!c) { p.hidden = true; p.replaceChildren(); delete p.dataset.clip; return; }
  p.hidden = false;
  if (!force && p.dataset.clip === String(c.id)) return;   // keep what's being typed and ticked
  p.dataset.clip = String(c.id);
  const sig = c.signals || {}, manual = !!sig.manual, parts = [];
  if (S.note && S.note.clip.id !== c.id) parts.push(noteLine());

  const who = h("div", { class: "who" }, h("h2", { text: c.streamer_name }),
    h("p", {}, `${plat(c.platform)} · clipped `, h("span", { "data-ago": c.created_at, text: ago(c.created_at) }), ` · ${dur(c.duration)} long`));
  if (c.status === "failed") who.append(failedLine(c));
  parts.push(who);

  const score = h("section", { "aria-label": "Hype score" });
  if (manual) {
    score.append(h("div", { class: "score-row" }, h("span", { class: "score-num hand", text: "Clipped by hand" })),
      h("p", { class: "score-note", text: "Someone pressed Clip last minute while the stream was recording, so it has no hype score." }));
  } else {
    const rank = S.clips.findIndex((x) => x.id === c.id) + 1, waiting = S.clips.length;
    const note = c.status === "failed" || waiting < 2 ? "hype score" : rank === 1 ? `hype score, the best of ${n0(waiting)} waiting` : `hype score, number ${n0(rank)} of ${n0(waiting)} waiting`;
    score.append(h("div", { class: "score-row" }, h("span", { class: "score-num", text: score1(c.score) }), h("span", { class: "score-note", text: note })), why(c));
  }
  parts.push(score);

  const said = h("section", { class: "said" }, h("h3", { text: "What was said" }));
  if (c.transcript) said.append(h("p", { text: `“${c.transcript}”` }));
  else said.append(h("p", { class: "muted", text: cfgOr("use_whisper", true) ? "No words were picked up around the moment." : "Transcripts are off. Turn on “Listen for laughter” in Settings to get one." }));
  parts.push(said);

  const draft = S.drafts[c.id] ?? c.caption ?? "";
  const ta = h("textarea", { id: "caption", maxlength: 2200, rows: 4, "aria-describedby": "cap-count" });
  ta.value = draft;
  const count = h("span", { class: "cap-count", id: "cap-count" });
  const setCount = () => { count.textContent = `${n0(ta.value.length)} of 2,200 characters`; count.classList.toggle("near", ta.value.length > 2000); };
  setCount();
  ta.addEventListener("input", () => { S.drafts[c.id] = ta.value; setCount(); });
  ta.addEventListener("change", () => api(`/api/clips/${c.id}`, { method: "PATCH", body: { caption: ta.value } }).then(() => { c.caption = ta.value; }).catch(() => {}));
  parts.push(h("section", {}, h("div", { class: "cap-label" }, h("label", { for: "caption", text: "Caption" }), count), ta));

  parts.push(h("section", { "aria-labelledby": "post-to" }, h("h3", { id: "post-to", text: "Post to" }), h("div", { class: "accounts", id: "accounts" })));

  const keysHint = h("p", { class: "keys-hint" }, h("kbd", { text: "A" }), " approve · ", h("kbd", { text: "R" }), " reject · ",
    h("kbd", { text: "J" }), " ", h("kbd", { text: "K" }), " next, previous · ", h("button", { type: "button", onclick: openKeys }, "all keys"));
  parts.push(h("p", { class: "download" }, h("a", { class: "link", href: c.video, download: "", text: "Download the clip" }), " to post it yourself or keep a copy."));
  parts.push(h("section", { class: "decide-box", "aria-label": "Decide" },
    h("p", { class: "msg", id: "decide-msg", role: "status", hidden: true }),
    h("div", { class: "decide" },
      h("button", { class: "btn primary big approve", id: "approve", type: "button", onclick: approve }, "Approve and post"),
      h("button", { class: "btn big", id: "reject", type: "button", onclick: reject }, "Reject")),
    keysHint));
  p.replaceChildren(...parts);
  p.scrollTop = 0;
  renderAccounts();
}

function renderAccounts() {
  const box = $("#accounts"), c = cur();
  if (!box || !c) return;
  const key = S.state?.setup.postforme_api_key;
  box.replaceChildren();
  if (!key) box.append(h("p", { class: "msg" }, "Connect Post for Me to post from here. ", h("a", { class: "link", href: "#settings-postforme", text: "Add your key" })));
  else if (S.accounts === null) box.append(h("p", { class: "msg busy", text: "Loading your Post for Me accounts…" }));
  else if (S.accountsError) {
    box.append(h("p", { class: "msg err", text: `Post for Me: ${S.accountsError}` }),
      h("p", { class: "msg" }, h("button", { class: "link", type: "button", onclick: () => { S.accounts = null; renderAccounts(); loadAccounts(true); } }, "Try again")));
  } else if (!S.accounts.length) {
    box.append(h("p", { class: "msg" }, "No accounts are connected in Post for Me yet. Connect TikTok, YouTube or Instagram in your Post for Me dashboard, then ",
      h("button", { class: "link", type: "button", onclick: () => loadAccounts(true) }, "check again"), "."));
  } else {
    const defaults = new Set(cfgOr("default_accounts", []));
    const saved = Array.isArray(S.ticked[c.id]) ? S.ticked[c.id] : null;   // what you ticked for this clip beats the defaults
    for (const a of S.accounts) {
      const cb = h("input", { type: "checkbox", value: a.id, name: "acct" });
      cb.checked = saved ? saved.includes(a.id) : defaults.size ? defaults.has(a.id) : true;
      cb.addEventListener("change", () => { S.ticked[c.id] = $$('input[name="acct"]:checked', box).map((i) => i.value); saveTicked(); });
      const photo = webUrl(a.profile_photo_url);
      box.append(h("label", { class: "acct" }, cb, photo ? h("img", { src: photo, alt: "", referrerpolicy: "no-referrer" }) : "",
        h("span", { class: "who-a", text: `@${a.username || a.id}` }), h("span", { class: "plat", text: plat(a.platform) })));
    }
  }
  updateApprove();
}
function updateApprove() {
  const btn = $("#approve");
  if (btn) btn.disabled = S.busy || !S.state?.setup.postforme_api_key || !S.accounts?.length;
}

function trimMark() {
  if (calm()) return;
  const m = $("#mark");
  m.classList.add("trim");
  setTimeout(() => m.classList.remove("trim"), 450);
}

function leave(id) {
  S.decided.set(id, Date.now());
  const li = $(`#queue li[data-key="${id}"]`);
  return new Promise((done) => {
    const finish = () => { li?.remove(); nextAfter(id); done(); };
    if (!li || calm()) { finish(); return; }
    li.removeAttribute("data-key");
    li.classList.add("leaving");   // the brackets close on it and the row folds away
    setTimeout(finish, 240);
  });
}

function nextAfter(id) {
  const i = S.clips.findIndex((c) => c.id === id);
  if (i >= 0) S.clips.splice(i, 1);
  if (S.current === id || !S.clips.some((c) => c.id === S.current)) S.current = S.clips[Math.min(Math.max(i, 0), S.clips.length - 1)]?.id ?? null;
  delete S.drafts[id]; delete S.ticked[id]; saveTicked();
  history.replaceState(null, "", S.current ? `#clip-${S.current}` : "#review");
  if (typeof S.lastPending === "number") S.lastPending = Math.max(0, S.lastPending - 1);
  renderCounts();
  renderReview();
}

async function approve() {
  const c = cur(), msg = $("#decide-msg"), btn = $("#approve");
  if (!c || S.busy || !btn) return;
  if (btn.disabled) {
    say(msg, "err", S.state?.setup.postforme_api_key ? "There are no Post for Me accounts to post to yet." : "Connect Post for Me first: Settings, Post for Me.");
    return;
  }
  const accounts = $$('#panel input[name="acct"]:checked').map((i) => i.value);
  const ta = $("#caption"), caption = (ta?.value || "").trim();
  if (!caption) { say(msg, "err", "Write a caption first."); ta?.focus(); return; }
  if (!accounts.length) { say(msg, "err", "Tick at least one account to post to."); return; }
  S.busy = true; btn.disabled = true; btn.textContent = "Sending…";
  say(msg, "busy", "Uploading to Post for Me…");
  try {
    await api(`/api/clips/${c.id}/approve`, { method: "POST", body: { caption, accounts } });
    S.note = { kind: "approved", clip: c, count: accounts.length, at: Date.now() };
    trimMark();
    await leave(c.id);
  } catch (e) {
    say(msg, "err", e.message);
    btn.textContent = "Approve and post";
  } finally { S.busy = false; updateApprove(); }
}

async function reject() {
  const c = cur(), msg = $("#decide-msg");
  if (!c || S.busy) return;
  S.busy = true;
  try {
    await api(`/api/clips/${c.id}/reject`, { method: "POST" });
    S.note = { kind: "rejected", clip: c, at: Date.now() };
    await leave(c.id);
  } catch (e) { say(msg, "err", e.message); }
  finally { S.busy = false; updateApprove(); }
}

async function undoReject(id) {
  try { await api(`/api/clips/${id}/restore`, { method: "POST" }); }
  catch (e) {
    S.note = { ...S.note, error: `Couldn't undo: ${e.message}`, at: Date.now() };
    renderPanel(true); renderStage();
    return;
  }
  S.decided.delete(id); S.note = null; S.current = id;
  history.replaceState(null, "", `#clip-${id}`);
  S.lastPending = (S.lastPending || 0) + 1;
  $("#panel").dataset.clip = "";
  await loadClips();
}

function openKeys() { const d = $("#keys"); if (!d.open) d.showModal(); }

document.addEventListener("keydown", (e) => {
  if (e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;   // holding A must not approve a run of unwatched clips
  const el = document.activeElement;
  if (el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)) return;
  if ($("#keys").open) return;
  if (e.key === "?") { e.preventDefault(); openKeys(); return; }
  if (S.tab !== "review") return;
  const k = e.key.toLowerCase(), i = S.clips.findIndex((c) => c.id === S.current), video = $("#stage video");
  if (k === "j") { if (i < S.clips.length - 1) select(S.clips[i + 1].id); }
  else if (k === "k") { if (i > 0) select(S.clips[i - 1].id); }
  else if (k === "a") approve();
  else if (k === "r") reject();
  else if (e.key === " " && video && el !== video && !el?.closest("button, a, [role=tab]")) {
    e.preventDefault();
    if (video.paused) video.play().catch(() => {}); else video.pause();
  }
});

async function checkNow(btn) {
  btn.disabled = true; btn.textContent = "Checking…";
  try { await api("/api/monitor/check", { method: "POST" }); } catch { /* the reason lands in Activity */ }
  btn.disabled = false; btn.textContent = "Check now";
  refresh();
}

// ---------- streamers ----------
function renderStreamers() {
  const st = S.state;
  if (!st) return;
  performance.mark("streamers-start");
  setText($("#poll-every"), every(st.poll_seconds));
  const checked = st.streamers.map((s) => s.checked_at).filter(Boolean), line = $("#check-line");
  const lineKey = !st.streamers.length ? "none" : checked.length ? `${Math.max(...checked)}:${st.poll_seconds}` : "first";
  if (line.dataset.key !== lineKey) {
    line.dataset.key = lineKey;
    if (lineKey === "none") line.replaceChildren();
    else if (lineKey === "first") line.replaceChildren("First live check running…");
    else line.replaceChildren("Checked ", h("span", { "data-ago": Math.max(...checked), text: ago(Math.max(...checked)) }), `, every ${every(st.poll_seconds)}`);
  }
  $("#check-now").hidden = !st.streamers.length;
  const disk = $("#disk-line"), y = S.system;
  disk.hidden = !y;
  if (y) {
    const low = y.disk_free_gb < 20;
    disk.classList.toggle("low", low);
    setText(disk, low ? `Only ${y.disk_free_gb} GB free on this drive. Recording takes about 1.5 GB an hour for each streamer at 720p.` : diskWords(y.disk_free_gb));
  }
  const all = st.streamers, f = S.streamerFilter.trim().toLowerCase();
  const list = f ? all.filter((s) => s.channel.toLowerCase().includes(f) || plat(s.platform).toLowerCase().includes(f)) : all;
  const shown = list.slice(0, S.streamerLimit), rest = list.length - shown.length;
  $("#streamer-tools").hidden = all.length < 8 && !f;
  setText($("#streamer-shown"), f ? (list.length ? `${plural(list.length, "match", "matches")} of ${n0(all.length)}` : `No streamer matches “${S.streamerFilter.trim()}”.`) : `${plural(all.length, "streamer")}`);
  $("#streamers-head").hidden = !shown.length;
  $("#streamers-empty").hidden = !!all.length;
  keyed($("#streamer-list"), shown, (s) => s.id, streamerRow, updateStreamerRow);
  const more = $("#streamers-more");
  more.hidden = rest <= 0;
  if (rest > 0) setText(more, `Show ${Math.min(100, rest)} more (${n0(rest)} not shown)`);
  performance.measure("streamers-render", "streamers-start");
}
$("#streamers-more").addEventListener("click", () => { S.streamerLimit += 100; renderStreamers(); });
$("#streamer-filter").addEventListener("input", (e) => { S.streamerFilter = e.target.value; S.streamerLimit = 100; renderStreamers(); });

function streamerRow() {
  return h("div", { class: "srow", role: "row" },
    h("div", { class: "name", role: "cell" }, h("a", { target: "_blank", rel: "noopener noreferrer" }), h("span", { class: "sub" })),
    h("div", { class: "state", role: "cell" }),
    h("div", { class: "now", role: "cell" }),
    h("div", { class: "clips", role: "cell" }),
    h("div", { class: "acts", role: "cell" }),
    h("p", { class: "msg row-msg", role: "status", hidden: true }));
}

function statusParts(s) {
  if (s.session_id) return [h("b", { class: "rec", text: "Recording " }), h("span", { class: "num", "data-since": s.recording_since, text: dur(nowS() - s.recording_since) }),
    h("span", { class: "sub", text: `${plural(s.chunk_count, "piece")} saved so far` })];
  if (!s.enabled) return [h("b", { class: "muted", text: "Paused" }), h("span", { class: "sub", text: "Not checked until you resume" })];
  if (s.check_error) return [h("b", { class: "warn", text: "Check failed" }), h("span", { class: "sub", text: s.check_error })];
  if (s.live) return [h("b", { class: "rec", text: "Live" }), h("span", { class: "sub", text: "Not recording right now" })];
  return [h("b", { class: "muted", text: "Offline" }), s.checked_at
    ? h("span", { class: "sub" }, "Checked ", h("span", { "data-ago": s.checked_at, text: ago(s.checked_at) }))
    : h("span", { class: "sub", text: "Checking now…" })];
}

function updateStreamerRow(row, s) {
  const [name, state, now, clips, acts] = row.children;
  const link = name.firstElementChild, url = webUrl(s.url);
  if (url) { if (link.getAttribute("href") !== url) link.setAttribute("href", url); } else link.removeAttribute("href");
  setText(link, s.channel);
  setText(name.lastElementChild, plat(s.platform));
  row.classList.toggle("off", !s.enabled);
  const sig = JSON.stringify([s.session_id, s.recording_since, s.chunk_count, s.enabled, s.check_error, s.live, s.checked_at]);
  if (state.dataset.sig !== sig) { state.dataset.sig = sig; state.replaceChildren(...statusParts(s)); }
  setText(now, s.live ? [s.title, s.category].filter(Boolean).join(" · ") : s.last_clip_at ? `Last clip ${ago(s.last_clip_at)}` : "");
  const cs = `${s.clip_count}`;
  if (clips.dataset.sig !== cs) { clips.dataset.sig = cs; clips.replaceChildren(n0(s.clip_count), h("span", { class: "sub", text: s.clip_count === 1 ? " clip" : " clips" })); }
  // the buttons change only when what you can do changes, and never under the pointer, the focus or a running action
  const asig = `${!!s.session_id}:${s.enabled}`;
  const force = acts.dataset.force === "1";
  if (acts.dataset.sig !== asig && !acts.dataset.busy && !acts.dataset.confirm && (force || (!acts.matches(":hover") && !acts.contains(document.activeElement)))) {
    const focusAt = force ? acts.dataset.focusAt : null;
    acts.dataset.sig = asig; delete acts.dataset.force; delete acts.dataset.focusAt;
    acts.replaceChildren(...actionButtons(s, row));
    if (focusAt !== null && focusAt !== undefined) (acts.querySelectorAll("button")[Number(focusAt)] || acts.querySelector("button"))?.focus();
  }
}

function actionButtons(s, row) {
  const out = [];
  const secs = clipPlan().len;
  if (s.session_id) {
    out.push(h("button", { class: "btn", type: "button", title: secs ? `Makes a clip of the last ${secs} seconds` : null,
      onclick: (e) => act(e, row, `/api/streamers/${s.id}/clip`, "POST", "Clipping…", undefined,
        `Cutting the last ${secs ? `${secs} s` : "minute"}. It can take up to 3 minutes and opens in Review when it's ready.`) }, "Clip last minute"));
    out.push(h("button", { class: "btn quiet", type: "button", onclick: (e) => act(e, row, `/api/streamers/${s.id}/stop`, "POST", "Stopping…") }, "Stop recording"));
  }
  out.push(h("button", { class: "btn quiet", type: "button", onclick: (e) => act(e, row, `/api/streamers/${s.id}`, "PATCH", s.enabled ? "Pausing…" : "Resuming…", { enabled: !s.enabled }) },
    s.enabled ? "Pause" : "Resume"));
  out.push(h("button", { class: "btn quiet danger", type: "button", onclick: () => askRemove(row, s) }, "Remove"));
  return out;
}

async function act(e, row, path, method, busy, body, wait) {
  const btn = e.currentTarget, label = btn.textContent, acts = row.querySelector(".acts"), msg = row.querySelector(".row-msg");
  const index = [...acts.querySelectorAll("button")].indexOf(btn);
  btn.disabled = true; acts.dataset.busy = "1";
  if (busy) btn.textContent = busy;
  say(msg, wait ? "busy" : "", wait || "");
  let ok = false;
  try {
    const r = await api(path, { method, body });
    ok = true;
    say(msg, "", "");
    if (r.clip_id) { location.hash = `#clip-${r.clip_id}`; }
  } catch (err) { say(msg, "err", err.message); }
  finally {
    delete acts.dataset.busy;
    if (btn.isConnected) { btn.disabled = false; btn.textContent = label; }
  }
  if (ok) { acts.dataset.force = "1"; acts.dataset.focusAt = String(Math.max(0, index)); }
  refresh();
}

function askRemove(row, s) {
  const acts = row.querySelector(".acts");
  acts.dataset.confirm = "1";
  const cancel = h("button", { class: "btn quiet", type: "button" }, "Cancel");
  cancel.addEventListener("click", () => {
    delete acts.dataset.confirm; acts.dataset.sig = ""; acts.dataset.force = "1"; acts.dataset.focusAt = "999";
    const fresh = S.state.streamers.find((x) => x.id === s.id);
    if (fresh) updateStreamerRow(row, fresh);
  });
  const yes = h("button", { class: "btn danger-text", type: "button" }, "Remove");
  yes.addEventListener("click", async (e) => { delete acts.dataset.confirm; await act(e, row, `/api/streamers/${s.id}`, "DELETE", "Removing…"); });
  acts.replaceChildren(h("span", { class: "ask", text: `Stop tracking ${s.channel}? Its clips stay.` }), yes, cancel);
  cancel.focus();
}

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#add-url"), msg = $("#add-msg"), btn = e.target.querySelector("button[type=submit]");
  if (!input.value.trim()) { say(msg, "err", "Paste a channel link first, like https://www.twitch.tv/name"); input.setAttribute("aria-invalid", "true"); input.focus(); return; }
  btn.disabled = true;
  try {
    const s = await api("/api/streamers", { method: "POST", body: { url: input.value } });
    input.value = ""; input.removeAttribute("aria-invalid");
    say(msg, "ok", `Added ${s.channel} (${plat(s.platform)}). Checking if they're live now…`);
    refresh(); setTimeout(refresh, 6000);
  } catch (err) {
    say(msg, "err", err.message);
    input.setAttribute("aria-invalid", "true"); input.focus();
  } finally { btn.disabled = false; }
});
$("#add-url").addEventListener("input", (e) => e.target.removeAttribute("aria-invalid"));
$("#check-now").addEventListener("click", (e) => checkNow(e.currentTarget));

// ---------- posted ----------
for (const b of $$("#view-posted [role=tab]")) {
  b.addEventListener("click", () => choosePosted(b));
  b.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const other = $$("#view-posted [role=tab]").find((x) => x !== b);
    other.focus(); choosePosted(other);
  });
}
function choosePosted(b) {
  S.postedFilter = b.dataset.filter;
  for (const x of $$("#view-posted [role=tab]")) {
    const on = x === b;
    x.setAttribute("aria-selected", String(on));
    x.tabIndex = on ? 0 : -1;
  }
  $("#posted-list").setAttribute("aria-labelledby", b.id);
  $("#posted-list").replaceChildren();
  S.postedKey = "";
  loadPosted();
}

function maybeReloadPosted() {
  const c = S.state.counts, key = [c.posted, c.failed, c.posting, c.rejected].join();
  if (key !== S.postedKey || S.postedWaiting) loadPosted();
}

async function loadPosted() {
  const filter = S.postedFilter, seq = ++S.postedSeq, meta = $("#posted-meta");
  let clips;
  try { clips = await api(`/api/clips?status=${filter}&limit=200`); }
  catch (e) { say(meta, "err", `Couldn't load: ${e.message}`); return; }
  if (seq !== S.postedSeq) return;
  const c = S.state?.counts || {};
  S.postedKey = [c.posted, c.failed, c.posting, c.rejected].join();
  S.postedWaiting = clips.some((x) => x.status === "posting");
  keyed($("#posted-list"), clips, (x) => x.id, (x) => h("div", { class: "prow" }), updatePostedRow);
  const empty = $("#posted-empty");
  empty.hidden = !!clips.length;
  if (!clips.length) {
    const waiting = S.state?.counts.pending || 0;
    empty.textContent = filter === "done"
      ? `Nothing posted yet. Approved clips show here with a result for each account.${waiting ? ` ${plural(waiting, "clip")} ${waiting === 1 ? "is" : "are"} waiting in Review.` : ""}`
      : "No rejected clips. Clips you reject land here, and you can send any of them back to the queue.";
  }
  meta.classList.remove("err");
  setText(meta, clips.length >= 200 ? "Showing the latest 200" : "");
  renderPostedTabs();
}
function renderPostedTabs() {
  const c = S.state?.counts || {};
  setText($("#n-done"), n0((c.posting || 0) + (c.posted || 0) + (c.failed || 0)));
  setText($("#n-rejected"), n0(c.rejected || 0));
}

function updatePostedRow(row, c) {
  const sig = JSON.stringify([c.status, c.caption, c.post && [c.post.status, c.post.results, c.post.error], c.decided_at]);
  if (row.dataset.sig === sig || row.contains(document.activeElement)) return;
  row.dataset.sig = sig;
  const right = h("div", { class: "acts" }), results = h("ul", { class: "results" });
  if (c.status === "rejected") {
    right.append(h("button", { class: "btn", type: "button", onclick: (e) => restore(e, c) }, "Back to queue"));
  } else {
    const p = c.post;
    if (c.status === "posting") results.append(h("li", { class: "muted", text: p?.status === "uploading" ? "Uploading to Post for Me…" : "Sent. Waiting for each platform to confirm…" }));
    for (const r of p?.results || []) {
      const who = `${plat(r.platform)} @${r.username || r.account_id}`, url = webUrl(r.url);
      if (r.success === true) results.append(h("li", {}, h("b", { class: "ok", text: "Posted" }), url ? h("a", { class: "link", href: url, target: "_blank", rel: "noopener noreferrer", text: who }) : who));
      else if (r.success === false) results.append(h("li", {}, h("b", { class: "err", text: "Failed" }), `${who}: ${r.error || "no reason given"}`));
      else results.append(h("li", {}, h("b", { class: "muted", text: "Waiting" }), who));
    }
    if (c.status === "failed" && p?.error) results.append(h("li", { class: "err", text: p.error }));
    if (c.status === "failed") right.append(h("a", { class: "btn", href: `#clip-${c.id}`, text: "Try again" }));
  }
  row.replaceChildren(
    c.thumb_url ? h("img", { src: c.thumb_url, alt: "", loading: "lazy", decoding: "async" }) : h("span", { class: "noimg" }),
    h("div", {},
      h("div", { class: "title", text: `${c.streamer_name} · ${plat(c.platform)}${c.signals?.manual ? " · by hand" : ` · score ${score1(c.score)}`}` }),
      h("div", { class: "sub" }, "Clipped ", h("span", { "data-ago": c.created_at, text: ago(c.created_at) }),
        c.decided_at ? h("span", {}, `, ${c.status === "rejected" ? "rejected" : "approved"} `, h("span", { "data-ago": c.decided_at, text: ago(c.decided_at) })) : ""),
      h("p", { class: "cap", text: c.caption || "" }), results),
    right);
}

async function restore(e, c) {
  const btn = e.currentTarget;
  btn.disabled = true; btn.textContent = "Moving…";
  try {
    await api(`/api/clips/${c.id}/restore`, { method: "POST" });
    S.decided.delete(c.id);
    btn.closest(".prow")?.remove();
    if (!$("#posted-list").children.length) loadPosted();
  } catch (err) {
    btn.disabled = false; btn.textContent = "Back to queue";
    btn.closest(".prow")?.querySelector(".results")?.append(h("li", { class: "err", text: err.message }));
  }
  refresh();
}

// ---------- activity ----------
const KIND = { live: "Live", offline: "Offline", record: "Recording", chunk: "Analysis", clip: "New clip", post: "Posting",
  discord: "Discord", config: "Settings", error: "Problem" };
function renderLog() {
  const ev = S.state?.events || [];
  keyed($("#log"), ev, (e) => e.id, (e) => h("li", { class: `lvl-${e.level}` },
    h("time", { datetime: new Date(e.ts * 1000).toISOString(), title: new Date(e.ts * 1000).toLocaleString(), text: when(e.ts) }),
    h("span", { class: "kind", text: KIND[e.kind] || e.kind }),
    h("span", { class: "text", text: e.message })));
  $("#log-empty").hidden = !!ev.length;
  setText($("#activity-meta"), ev.length ? `The latest ${plural(ev.length, "event")}, newest first` : "");
}

// ---------- settings ----------
const RANGE = { poll_seconds: [30, 3600], chunk_seconds: [10, 3600], preroll: [1, 300], postroll: [1, 300], max_clips_per_chunk: [1, 20],
  min_gap_seconds: [10, 3600], keep_chunks_hours: [1, 720], score_threshold: [0.5, 20] };
const CONNECT = [
  { id: "discord", title: "Discord", optional: true,
    blurb: "A message in your Discord channel when a streamer goes live and when a clip is ready, with the clip attached.",
    how: ["In Discord, open the channel's settings.", "Integrations, Webhooks, New Webhook.", "Copy Webhook URL and paste it here."],
    fields: [{ key: "discord_webhook_url", label: "Webhook URL", placeholder: "https://discord.com/api/webhooks/…" }], test: ["discord", "Send a test message"] },
  { id: "postforme", title: "Post for Me",
    blurb: "Needed to post. Approved clips go to the TikTok, YouTube and Instagram accounts you connect in Post for Me ($10 a month plan).",
    how: ["Sign up at postforme.dev.", "Connect your social accounts in their dashboard.", "Create an API key and paste it here."],
    link: ["postforme.dev", "https://www.postforme.dev"],
    fields: [{ key: "postforme_api_key", label: "API key" }], test: ["postforme", "Test connection"] },
  { id: "twitch", title: "Twitch chat", optional: true,
    blurb: "Adds chat speed and emotes as signals for Twitch streams. Twitch live checks work without it.",
    how: ["At dev.twitch.tv/console/apps, register an app: any name, OAuth redirect http://localhost, category Other, client type Confidential.",
      "Paste its Client ID and a new secret here and save.", "Press Connect Twitch chat and enter the code Twitch shows you."],
    link: ["dev.twitch.tv/console/apps", "https://dev.twitch.tv/console/apps"],
    fields: [{ key: "twitch_client_id", label: "Client ID", plain: true }, { key: "twitch_client_secret", label: "Client secret" }], twitch: true },
  { id: "youtube", title: "YouTube chat", optional: true,
    blurb: "Adds YouTube live chat as a signal. Uses about 900 of the 10,000 free daily quota units for each hour of stream.",
    how: ["At console.cloud.google.com, enable YouTube Data API v3.", "Credentials, Create credentials, API key. Paste it here."],
    link: ["console.cloud.google.com", "https://console.cloud.google.com/apis/library/youtube.googleapis.com"],
    fields: [{ key: "youtube_api_key", label: "Data API key" }] },
  { id: "kick", title: "Kick", optional: true,
    blurb: "Live checks work without it. Kick chat isn't read: Kick only sends chat to a public web address.",
    link: ["kick.com/settings/developer", "https://kick.com/settings/developer"],
    fields: [{ key: "kick_client_id", label: "Client ID", plain: true }, { key: "kick_client_secret", label: "Client secret" }] },
];
const QUALITY = [["720p60,720p,best", "720p (recommended, about 1.5 GB an hour)"], ["1080p60,1080p,best", "1080p (sharper, uses more disk)"],
  ["480p,best", "480p (saves disk)"], ["best", "The best the stream offers"]];
const MODELS = [["tiny", "Tiny: fastest, least accurate"], ["base", "Base: balanced"], ["small", "Small: most accurate, slowest"]];

function group(id, title, blurb, fields, extra = {}) {
  const head = h("header", {}, h("h2", { id: `set-${id}-title` }, title, extra.optional ? h("span", { class: "opt", text: "Optional" }) : ""));
  if (blurb) head.append(h("p", { text: blurb }));
  if (extra.how) head.append(h("ol", {}, ...extra.how.map((t) => h("li", { text: t }))));
  if (extra.link) head.append(h("p", {}, "Get it at ", h("a", { class: "link", href: extra.link[1], target: "_blank", rel: "noopener noreferrer", text: extra.link[0] }), "."));
  return h("section", { class: "group", id: `set-${id}`, "aria-labelledby": `set-${id}-title` }, head, h("div", { class: "fields" }, ...fields));
}

function secretField(f, cfg) {
  const id = `f-${f.key}`, set = cfg[f.key] === true;
  const input = h("input", { id, name: f.key, type: "password", autocomplete: "off", spellcheck: "false", "data-secret": "1",
    placeholder: set ? "Saved. Paste a new one to replace it" : f.placeholder || "Paste it here" });
  const row = h("div", { class: "secret-row" }, input);
  if (set) row.append(h("button", { class: "btn quiet danger", type: "button", onclick: (e) => removeSecret(e, f) }, "Remove"));
  return h("div", { class: "field", "data-field": f.key },
    h("div", { class: "label" }, h("label", { for: id, text: f.label }), h("span", { class: `state ${set ? "ok" : "muted"}`, text: set ? "Saved" : "Not set" })),
    row);
}
function textField(f, cfg) {
  const id = `f-${f.key}`, input = h("input", { id, name: f.key, type: "text", spellcheck: "false", autocomplete: "off" });
  input.value = cfg[f.key] || ""; input.dataset.initial = input.value;
  return h("div", { class: "field" }, h("label", { for: id, text: f.label }), input);
}
async function removeSecret(e, f) {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    S.settings = await api("/api/settings", { method: "PUT", body: { [f.key]: "" } });
    btn.closest(".field").replaceWith(secretField(f, S.settings));
    if (f.key === "postforme_api_key") { S.accounts = null; S.testMsg.postforme = null; }
    if (f.key === "discord_webhook_url") S.testMsg.discord = null;
    refresh();
  } catch (err) { btn.disabled = false; say($("#save-status"), "err", err.message); }
}

function testRow(g) {
  const out = h("p", { class: "msg", role: "status", hidden: true });
  const prev = S.testMsg[g.test[0]];
  if (prev) say(out, prev[0], prev[1]);
  const btn = h("button", { class: "btn", type: "button" }, g.test[1]);
  btn.addEventListener("click", async () => {
    btn.disabled = true; say(out, "busy", "Testing…");
    let r;
    try {
      // a key pasted but not saved yet is the one you mean to test: save it first
      const unsaved = {};
      for (const f of g.fields) { const v = $(`#f-${f.key}`)?.value.trim(); if (v) unsaved[f.key] = v; }
      if (Object.keys(unsaved).length) {
        S.settings = await api("/api/settings", { method: "PUT", body: unsaved });
        for (const f of g.fields) if (unsaved[f.key]) $(`[data-field="${f.key}"]`)?.replaceWith(secretField(f, S.settings));
        S.accounts = null;
        refresh();
      }
      r = await api(`/api/test/${g.test[0]}`, { method: "POST" });
    } catch (err) { r = { ok: false, detail: err.message }; }
    const detail = g.test[0] === "discord" && r.ok ? "Sent. Check your Discord channel for the message." : r.detail;
    S.testMsg[g.test[0]] = [r.ok ? "ok" : "err", detail];
    say(out, r.ok ? "ok" : "err", detail);
    btn.disabled = false;
    if (g.test[0] === "postforme" && r.ok) { loadAccounts(true); fillDefaultAccounts(); }
  });
  return h("div", { class: "test-row" }, btn, out);
}

function twitchBox(cfg) {
  const line = h("div", { class: "test-row" }), out = h("div", { class: "device" });
  const btn = h("button", { class: "btn", type: "button", disabled: !cfg.twitch_client_id }, cfg.twitch_user_token ? "Reconnect Twitch chat" : "Connect Twitch chat");
  btn.addEventListener("click", () => twitchConnect(out, btn));
  line.append(btn);
  if (cfg.twitch_user_token) line.append(h("p", { class: "msg ok", text: `Chat connected as ${cfg.twitch_login || "your account"}.` }));
  else if (!cfg.twitch_client_id) line.append(h("p", { class: "msg", text: "Save your Client ID and secret first." }));
  return h("div", { class: "field", id: "twitch-box" }, line, out);
}

async function twitchConnect(out, btn) {
  out.replaceChildren(); btn.disabled = true;
  let f;
  try { f = await api("/api/twitch/device/start", { method: "POST" }); }
  catch (e) { out.append(h("p", { class: "msg err", text: e.message })); btn.disabled = false; return; }
  const verify = webUrl(f.verification_uri) || "https://www.twitch.tv/activate";
  const wait = h("p", { class: "msg busy", role: "status" }, "Waiting for you to approve. The code expires in ",
    h("span", { class: "num", "data-until": nowS() + f.expires_in, text: dur(f.expires_in) }), ".");
  out.append(h("p", { class: "help" }, "Open ", h("a", { class: "link", href: verify, target: "_blank", rel: "noopener noreferrer", text: verify.replace(/^https?:\/\//, "") }), " and enter this code:"),
    h("p", { class: "device-code", text: f.user_code }), wait);
  const started = Date.now(), every_ = Math.max(2, f.interval || 5) * 1000;
  const poll = async () => {
    if (!out.isConnected) return;   // the page was rebuilt: this attempt is over
    if (Date.now() - started > f.expires_in * 1000) { say(wait, "err", "The code expired. Press the button again for a new one."); btn.disabled = false; return; }
    try {
      const r = await api("/api/twitch/device/poll", { method: "POST" });
      if (r.status === "connected") {
        S.settings = { ...S.settings, twitch_user_token: true, twitch_login: r.login || "" };
        $("#twitch-box")?.replaceWith(twitchBox(S.settings));
        refresh();
        return;
      }
      if (r.status === "failed") { say(wait, "err", r.detail || "Twitch said no. Try again."); btn.disabled = false; return; }
    } catch (e) { say(wait, "err", e.message); btn.disabled = false; return; }
    setTimeout(poll, every_);
  };
  setTimeout(poll, every_);
}

function numField(cfg, key, label, help, unit = "", scale = 1) {
  const id = `f-${key}`, [lo, hi] = RANGE[key], value = String(+(cfg[key] / scale).toFixed(2));
  const input = h("input", { id, name: key, type: "number", inputmode: scale === 1 ? "numeric" : "decimal", min: +(lo / scale).toFixed(2), max: hi / scale,
    step: scale === 1 ? 1 : "any", "data-scale": scale, "aria-describedby": `${id}-help ${id}-err` });
  input.value = value; input.dataset.initial = value;
  input.addEventListener("change", () => checkField(input));
  return h("div", { class: "field" }, h("label", { for: id, text: label }),
    h("div", { class: "unit" }, input, unit ? h("span", { text: unit }) : ""),
    h("p", { class: "help", id: `${id}-help`, text: help }),
    h("p", { class: "field-error", id: `${id}-err`, hidden: true }));
}
function fieldError(input) {
  const [lo, hi] = RANGE[input.name], raw = input.value.trim(), v = Number(raw);
  if (raw === "" || !Number.isFinite(v)) return "Enter a number.";
  if (input.name === "chunk_seconds") {
    const s = Math.round(v * 60);
    return s < lo || s > hi ? "Use 0.17 to 60 minutes (10 seconds to an hour)." : "";
  }
  if (!Number.isInteger(v) || v < lo || v > hi) return `Use a whole number from ${lo} to ${hi}.`;
  return "";
}
function checkField(input) {
  const msg = fieldError(input), out = $(`#${input.id}-err`);
  if (msg) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
  if (out) { out.textContent = msg; out.hidden = !msg; }
  return !msg;
}

function clipWindow(pre, post) {
  const win = h("div", { class: "window", "aria-hidden": "true" }, h("span", { class: "w-pre" }), h("i", { class: "w-peak" }), h("span", { class: "w-post" }));
  const line = h("p", { class: "help", "aria-live": "polite" });
  const paint = () => {
    const a = Number(pre.value), b = Number(post.value);
    if (!(a > 0) || !(b > 0)) return;
    win.children[0].style.flexGrow = a; win.children[0].textContent = `${a} s build-up`;
    win.children[2].style.flexGrow = b; win.children[2].textContent = `${b} s after`;
    line.textContent = `Each clip is ${a + b} seconds long, and the moment lands ${a} seconds in.`;
  };
  pre.addEventListener("input", paint); post.addEventListener("input", paint);
  paint();
  return h("div", { class: "field" }, h("span", { class: "label", text: "The clip" }), win, line);
}

function sensitivity(cfg, scores) {
  const v = Number(cfg.score_threshold), id = "f-score_threshold";
  const input = h("input", { type: "range", id, name: "score_threshold", min: Math.min(1, v), max: Math.max(8, v), step: 0.05, "aria-describedby": `${id}-help` });
  input.value = String(v); input.dataset.initial = input.value;
  const out = h("output", { class: "range-val", for: id, text: v.toFixed(2) });
  input.addEventListener("input", () => (out.textContent = Number(input.value).toFixed(2)));
  const lo = Math.min(...scores), hi = Math.max(...scores);
  const help = "Every second of a stream gets a hype score from loudness, chat and emotes. A moment becomes a clip only if it scores at least this much. "
    + (scores.length ? `Your last ${plural(scores.length, "clip")} scored ${lo.toFixed(1)} to ${hi.toFixed(1)}.` : "Move it right if you get too many weak clips, left if you get too few.");
  return h("div", { class: "field" }, h("label", { for: id, text: "How big a moment must be" }),
    h("div", { class: "range" }, h("span", { class: "end", text: "More clips" }), input, h("span", { class: "end", text: "Fewer, bigger" })),
    h("p", { class: "help" }, "Set to ", out, ". ", h("span", { id: `${id}-help`, text: help })));
}

function checkbox(cfg, key, label, help) {
  const id = `f-${key}`, cb = h("input", { type: "checkbox", id, name: key, "aria-describedby": `${id}-help` });
  cb.checked = !!cfg[key]; cb.dataset.initial = String(cb.checked);
  return h("label", { class: "check", for: id }, cb, h("span", {}, h("b", { text: label }), h("p", { class: "help", id: `${id}-help`, text: help })));
}
function selectField(cfg, key, label, options) {
  const id = `f-${key}`, sel = h("select", { id, name: key });
  const known = options.some(([v]) => v === cfg[key]);
  for (const [v, text] of known ? options : [...options, [cfg[key], `Custom: ${cfg[key]}`]]) {
    const o = h("option", { value: v, text }); if (v === cfg[key]) o.selected = true; sel.append(o);
  }
  sel.dataset.initial = sel.value;
  return h("div", { class: "field" }, h("label", { for: id, text: label }), sel);
}

async function fillDefaultAccounts() {
  const box = $("#default-accounts");
  if (!box) return;
  if (!S.settings?.postforme_api_key) { box.replaceChildren(h("p", { class: "help", text: "Connect Post for Me above to choose which accounts start ticked." })); return; }
  box.replaceChildren(h("p", { class: "msg busy", text: "Loading your Post for Me accounts…" }));
  let accs;
  try { accs = await api("/api/accounts"); } catch (e) { box.replaceChildren(h("p", { class: "msg err", text: e.message })); return; }
  if (!box.isConnected) return;
  if (!accs.length) { box.replaceChildren(h("p", { class: "help", text: "No accounts are connected in Post for Me yet." })); return; }
  const chosen = new Set(S.settings.default_accounts || []);
  box.replaceChildren(...accs.map((a) => {
    const cb = h("input", { type: "checkbox", name: "default_accounts", value: a.id });
    cb.checked = chosen.size ? chosen.has(a.id) : true;
    return h("label", { class: "acct" }, cb, h("span", { class: "who-a", text: `@${a.username || a.id}` }), h("span", { class: "plat", text: plat(a.platform) }));
  }));
  box.dataset.initial = $$("input", box).filter((x) => x.checked).map((x) => x.value).sort().join();
}

async function renderSettings(target, focus) {
  const form = $("#settings-form"), seq = ++S.settingsSeq;
  let cfg, scores = [];
  try {
    [cfg, scores] = await Promise.all([api("/api/settings"),
      api("/api/clips?status=all&limit=100").then((cs) => cs.filter((c) => !c.signals?.manual).map((c) => c.score)).catch(() => [])]);
  } catch (e) {
    form.replaceChildren(h("div", { class: "page-head" }, h("h1", { text: "Settings" })), h("p", { class: "msg err", text: `Couldn't load settings: ${e.message}` }));
    return;
  }
  if (seq !== S.settingsSeq) return;
  S.settings = cfg; S.dirty = false;
  const steps = S.state ? setupSteps() : [], done = steps.filter((s) => s.done).length;
  const connect = CONNECT.map((g) => {
    const fields = g.fields.map((f) => (f.plain ? textField(f, cfg) : secretField(f, cfg)));
    if (g.twitch) fields.push(twitchBox(cfg));
    if (g.test) fields.push(testRow(g));
    return group(g.id, g.title, g.blurb, fields, g);
  });
  const pre = numField(cfg, "preroll", "Seconds of build-up before the moment", "How much of the lead-up to keep. 35 to 45 catches most build-ups.", "seconds");
  const post = numField(cfg, "postroll", "Seconds after the moment", "The reaction. Keep the whole clip over 60 seconds for TikTok Creator Rewards.", "seconds");
  const clipping = group("clipping", "How clips are cut", "What counts as a moment, and how much of it goes into the clip.", [
    h("div", { class: "cols" }, pre, post),
    clipWindow($("input", pre), $("input", post)),
    sensitivity(cfg, scores),
    h("div", { class: "cols" },
      numField(cfg, "chunk_seconds", "Record in pieces of", "Streams are saved in pieces (chunks) this long. Each piece is checked once the next one starts, so shorter pieces bring clips sooner but use more CPU.", "minutes", 60),
      numField(cfg, "max_clips_per_chunk", "Most clips from one piece", "Only the strongest moments in each piece become clips."),
      numField(cfg, "min_gap_seconds", "Seconds between two clips", "Two moments closer than this become one clip.", "seconds")),
  ]);
  const whisper = checkbox(cfg, "use_whisper", "Listen for laughter and write a transcript",
    "Runs a speech model on this computer for each clip (about 20 s each with the base model). Laughter lifts a clip up the queue.");
  const model = selectField(cfg, "whisper_model", "Speech model", MODELS);
  const syncModel = () => { $("select", model).disabled = !$("input", whisper).checked; };
  $("input", whisper).addEventListener("change", syncModel); syncModel();
  const recording = group("recording", "Recording", "How often it looks for live streams, and what it keeps on this computer.", [
    h("div", { class: "cols" },
      numField(cfg, "poll_seconds", "Check who's live every", "Each check takes a few seconds per streamer.", "seconds"),
      numField(cfg, "keep_chunks_hours", "Keep raw recordings for", "Recordings are deleted this long after they've been checked. Clips are always kept.", "hours")),
    selectField(cfg, "quality", "Recording quality", QUALITY),
    checkbox(cfg, "vertical", "Make clips vertical (9:16)", "For TikTok, Shorts and Reels: a blurred copy of the stream fills the space above and below. Off keeps the stream's own shape."),
    whisper, model,
  ]);
  const caption = h("input", { type: "text", id: "f-default_caption", name: "default_caption", maxlength: 2200, "aria-describedby": "f-default_caption-help" });
  caption.value = cfg.default_caption; caption.dataset.initial = caption.value;
  const example = S.state?.streamers[0]?.channel || "name";
  const preview = h("p", { class: "help", id: "f-default_caption-help" });
  const paintPreview = () => { preview.textContent = `Every new clip starts with this. {streamer} becomes the streamer's name, so a clip of ${example} starts as: ${caption.value.replaceAll("{streamer}", example)}`; };
  caption.addEventListener("input", paintPreview); paintPreview();
  const captions = group("captions", "Captions and accounts", "What a new clip starts with in Review.", [
    h("div", { class: "field" }, h("label", { for: "f-default_caption", text: "Starting caption" }), caption, preview),
    h("div", { class: "field" }, h("span", { class: "label", text: "Ticked by default when you review" }), h("div", { class: "accounts", id: "default-accounts" })),
  ]);
  const status = h("p", { id: "save-status", class: "msg", role: "status", hidden: true });
  if (S.saveMsg) say(status, S.saveMsg[0], S.saveMsg[1]);
  form.replaceChildren(
    h("div", { class: "page-head" }, h("h1", { id: "settings-title", text: "Settings" })),
    h("p", { class: "setup-note" }, steps.length ? `Setup checklist: ${done} of ${steps.length} done. ` : "", h("a", { class: "link", href: "#setup", text: "Open the setup checklist" })),
    ...connect, clipping, recording, captions,
    h("div", { class: "save-bar" }, h("button", { class: "btn primary", type: "submit" }, "Save settings"), status));
  fillDefaultAccounts();
  form.onsubmit = saveSettings;
  form.oninput = () => {
    S.dirty = Object.keys(collect(form)).length > 0;
    if (S.dirty) say($("#save-status"), "", "You have unsaved changes.");
    else if (!S.saveMsg) say($("#save-status"), "", "");
  };
  if (target) goToGroup(target, focus);
}

function goToGroup(target, focus) {
  const sec = $(`#set-${target}`);
  if (!sec) return;
  sec.scrollIntoView({ block: "start" });
  if (focus) sec.querySelector("input:not([type=hidden]), select, textarea, button")?.focus({ preventScroll: true });
}

function collect(form) {
  const out = {};
  for (const el of $$("input[name], select[name]", form)) {
    if (el.name === "default_accounts") continue;
    if (el.dataset.secret) { if (el.value.trim()) out[el.name] = el.value.trim(); continue; }
    const value = el.type === "checkbox" ? String(el.checked) : el.value;
    if (value === el.dataset.initial) continue;   // only what you changed is sent, so odd stored values are never rewritten
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = Math.round(Number(el.value) * Number(el.dataset.scale || 1));
    else if (el.type === "range") out[el.name] = Math.round(Number(el.value) * 100) / 100;
    else out[el.name] = el.value;
  }
  const box = $("#default-accounts"), defs = box ? $$('input[name="default_accounts"]', box) : [];
  if (defs.length) {
    const ticked = defs.filter((d) => d.checked).map((d) => d.value);
    if (ticked.slice().sort().join() !== box.dataset.initial) out.default_accounts = ticked;
  }
  return out;
}

async function saveSettings(e) {
  e.preventDefault();
  const form = $("#settings-form"), status = $("#save-status");
  const bad = $$("input[type=number]", form).filter((i) => !checkField(i));
  if (bad.length) {
    say(status, "err", bad.length === 1 ? "One setting needs fixing before it can save." : `${bad.length} settings need fixing before they can save.`);
    bad[0].focus();
    return;
  }
  const body = collect(form);
  if (!Object.keys(body).length) { say(status, "", "Nothing to save: you haven't changed anything."); return; }
  say(status, "busy", "Saving…");
  try {
    S.settings = await api("/api/settings", { method: "PUT", body });
    S.saveMsg = ["ok", `Saved at ${clock(Date.now() / 1000)}`];
    S.accounts = null; S.dirty = false;
    await renderSettings(null, false);
    refresh();
  } catch (err) {
    const m = err.message.match(/^(\w+) must be/), input = m && $(`#f-${m[1]}`);
    if (input) { input.setAttribute("aria-invalid", "true"); const out = $(`#f-${m[1]}-err`); if (out) { out.textContent = err.message; out.hidden = false; } input.focus(); }
    say(status, "err", err.message);
  }
}

// ---------- boot ----------
async function boot() {
  paintTheme();
  const [st, sys, cfg] = await Promise.allSettled([api("/api/state"), api("/api/system"), api("/api/settings")]);
  if (st.status === "fulfilled") {
    S.state = st.value; S.skew = st.value.now - Date.now() / 1000; S.lastPending = st.value.counts.pending || 0;
    renderPulse(); renderCounts();
  }
  if (sys.status === "fulfilled") S.system = sys.value;
  if (cfg.status === "fulfilled") S.settings = cfg.value;
  if (S.state?.setup.postforme_api_key) loadAccounts();
  route();
  if (!S.state) refresh();
  setInterval(refresh, 3000);
  setInterval(tick, 1000);
}
boot();
