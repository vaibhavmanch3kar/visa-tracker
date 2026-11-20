/* Visa slot tracker dashboard: vanilla JS, Chart.js, SSE. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids) if (c != null) n.append(c);
  return n;
};

const ui = { status: null, hmLoc: null, hmType: "OFC", chType: "OFC", chDays: 30, selDate: null, formLoaded: false, chart: null };
const PALETTE = ["#2457d6", "#d1495b", "#0f8a4b", "#b26b00", "#7b3fbf", "#00838f", "#5d6d7e"];

const fmtDate = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", weekday: "short" }) : "—";
const ago = (iso) => {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
const localISO = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

const sc = (state) => "s-" + String(state).replace(/\s+/g, "_");
function pill(node, state) {
  node.className = `pill ${sc(state)}`;
  node.lastElementChild.textContent = state;
}

/* ---------- rendering ---------- */
function render(st) {
  ui.status = st;
  const p = st.portal;
  pill($("#state"), p.state);
  $("#state").title = p.detail || "";
  const ch = $("#chrome");
  ch.className = "pill " + (p.chrome_connected ? (p.tabs ? "s-OK" : "s-WAITING") : "s-ERROR");
  ch.lastElementChild.textContent = p.chrome_connected ? (p.tabs ? `Chrome · ${p.tabs} portal tab${p.tabs > 1 ? "s" : ""}` : "Chrome · no portal tab") : "Chrome not connected";

  const pct = Math.min(100, Math.round((p.views_today / Math.max(1, p.budget)) * 100));
  const bar = $("#vbar");
  bar.style.width = pct + "%";
  bar.style.background = pct >= 85 ? "var(--bad)" : pct >= 60 ? "var(--warn)" : "var(--ok)";
  $("#vtext").textContent = `${p.views_today} / ${p.budget}`;

  renderBanner(st);
  renderLocs(st);
  renderCards(st);
  renderHeatLocSelect(st);
  renderHeat();
  renderSettings(st);
  renderAlerts(st);
  $("#channels").textContent = p.alert_channels.length ? p.alert_channels.join(", ") : "none configured (.env)";
}

function renderBanner(st) {
  const p = st.portal, b = $("#banner");
  let cls = "", msg = "";
  const over = p.views_today >= p.budget, near = p.views_today >= Math.ceil(p.budget * 0.75);
  if (["LOGIN NEEDED"].includes(p.state)) { cls = "bad"; msg = "Login needed: sign in again in the tracker's Chrome window. Nothing is captured until you do."; }
  else if (["LIMIT EXCEEDED", "ACCESS LIMITED", "BLOCKED", "RATE LIMITED"].includes(p.state)) { cls = "bad"; msg = `Portal says ${p.state}. Stop browsing the appointment page until the daily reset (reportedly midnight US Eastern). Continuing can freeze the account.`; }
  else if (p.state === "LIMIT WARNING" || over) { cls = "bad"; msg = "You're at or past your daily view budget. Close the appointment page for today."; }
  else if (near) { cls = "warn"; msg = `${p.views_today} of ${p.budget} portal views used today. Make the remaining ones count.`; }
  else if (!p.chrome_connected) { cls = "info"; msg = "Waiting for Chrome. Run  python -m tracker chrome  and log in to the portal in that window."; }
  else if (p.discovery_mode) { cls = "info"; msg = "Discovery mode is on: captured request/response shapes are written (redacted) to data/discovery.jsonl."; }
  b.hidden = !msg;
  b.className = "banner " + cls;
  b.textContent = msg;
}

function renderLocs(st) {
  const box = $("#locs");
  box.replaceChildren(...st.locations.map((l) =>
    el("label", { class: "chip" },
      el("input", { type: "checkbox", checked: l.selected, onchange: saveLocations, value: l.name }),
      el("span", { text: l.name }))));
}

async function saveLocations() {
  const sel = [...document.querySelectorAll("#locs input:checked")].map((i) => i.value);
  await postSettings({ selected_locations: sel });
}

function typeRow(label, d) {
  const sub = d.status === "NO DATA" ? "not seen yet" : `${d.open_days_90} open days / 90d · ${ago(d.last_seen)}`;
  return el("div", { class: "row" },
    el("span", { class: "k", text: label }),
    el("span", { class: "v", text: d.earliest ? fmtDate(d.earliest) : "—" }),
    el("span", { class: `pill ${sc(d.status)}` }, el("span", { class: "dot" }), el("span", { text: d.status })),
    el("span", { class: "sub", text: sub }));
}

function renderCards(st) {
  const sel = st.locations.filter((l) => l.selected);
  const box = $("#cards");
  if (!sel.length) { box.replaceChildren(el("p", { class: "muted", text: "Select at least one location." })); return; }
  box.replaceChildren(...sel.map((l) =>
    el("div", { class: "card" + (l.name === ui.hmLoc ? " active" : ""), tabindex: "0", onclick: () => { ui.hmLoc = l.name; ui.selDate = null; render(ui.status); } },
      el("h3", { text: l.name }),
      typeRow("OFC", l.OFC),
      typeRow("Interview", l.INTERVIEW))));
}

function renderHeatLocSelect(st) {
  const sel = st.locations.filter((l) => l.selected).map((l) => l.name);
  if (!sel.includes(ui.hmLoc)) ui.hmLoc = sel[0] || null;
  const s = $("#hmLoc");
  s.replaceChildren(...sel.map((n) => el("option", { value: n, text: n, selected: n === ui.hmLoc })));
}

function level(count) {
  if (count == null) return 2;          // date open, time slots not captured yet
  if (count <= 2) return 1;
  if (count <= 6) return 2;
  if (count <= 15) return 3;
  return 4;
}

function renderHeat() {
  const box = $("#heat");
  const loc = ui.status?.locations.find((l) => l.name === ui.hmLoc);
  if (!loc) { box.replaceChildren(); return; }
  const d = loc[ui.hmType];
  const avail = new Set(d.dates);
  const counts = d.slot_counts || {};
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const end = new Date(today); end.setDate(end.getDate() + 90);
  const months = [];
  for (let m = new Date(today.getFullYear(), today.getMonth(), 1); m <= end; m = new Date(m.getFullYear(), m.getMonth() + 1, 1)) {
    const grid = el("div", { class: "grid7" }, ...["M", "T", "W", "T", "F", "S", "S"].map((x) => el("span", { class: "dow", text: x })));
    const lead = (m.getDay() + 6) % 7;
    for (let i = 0; i < lead; i++) grid.append(el("span", { class: "day empty" }));
    const days = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
    for (let day = 1; day <= days; day++) {
      const dt = new Date(m.getFullYear(), m.getMonth(), day);
      const iso = localISO(dt);
      const inRange = dt >= today && dt <= end;
      const open = inRange && avail.has(iso);
      const c = counts[iso];
      const b = el("button", {
        class: `day${open ? ` avail h${level(c)}` : ""}${!inRange ? " past" : ""}${ui.selDate === iso ? " sel" : ""}`,
        text: String(day), disabled: !open,
        title: open ? `${iso}${c != null ? ` · ${c} slots` : " · open"}` : iso,
        "aria-label": `${iso}${open ? " available" : ""}`,
        onclick: open ? () => { ui.selDate = iso; renderHeat(); loadSlots(); } : null,
      });
      grid.append(b);
    }
    months.push(el("div", { class: "month" }, el("h4", { text: m.toLocaleDateString(undefined, { month: "long", year: "numeric" }) }), grid));
  }
  box.replaceChildren(...months);
  if (!d.dates.length) $("#slots").textContent = d.status === "NO DATA" ? "No calendar captured for this location yet. Open it on the portal." : "No open dates in the last capture.";
  else if (!ui.selDate) $("#slots").textContent = "Tap a green date to see its time slots.";
}

async function loadSlots() {
  const box = $("#slots");
  box.textContent = "Loading…";
  const q = new URLSearchParams({ location: ui.hmLoc, appt_type: ui.hmType, date: ui.selDate });
  const r = await fetch("/slots?" + q).then((x) => x.json()).catch(() => null);
  if (!r) { box.textContent = "Could not load slots."; return; }
  if (!r.times.length) { box.textContent = `${fmtDate(ui.selDate)}: time slots not captured yet. Click this date on the portal and they'll appear here.`; return; }
  box.replaceChildren(
    el("div", { class: "muted", text: `${fmtDate(ui.selDate)} · captured ${ago(r.captured_at)}` }),
    ...r.times.map((t) => el("span", { class: "t", text: t.count != null ? `${t.time} · ${t.count}` : t.time })));
}

function renderSettings(st) {
  if (ui.formLoaded) return;
  const f = $("#settings"), s = st.settings;
  f.target_before.value = s.target_before || "";
  f.current_ofc_date.value = s.current_ofc_date || "";
  f.current_interview_date.value = s.current_interview_date || "";
  f.daily_view_budget.value = s.daily_view_budget;
  f.alerts_enabled.checked = !!s.alerts_enabled;
  ui.formLoaded = true;
}

function renderAlerts(st) {
  const ul = $("#alerts");
  if (!st.alerts.length) { ul.replaceChildren(el("li", { class: "muted", text: "None yet." })); return; }
  ul.replaceChildren(...st.alerts.map((a) => el("li", {},
    el("div", { class: "muted", text: `${new Date(a.sent_at).toLocaleString()} · ${a.location} ${a.appt_type}` }),
    el("div", { text: a.message.split("\n").slice(0, 3).join(" · ") }))));
}

/* ---------- chart ---------- */
async function loadChart() {
  if (!window.Chart) return;
  const rows = await fetch(`/history?days=${ui.chDays}`).then((r) => r.json()).catch(() => []);
  const sel = (ui.status?.locations || []).filter((l) => l.selected).map((l) => l.name);
  const datasets = sel.map((name, i) => ({
    label: name,
    data: rows.filter((r) => r.location === name && r.appt_type === ui.chType)
      .map((r) => ({ x: Date.parse(r.checked_at), y: r.earliest_date ? Date.parse(r.earliest_date + "T00:00:00") : null })),
    borderColor: PALETTE[i % PALETTE.length], backgroundColor: PALETTE[i % PALETTE.length],
    pointRadius: 2, borderWidth: 2, tension: 0.15, spanGaps: false,
  }));
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const grid = dark ? "#2a2f39" : "#e3e6eb", ink = dark ? "#9aa1ad" : "#5f6673";
  const day = (v) => new Date(v).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const cfg = {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "linear", grid: { color: grid }, ticks: { color: ink, callback: day, maxTicksLimit: 8 }, title: { display: true, text: "checked on", color: ink } },
        y: { type: "linear", grid: { color: grid }, ticks: { color: ink, callback: day }, title: { display: true, text: "earliest available", color: ink } },
      },
      plugins: {
        legend: { labels: { color: ink, boxWidth: 12 } },
        tooltip: { callbacks: { title: (it) => "Checked " + new Date(it[0].parsed.x).toLocaleString(), label: (it) => `${it.dataset.label}: ${fmtDate(localISO(new Date(it.parsed.y)))}` } },
      },
    },
  };
  if (ui.chart) { ui.chart.data = cfg.data; ui.chart.options = cfg.options; ui.chart.update(); }
  else ui.chart = new Chart($("#chart"), cfg);
}
let chartTimer = null;
const refreshChart = () => { clearTimeout(chartTimer); chartTimer = setTimeout(loadChart, 300); };

/* ---------- settings ---------- */
async function postSettings(body) {
  const r = await fetch("/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
$("#settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await postSettings({
      target_before: f.target_before.value || null,
      current_ofc_date: f.current_ofc_date.value || null,
      current_interview_date: f.current_interview_date.value || null,
      daily_view_budget: Number(f.daily_view_budget.value) || 12,
      alerts_enabled: f.alerts_enabled.checked,
    });
    $("#saved").textContent = "Saved";
  } catch (err) { $("#saved").textContent = "Not saved: check the values"; }
  setTimeout(() => ($("#saved").textContent = ""), 2500);
});

/* ---------- controls ---------- */
$("#hmLoc").addEventListener("change", (e) => { ui.hmLoc = e.target.value; ui.selDate = null; render(ui.status); });
for (const [sel, key, after] of [[".toolbar .seg:not(#chType)", "hmType", () => { ui.selDate = null; renderHeat(); }], ["#chType", "chType", loadChart]]) {
  const group = $(sel);
  group.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    ui[key] = b.dataset.type;
    group.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    after();
  });
}
$("#chDays").addEventListener("change", (e) => { ui.chDays = Number(e.target.value); loadChart(); });

/* ---------- live updates ---------- */
let firstStatus = true;
function connect() {
  const es = new EventSource("/events");
  es.addEventListener("status", (e) => {
    const prevSel = JSON.stringify(ui.status?.settings.selected_locations);
    render(JSON.parse(e.data));
    if (firstStatus || prevSel !== JSON.stringify(ui.status.settings.selected_locations)) { firstStatus = false; refreshChart(); }
  });
  es.addEventListener("snapshot", refreshChart);
  es.addEventListener("slots", (e) => {
    const d = JSON.parse(e.data);
    if (d.location === ui.hmLoc && d.appt_type === ui.hmType && d.date === ui.selDate) loadSlots();
  });
  es.onerror = () => { const n = $("#state"); n.className = "pill s-ERROR"; n.lastElementChild.textContent = "DASHBOARD OFFLINE"; };
}
connect();
setInterval(() => ui.status && renderCards(ui.status), 60_000); // keep "x min ago" fresh
