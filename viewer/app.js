"use strict";

const RES_COLOR = { WOOD: "#3f8a45", BRICK: "#c4613e", SHEEP: "#a6d06a", WHEAT: "#f0c84a", ORE: "#9aa3b5", DESERT: "#ddc995" };
const RES_SHORT = { WOOD: "wood", BRICK: "brick", SHEEP: "sheep", WHEAT: "wheat", ORE: "ore" };
const PCOL = { RED: "#e0473f", BLUE: "#4a7fe0", ORANGE: "#ef8a2c", WHITE: "#eef0f3" };
const ICON = { ROLL: "🎲", BUILD_SETTLEMENT: "🏠", BUILD_CITY: "🏙️", BUILD_ROAD: "🛤️", BUY_DEVELOPMENT_CARD: "🃏",
  PLAY_KNIGHT_CARD: "⚔️", PLAY_MONOPOLY: "🃏", PLAY_YEAR_OF_PLENTY: "🃏", PLAY_ROAD_BUILDING: "🃏",
  MOVE_ROBBER: "🦹", MARITIME_TRADE: "⚓", DISCARD: "🗑️", END_TURN: "⏭️" };
const SVGNS = "http://www.w3.org/2000/svg";

const S = {
  gameId: null, meta: null, events: [], snapIdx: [], costAt: [], cursor: -1,
  tab: "feed", hidden: new Set(), playing: null, poll: null, live: false,
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

// ------------------------------------------------------------------ games list
async function loadGames() {
  const games = await api("/api/games");
  const sel = $("gameSelect");
  sel.innerHTML = games.map((g) => {
    const players = g.seats.map((s) => `${s.color[0]}:${s.label}`).join(" ");
    const st = g.status === "running" ? "● LIVE" : (g.winner ? `🏆 ${g.winner}` : g.status);
    return `<option value="${esc(g.id)}">${esc(g.id)} — ${esc(st)} — ${esc(players)}</option>`;
  }).join("") || "<option>No games yet — run run_game.py</option>";
  const want = new URLSearchParams(location.search).get("game") || (games[0] && games[0].id);
  if (want) { sel.value = want; await openGame(want); }
}

async function openGame(id) {
  clearInterval(S.poll); stopPlay();
  S.gameId = id; S.events = []; S.snapIdx = []; S.costAt = []; S.cursor = -1;
  $("feed").innerHTML = "";
  history.replaceState(null, "", "?game=" + encodeURIComponent(id));
  S.meta = await api(`/api/games/${id}/meta`);
  drawStaticBoard();
  buildFilters();
  await pullEvents();
  S.poll = setInterval(tick, 1500);
}

async function tick() {
  if (!S.gameId) return;
  const before = S.events.length;
  await pullEvents();
  if (S.live || S.events.length !== before) {
    try { S.meta = Object.assign(S.meta, await api(`/api/games/${S.gameId}/meta`)); } catch (e) {}
  }
  updateStatus();
}

async function pullEvents() {
  const last = S.events.length ? S.events[S.events.length - 1].seq : 0;
  const evs = await api(`/api/games/${S.gameId}/events?since=${last}`);
  if (!evs.length) { updateStatus(); return; }
  for (const ev of evs) {
    const i = S.events.length;
    S.events.push(ev);
    S.snapIdx.push(ev.snapshot ? i : (i ? S.snapIdx[i - 1] : -1));
    const prevCost = i ? S.costAt[i - 1] : {};
    const c = Object.assign({}, prevCost);
    if (ev.kind === "decision" && ev.data && ev.data.cost) c[ev.player] = (c[ev.player] || 0) + ev.data.cost;
    S.costAt.push(c);
    appendFeedItem(ev, i);
  }
  const atEnd = S.cursor >= S.events.length - evs.length - 1;
  $("slider").max = S.events.length - 1;
  if ($("follow").checked && (atEnd || S.cursor < 0)) setCursor(S.events.length - 1);
  else updatePos();
  updateStatus();
}

async function updateStatus() {
  const m = S.meta;
  S.live = m.status === "running";
  const pill = $("statusPill");
  pill.className = "pill " + (S.live ? "live" : "done");
  pill.textContent = S.live ? "● LIVE" : (m.winner ? `🏆 ${m.winner} won` : `stopped: ${m.reason || m.status}`);
  $("costTotal").textContent = `est. cost $${(m.cost_total || 0).toFixed(2)}` + (m.stub ? " (stub players)" : "");
  if (S.live) {
    try {
      const st = await api(`/api/games/${S.gameId}/status`);
      $("thinking").innerHTML = st.thinking
        ? `<span class="dot" style="background:${PCOL[st.thinking]}"></span>${esc(st.thinking)} (${esc(st.model)}) is thinking: ${esc(st.decision || st.what)}`
        : "";
    } catch (e) {}
  } else $("thinking").textContent = "";
}

// ------------------------------------------------------------------ board
function hexPoints(x, y, r, flat) {
  const pts = [];
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 180 * (flat ? 60 * i : 60 * i - 90);
    pts.push(`${(x + r * Math.cos(a)).toFixed(1)},${(y + r * Math.sin(a)).toFixed(1)}`);
  }
  return pts.join(" ");
}
function el(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function drawStaticBoard() {
  const b = S.meta.board, s = b.size, svg = $("board");
  svg.innerHTML = "";
  const R = 5.6 * s;
  svg.setAttribute("viewBox", `${-R} ${-R * 0.92} ${2 * R} ${2 * R * 0.92}`);
  el("polygon", { points: hexPoints(0, 0, 5.15 * s, true), fill: "#2b6f96", stroke: "#24597a", "stroke-width": 6 }, svg);
  const gStatic = el("g", {}, svg);
  // ports
  for (const p of b.ports) {
    const [ax, ay] = b.nodes[p.nodes[0]], [bx, by] = b.nodes[p.nodes[1]];
    const mx = (ax + bx) / 2, my = (ay + by) / 2;
    const px = p.x + (mx - p.x) * 0.25, py = p.y + (my - p.y) * 0.25;
    el("line", { x1: px, y1: py, x2: ax, y2: ay, stroke: "#d9c9a0", "stroke-width": 4 }, gStatic);
    el("line", { x1: px, y1: py, x2: bx, y2: by, stroke: "#d9c9a0", "stroke-width": 4 }, gStatic);
    el("circle", { cx: px, cy: py, r: s * 0.36, fill: p.resource ? RES_COLOR[p.resource] : "#f3ead2", stroke: "#5b4a2a", "stroke-width": 2 }, gStatic);
    const t = el("text", { x: px, y: py + 4, "text-anchor": "middle", "font-size": 12, "font-weight": 700, fill: "#222" }, gStatic);
    t.textContent = p.resource ? "2:1" : "3:1";
  }
  // tiles
  for (const t of b.tiles) {
    el("polygon", { points: hexPoints(t.x, t.y, s - 1.5, false), fill: RES_COLOR[t.resource], stroke: "#e8dcb6", "stroke-width": 3, id: `tile-${t.id}` }, gStatic);
    if (t.number) {
      el("circle", { cx: t.x, cy: t.y, r: s * 0.32, fill: "#f6efd9", stroke: "#8a7a55" }, gStatic);
      const red = t.number === 6 || t.number === 8;
      const n = el("text", { x: t.x, y: t.y + 5, "text-anchor": "middle", "font-size": 18, "font-weight": 800, fill: red ? "#c0262d" : "#222" }, gStatic);
      n.textContent = t.number;
      for (let i = 0; i < t.pips; i++) el("circle", { cx: t.x + (i - (t.pips - 1) / 2) * 5, cy: t.y + 12, r: 1.8, fill: red ? "#c0262d" : "#222" }, gStatic);
    }
    const lab = el("text", { x: t.x, y: t.y - s * 0.5, "text-anchor": "middle", class: "tile-id ids" }, gStatic);
    lab.textContent = `T${t.id} ${t.resource === "DESERT" ? "" : RES_SHORT[t.resource]}`;
  }
  el("g", { id: "gDyn" }, svg);
  const gIds = el("g", { id: "gNodeIds", class: "ids" }, svg);
  for (const [n, [x, y]] of Object.entries(b.nodes)) {
    el("circle", { cx: x, cy: y, r: 7, fill: "rgba(0,0,0,.55)" }, gIds);
    const tx = el("text", { x, y: y + 3, "text-anchor": "middle", class: "node-id" }, gIds);
    tx.textContent = n;
  }
  applyIdToggle();
}

function applyIdToggle() {
  const on = $("showIds").checked;
  document.querySelectorAll("#board .ids").forEach((e) => (e.style.display = on ? "" : "none"));
}

function drawDynamic(snap, ev) {
  const b = S.meta.board, s = b.size, g = $("gDyn");
  g.innerHTML = "";
  if (!snap) return;
  const hl = highlightOf(ev);
  for (const [a, c, col] of snap.roads) {
    const [x1, y1] = b.nodes[a], [x2, y2] = b.nodes[c];
    const dx = x2 - x1, dy = y2 - y1, k = 0.14;
    const attrs = { x1: x1 + dx * k, y1: y1 + dy * k, x2: x2 - dx * k, y2: y2 - dy * k, "stroke-linecap": "round" };
    el("line", { ...attrs, stroke: "#1a1a1a", "stroke-width": 11 }, g);
    const line = el("line", { ...attrs, stroke: PCOL[col], "stroke-width": 7 }, g);
    if (hl.edge && ((hl.edge[0] === a && hl.edge[1] === c) || (hl.edge[0] === c && hl.edge[1] === a))) line.classList.add("hl");
  }
  const rt = b.tiles.find((t) => t.id === snap.robber_tile);
  if (rt) {
    el("circle", { cx: rt.x + s * 0.42, cy: rt.y - s * 0.12, r: s * 0.17, fill: "#222", stroke: hl.robber ? "#fff" : "#000", "stroke-width": hl.robber ? 3 : 1 }, g);
    const t = el("text", { x: rt.x + s * 0.42, y: rt.y - s * 0.12 + 4, "text-anchor": "middle", "font-size": 11, fill: "#fff", "font-weight": 700 }, g);
    t.textContent = "R";
  }
  for (const [n, col, kind] of snap.buildings) {
    const [x, y] = b.nodes[n];
    let shape;
    if (kind === "CITY") {
      shape = el("path", { d: `M${x - 12},${y + 9} V${y - 3} L${x - 6},${y - 10} L${x},${y - 3} H${x + 12} V${y + 9} Z` }, g);
    } else {
      shape = el("path", { d: `M${x - 8},${y + 7} V${y - 2} L${x},${y - 10} L${x + 8},${y - 2} V${y + 7} Z` }, g);
    }
    shape.setAttribute("fill", PCOL[col]);
    shape.setAttribute("stroke", "#111");
    shape.setAttribute("stroke-width", 2);
    if (hl.node === n) shape.classList.add("hl");
  }
}

function highlightOf(ev) {
  const out = {};
  if (!ev || ev.kind !== "action" || !ev.data) return out;
  const t = ev.data.type, v = ev.data.value;
  if (t === "BUILD_ROAD") out.edge = v;
  if (t === "BUILD_SETTLEMENT" || t === "BUILD_CITY") out.node = v;
  if (t === "MOVE_ROBBER") out.robber = true;
  return out;
}

// ------------------------------------------------------------------ players
function drawPlayers(snap, i) {
  const wrap = $("players");
  if (!snap) { wrap.innerHTML = ""; return; }
  const cost = S.costAt[i] || {};
  wrap.innerHTML = S.meta.seats.map((seat) => {
    const c = seat.color, p = snap.players[c];
    const hand = Object.entries(p.hand).map(([r, n]) =>
      `<span class="chip ${n ? "" : "zero"}" style="background:${RES_COLOR[r]}" title="${r}">${RES_SHORT[r]} ${n}</span>`).join("");
    const dev = Object.entries(p.dev).filter(([, n]) => n).map(([d, n]) => `${n} ${d.toLowerCase().replace(/_/g, " ")}`).join(", ") || "none";
    const total = Object.values(p.hand).reduce((a, b) => a + b, 0);
    const hidden = p.vp_actual !== p.vp ? ` <small>(${p.vp_actual} true)</small>` : "";
    return `<div class="pcard ${snap.turn_player === c ? "turn" : ""}" style="--c:${PCOL[c]}">
      <div class="top"><span class="name">${c}${snap.turn_player === c ? " ◀" : ""}</span><span class="vp">${p.vp}${hidden} <small>VP</small></span></div>
      <div class="model" title="${esc(seat.label)}">${esc(seat.label)} · ${esc(seat.provider)}</div>
      <div class="res">${hand}</div>
      <div class="stats">${total} cards · dev: ${esc(dev)}</div>
      <div class="stats">⚔️ ${p.knights_played} · 🛤️ ${p.longest_road} · left: ${p.settlements_left}🏠 ${p.cities_left}🏙️ ${p.roads_left}🛤️
        ${p.has_road ? '<span class="badge">Longest Road</span>' : ""}${p.has_army ? '<span class="badge">Largest Army</span>' : ""}</div>
      <div class="stats">est. spend $${(cost[c] || 0).toFixed(2)}</div>
    </div>`;
  }).join("");
}

// ------------------------------------------------------------------ feed
let lastRound = null;
function appendFeedItem(ev, i) {
  const feed = $("feed");
  if (i === 0) lastRound = null;
  if (ev.round !== lastRound) {
    lastRound = ev.round;
    const r = document.createElement("div");
    r.className = "ev round";
    r.dataset.i = i;
    r.dataset.kind = "round";
    r.textContent = ev.round === 0 ? "Setup" : `Round ${ev.round}`;
    feed.appendChild(r);
  }
  const d = document.createElement("div");
  d.className = `ev ${ev.kind}`;
  d.dataset.i = i;
  d.dataset.kind = ev.kind;
  d.dataset.player = ev.player || "";
  d.style.setProperty("--c", PCOL[ev.player] || "#aaa");
  d.innerHTML = renderEvent(ev);
  d.addEventListener("click", (e) => {
    if (e.target.closest("details, .linkbtn")) return;
    $("follow").checked = false;
    setCursor(i);
  });
  feed.appendChild(d);
  applyVisibility(d);
}

function renderEvent(ev) {
  const who = ev.player ? `<span class="who">${ev.player}</span> ` : "";
  const meta = `<span class="meta">#${ev.seq} · ${fmtTime(ev.ts)}</span>`;
  if (ev.kind === "chat") return `${who}${meta}<br><span class="bubble">💬 ${esc(ev.text)}</span>`;
  if (ev.kind === "decision") {
    const d = ev.data || {};
    if (d.fallback) return `${who}🧠 <i>no valid answer — default used</i> ${meta}`;
    const opts = d.options ? `<ol start="0" class="opts">${d.options.map((o, k) =>
      `<li class="${d.raw && d.raw.choice === k ? "picked" : ""}">${esc(o)}</li>`).join("")}</ol>` : "";
    const calls = (d.call_ids || []).map((c) => `<button class="linkbtn" data-call="${c}">call ${c}</button>`).join(" ");
    return `${who}🧠 <span class="choice">${esc(ev.text)}</span> ${meta}
      <div class="reason"><b>Says why:</b> ${esc(d.reason)}</div>
      <details ${d.thinking ? "" : ""}><summary>thinking summary · ${esc(d.model)} · $${(d.cost || 0).toFixed(4)} · ${fmtTok(d.usage)} · ${d.latency}s</summary>
        ${d.thinking ? `<div class="thinkbox">${esc(d.thinking)}</div>` : '<div class="muted">(no thinking summary returned)</div>'}
        <div class="muted"><b>Decision:</b> ${esc(d.decision)}</div>${opts}
        ${d.notebook ? `<div class="muted"><b>Notebook now:</b></div><div class="thinkbox">${esc(d.notebook)}</div>` : ""}
        <div>${calls}</div>
      </details>`;
  }
  const icon = ev.kind === "action" ? (ICON[ev.data && ev.data.type] || "•") : ev.kind === "trade" ? "🤝" : ev.kind === "game" ? "🏁" : "•";
  const detail = ev.detail ? ` <span class="detail">(${esc(ev.detail)})</span>` : "";
  const auto = ev.data && ev.data.auto ? ' <span class="meta">auto</span>' : "";
  return `${icon} ${esc(ev.text)}${detail}${auto} ${meta}`;
}
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtTok = (u) => u ? `${(u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0)} in / ${u.output || 0} out tok` : "";

function applyVisibility(d) {
  const i = +d.dataset.i, kind = d.dataset.kind, p = d.dataset.player;
  d.classList.toggle("future", i > S.cursor);
  let filtered = false;
  if (S.tab !== "feed" && kind !== S.tab && !(kind === "round")) filtered = true;
  if (p && S.hidden.has(p)) filtered = true;
  d.classList.toggle("filtered", filtered);
  d.classList.toggle("cur", i === S.cursor && kind !== "round");
}

function buildFilters() {
  $("filters").innerHTML = S.meta.seats.map((s) =>
    `<button data-p="${s.color}" style="--c:${PCOL[s.color]}" class="${S.hidden.has(s.color) ? "off" : ""}">${s.color} · ${esc(s.label)}</button>`).join("");
}

// ------------------------------------------------------------------ cursor
function setCursor(i) {
  if (!S.events.length) return;
  i = Math.max(0, Math.min(S.events.length - 1, i));
  S.cursor = i;
  $("slider").value = i;
  const si = S.snapIdx[i];
  const snap = si >= 0 ? S.events[si].snapshot : null;
  drawDynamic(snap, S.events[i]);
  drawPlayers(snap, i);
  const ev = S.events[i];
  $("caption").textContent = ev.kind === "chat" ? `${ev.player}: “${ev.text}”` : (ev.player && ev.kind === "decision" ? `${ev.player} → ${ev.text}` : ev.text);
  document.querySelectorAll("#feed .ev").forEach(applyVisibility);
  const cur = document.querySelector(`#feed .ev.cur`);
  if (cur) cur.scrollIntoView({ block: "nearest" });
  updatePos();
}
function updatePos() {
  const ev = S.events[S.cursor];
  $("pos").textContent = ev ? `event ${S.cursor + 1} / ${S.events.length} · ${ev.round === 0 ? "setup" : "round " + ev.round}` : "";
}
function stopPlay() { clearInterval(S.playing); S.playing = null; $("btnPlay").textContent = "▶"; }
function togglePlay() {
  if (S.playing) return stopPlay();
  $("follow").checked = false;
  $("btnPlay").textContent = "⏸";
  S.playing = setInterval(() => {
    if (S.cursor >= S.events.length - 1) return stopPlay();
    setCursor(S.cursor + 1);
  }, +$("speed").value);
}

// ------------------------------------------------------------------ wiring
$("gameSelect").addEventListener("change", (e) => openGame(e.target.value));
$("slider").addEventListener("input", (e) => { $("follow").checked = false; setCursor(+e.target.value); });
$("btnStart").onclick = () => { $("follow").checked = false; setCursor(0); };
$("btnPrev").onclick = () => { $("follow").checked = false; setCursor(S.cursor - 1); };
$("btnNext").onclick = () => { $("follow").checked = false; setCursor(S.cursor + 1); };
$("btnEnd").onclick = () => setCursor(S.events.length - 1);
$("btnPlay").onclick = togglePlay;
$("speed").onchange = () => { if (S.playing) { stopPlay(); togglePlay(); } };
$("follow").onchange = (e) => { if (e.target.checked) setCursor(S.events.length - 1); };
$("showIds").onchange = applyIdToggle;
document.querySelector(".tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  S.tab = b.dataset.tab;
  document.querySelectorAll("#feed .ev").forEach(applyVisibility);
});
$("filters").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const p = b.dataset.p;
  S.hidden.has(p) ? S.hidden.delete(p) : S.hidden.add(p);
  b.classList.toggle("off");
  document.querySelectorAll("#feed .ev").forEach(applyVisibility);
});
$("feed").addEventListener("click", async (e) => {
  const b = e.target.closest(".linkbtn"); if (!b) return;
  const rec = await api(`/api/games/${S.gameId}/call/${b.dataset.call}`);
  $("modalBody").innerHTML = `<h3>Call ${rec.call_id} · ${esc(rec.player)} · ${esc(rec.model)} (attempt ${rec.attempt})</h3>
    ${rec.error ? `<p style="color:#f77">Error: ${esc(rec.error)}</p>` : ""}
    ${rec.invalid ? `<p style="color:#f77">Invalid reply: ${esc(rec.invalid)}</p>` : ""}
    <h4>Prompt (user message; the system prompt is in system_prompt.txt)</h4><pre>${esc(rec.user_prompt)}</pre>
    <h4>Raw reply</h4><pre>${esc(rec.raw)}</pre>
    <h4>Thinking summary</h4><pre>${esc(rec.thinking || "(none)")}</pre>
    <h4>Usage</h4><pre>${esc(JSON.stringify({ usage: rec.usage, cost: rec.cost, latency: rec.latency, served_model: rec.served_model, notes: rec.notes }, null, 1))}</pre>`;
  $("modal").hidden = false;
});
$("modalClose").onclick = () => ($("modal").hidden = true);
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("modal").hidden = true; });
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" && e.target.type !== "range") return;
  if (e.key === "ArrowRight") { $("follow").checked = false; setCursor(S.cursor + 1); e.preventDefault(); }
  if (e.key === "ArrowLeft") { $("follow").checked = false; setCursor(S.cursor - 1); e.preventDefault(); }
  if (e.key === " ") { togglePlay(); e.preventDefault(); }
  if (e.key === "Escape") $("modal").hidden = true;
});

loadGames().catch((e) => { $("feed").textContent = "Could not load games: " + e.message; });
setInterval(async () => {  // refresh game list occasionally (new games)
  const sel = $("gameSelect"), cur = sel.value;
  try {
    const games = await api("/api/games");
    if (games.length !== sel.options.length) {
      sel.innerHTML = games.map((g) => `<option value="${esc(g.id)}">${esc(g.id)} — ${esc(g.status === "running" ? "● LIVE" : (g.winner ? "🏆 " + g.winner : g.status))}</option>`).join("");
      sel.value = cur;
    }
  } catch (e) {}
}, 10000);
