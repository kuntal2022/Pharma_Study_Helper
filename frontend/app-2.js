// Pharma Study Analyser - plain JS UI (no build step)
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const has = (v) => v !== null && v !== undefined && v !== "";
const num = (v, d = 2) => (has(v) ? Number(v).toLocaleString(undefined, { maximumFractionDigits: d }) : "–");
const COL = { main_drug: "#6d5dfc", placebo: "#ff7a59", comparator: "#14b8a6", All: "#8b5cf6" };
const TONE = { good: "#16a34a", bad: "#e11d48", warn: "#f59e0b", neutral: "#64748b" };
const ICON = { good: "✅", bad: "⚠️", warn: "❔", neutral: "➖" };

let token = localStorage.getItem("token");
let S = null;            // current study
let tab = "overview";
let aset = "all";        // analysis-set filter

/* ---------------- api ---------------- */
async function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, token ? { Authorization: "Bearer " + token } : {});
  const r = await fetch(path, opts);
  if (r.status === 401 && token) { logout(); throw new Error("Please log in again."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong (" + r.status + ")");
  return data;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); setTimeout(() => t.classList.add("hidden"), 3500); }
function logout() { token = null; localStorage.removeItem("token"); S = null; go("auth"); }

/* ---------------- routing ---------------- */
async function go(view, arg) {
  $("#top").classList.toggle("hidden", view === "auth");
  if (!token && view !== "auth") view = "auth";
  if (view === "auth") return viewAuth();
  try { const me = await api("/auth/me"); $("#whoami").textContent = me.email; } catch (e) { return; }
  if (view === "home") return viewHome();
  if (view === "study") return viewStudy(arg);
  if (view === "marketing") return viewMarketing();
}

/* ---------------- auth ---------------- */
function viewAuth(mode = "login") {
  $("#app").innerHTML = `
  <div class="auth-wrap"><div class="auth">
    <div class="banner"><h1>💊 Pharma Study Analyser</h1><p>Upload a clinical study PDF and see the facts as clear visuals.</p></div>
    <form id="af">
      <div class="tabs2"><button type="button" class="${mode === "login" ? "on" : ""}" onclick="viewAuth('login')">Log in</button>
      <button type="button" class="${mode === "signup" ? "on" : ""}" onclick="viewAuth('signup')">Sign up</button></div>
      <label>Email</label><input type="email" id="em" required autocomplete="email">
      <label>Password ${mode === "signup" ? "(8 to 72 characters)" : ""}</label><input type="password" id="pw" required autocomplete="${mode === "signup" ? "new-password" : "current-password"}">
      <div class="err" id="er"></div>
      <button class="btn" style="width:100%;margin-top:8px">${mode === "login" ? "Log in" : "Create account"}</button>
    </form></div></div>`;
  $("#af").onsubmit = async (e) => {
    e.preventDefault(); $("#er").textContent = "";
    const email = $("#em").value, password = $("#pw").value;
    try {
      if (mode === "signup") {
        await api("/auth/signup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
      }
      const r = await fetch("/auth/login", { method: "POST", body: new URLSearchParams({ username: email, password }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Login failed");
      token = d.access_token; localStorage.setItem("token", token); go("home");
    } catch (err) { $("#er").textContent = err.message; }
  };
}

/* ---------------- home ---------------- */
async function viewHome() {
  $("#app").innerHTML = `<div class="split"><div class="mainpane">
    <div class="card"><div class="drop" id="drop">
      <div class="big">📄</div><h2 style="margin:6px 0">Upload a study PDF</h2>
      <div class="muted">Drag and drop here or click. Reading a paper takes about a minute.</div>
      <input type="file" id="pdf" accept="application/pdf" hidden>
      <div id="up" style="margin-top:12px"></div>
    </div>
    <div style="text-align:center;margin-top:14px"><button class="btn light" onclick="demo()">✨ Try the TECOS demo (no upload)</button></div></div>
    <h2>My studies</h2><div id="bar" class="bbar"></div><div class="grid" id="list"><div class="muted">Loading…</div></div></div>
    <aside class="side" id="cmp"></aside></div>`;
  const drop = $("#drop"), inp = $("#pdf");
  drop.onclick = (e) => { if (e.target.tagName !== "BUTTON") inp.click(); };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
  inp.onchange = () => inp.files[0] && upload(inp.files[0]);
  try { await loadHome(); } catch (e) { $("#list").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}

/* ---------------- buckets (the user's own folders) ---------------- */
let homeBuckets = [], activeBucket = "all", barMode = null;      // barMode: null | "new" | "rename"
async function loadHome() {
  [homeRows, homeBuckets] = await Promise.all([api("/api/studies"), api("/api/buckets")]);
  if (activeBucket !== "all" && !homeBuckets.find((b) => b.bucket_id === activeBucket)) activeBucket = "all";
  renderHome();
}
function visibleRows() {
  if (activeBucket === "all") return homeRows;
  const b = homeBuckets.find((x) => x.bucket_id === activeBucket), ids = new Set(b ? b.study_ids : []);
  return homeRows.filter((r) => ids.has(r.study_id));
}
function bucketNames(study_id) { return homeBuckets.filter((b) => b.study_ids.includes(study_id)).map((b) => b.name); }
function renderHome() {
  const cur = homeBuckets.find((b) => b.bucket_id === activeBucket);
  $("#bar").innerHTML = `<button class="bk ${activeBucket === "all" ? "on" : ""}" onclick="setBucket('all')">All <i>${homeRows.length}</i></button>
    ${homeBuckets.map((b) => `<button class="bk ${activeBucket === b.bucket_id ? "on" : ""}" onclick="setBucket('${b.bucket_id}')">📁 ${esc(b.name)} <i>${b.study_ids.length}</i></button>`).join("")}
    ${barMode ? `<form class="bnew" onsubmit="saveBucket(event)"><input id="bn" type="text" maxlength="60" placeholder="Bucket name" value="${barMode === "rename" && cur ? esc(cur.name) : ""}" autofocus><button class="btn">Save</button><button type="button" class="chip ask" onclick="barMode=null;renderHome()">Cancel</button></form>`
      : `<button class="bk add" onclick="barMode='new';renderHome();$('#bn').focus()">＋ New bucket</button>`}
    ${cur && !barMode ? `<button class="bk tool" title="Rename" onclick="barMode='rename';renderHome();$('#bn').focus()">✏️</button><button class="bk tool" title="Delete bucket (studies stay)" onclick="delBucket()">🗑</button>` : ""}`;
  const rows = visibleRows();
  $("#list").innerHTML = rows.length ? rows.map((s) => `
    <div class="scard" onclick="go('study','${s.study_id}')">
      <div class="acr">${esc(s.label || s.acronym || "Study")}</div>
      <div class="nm">${esc(s.study_name)}</div>
      ${has(s.publication_year) ? `<span class="chip">${s.publication_year}</span>` : ""}
      ${has(s.patient_count) ? `<span class="chip plc">${num(s.patient_count, 0)} patients</span>` : ""}
      ${bucketNames(s.study_id).map((n) => `<span class="chip cmp">📁 ${esc(n)}</span>`).join("")}
      <div style="text-align:right"><button class="btn light sm" onclick="event.stopPropagation();assignOpen('${s.study_id}')">📁 Buckets</button>
        <button class="btn danger" onclick="event.stopPropagation();del('${s.study_id}')">Delete</button></div>
    </div>`).join("") : `<div class="muted">${activeBucket === "all" ? "No studies yet. Upload a PDF or try the demo." : "This bucket is empty. Use the 📁 Buckets button on a study to add it."}</div>`;
  picked = null; compareChat();
}
function setBucket(id) { activeBucket = id; barMode = null; renderHome(); }
async function saveBucket(e) {
  e.preventDefault(); const name = $("#bn").value.trim(); if (!name) return;
  try {
    if (barMode === "rename") await api("/api/buckets/" + activeBucket, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    else { const r = await api("/api/buckets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }); activeBucket = r.bucket_id; }
    barMode = null; await loadHome();
  } catch (err) { toast(err.message); }
}
async function delBucket() {
  if (!confirm("Delete this bucket? The studies inside will stay.")) return;
  await api("/api/buckets/" + activeBucket, { method: "DELETE" }); activeBucket = "all"; await loadHome();
}
function assignOpen(study_id) {
  const m = document.createElement("div"); m.className = "modal"; m.id = "modal";
  m.onclick = (e) => e.target === m && m.remove();
  m.innerHTML = `<div class="mbox"><h3>📁 Put this study in buckets</h3><div id="mlist"></div>
    <form class="bnew" id="mnew"><input id="mn" type="text" maxlength="60" placeholder="New bucket name"><button class="btn">Create and add</button></form>
    <div style="text-align:right;margin-top:12px"><button class="btn" onclick="document.getElementById('modal').remove()">Done</button></div></div>`;
  document.body.appendChild(m);
  const draw = () => { $("#mlist").innerHTML = homeBuckets.length ? homeBuckets.map((b) => `<div class="pk big ${b.study_ids.includes(study_id) ? "on" : ""}" data-b="${b.bucket_id}" role="button" tabindex="0">${b.study_ids.includes(study_id) ? "✓ " : ""}${esc(b.name)}</div>`).join("") : `<div class="muted">No buckets yet. Create one below.</div>`;
    document.querySelectorAll("#mlist .pk").forEach((el) => (el.onclick = async () => {
      const id = el.dataset.b, was = el.classList.contains("on");
      try {
        if (!was) await api(`/api/buckets/${id}/studies`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ study_ids: [study_id] }) });
        else await api(`/api/buckets/${id}/studies/${study_id}`, { method: "DELETE" });
        homeBuckets = await api("/api/buckets"); draw(); renderHome();
      } catch (err) { toast(err.message); }
    })); };
  draw();
  $("#mnew").onsubmit = async (e) => {
    e.preventDefault(); const name = $("#mn").value.trim(); if (!name) return;
    try {
      const r = await api("/api/buckets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
      await api(`/api/buckets/${r.bucket_id}/studies`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ study_ids: [study_id] }) });
      homeBuckets = await api("/api/buckets"); $("#mn").value = ""; draw(); renderHome();
    } catch (err) { toast(err.message); }
  };
}
async function upload(file) {
  const box = $("#up"); box.innerHTML = `<span class="spinner"></span>Reading <b>${esc(file.name)}</b> … please wait`;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api("/api/studies", { method: "POST", body: fd });
    toast(!r.created ? "You already uploaded this PDF" : (typeof r.indexed === "string" ? "Saved, but chat is not ready: " + r.indexed : "Study extracted and saved"));
    go("study", r.study_id);
  } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
async function demo() { try { const r = await api("/api/demo", { method: "POST" }); go("study", r.study_id); } catch (e) { toast(e.message); } }
async function del(id) { if (!confirm("Delete this study?")) return; await api("/api/studies/" + id, { method: "DELETE" }); viewHome(); }

/* ---------------- study ---------------- */
async function viewStudy(id) {
  $("#app").innerHTML = `<div class="muted"><span class="spinner"></span>Loading…</div>`;
  try { S = await api("/api/studies/" + id); } catch (e) { toast(e.message); return go("home"); }
  tab = "overview"; aset = "all"; renderStudy();
}
const TABS = [["overview", "Overview"], ["outcomes", "Outcomes"], ["baseline", "Baseline"], ["safety", "Safety"], ["stats", "Stats & conclusion"]];
function renderStudy() {
  const d = S.design;
  $("#app").innerHTML = `
    <span class="back" onclick="go('home')">← All studies</span>
    <div class="hero"><div class="acr">${esc(S.label || d.acronym || "Study")}</div><div class="nm">${esc(d.study_name)}</div>
      ${[d.journal, d.publication_year, d.registry_id, d.funder].filter(has).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}
      ${has(d.design_type) ? `<span class="chip">${esc(d.design_type)}</span>` : ""}</div>
    <div class="nav">${TABS.map(([k, n]) => `<button class="${tab === k ? "on" : ""}" onclick="tab='${k}';renderStudy()">${n}</button>`).join("")}</div>
    <div class="split"><div id="pane" class="mainpane"></div><aside class="side" id="chat"></aside></div>`;
  chatPanel($("#chat"), {
    key: "study:" + S.study_id, base: "/api/studies/" + S.study_id + "/chat", title: "Ask about this study",
    sub: "Answers come only from this paper, with page numbers.",
    starters: ["What was the primary outcome and result?", "How many patients were enrolled?", "What were the main safety findings?", "What are the limitations?"],
  });
  ({ overview: pOverview, outcomes: pOutcomes, baseline: pBaseline, safety: pSafety, stats: pStats }[tab])($("#pane"));
}

/* ---- colours / small charts ---- */
function armCol(name) { const a = S.arms.find((x) => x.name === name); return a ? COL[a.role] : COL.All; }
function legend() { return `<div class="legend">${S.arms.map((a) => `<span><i class="dot" style="background:${COL[a.role]}"></i>${esc(a.name)}</span>`).join("")}</div>`; }
function pairBars(title, rows, unit = "%", max = null) {
  // rows: [{label, value, color}] - horizontal bars on one shared scale
  const vals = rows.filter((r) => has(r.value)); if (!vals.length) return "";
  const m = max || Math.max(...vals.map((r) => r.value)) * 1.15 || 1;
  return `<div class="pbar"><div class="t">${esc(title)}</div>${rows.map((r) => has(r.value) ? `
    <div class="row"><div class="lab" title="${esc(r.label)}">${esc(r.label)}</div>
    <div class="track"><div class="fill" style="width:${Math.max(1, (r.value / m) * 100)}%;background:${r.color}"></div></div>
    <div class="val">${num(r.value, 1)}${unit}</div></div>` : "").join("")}</div>`;
}

/* ---- forest plot (SVG) ---- */
function forest(rows, margin) {
  rows = rows.filter((o) => has(o.effect) && has(o.ci_lower) && has(o.ci_upper) && ["HR", "OR", "RR"].includes(o.effect_measure));
  if (!rows.length) return `<div class="muted">No effect sizes with confidence intervals to plot.</div>`;
  const W = 920, LX = 360, RX = 880, H = 46, top = 34, bot = 46, n = rows.length;
  const lo = Math.max(0.2, Math.min(0.5, ...rows.map((o) => o.ci_lower)) * 0.92);
  const hi = Math.min(6, Math.max(2, margin ? margin * 1.15 : 2, ...rows.map((o) => o.ci_upper)) * 1.08);
  const X = (v) => LX + ((Math.log(Math.min(hi, Math.max(lo, v))) - Math.log(lo)) / (Math.log(hi) - Math.log(lo))) * (RX - LX);
  const height = top + n * H + bot;
  let g = `<svg viewBox="0 0 ${W} ${height}" style="width:100%;height:auto">`;
  [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5].filter((t) => t >= lo && t <= hi).forEach((t) => {
    g += `<line x1="${X(t)}" x2="${X(t)}" y1="${top - 8}" y2="${top + n * H}" stroke="#c5c9ee"/><text x="${X(t)}" y="${top + n * H + 18}" font-size="12" fill="#6b7090" text-anchor="middle">${t}</text>`;
  });
  g += `<line x1="${X(1)}" x2="${X(1)}" y1="${top - 12}" y2="${top + n * H}" stroke="#1f2340" stroke-width="1.8"/>`;
  if (margin && rows.some((o) => o.p_value_type === "noninferiority")) {
    g += `<line x1="${X(margin)}" x2="${X(margin)}" y1="${top - 12}" y2="${top + n * H}" stroke="#f59e0b" stroke-width="2" stroke-dasharray="6 4"/>
          <text x="${X(margin)}" y="${top - 16}" font-size="11.5" fill="#b45309" font-weight="700" text-anchor="middle">NI margin ${margin}</text>`;
  }
  const A = S.arms.find((a) => a.role === "main_drug") || S.arms[0];
  g += `<text x="${X(1) - 8}" y="${top + n * H + 38}" font-size="12" fill="${COL.main_drug}" font-weight="700" text-anchor="end">← fewer events with ${esc(A.name)}</text>
        <text x="${X(1) + 8}" y="${top + n * H + 38}" font-size="12" fill="${COL.placebo}" font-weight="700">more events with ${esc(A.name)} →</text>`;
  rows.forEach((o, i) => {
    const y = top + i * H + H / 2, c = TONE[(o.verdict && o.verdict.tone) || "neutral"];
    const name = o.outcome.length > 52 ? o.outcome.slice(0, 50) + "…" : o.outcome;
    const tag = { ITT: "ITT", per_protocol: "Per-protocol" }[o.analysis_set] || "";
    if (i % 2 === 0) g += `<rect x="0" y="${y - H / 2}" width="${W}" height="${H}" fill="#e6e4ff"/>`;
    g += `<text x="10" y="${y - 4}" font-size="13" font-weight="600" fill="#1f2340">${esc(name)}</text>
          <text x="10" y="${y + 13}" font-size="12" fill="#6b7090">${o.effect_measure} ${o.effect} (${o.ci_lower}–${o.ci_upper})${tag ? " · " + tag : ""}</text>
          <line x1="${X(o.ci_lower)}" x2="${X(o.ci_upper)}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="3.5" stroke-linecap="round"/>
          <rect x="${X(o.effect) - 7}" y="${y - 7}" width="14" height="14" rx="3" fill="${c}" transform="rotate(45 ${X(o.effect)} ${y})"/>`;
  });
  return g + "</svg>";
}

/* ---------------- panes ---------------- */
function pOverview(el) {
  const v = S.verdict, st = S.statistics, p = S.population, c = S.conclusions;
  const prim = S.outcomes.filter((o) => o.is_primary);
  const evs = prim.length ? (prim[0].events_a || 0) + (prim[0].events_b || 0) : null;
  let h = "";
  if (v) h += `<div class="verdict ${v.tone}"><div class="ico">${ICON[v.tone]}</div><div><b>${esc(v.summary || v.label)}</b><div style="opacity:.95;font-size:14px">${esc(v.text)} <span style="opacity:.8">(${esc(v.analysis_set === "per_protocol" ? "per-protocol" : v.analysis_set || "")} · rule-based, not AI opinion)</span></div></div></div>`;
  h += `<div class="tiles">
    <div class="tile t1"><div class="v">${num(S.patient_count, 0)}</div><div class="l">Patients</div></div>
    <div class="tile t2"><div class="v">${has(S.design.median_follow_up_years) ? S.design.median_follow_up_years + " y" : "–"}</div><div class="l">Median follow-up</div></div>
    <div class="tile t3"><div class="v">${S.arms.length}</div><div class="l">Arms</div></div>
    <div class="tile t4"><div class="v">${has(evs) ? num(evs, 0) : "–"}</div><div class="l">Primary events</div></div></div>`;
  if (has(c.key_message_one_line)) h += `<div class="card"><h3>💡 Key message</h3>${esc(c.key_message_one_line)}</div>`;
  h += `<h2>Treatment arms</h2><div class="arms">${S.arms.map((a) => {
    const rows = [["Randomized", a.n_randomized], ["ITT", a.n_itt], ["Per-protocol", a.n_per_protocol], ["Discontinued drug", a.discontinued_drug_n]].filter((r) => has(r[1]));
    const mx = Math.max(...rows.map((r) => r[1]), 1);
    return `<div class="arm ${a.role}"><div class="role">${esc(a.role.replace("_", " "))}</div><div class="an">${esc(a.name)}</div>
      <div class="dose">${esc(a.dose || "")}</div><div class="nn">${num(a.n_randomized ?? a.n_itt, 0)}</div><div style="font-size:12px;opacity:.9;margin-bottom:10px">patients</div>
      ${rows.map((r) => `<div style="font-size:12px;margin-top:6px">${r[0]} <b>${num(r[1], 0)}</b></div><div style="background:rgba(255,255,255,.28);border-radius:6px;height:8px"><div style="width:${(r[1] / mx) * 100}%;background:#fff;height:8px;border-radius:6px"></div></div>`).join("")}</div>`;
  }).join("")}</div>`;
  const kv = [["Design", S.design.design_type], ["Blinding", S.design.blinding], ["Randomization", S.design.randomization_ratio], ["Comparator", S.design.comparator_type],
    ["Sites / countries", has(S.design.n_sites) ? `${S.design.n_sites} sites, ${S.design.n_countries ?? "?"} countries` : null],
    ["Enrollment", [S.design.enrollment_start, S.design.enrollment_end].filter(has).join(" to ")], ["Who was studied", p.summary]].filter((r) => has(r[1]));
  h += `<div class="card" style="margin-top:20px"><h2>At a glance</h2><div class="kv">${kv.map((r) => `<div>${r[0]}</div><div>${esc(r[1])}</div>`).join("")}</div></div>`;
  if (p.inclusion_criteria.length || p.exclusion_criteria.length)
    h += `<div class="two"><div class="card"><h3>✅ Inclusion</h3><ul class="list">${p.inclusion_criteria.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
          <div class="card"><h3>⛔ Exclusion</h3><ul class="list">${p.exclusion_criteria.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>`;
  el.innerHTML = h;
}

function pOutcomes(el) {
  const margin = S.statistics.noninferiority_margin;
  const efficacy = S.outcomes.filter((o) => o.outcome_type !== "safety");
  const sets = [["all", "All"], ["ITT", "ITT"], ["per_protocol", "Per-protocol"]];
  const rows = efficacy.filter((o) => aset === "all" || o.analysis_set === aset);
  let h = `<div class="seg">${sets.map(([k, n]) => `<button class="${aset === k ? "on" : ""}" onclick="aset='${k}';renderStudy()">${n}</button>`).join("")}</div>`;
  h += `<div class="card"><h2>🎯 Forest plot</h2><div class="muted" style="margin-bottom:8px">Each line is the 95% confidence interval. Line on the right of 1 = more events, left = fewer. Green = clearly better or non-inferior, red = clearly worse, grey = no clear difference.</div>${forest(rows, margin)}</div>`;
  const withPct = rows.filter((o) => has(o.pct_a) && has(o.pct_b));
  if (withPct.length) h += `<div class="card"><h2>📊 Event rates side by side</h2>${legend()}${withPct.map((o) =>
    pairBars(o.outcome + (o.analysis_set ? ` (${o.analysis_set === "per_protocol" ? "per-protocol" : o.analysis_set})` : ""),
      [{ label: o.arm_a, value: o.pct_a, color: armCol(o.arm_a) }, { label: o.arm_b, value: o.pct_b, color: armCol(o.arm_b) }])).join("")}</div>`;
  h += `<div class="card"><h2>All outcome rows</h2><div style="overflow:auto"><table><tr><th>Outcome</th><th>Set</th><th>Events A / B</th><th>Effect (95% CI)</th><th>p</th><th>Verdict</th><th>Pg</th></tr>
    ${rows.map((o) => `<tr><td>${esc(o.outcome)}${o.is_primary ? ' <span class="chip">primary</span>' : ""}</td><td>${esc(o.analysis_set || "")}</td>
      <td>${num(o.events_a, 0)} / ${num(o.events_b, 0)}</td><td>${has(o.effect) ? `${o.effect_measure} ${o.effect} (${num(o.ci_lower)}–${num(o.ci_upper)})` : "–"}</td>
      <td>${esc(o.p_value_text || (has(o.p_value) ? o.p_value : "–"))}${o.p_value_type === "noninferiority" ? ' <span class="chip warn">NI</span>' : ""}</td>
      <td>${o.verdict ? `<span class="chip ${o.verdict.tone}" title="${esc(o.verdict.text)}">${esc(o.verdict.label)}</span>` : ""}</td><td>${o.source_page ?? ""}</td></tr>`).join("")}</table></div></div>`;
  if (S.continuous_outcomes.length) h += `<div class="card"><h2>📈 Continuous outcomes</h2><div style="overflow:auto"><table><tr><th>Measure</th><th>Time</th><th>Arm A</th><th>Arm B</th><th>Difference (CI)</th><th>p</th></tr>
    ${S.continuous_outcomes.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.timepoint || "")}</td><td>${num(c.value_a)}</td><td>${num(c.value_b)}</td>
    <td>${has(c.difference) ? `${c.difference} (${num(c.ci_lower)} to ${num(c.ci_upper)})` : "–"} ${esc(c.unit || "")}</td><td>${esc(c.p_value_text || (has(c.p_value) ? c.p_value : "–"))}</td></tr>`).join("")}</table></div></div>`;
  const sg = S.subgroups.filter((s) => has(s.effect));
  if (sg.length) h += `<div class="card"><h2>🧩 Subgroups</h2><table><tr><th>Outcome</th><th>Subgroup</th><th>Effect (CI)</th><th>p interaction</th></tr>${sg.map((s) => `<tr><td>${esc(s.outcome)}</td><td>${esc(s.subgroup)} ${esc(s.level || "")}</td><td>${s.effect_measure} ${s.effect} (${num(s.ci_lower)}–${num(s.ci_upper)})</td><td>${num(s.p_interaction, 3)}</td></tr>`).join("")}</table></div>`;
  el.innerHTML = h;
}

function pBaseline(el) {
  const b = S.baseline;
  if (!b.available_in_paper || !b.items.length)
    return (el.innerHTML = `<div class="card"><h2>Baseline table not in the main paper</h2><p>${esc(b.location_note || "The paper does not print the baseline characteristics in the main text.")}</p><div class="muted">Upload the supplement PDF as well to see it here.</div></div>`);
  const groups = {};
  b.items.forEach((i) => ((groups[i.category] = groups[i.category] || {})[i.name] = (groups[i.category][i.name] || []).concat(i)));
  const nice = { demographic: "👥 Demographics", comorbidity: "🩺 Comorbidities", lab_vital: "🧪 Labs and vitals", medication: "💊 Medications", diabetes_history: "📋 Disease history", other: "Other" };
  let h = legend();
  Object.keys(groups).forEach((cat) => {
    const names = Object.keys(groups[cat]);
    const pctNames = names.filter((n) => groups[cat][n].some((i) => has(i.percent)));
    const numNames = names.filter((n) => !pctNames.includes(n));
    h += `<div class="card"><h2>${nice[cat] || cat}</h2>`;
    h += pctNames.map((n) => pairBars(n, groups[cat][n].map((i) => ({ label: i.arm, value: i.percent, color: armCol(i.arm) })), "%", 100)).join("");
    if (numNames.length) h += `<table><tr><th>Measure</th><th>Arm</th><th>Value</th></tr>${numNames.map((n) => groups[cat][n].map((i) => `<tr><td>${esc(n)}</td><td>${esc(i.arm)}</td><td>${i.value_type === "mean_sd" ? `${num(i.mean)} ± ${num(i.sd)}` : i.value_type === "median_iqr" ? `${num(i.median)} (${esc(i.iqr || "")})` : esc(i.text || num(i.count, 0))} ${esc(i.unit || "")}</td></tr>`).join("")).join("")}</table>`;
    h += `</div>`;
  });
  el.innerHTML = h;
}

function pSafety(el) {
  let h = "";
  const sOut = S.outcomes.filter((o) => o.outcome_type === "safety");
  if (sOut.length) h += `<div class="card"><h2>🛡️ Safety outcomes (effect sizes)</h2>${forest(sOut, null)}</div>`;
  const byEv = {};
  S.adverse_events.forEach((a) => (byEv[a.event] = (byEv[a.event] || []).concat(a)));
  const names = Object.keys(byEv);
  if (names.length) h += `<div class="card"><h2>⚠️ Adverse events</h2>${legend()}${names.map((n) =>
    pairBars(n + (byEv[n].some((a) => a.is_serious) ? " (serious)" : ""), byEv[n].map((a) => ({ label: a.arm, value: has(a.patients_pct) ? a.patients_pct : a.patients_n, color: armCol(a.arm) })), byEv[n].some((a) => has(a.patients_pct)) ? "%" : "")).join("")}</div>`;
  const byD = {};
  S.deaths.forEach((d) => (byD[d.cause] = (byD[d.cause] || []).concat(d)));
  if (Object.keys(byD).length) h += `<div class="card"><h2>🕊️ Deaths</h2>${legend()}${Object.keys(byD).map((c) =>
    pairBars(c, byD[c].map((d) => ({ label: d.arm, value: has(d.pct) ? d.pct : d.n, color: armCol(d.arm) })), byD[c].some((d) => has(d.pct)) ? "%" : "")).join("")}</div>`;
  el.innerHTML = h || `<div class="card">No safety data was extracted from this paper.</div>`;
}

function pStats(el) {
  const s = S.statistics, c = S.conclusions, d = S.disposition;
  const kv = [["Hypothesis", s.primary_hypothesis], ["Non-inferiority margin", has(s.noninferiority_margin) ? `${s.noninferiority_margin} (${s.margin_scale})` : null], ["Primary analysis set", s.primary_analysis_set],
    ["Supporting set", s.supporting_analysis_set], ["Model", s.model], ["Testing order", s.testing_sequence], ["Target events", s.target_events], ["Power", has(s.power_pct) ? s.power_pct + "%" : null], ["Software", s.software]].filter((r) => has(r[1]));
  let h = `<div class="card"><h2>🧮 Statistical plan</h2><div class="kv">${kv.map((r) => `<div>${r[0]}</div><div>${esc(r[1])}</div>`).join("")}</div></div>`;
  if (has(d.n_randomized_total)) h += `<div class="card"><h2>🔀 Patient flow</h2><div class="kv"><div>Randomized</div><div>${num(d.n_randomized_total, 0)}</div><div>Excluded from all analyses</div><div>${num(d.n_excluded_from_all_analyses, 0)}</div>
    <div>Reasons</div><div>${esc((d.exclusion_reasons || []).join("; ") || "–")}</div><div>Vital status obtained</div><div>${has(d.vital_status_obtained_pct) ? d.vital_status_obtained_pct + "%" : "–"}</div></div></div>`;
  if (has(c.authors_conclusion)) h += `<div class="card"><h2>📝 Authors' conclusion</h2>${esc(c.authors_conclusion)}</div>`;
  h += `<div class="two"><div class="card"><h3>💪 Strengths</h3><ul class="list">${c.strengths.map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='muted'>None listed</li>"}</ul></div>
        <div class="card"><h3>🚧 Limitations</h3><ul class="list">${c.limitations.map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='muted'>None listed</li>"}</ul></div></div>`;
  if (S.not_reported.length || S.supplement_refs.length) h += `<div class="card"><h3>ℹ️ Not in the main paper</h3><ul class="list">${[...S.not_reported, ...S.supplement_refs].map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
  el.innerHTML = h;
}

/* ---------------- chat (study chat on the right of every study, compare chat on the home page) ---------------- */
const CH = {};          // key -> {turns, loaded, busy}
let homeRows = [], picked = null;
const fmt = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
const chipSrc = (x) => `<span class="chip cmp" title="${esc(x.label || "")}">${x.label ? esc(x.label.length > 14 ? x.label.slice(0, 13) + "…" : x.label) + " " : ""}p.${x.page}</span>`;

function chatPanel(el, cfg) {
  const st = (CH[cfg.key] = CH[cfg.key] || { turns: [], loaded: false, busy: false });
  cfg.el = el; cfg.st = st;
  const last = st.turns[st.turns.length - 1];
  const sugg = st.turns.length ? (last.follow_ups || []) : cfg.starters;
  const lock = cfg.locked ? cfg.locked() : null;
  el.innerHTML = `<div class="card chatbox"><div class="chead"><h3>💬 ${esc(cfg.title)}</h3>
      ${st.turns.length ? `<button class="chip ask" id="cnew">↺ New chat</button>` : ""}</div>
    <div class="muted" style="margin-bottom:8px">${esc(cfg.sub)}</div>${cfg.top ? cfg.top() : ""}
    <div class="msgs" id="msgs">${st.turns.map((t) => `<div class="b user">${esc(t.q)}</div><div class="b bot">${fmt(t.a)}${(t.sources || []).length ? `<div class="src">${t.sources.map(chipSrc).join("")}${t.cached ? '<span class="chip warn">⚡ cache</span>' : ""}</div>` : ""}</div>`).join("")}
      ${st.busy ? `<div class="b user">${esc(st.pending || "")}</div><div class="b bot"><span class="spinner"></span>Thinking…</div>` : ""}
      ${!st.turns.length && !st.busy ? `<div class="muted">${st.loaded ? "Try one of these:" : "Loading…"}</div>` : ""}</div>
    ${lock ? `<div class="err">${esc(lock)}</div>` : `<div class="asks">${!st.busy ? sugg.map((q, i) => `<button class="chip ask" data-q="${i}">${esc(q)}</button>`).join("") : ""}</div>`}
    <form id="cf" class="cform"><input type="text" id="cq" maxlength="500" placeholder="Ask a question…" autocomplete="off" ${st.busy || lock ? "disabled" : ""}><button class="btn" ${st.busy || lock ? "disabled" : ""}>Send</button></form></div>`;
  const m = el.querySelector("#msgs"); m.scrollTop = m.scrollHeight;
  el.querySelectorAll(".ask[data-q]").forEach((b) => (b.onclick = () => chatSend(cfg, sugg[+b.dataset.q])));
  el.querySelector("#cf").onsubmit = (e) => { e.preventDefault(); const v = el.querySelector("#cq").value.trim(); if (v) chatSend(cfg, v); };
  const nb = el.querySelector("#cnew");
  if (nb) nb.onclick = async () => { await api(cfg.base, { method: "DELETE" }); st.turns = []; chatPanel(el, cfg); };
  if (!st.loaded) {                          // restore the saved conversation (memory lives on the server)
    st.loaded = true;
    api(cfg.base).then((r) => { st.turns = r.turns || []; if (el.isConnected) chatPanel(el, cfg); }).catch(() => { if (el.isConnected) chatPanel(el, cfg); });
  }
}
async function chatSend(cfg, q) {
  const st = cfg.st; if (st.busy) return;
  st.busy = true; st.pending = q;
  chatPanel(cfg.el, cfg);
  try {
    const body = Object.assign({ question: q }, cfg.body ? cfg.body() : {});
    const r = await api(cfg.base, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    st.turns.push({ q, a: r.answer, sources: r.sources, cached: r.cached, follow_ups: r.follow_ups });
  } catch (e) { st.turns.push({ q, a: "⚠️ " + e.message, sources: [], follow_ups: [] }); }
  st.busy = false; st.pending = null;
  if (cfg.el.isConnected) chatPanel(cfg.el, cfg);
}

function compareChat() {
  const el = $("#cmp"); if (!el) return;
  const vis = visibleRows();
  if (!picked) picked = new Set(vis.map((r) => r.study_id));
  [...picked].forEach((id) => !vis.find((r) => r.study_id === id) && picked.delete(id));
  const bname = activeBucket === "all" ? "" : (homeBuckets.find((b) => b.bucket_id === activeBucket) || {}).name;
  chatPanel(el, {
    key: "compare:" + activeBucket, base: "/api/compare/chat?bucket=" + encodeURIComponent(activeBucket),
    title: bname ? "Compare: " + bname : "Compare my studies",
    sub: "Pick the studies, then ask. Answers use your own uploaded papers only.",
    starters: ["Compare the primary outcomes", "Which study had the most patients?", "Compare the safety findings", "How do the study populations differ?"],
    body: () => ({ study_ids: [...picked] }),
    locked: () => (picked.size < 2 ? "Upload and select at least 2 studies to compare." : null),
    top: () => `<div class="pick">${vis.map((r) => `<label class="pk ${picked.has(r.study_id) ? "on" : ""}"><input type="checkbox" ${picked.has(r.study_id) ? "checked" : ""} onchange="togglePick('${r.study_id}')">${esc(r.label || r.acronym || "Study")}</label>`).join("") || '<span class="muted">No studies yet.</span>'}</div>`,
  });
}
function togglePick(id) { picked.has(id) ? picked.delete(id) : picked.add(id); compareChat(); }

/* ---------------- sales & marketing ---------------- */
let mkRows = [], mkPick = new Set(), mkOut = null, mkBusy = false;
const MK = { brochure: "📄 Doctor brochure", slides: "🖼 Slides", qa: "❓ Q&A", mr_short: "📞 MR short call", mr_long: "☎️ MR long call" };
async function viewMarketing() {
  $("#app").innerHTML = `<div class="card mk"><h2>📣 Sales &amp; Marketing</h2>
    <div class="muted">Pick 1 to 3 studies, then choose what to prepare. Content uses only numbers from your saved studies.</div>
    <div class="pick" id="mkpick" style="margin-top:12px"></div>
    <div class="mkbtns" id="mkbtns"></div><div id="mkout"></div></div>`;
  try { mkRows = await api("/api/studies"); } catch (e) { return toast(e.message); }
  mkPick = new Set([...mkPick].filter((id) => mkRows.find((r) => r.study_id === id)));
  mkDraw();
}
function mkDraw() {
  $("#mkpick").innerHTML = mkRows.map((r) => `<div class="pk ${mkPick.has(r.study_id) ? "on" : ""}" onclick="mkToggle('${r.study_id}')">${mkPick.has(r.study_id) ? "✓ " : ""}${esc(r.label || r.acronym || "Study")}</div>`).join("") || '<span class="muted">Upload a study first.</span>';
  $("#mkbtns").innerHTML = Object.entries(MK).map(([k, v]) => `<button class="btn ${mkBusy || !mkPick.size ? "light" : ""}" ${mkBusy || !mkPick.size ? "disabled" : ""} onclick="mkRun('${k}')">${v}</button>`).join("")
    + `<button class="btn pink" ${mkBusy || !mkPick.size ? "disabled" : ""} onclick="pptStart()">🤖 Smart PPT (agent asks you first)</button>`;
}
function mkToggle(id) {
  if (mkPick.has(id)) mkPick.delete(id); else if (mkPick.size >= 3) return toast("Maximum 3 studies");
  else mkPick.add(id);
  mkDraw();
}
async function mkRun(kind) {
  mkBusy = true; mkDraw();
  $("#mkout").innerHTML = `<div style="margin:18px 0"><span class="spinner"></span>Preparing ${esc(MK[kind])} … about 10-20 seconds</div>`;
  try {
    mkOut = await api("/api/marketing/" + kind, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ study_ids: [...mkPick] }) });
    mkShow();
  } catch (e) { $("#mkout").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  mkBusy = false; mkDraw();
}
/* ---- PPT agent: plan -> human -> (revise)* -> approve -> create ---- */
let P = null, pptTimer = null, pptLeft = 60, pptOpen = -1;
function pptStop() { clearInterval(pptTimer); pptTimer = null; }
function pptTick() {
  pptStop(); pptLeft = P ? P.timer_seconds : 60;
  pptTimer = setInterval(() => {
    if (!P || !$("#pptt")) return pptStop();
    pptLeft--; $("#pptt").textContent = `${Math.floor(pptLeft / 60)}:${String(pptLeft % 60).padStart(2, "0")}`;
    if (pptLeft <= 0) { pptStop(); pptApprove(true); }
  }, 1000);
}
async function pptStart() {
  mkBusy = true; mkDraw(); P = null;
  $("#mkout").innerHTML = `<div style="margin:18px 0"><span class="spinner"></span>Agent is scanning your studies … about 15-30 seconds</div>`;
  try { P = await api("/api/ppt/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ study_ids: [...mkPick] }) }); pptOpen = -1; pptRender(); }
  catch (e) { $("#mkout").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  mkBusy = false; mkDraw();
}
function pptNotes() { const n = {}; P.plan.forEach((s, i) => (n[i] = s.human_note || "")); return n; }
function pptRender() {
  const opts = Array.from({ length: P.max - P.min + 1 }, (_, i) => P.min + i).map((v) => `<option ${v === P.n ? "selected" : ""}>${v}</option>`).join("");
  $("#mkout").innerHTML = `<div class="mkres ppt">
    <div class="agentmsg">🤖 <b>Agent:</b> I scanned your studies and found ${P.found} good points. Here is what I plan to put in <b>${P.plan.length} slides</b>. ${esc(P.message)}</div>
    <div class="muted" style="margin:6px 0">Click a slide to add your own point to it (optional). Nothing typed = I choose the best.</div>
    ${P.plan.map((s, i) => `<div class="pslide ${s.from_human ? "mine" : ""}" onclick="pptToggle(${i})">
        <span class="pn">${i + 1}</span><div class="pb"><b>${esc(s.title)}</b> <span class="chip">${esc(s.layout)}</span>${s.from_human ? '<span class="chip plc">you asked</span>' : ""}
        <div>${esc(s.focus)}</div>${s.human_note ? `<div class="pnote">✍ Your point: ${esc(s.human_note)}</div>` : ""}
        ${pptOpen === i ? `<div onclick="event.stopPropagation()" style="margin-top:8px"><textarea id="pn" rows="2" placeholder="Optional: what should this slide cover? Leave empty and press Save = I will choose the best. Whatever you type is added to my choice." oninput="pptTick()">${esc(s.human_note || "")}</textarea>
        <button class="btn sm" onclick="pptSaveNote(${i})">Save</button></div>` : ""}</div></div>`).join("")}
    <div class="preq"><b>Want something different?</b> Slides: <select id="pnn" onchange="pptTick()">${opts}</select> (max ${P.max})
      <textarea id="preq" rows="2" placeholder="Optional: e.g. I need heart-failure data and subgroup results. I will scan again, keep your points and add the best of the rest." oninput="pptTick();P._req=this.value">${esc(P._req || "")}</textarea>
      <button class="btn light" onclick="pptRevise()">🔁 Send to agent</button></div>
    <div class="pact"><button class="btn" onclick="pptApprove(false)">✅ Approve and create ${P.plan.length} slides</button>
      <span class="muted">No reply? I create this plan automatically in <b id="pptt">1:00</b></span></div></div>`;
  pptTick();
}
function pptToggle(i) { P._req = ($("#preq") || {}).value || ""; pptOpen = pptOpen === i ? -1 : i; pptRender(); }
function pptSaveNote(i) { P._req = ($("#preq") || {}).value || ""; P.plan[i].human_note = $("#pn").value.trim(); pptOpen = -1; pptRender(); }
async function pptRevise() {
  pptStop(); const n = +$("#pnn").value, request = $("#preq").value.trim(), notes = pptNotes(); P._req = "";
  $("#mkout").innerHTML = `<div style="margin:18px 0"><span class="spinner"></span>Agent is scanning again with your points …</div>`;
  try { P = await api(`/api/ppt/${P.sid}/revise`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ n, request, notes }) }); pptOpen = -1; pptRender(); }
  catch (e) { $("#mkout").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
async function pptApprove(timedOut) {
  pptStop(); const notes = pptNotes();
  $("#mkout").innerHTML = `<div style="margin:18px 0"><span class="spinner"></span>${timedOut ? "No reply, so creating the planned slides" : "Creating your slides"} … about 20 seconds</div>`;
  try { mkOut = await api(`/api/ppt/${P.sid}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notes, timed_out: !!timedOut }) }); P = null; mkShow(); }
  catch (e) { $("#mkout").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
const mkPoints = (arr) => `<ol class="mkpts">${(arr || []).map((p) => `<li><b>${esc(p.headline)}</b><div>${esc(p.evidence)}</div><span class="chip">${esc(p.source)}</span></li>`).join("")}</ol>`;
function mkShow() {
  const o = mkOut, c = o.content; let h = "";
  if (o.kind === "brochure") h = `<h3>${esc(c.title)}</h3><i>${esc(c.tagline)}</i>${c.sections.map((s) => `<h4>${esc(s.heading)}</h4><ul>${s.bullets.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`).join("")}<h4>Key numbers</h4><ul>${c.key_numbers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`;
  else if (o.kind === "slides") h = c.slides.map((s, i) => `<div class="mkslide"><b>${i + 1}. ${esc(s.title)}</b><ul>${[...(s.steps || []).map((b, j) => `Step ${j + 1}: ${b}`), ...(s.kpis || []).map((k) => `${k.value} - ${k.label}`), ...s.bullets].map((b) => `<li>${esc(b)}</li>`).join("")}</ul>${s.notes ? `<small>Notes: ${esc(s.notes)}</small>` : ""}</div>`).join("");
  else if (o.kind === "qa") h = c.items.map((q) => `<div class="mkslide"><b>Q. ${esc(q.question)}</b><div>${esc(q.answer)}</div></div>`).join("");
  else if (o.kind === "mr_short") h = `<h3>Opening</h3><p>${esc(c.opening_line)}</p><h4>Top 5 points</h4>${mkPoints(c.points)}`;
  else h = `<h3>Opening</h3><p>${esc(c.opening_line)}</p><h4>Key USP points</h4>${mkPoints(c.usp_points)}<h4>Why this medicine</h4>${mkPoints(c.drug_points)}<h4>Comparison points</h4>${mkPoints(c.comparison_points)}${c.comparison_note ? `<p class="muted">${esc(c.comparison_note)}</p>` : ""}`;
  const dl = o.kind === "brochure" || o.kind === "slides" ? `<button class="btn" onclick="mkDownload()">⬇ Download ${o.kind === "brochure" ? ".docx" : ".pptx"}</button>` : "";
  $("#mkout").innerHTML = `<div class="mkwarn">⚠️ ${esc(o.note)}</div>
    ${o.warnings.length ? `<div class="mkwarn bad">Numbers not found in the extracted data (please check): ${esc(o.warnings.join(", "))}</div>` : ""}
    <div class="mkres"><div style="display:flex;justify-content:space-between;align-items:center"><b>${esc(MK[o.kind])} — ${esc(o.studies.join(" + "))}</b>${dl}</div>${h}</div>`;
}
async function mkDownload() {
  try {
    const r = await fetch("/api/marketing-export", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token }, body: JSON.stringify({ kind: mkOut.kind, content: mkOut.content, studies: mkOut.studies }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Download failed");
    const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = mkOut.kind === "brochure" ? "doctor_brochure.docx" : "study_slides.pptx"; a.click();
  } catch (e) { toast(e.message); }
}

go(token ? "home" : "auth");
