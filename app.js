"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
// Safe DOM builder: strings always become text nodes, never HTML.
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}
async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
  let r;
  try { r = await fetch(path, o); } catch { throw new Error("Cannot reach the TraceVision server."); }
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.error) || `Request failed (${r.status})`);
  return data;
}
function toast(msg, kind = "") {
  const t = h("div", { class: "t " + kind }, msg);
  $("#toast").append(t); setTimeout(() => t.remove(), 4000);
}
const fmt = ts => new Date(ts).toLocaleString();
const loading = () => [1, 2, 3].map(() => h("div", { class: "skel" }));
const emptyState = (title, text) => h("div", { class: "empty" },
  (() => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24");
    s.innerHTML = '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>'; return s; })(),
  h("b", {}, title), h("span", {}, text));
const errBox = (e, retry) => h("div", { class: "empty" }, h("b", { class: "err" }, "Could not load this view"), h("span", {}, e.message),
  retry ? h("button", { class: "btn", onclick: retry }, "Try again") : null);
const setView = (el, ...kids) => { el.replaceChildren(...kids.flat()); };

let view = "dashboard", cam = {}, lastSig = "", lastMotionId, firstPoll = true, motionTimer;
const TITLES = { dashboard: "Dashboard", live: "Live Camera", events: "Security Events", incidents: "Incidents", cameras: "Camera Management", history: "Event History", audit: "Audit Log", settings: "Settings" };

// ---------- navigation
function go(v) {
  view = v;
  $$(".side button").forEach(b => { b.classList.toggle("active", b.dataset.view === v); b.toggleAttribute("aria-current", b.dataset.view === v); });
  $$(".view").forEach(s => s.hidden = s.id !== "view-" + v);
  $("#view-title").textContent = TITLES[v]; history.replaceState(null, "", "#" + v);
  refresh();
}
$$(".side button").forEach(b => b.addEventListener("click", () => go(b.dataset.view)));
function refresh() {
  ({ dashboard: loadDashboard, live: renderLive, events: () => loadEvents("events"), history: () => loadEvents("history"),
     incidents: loadIncidents, cameras: renderCamMgmt, audit: loadAudit, settings: loadSettings })[view]();
}

// ---------- badges and event cards
const SRC = { motion: ["Real motion candidate", "motion"], simulated: ["DEMO (simulated)", "simulated"], camera: ["Camera error", "camera"] };
const ACTION = { acknowledged: "Acknowledge", reviewed: "Mark reviewed", verified: "Verify as real", dismissed: "Dismiss", closed: "Close" };
const srcBadge = s => h("span", { class: "badge " + SRC[s][1] }, SRC[s][0]);
function eventCard(e, withActions = true) {
  const acts = [];
  if (withActions) {
    for (const s of e.allowed) acts.push(h("button", { class: "btn sm", onclick: () => eventAction(e.id, s) }, ACTION[s]));
    if (["acknowledged", "reviewed", "verified"].includes(e.status))
      acts.push(h("button", { class: "btn sm primary", onclick: () => eventToIncident(e) }, "Create incident"));
  }
  return h("div", { class: "item " + e.severity },
    h("div", { class: "row sp" }, h("div", { class: "row" }, h("b", {}, `#${e.id} ${e.event_type.replace(/_/g, " ")}`), srcBadge(e.source_type),
      h("span", { class: "badge" }, e.severity), h("span", { class: "badge" + (e.status === "verified" ? " ok" : "") }, e.status)), h("span", { class: "time" }, fmt(e.ts))),
    e.note ? h("p", {}, e.note) : null, acts.length ? h("div", { class: "actions" }, acts) : null);
}
async function eventAction(id, status) {
  try { await api(`/api/events/${id}/status`, { method: "POST", body: { status } }); toast(`Alert ${id}: ${ACTION[status].toLowerCase()} done`, "good"); refresh(); poll(); }
  catch (e) { toast(e.message, "bad"); }
}
async function eventToIncident(e) {
  try { const i = await api(`/api/events/${e.id}/incident`, { method: "POST", body: {} }); toast(`Incident ${i.id} created`, "good"); openIncident(i.id); poll(); }
  catch (err) { toast(err.message, "bad"); }
}
function filters(list) {
  const f = $(`.filters[data-list="${list}"]`);
  const p = new URLSearchParams({ source: $(".f-source", f).value, status: $(".f-status", f).value, severity: $(".f-sev", f).value, q: $(".f-q", f).value });
  if (list === "events") p.set("active", "1");
  return p;
}
async function loadEvents(list) {
  const box = $("#list-" + list); if (!box.children.length) setView(box, loading());
  try {
    const ev = await api("/api/events?" + filters(list));
    if (!ev.length) return setView(box, emptyState("No events match", "New events appear here when the camera reports an error, motion is found, or you add a demo event."));
    if (list === "events") return setView(box, ev.map(e => eventCard(e)));
    setView(box, h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["ID", "Time", "Source", "Type", "Severity", "Status", "Note"].map(x => h("th", {}, x)))),
      h("tbody", {}, ev.map(e => h("tr", {}, h("td", {}, e.id), h("td", { class: "time" }, fmt(e.ts)), h("td", {}, SRC[e.source_type][0]), h("td", {}, e.event_type.replace(/_/g, " ")), h("td", {}, e.severity), h("td", {}, e.status), h("td", {}, e.note)))))));
  } catch (e) { setView(box, errBox(e, () => loadEvents(list))); }
}
$$(".filters[data-list]").forEach(f => { f.addEventListener("input", () => loadEvents(f.dataset.list)); });

// ---------- dashboard
const CARDS = [["events_total", "Events recorded", ""], ["events_needing_review", "Alerts awaiting review", "w"], ["events_real_motion", "Real motion candidates", "g"],
  ["events_simulated", "Demo events", ""], ["events_camera_errors", "Camera errors", ""], ["events_verified", "Human-verified events", "g"],
  ["incidents_open", "Open incidents", "w"], ["audit_entries", "Audit entries", ""]];
async function loadDashboard() {
  try {
    const d = await api("/api/dashboard"), cards = $("#cards");
    if (!cards.children.length || cards.querySelector(".skel")) setView(cards, CARDS.map(([k, l, c]) => h("div", { class: "card " + c }, h("b", { "data-k": k }, d.metrics[k]), h("span", {}, l))));
    else CARDS.forEach(([k]) => { const b = $(`[data-k=${k}]`, cards); if (b.textContent != d.metrics[k]) { b.textContent = d.metrics[k]; b.classList.add("bump"); setTimeout(() => b.classList.remove("bump"), 900); } });
    setView($("#dash-camera"), h("p", {}, cam.message || ""), h("p", { class: "hint" }, `State: ${cam.state}. Scanning: ${cam.scanning ? "on" : "off"}. Mode: ${cam.mode === "live" ? "live webcam" : "simulation (no webcam feed)"}.`));
    const ev = await api("/api/events?limit=5");
    setView($("#dash-events"), ev.length ? ev.map(e => eventCard(e, false)) : emptyState("No events yet", "Start the camera and turn on scanning, or add a demo event."));
  } catch (e) { setView($("#cards"), errBox(e, loadDashboard)); }
}
setView($("#cards"), loading());

// ---------- live camera
async function renderLive() {
  const run = cam.state === "running", busy = cam.state === "starting";
  $("#btn-start").disabled = run || busy; $("#btn-stop").disabled = !(run || busy);
  const sc = $("#scan-toggle"); sc.disabled = !run; sc.checked = !!cam.scanning;
  const img = $("#feed"), show = ["running", "unavailable", "error"].includes(cam.state);
  if (show && !img.getAttribute("src")) img.src = "/video_feed";
  if (!show && img.getAttribute("src")) img.removeAttribute("src");
  img.hidden = !show; $("#video-empty").hidden = show; $("#overlay").hidden = !run;
  $("#video-empty").replaceChildren(emptyState(busy ? "Opening webcam" : "Camera is off", "Press Start camera. Your browser is not used for capture; the server opens the webcam."));
  $("#live-msg").textContent = cam.state === "unavailable" || cam.state === "error" ? `${cam.message} Showing simulation mode. Demo events still work.` : cam.message || "";
  $("#live-msg").className = "hint " + (["unavailable", "error"].includes(cam.state) ? "err" : "");
}
async function camCall(path, body) { try { await api(path, { method: "POST", body }); } catch (e) { toast(e.message, "bad"); } await poll(); }
$("#btn-start").onclick = () => camCall("/api/camera/start");
$("#btn-stop").onclick = () => camCall("/api/camera/stop");
$("#scan-toggle").onchange = e => camCall("/api/camera/scan", { enabled: e.target.checked });
$$("[data-demo]").forEach(b => b.onclick = async () => { try { const e = await api("/api/demo/event", { method: "POST", body: { event_type: b.dataset.demo } }); toast(`DEMO event ${e.id} added`, "good"); poll(); } catch (er) { toast(er.message, "bad"); } });
$("#live-sens").oninput = e => $("#live-sens-out").textContent = e.target.value;
$("#live-sens").onchange = async e => { try { await api("/api/settings", { method: "POST", body: { sensitivity: +e.target.value } }); toast("Sensitivity saved", "good"); } catch (er) { toast(er.message, "bad"); } };
setInterval(() => { if (cam.state === "running") $("#stamp").textContent = new Date().toLocaleString(); }, 1000);

// ---------- camera management
async function renderCamMgmt() {
  let s = {}; try { s = await api("/api/settings"); } catch {}
  const row = (k, v) => h("tr", {}, h("th", {}, k), h("td", {}, v));
  setView($("#cam-mgmt"), h("h3", {}, "Built-in webcam (index " + (s.camera_index ?? "?") + ")"), h("table", {}, h("tbody", {},
    row("State", cam.state), row("Message", cam.message), row("Mode", cam.mode === "live" ? "Live webcam" : "Simulation (no feed)"), row("Resolution", cam.resolution ? cam.resolution.join(" x ") : "-"),
    row("Frames per second", cam.fps), row("Frames captured", cam.frames), row("Feed stalled", cam.stalled ? "Yes: no frame for 5 seconds" : "No"), row("Scanning", cam.scanning ? "On" : "Off"))),
    h("p", { class: "hint" }, "This build manages one local webcam. Change its index in Settings."));
}

// ---------- incidents
async function loadIncidents() {
  const box = $("#list-incidents"); if (!box.children.length) setView(box, loading());
  try {
    const list = await api("/api/incidents?" + new URLSearchParams({ q: $("#inc-q").value, status: $("#inc-status").value }));
    setView(box, list.length ? list.map(i => h("div", { class: "item " + i.severity }, h("div", { class: "row sp" }, h("div", { class: "row" }, h("b", {}, `#${i.id} ${i.title}`), h("span", { class: "badge" }, i.status), i.event_source ? srcBadge(i.event_source) : null,
      i.human_verified ? h("span", { class: "badge ok" }, "Human verified") : null), h("span", { class: "time" }, fmt(i.ts))),
      h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: () => openIncident(i.id) }, "Open")))) : emptyState("No incidents", "Create one here, or from an acknowledged alert."));
  } catch (e) { setView(box, errBox(e, loadIncidents)); }
}
$("#inc-q").oninput = loadIncidents; $("#inc-status").onchange = loadIncidents;
const drawer = $("#drawer");
function closeDrawer() { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); }
$("#drawer-close").onclick = closeDrawer;
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
function openDrawer(title, ...kids) { $("#drawer-title").textContent = title; setView($("#drawer-body"), kids); drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); $("#drawer-close").focus(); }
$("#inc-new").onclick = () => {
  const t = h("input", { required: true, maxlength: 120 }), d = h("textarea", { maxlength: 1000 }), s = h("select", {}, ["low", "medium", "high"].map(x => h("option", { value: x, selected: x === "medium" }, x))), msg = h("p", { class: "hint err" });
  openDrawer("New incident", h("form", { novalidate: true, onsubmit: async ev => {
    ev.preventDefault(); if (!t.value.trim()) { msg.textContent = "Title is required."; t.focus(); return; }
    try { const i = await api("/api/incidents", { method: "POST", body: { title: t.value, description: d.value, severity: s.value } }); toast("Incident created", "good"); loadIncidents(); poll(); openIncident(i.id); } catch (e) { msg.textContent = e.message; } } },
    h("label", {}, "Title", t), h("label", {}, "Description", d), h("label", {}, "Severity", s), h("button", { class: "btn primary", type: "submit" }, "Create incident"), msg));
};
async function openIncident(id) {
  try {
    const i = await api("/api/incidents/" + id), msg = h("p", { class: "hint err" });
    const res = h("textarea", { maxlength: 1000, placeholder: "Resolution summary (required to resolve)" }, i.resolution);
    const note = h("textarea", { maxlength: 1000, placeholder: "Operator note" });
    const act = async (fn) => { try { await fn(); loadIncidents(); poll(); openIncident(id); } catch (e) { msg.textContent = e.message; } };
    openDrawer(`Incident #${i.id}`, h("p", {}, h("b", {}, i.title)), h("p", { class: "hint" }, i.description || "No description."),
      h("div", { class: "row" }, h("span", { class: "badge" }, i.status), h("span", { class: "badge" }, i.severity), i.event_source ? srcBadge(i.event_source) : null,
        h("span", { class: "badge" + (i.human_verified ? " ok" : "") }, i.human_verified ? "Human verified" : "Not human verified")),
      h("p", { class: "hint" }, `Created ${fmt(i.ts)}. Updated ${fmt(i.updated_ts)}.`),
      h("h3", {}, "Status"), i.status === "closed" ? h("p", {}, i.resolution) : [res, h("div", { class: "actions" }, i.allowed.map(s => h("button", { class: "btn sm", onclick: () => act(() => api(`/api/incidents/${id}/status`, { method: "POST", body: { status: s, resolution: res.value } })) }, "Mark " + s)))],
      h("h3", {}, "Notes"), i.notes.length ? i.notes.map(n => h("div", { class: "item" }, h("span", { class: "time" }, fmt(n.ts)), h("p", {}, n.text))) : h("p", { class: "hint" }, "No notes yet."),
      i.status === "closed" ? null : [note, h("div", { class: "actions" }, h("button", { class: "btn sm primary", onclick: () => act(() => api(`/api/incidents/${id}/notes`, { method: "POST", body: { text: note.value } })) }, "Add note"))], msg);
  } catch (e) { toast(e.message, "bad"); }
}

// ---------- audit and settings
async function loadAudit() {
  const box = $("#list-audit"); if (!box.children.length) setView(box, loading());
  try {
    const a = await api("/api/audit");
    setView(box, a.length ? h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Time", "Actor", "Action", "Detail"].map(x => h("th", {}, x)))),
      h("tbody", {}, a.map(r => h("tr", {}, h("td", {}, r.id), h("td", { class: "time" }, fmt(r.ts)), h("td", {}, r.actor), h("td", {}, r.action), h("td", {}, r.detail)))))) : emptyState("Audit log is empty", "Actions will be recorded here."));
  } catch (e) { setView(box, errBox(e, loadAudit)); }
}
$("#btn-verify").onclick = async () => {
  const out = $("#verify-out"); out.textContent = "Checking..."; out.className = "hint";
  try { const r = await api("/api/audit/verify"); out.className = "hint " + (r.valid ? "" : "err");
    out.textContent = (r.valid ? `Chain intact: ${r.entries} entries checked.` : `Chain broken at entry ${r.first_bad_id}.`) + " " + r.note; } catch (e) { out.textContent = e.message; out.className = "hint err"; }
};
async function loadSettings() {
  try { const s = await api("/api/settings"); for (const k in s) $(`[name=${k}]`).value = s[k]; $("#live-sens").value = s.sensitivity; $("#live-sens-out").textContent = s.sensitivity; }
  catch (e) { $("#settings-msg").textContent = e.message; }
}
$("#settings-form").onsubmit = async ev => {
  ev.preventDefault(); const m = $("#settings-msg"), body = {};
  for (const i of $$("#settings-form input")) body[i.name] = i.value === "" ? null : Number(i.value);
  try { await api("/api/settings", { method: "POST", body }); m.textContent = "Settings saved."; m.className = "hint"; toast("Settings saved", "good"); }
  catch (e) { m.textContent = e.message; m.className = "hint err"; }
};

// ---------- polling and status
const PILL = { running: "ok", starting: "warn", stopped: "", unavailable: "warn", error: "bad" };
async function poll() {
  try {
    const d = await api("/api/dashboard"); cam = d.camera;
    const b = $("#pill-backend"); b.textContent = "Backend: online"; b.className = "pill ok";
    const c = $("#pill-camera"); c.textContent = "Camera: " + cam.state + (cam.stalled ? " (stalled)" : ""); c.className = "pill " + (cam.stalled ? "warn" : PILL[cam.state]);
    const s = $("#pill-scan"); s.textContent = "Scan: " + (cam.scanning ? "on" : "off"); s.className = "pill " + (cam.scanning ? "ok" : "");
    if (!firstPoll && cam.last_motion_event_id && cam.last_motion_event_id !== lastMotionId) {
      $("#video").classList.add("motion"); clearTimeout(motionTimer); motionTimer = setTimeout(() => $("#video").classList.remove("motion"), 3000);
      toast(`Motion candidate recorded (event ${cam.last_motion_event_id}). Not verified.`, "good");
    }
    lastMotionId = cam.last_motion_event_id; firstPoll = false;
    const sig = JSON.stringify(d.metrics) + cam.state + cam.scanning;
    if (sig !== lastSig) { lastSig = sig; refresh(); }
  } catch (e) { const b = $("#pill-backend"); b.textContent = "Backend: offline"; b.className = "pill bad"; }
}
setInterval(poll, 2000);

// ---------- startup: each line reflects a real response
async function boot() {
  const list = $("#boot-list"), line = (t, k) => list.append(h("li", { class: k }, t));
  try {
    const hl = await api("/api/health"); line("Backend responded", "ok"); line("Database: " + hl.database, hl.database === "ok" ? "ok" : "bad");
    line("Camera: " + hl.camera); line("Processing is local only");
  } catch (e) { line("Backend: " + e.message, "bad"); }
  await poll(); setTimeout(() => $("#boot").classList.add("done"), 700);
}
(function grid() { const c = $("#grid-bg"), x = c.getContext("2d"); c.width = innerWidth; c.height = innerHeight; x.strokeStyle = "rgba(0,229,255,.08)";
  for (let i = 0; i < c.width; i += 48) { x.beginPath(); x.moveTo(i, 0); x.lineTo(i, c.height); x.stroke(); }
  for (let j = 0; j < c.height; j += 48) { x.beginPath(); x.moveTo(0, j); x.lineTo(c.width, j); x.stroke(); } })();
boot().then(() => go(TITLES[location.hash.slice(1)] ? location.hash.slice(1) : "dashboard"));
