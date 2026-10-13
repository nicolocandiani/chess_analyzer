"use strict";

// ------------------------------------------------------------------ constants
const CLS = {
  brilliant:  { sym: "!!", label: "Brilliant" },
  great:      { sym: "!",  label: "Great" },
  best:       { sym: "★",  label: "Best" },
  excellent:  { sym: "✓",  label: "Excellent" },
  good:       { sym: "✓",  label: "Good" },
  book:       { sym: "≡",  label: "Book" },
  forced:     { sym: "→",  label: "Forced" },
  inaccuracy: { sym: "?!", label: "Inaccuracy" },
  mistake:    { sym: "?",  label: "Mistake" },
  miss:       { sym: "✗",  label: "Miss" },
  blunder:    { sym: "??", label: "Blunder" },
};
const BAD = new Set(["inaccuracy", "mistake", "miss", "blunder"]);
const TABLE_ORDER = ["brilliant", "great", "best", "excellent", "good", "book", "inaccuracy", "mistake", "miss", "blunder"];
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const TC_ICON = { bullet: "➶", blitz: "⚡", rapid: "⏱", daily: "☀" };
const REASONS = {
  checkmated: "Checkmate", resigned: "Resignation", timeout: "Timeout", abandoned: "Abandoned",
  agreed: "Agreement", repetition: "Repetition", stalemate: "Stalemate", insufficient: "Insufficient material",
  "50move": "50-move rule", timevsinsufficient: "Timeout vs insufficient", kingofthehill: "King of the hill",
  threecheck: "Three check", bughousepartnerlose: "Bughouse partner",
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (cls, extra = "") => `<span class="cls-icon cls-${cls} ${extra}" title="${CLS[cls].label}">${CLS[cls].sym}</span>`;

// ------------------------------------------------------------------ state
const state = {
  username: null,
  games: [],
  review: null,
  ply: 0,
  flipped: false,
  playTimer: null,
  playerColor: null,
  reviewKey: null,
  mode: "review", // review | retry | explore | line
  coach: null,
  boardView: null,
};

// ------------------------------------------------------------------ utils
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 4500);
}

function show(view) {
  for (const v of ["home", "profile", "review"]) $("#" + v).classList.toggle("hidden", v !== view);
  if (view !== "review") stopPlay();
  window.scrollTo(0, 0);
}

function winPct(cp) {
  cp = Math.max(-10000, Math.min(10000, cp));
  return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * cp)) - 1);
}

function fmtClock(sec) {
  if (sec == null) return "";
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const ss = s < 10 && sec < 20 ? s.toFixed(1).padStart(4, "0") : String(Math.floor(s)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

function wdlBar(b) {
  const n = b.games || 1;
  return `<div class="wdl-bar"><div class="w" style="width:${(100 * b.win) / n}%"></div><div class="d" style="width:${(100 * b.draw) / n}%"></div><div class="l" style="width:${(100 * b.loss) / n}%"></div></div>`;
}

// ------------------------------------------------------------------ search / routing
function goPlayer(name) {
  name = name.trim();
  if (!name) return;
  location.hash = `#/player/${encodeURIComponent(name)}`;
}

$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); goPlayer($("#search-input").value); });
$("#hero-form").addEventListener("submit", (e) => { e.preventDefault(); goPlayer($("#hero-input").value); });
window.addEventListener("hashchange", route);

function route() {
  const rv = location.hash.match(/^#\/review\/([0-9a-f]{40})(?:\?(.*))?$/);
  if (rv) {
    const q = new URLSearchParams(rv[2] || "");
    const as = ["white", "black"].includes(q.get("as")) ? q.get("as") : null;
    if (rv[1] !== state.reviewKey) loadSavedReview(rv[1], as, +q.get("ply") || 0);
    else show("review");
    return;
  }
  const m = location.hash.match(/^#\/player\/([^/]+)/);
  if (m) {
    const name = decodeURIComponent(m[1]);
    state.reviewKey = null;
    $("#search-input").value = name;
    loadPlayer(name);
  } else {
    state.reviewKey = null;
    show("home");
  }
}

// ------------------------------------------------------------------ profile
async function loadPlayer(name) {
  show("profile");
  if (state.username && state.username.toLowerCase() === name.toLowerCase()) return;
  state.username = name;
  $("#profile-head").innerHTML = `<div class="muted">Loading ${esc(name)}…</div>`;
  $("#insights").innerHTML = "";
  $("#games").innerHTML = `<div class="empty">Loading games…</div>`;
  try {
    const [prof, archives] = await Promise.all([
      api(`/api/player/${encodeURIComponent(name)}`),
      api(`/api/player/${encodeURIComponent(name)}/archives`),
    ]);
    renderProfile(prof);
    const sel = $("#month-select");
    sel.innerHTML = archives.map((a) => {
      const d = new Date(a.year, a.month - 1, 1);
      return `<option value="${a.year}/${a.month}">${d.toLocaleString(undefined, { month: "long", year: "numeric" })}</option>`;
    }).join("");
    if (!archives.length) {
      $("#games").innerHTML = `<div class="empty">No games found.</div>`;
      return;
    }
    loadGames();
    loadInsights();
  } catch (err) {
    state.username = null;
    $("#profile-head").innerHTML = `<div class="empty">Couldn't load “${esc(name)}”: ${esc(err.message)}</div>`;
    $("#games").innerHTML = "";
  }
}

function renderProfile(p) {
  const ratings = Object.entries(p.ratings).map(([k, r]) =>
    `<div class="rating-pill"><span>${TC_ICON[k] || ""} ${k}</span><b>${r.rating ?? "–"}</b>${r.best ? `<span>best ${r.best}</span>` : ""}</div>`
  ).join("");
  $("#profile-head").innerHTML = `
    <img class="avatar" src="${esc(p.avatar || "")}" alt="" onerror="this.style.visibility='hidden'">
    <div>
      <div class="profile-name">${p.title ? `<span class="title-badge">${esc(p.title)}</span>` : ""}${esc(p.username)}</div>
      <div class="muted">${esc(p.name || "")} ${p.country ? "· " + esc(p.country) : ""}
        ${p.joined ? "· joined " + new Date(p.joined * 1000).getFullYear() : ""}</div>
      <div><a href="${esc(p.url)}" target="_blank" rel="noopener" style="color:var(--accent)">View on chess.com ↗</a></div>
    </div>
    <div class="ratings">${ratings}${p.puzzle_best ? `<div class="rating-pill"><span>🧩 puzzles</span><b>${p.puzzle_best}</b><span>highest</span></div>` : ""}</div>`;
}

$("#month-select").addEventListener("change", loadGames);
$("#class-filter").addEventListener("change", renderGames);
$("#insight-months").addEventListener("change", loadInsights);

async function loadGames() {
  const [y, m] = $("#month-select").value.split("/");
  $("#games").innerHTML = `<div class="empty">Loading games…</div>`;
  try {
    state.games = await api(`/api/player/${encodeURIComponent(state.username)}/games/${y}/${m}`);
    renderGames();
  } catch (err) {
    $("#games").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function renderGames() {
  const f = $("#class-filter").value;
  const list = state.games.filter((g) => !f || g.time_class === f);
  if (!list.length) { $("#games").innerHTML = `<div class="empty">No games for this filter.</div>`; return; }
  $("#games").innerHTML = list.map((g, i) => {
    const me = g.player_color;
    const acc = g.accuracies ? `${g.accuracies.white?.toFixed(1) ?? "–"} / ${g.accuracies.black?.toFixed(1) ?? "–"}` : "";
    const d = g.end_time ? new Date(g.end_time * 1000) : null;
    const pl = (c) => `<div><span class="sq-dot ${c[0]}"></span>${c === me ? "<b>" : ""}${esc(g[c].username)}${c === me ? "</b>" : ""} <span class="muted">(${g[c].rating})</span></div>`;
    const tc = g.time_control.includes("/") ? "daily" : (() => { const [b, inc] = g.time_control.split("+"); return `${Math.round(b / 60) || (b + "s")}${inc ? "|" + inc : ""}`; })();
    return `<div class="game-row" data-i="${state.games.indexOf(g)}" title="${g.rules !== "chess" ? "Variant games can't be analyzed" : "Click to review"}">
      <div class="tc-icon" title="${g.time_class}">${TC_ICON[g.time_class] || "♟"}</div>
      <div class="players-cell">${pl("white")}${pl("black")}</div>
      <div class="opening-cell">${esc(g.opening || "")}<br><span>${esc(REASONS[g.reason] || g.reason || "")}</span></div>
      <div class="acc">${acc ? "Accuracy<br>" + acc : ""}</div>
      <div class="result-badge ${g.outcome}">${g.outcome === "win" ? "Won" : g.outcome === "loss" ? "Lost" : "Draw"}</div>
      <div class="date muted">${tc} · ${d ? d.toLocaleDateString() : ""}</div>
    </div>`;
  }).join("");
  $$(".game-row").forEach((row) => row.addEventListener("click", () => {
    const g = state.games[+row.dataset.i];
    if (g.rules !== "chess") return toast("Only standard chess games can be analyzed.");
    startAnalysis(g.pgn, g.player_color);
  }));
}

// ------------------------------------------------------------------ insights
async function loadInsights() {
  const months = $("#insight-months").value;
  $("#insights").innerHTML = `<div class="card insight muted">Crunching your recent games…</div>`;
  try {
    const ins = await api(`/api/player/${encodeURIComponent(state.username)}/insights?months=${months}`);
    renderInsights(ins);
  } catch (err) {
    $("#insights").innerHTML = `<div class="card insight muted">Insights unavailable: ${esc(err.message)}</div>`;
  }
}

function renderInsights(ins) {
  const t = ins.total;
  if (!t.games) { $("#insights").innerHTML = `<div class="card insight muted">No standard games in this period.</div>`; return; }
  const pct = (b) => b.games ? Math.round((100 * b.win) / b.games) : 0;
  const rows = (obj, labelFn) => Object.entries(obj).sort((a, b) => b[1].games - a[1].games).map(([k, b]) =>
    `<div class="row-stat"><span>${labelFn(k)}</span>${wdlBar(b)}<span>${pct(b)}%</span></div>`).join("");

  const cards = [];
  cards.push(`<div class="card insight"><h3>Overall · ${ins.months.length} month${ins.months.length > 1 ? "s" : ""}</h3>
    <div class="big-stat"><b>${t.games}</b><span class="muted">games</span><b>${pct(t)}%</b><span class="muted">win rate</span></div>
    ${wdlBar(t)}<div class="wdl-legend"><span>${t.win} won</span><span>${t.draw} drawn</span><span>${t.loss} lost</span></div>
    <div class="section-title">By color</div>
    ${rows(ins.by_color, (k) => `<span class="sq-dot ${k[0]}"></span> ${k}`)}</div>`);

  cards.push(`<div class="card insight"><h3>By time control</h3>
    ${rows(ins.by_time_class, (k) => `${TC_ICON[k] || ""} ${k}`)}
    ${Object.keys(ins.avg_accuracy).length ? `<div class="section-title">Avg. chess.com accuracy</div><div class="chip-list">${Object.entries(ins.avg_accuracy).map(([k, v]) => `<span class="chip">${k}<b>${v}</b></span>`).join("")}</div>` : ""}
    ${hourInsight(ins.by_hour_utc)}</div>`);

  cards.push(`<div class="card insight"><h3>Rating trend</h3>${ratingChart(ins.rating_series)}</div>`);

  cards.push(`<div class="card insight"><h3>Most played openings</h3>
    ${ins.openings.slice(0, 8).map((o) => `<div class="opening-row"><span class="name" title="${esc(o.name)}">${esc(o.name)}</span>${wdlBar(o)}<span class="muted">${o.games}</span></div>`).join("") || '<div class="muted">No data</div>'}</div>`);

  const reasonChips = (obj) => Object.entries(obj).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span class="chip">${esc(REASONS[k] || k)}<b>${v}</b></span>`).join("") || '<span class="muted">–</span>';
  cards.push(`<div class="card insight"><h3>How games end</h3>
    <div class="section-title" style="margin-top:0">Your wins</div><div class="chip-list">${reasonChips(ins.win_reasons)}</div>
    <div class="section-title">Your losses</div><div class="chip-list">${reasonChips(ins.loss_reasons)}</div>
    ${lossTip(ins)}</div>`);

  $("#insights").innerHTML = cards.join("");
}

function hourInsight(byHour) {
  const offset = -new Date().getTimezoneOffset() / 60;
  const slots = Object.entries(byHour).map(([h, b]) => ({ h: (((+h + offset) % 24) + 24) % 24, ...b })).filter((s) => s.games >= 5);
  if (slots.length < 2) return "";
  slots.forEach((s) => (s.score = (s.win + 0.5 * s.draw) / s.games));
  slots.sort((a, b) => b.score - a.score);
  const fmt = (s) => `${String(Math.floor(s.h)).padStart(2, "0")}:00 (${Math.round(s.score * 100)}%)`;
  return `<div class="section-title">Time of day</div><div class="muted">Best around <b style="color:var(--win)">${fmt(slots[0])}</b>, worst around <b style="color:var(--loss)">${fmt(slots[slots.length - 1])}</b></div>`;
}

function lossTip(ins) {
  const l = ins.loss_reasons, total = Object.values(l).reduce((a, b) => a + b, 0);
  if (total < 5) return "";
  const timeouts = (l.timeout || 0) / total;
  if (timeouts > 0.3) return `<div class="section-title">Tip</div><div class="muted">${Math.round(timeouts * 100)}% of your losses are on time — try budgeting your clock more evenly.</div>`;
  if ((l.resigned || 0) / total > 0.6) return `<div class="section-title">Tip</div><div class="muted">Most losses are resignations: review those games for the blunder that turned them.</div>`;
  return "";
}

function ratingChart(series) {
  const entries = Object.entries(series).filter(([, s]) => s.length > 1);
  if (!entries.length) return `<div class="muted">Not enough rated games.</div>`;
  const colors = { bullet: "#e5a33a", blitz: "#f7c631", rapid: "#81b64c", daily: "#5c8bb0" };
  const all = entries.flatMap(([, s]) => s);
  const t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
  const r0 = Math.min(...all.map((p) => p[1])) - 10, r1 = Math.max(...all.map((p) => p[1])) + 10;
  const W = 300, H = 130;
  const x = (t) => 30 + ((t - t0) / (t1 - t0 || 1)) * (W - 36);
  const y = (r) => 8 + (1 - (r - r0) / (r1 - r0 || 1)) * (H - 24);
  const paths = entries.map(([k, s]) =>
    `<path d="${s.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("")}" fill="none" stroke="${colors[k] || "#ccc"}" stroke-width="2" stroke-linejoin="round"/>`).join("");
  const legend = entries.map(([k, s]) => `<span class="chip" style="border-left:3px solid ${colors[k] || "#ccc"}">${k}<b>${s[s.length - 1][1]}</b> <span class="muted">(${s[s.length - 1][1] - s[0][1] >= 0 ? "+" : ""}${s[s.length - 1][1] - s[0][1]})</span></span>`).join("");
  return `<svg class="rating-chart" viewBox="0 0 ${W} ${H}">
    <text x="0" y="14" fill="#9b9893" font-size="10">${r1 - 10}</text><text x="0" y="${H - 14}" fill="#9b9893" font-size="10">${r0 + 10}</text>
    <line x1="30" x2="${W}" y1="${H - 16}" y2="${H - 16}" stroke="#3c3a37"/>${paths}</svg><div class="chip-list">${legend}</div>`;
}

// ------------------------------------------------------------------ analysis job
$("#pgn-btn").addEventListener("click", () => $("#pgn-dialog").showModal());
$("#pgn-dialog").addEventListener("close", () => {
  if ($("#pgn-dialog").returnValue === "ok" && $("#pgn-text").value.trim()) startAnalysis($("#pgn-text").value, null);
});

async function startAnalysis(pgn, playerColor) {
  const depth = +$("#depth").value;
  $("#loading").classList.remove("hidden");
  $("#loading-text").textContent = "Starting Stockfish…";
  $("#loading-sub").textContent = `depth ${depth}`;
  $("#progress-bar").style.width = "0%";
  try {
    const { job_id, key } = await api("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pgn, depth }),
    });
    const t0 = Date.now();
    for (;;) {
      const j = await api(`/api/jobs/${job_id}`);
      if (j.status === "done") {
        openReview(j.result, playerColor, key);
        break;
      }
      if (j.status === "error") throw new Error(j.error);
      const pct = j.total ? (100 * j.progress) / j.total : 0;
      $("#progress-bar").style.width = pct + "%";
      $("#loading-text").textContent = j.total ? `Analyzing position ${j.progress} of ${j.total}` : "Queued…";
      if (j.progress > 2) {
        const eta = ((Date.now() - t0) / j.progress) * (j.total - j.progress) / 1000;
        $("#loading-sub").textContent = `depth ${depth} · ~${Math.ceil(eta)}s remaining`;
      }
      await new Promise((r) => setTimeout(r, 600));
    }
  } catch (err) {
    toast("Analysis failed: " + err.message);
  } finally {
    $("#loading").classList.add("hidden");
  }
}

// ------------------------------------------------------------------ review
async function loadSavedReview(key, playerColor, ply) {
  try {
    openReview(await api(`/api/analysis/${key}`), playerColor, key);
    if (ply) goto(ply);
  } catch (err) {
    toast("Couldn't load this review: " + err.message);
    show("home");
  }
}

function openReview(review, playerColor, key) {
  state.review = review;
  state.reviewKey = key;
  const hash = `#/review/${key}${playerColor ? "?as=" + playerColor : ""}`;
  if (key && !location.hash.startsWith(hash)) location.hash = hash;
  state.playerColor = playerColor;
  state.flipped = playerColor === "black";
  state.ply = 0;
  seedLegal(review);
  leaveCoach();
  show("review");
  renderSummary();
  renderMoveList();
  selectTab("summary");
  goto(0);
}

$("#back-btn").addEventListener("click", () => {
  state.reviewKey = null;
  location.hash = state.username ? `#/player/${encodeURIComponent(state.username)}` : "#";
  if (state.username) show("profile");
});
$$(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));
function selectTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-summary").classList.toggle("hidden", name !== "summary");
  $("#tab-moves").classList.toggle("hidden", name !== "moves");
  if (name === "moves") scrollActiveMove();
}

// The board shows either the game (review mode) or a coach position (retry / explore / line).
function goto(ply) {
  const r = state.review;
  if (!r) return;
  if (state.mode !== "review") leaveCoach();
  state.ply = Math.max(0, Math.min(r.moves.length, ply));
  const mv = state.ply ? r.moves[state.ply - 1] : null;
  const arrows = [];
  if (mv && mv.best_move && mv.best_move !== mv.uci && !["book", "forced"].includes(mv.classification)) arrows.push(arrow(mv.best_move, "best"));
  if ($("#show-engine").checked) {
    const next = mv ? mv.next_best : r.start_best;
    if (next) arrows.push(arrow(next, "engine"));
  }
  drawBoard({ fen: mv ? mv.fen : r.start_fen, last: mv && { from: mv.from, to: mv.to, cls: mv.classification }, check: mv ? mv.check : false, arrows });
  renderEvalBar(mv ? mv.eval : r.start_eval);
  renderPlayers();
  renderFeedback(mv);
  renderGraph();
  $$(".move-list .mv").forEach((el) => el.classList.toggle("active", +el.dataset.ply === state.ply));
  scrollActiveMove();
}

function scrollActiveMove() {
  const el = $(`.move-list .mv[data-ply="${state.ply}"]`);
  if (el && !$("#tab-moves").classList.contains("hidden")) el.scrollIntoView({ block: "nearest" });
}

// --- board
function parseFen(fen) {
  const rows = fen.split(" ")[0].split("/");
  const board = {};
  rows.forEach((row, ri) => {
    let f = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) f += +ch;
      else { board["abcdefgh"[f] + (8 - ri)] = ch; f++; }
    }
  });
  return { board, turn: fen.split(" ")[1] };
}

// Placement-only FEN for an optimistic redraw while the server validates a move.
function boardToFen(board, baseFen) {
  const rows = [];
  for (let r = 8; r >= 1; r--) {
    let row = "", empty = 0;
    for (const f of "abcdefgh") {
      const p = board[f + r];
      if (p) { row += (empty || "") + p; empty = 0; } else empty++;
    }
    rows.push(row + (empty || ""));
  }
  const parts = baseFen.split(" ");
  return [rows.join("/"), parts[1] === "w" ? "b" : "w", ...parts.slice(2)].join(" ");
}

function sqXY(sq) {
  const f = sq.charCodeAt(0) - 97, r = +sq[1] - 1;
  const col = state.flipped ? 7 - f : f, row = state.flipped ? r : 7 - r;
  return [col * 100 + 50, row * 100 + 50];
}

const ARROW_COLORS = { best: "rgba(129,182,76,0.85)", engine: "rgba(92,139,176,0.8)", bad: "rgba(202,52,49,0.75)" };
const arrow = (uci, kind) => ({ uci, color: ARROW_COLORS[kind] });

// view: {fen, last:{from,to,cls}, check, arrows, selected, targets, hintSq, animate}
// The 64 squares are created once per orientation; each redraw only touches squares whose
// content changed, and a piece that just moved slides into place.
let squareEls = null;

function buildSquares() {
  const boardEl = $("#board");
  boardEl.innerHTML = "";
  squareEls = { flipped: state.flipped, map: new Map() };
  for (let row = 0; row < 8; row++) {
    for (let col = 0; col < 8; col++) {
      const f = state.flipped ? 7 - col : col, r = state.flipped ? row : 7 - row;
      const sq = "abcdefgh"[f] + (r + 1);
      const el = document.createElement("div");
      el.dataset.sq = sq;
      el._base = `sq ${(f + r) % 2 === 1 ? "l" : "d"}`;
      el._coords = (col === 0 ? `<span class="coord rank">${r + 1}</span>` : "") + (row === 7 ? `<span class="coord file">${"abcdefgh"[f]}</span>` : "");
      el._sig = null;
      boardEl.appendChild(el);
      squareEls.map.set(sq, el);
    }
  }
}

function drawBoard(view) {
  const prev = state.boardView;
  state.boardView = view;
  if (!squareEls || squareEls.flipped !== state.flipped || !$("#board").firstChild) buildSquares();
  const { board, turn } = parseFen(view.fen);
  const checkSq = view.check ? Object.keys(board).find((s) => board[s] === (turn === "w" ? "K" : "k")) : null;
  const last = view.last;

  // Slide the moved piece if this position follows from the previous one by `last`.
  let slide = null;
  if (view.animate !== false && prev && prev.fen !== view.fen && last) {
    const before = parseFen(prev.fen).board;
    const moved = before[last.from];
    if (moved && !board[last.from] && board[last.to] && (board[last.to] === moved || moved.toLowerCase() === "p")) slide = last;
  }

  for (const [sq, el] of squareEls.map) {
    const p = board[sq];
    const hl = last && (sq === last.from || sq === last.to);
    const cls = [el._base, hl && "hl", hl && BAD.has(last.cls) && "cls-bad", sq === checkSq && "check",
      sq === view.selected && "sel", sq === view.hintSq && "hint"].filter(Boolean).join(" ");
    const target = view.targets && view.targets.includes(sq) ? (p ? "cap" : "dot") : "";
    const badge = last && last.cls && sq === last.to ? last.cls : "";
    const sig = `${p || ""}|${target}|${badge}`;
    if (el.className !== cls) el.className = cls;
    if (el._sig === sig) continue;
    el._sig = sig;
    el.innerHTML = el._coords
      + (p ? `<span class="piece ${p === p.toUpperCase() ? "w" : "b"}">${GLYPH[p.toLowerCase()]}\uFE0E</span>` : "")
      + (target === "dot" ? `<span class="dot"></span>` : target === "cap" ? `<span class="cap-ring"></span>` : "")
      + (badge ? icon(badge, "badge") : "");
  }

  if (slide) {
    const fromEl = squareEls.map.get(slide.from), toEl = squareEls.map.get(slide.to);
    const piece = toEl.querySelector(".piece");
    if (piece && piece.animate) {
      const a = fromEl.getBoundingClientRect(), b = toEl.getBoundingClientRect();
      piece.animate([{ transform: `translate(${a.left - b.left}px, ${a.top - b.top}px)` }, { transform: "translate(0, 0)" }],
        { duration: 170, easing: "cubic-bezier(.2,.7,.3,1)" });
    }
  }
  drawArrows(view.arrows || []);
}

function drawArrows(list) {
  const sig = list.map((a) => a.uci + a.color).join(",") + (state.flipped ? "f" : "");
  if (drawArrows._sig === sig) return;
  drawArrows._sig = sig;
  const defs = list.map((a, i) => `<marker id="ah${i}" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="3.2" markerHeight="3.2" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${a.color}"/></marker>`).join("");
  const lines = list.map((a, i) => {
    const [x1, y1] = sqXY(a.uci.slice(0, 2)), [x2, y2] = sqXY(a.uci.slice(2, 4));
    const len = Math.hypot(x2 - x1, y2 - y1);
    const ex = x2 - ((x2 - x1) / len) * 28, ey = y2 - ((y2 - y1) / len) * 28;
    return `<line x1="${x1}" y1="${y1}" x2="${ex}" y2="${ey}" stroke="${a.color}" stroke-width="18" stroke-linecap="round" marker-end="url(#ah${i})"/>`;
  }).join("");
  $("#arrows").innerHTML = `<defs>${defs}</defs>${lines}`;
}

function evalNum(e) { return e.mate != null && e.mate !== 0 ? (e.mate > 0 ? 10000 : -10000) : e.cp; }
function evalLabel(e) {
  if (e.mate != null) return e.mate === 0 ? (e.cp > 0 ? "1-0" : "0-1") : `M${Math.abs(e.mate)}`;
  return (Math.abs(e.cp) / 100).toFixed(1);
}

function renderEvalBar(e) {
  const w = e.mate != null ? (evalNum(e) > 0 ? 100 : 0) : Math.max(4, Math.min(96, winPct(e.cp)));
  $("#evalbar-white").style.height = w + "%";
  const bar = $("#evalbar");
  bar.classList.toggle("flipped", state.flipped);
  const txt = $("#evalbar-text");
  txt.textContent = evalLabel(e);
  const whiteAhead = evalNum(e) >= 0;
  // Put the label at the end of the leading side's portion.
  const atBottom = whiteAhead !== state.flipped;
  txt.style.top = atBottom ? "auto" : "4px";
  txt.style.bottom = atBottom ? "4px" : "auto";
  txt.style.color = whiteAhead ? "#222" : "#fff";
}

function renderPlayers() {
  const r = state.review;
  const acc = (c) => r.summary[c].accuracy;
  const clockAt = (color) => {
    for (let i = state.ply - 1; i >= 0; i--) if (r.moves[i].color === color && r.moves[i].clock != null) return r.moves[i].clock;
    return null;
  };
  const bar = (c) => {
    const name = c === "white" ? r.white : r.black, elo = c === "white" ? r.white_elo : r.black_elo;
    const clk = state.mode === "review" ? clockAt(c) : null;
    return `<span class="sq-dot ${c[0]}"></span>${esc(name || c)} <span class="elo">${elo ? "(" + esc(elo) + ")" : ""}</span>
      ${acc(c) != null ? `<span class="acc-pill">${acc(c)}%</span>` : ""}
      ${clk != null ? `<span class="clock">${fmtClock(clk)}</span>` : ""}`;
  };
  $("#bar-top").innerHTML = bar(state.flipped ? "white" : "black");
  $("#bar-bottom").innerHTML = bar(state.flipped ? "black" : "white");
}

const moveLabel = (mv) => `${mv.move_number}${mv.color === "white" ? "." : "..."} ${mv.san}`;
const sideName = (color) => (color === "white" ? "White" : "Black");
const coachSays = (html, mood = "") => `<div class="coach-bubble ${mood}"><div class="coach-avatar">🎓</div><div class="coach-text">${html}</div></div>`;
const notesHtml = (list) => `<ul class="fb-notes">${list.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`;
const tipHtml = (tip) => (tip ? `<div class="coach-tip"><b>Coach tip</b> ${esc(tip)}</div>` : "");
const actions = (btns) => `<div class="coach-actions">${btns.filter(Boolean).map(([id, label, primary]) => `<button data-act="${id}" class="${primary ? "" : "ghost"}">${label}</button>`).join("")}</div>`;

function renderFeedback(mv) {
  const r = state.review;
  $(".side-card").classList.remove("coach-mode");
  clearTimeout(renderFeedback._warm);
  if (mv && BAD.has(mv.classification) && mv.best_move) {
    // Pausing on a mistake: get its retry ready in case the user clicks "Retry".
    renderFeedback._warm = setTimeout(() => prefetch([{ fen: mv.fen_before, moves: [mv.best_move] }]), 500);
  }
  if (!mv) {
    $("#feedback").innerHTML = coachSays(`<div class="fb-head">Starting position <span class="eval-chip ${evalNum(r.start_eval) < 0 ? "neg" : ""}">${esc(r.start_eval_text)}</span></div>
      <div class="fb-line">Engine line: <span class="line-moves">${esc(r.start_line.join(" "))}</span></div>
      <div class="fb-meta">Step through the game with ← →, or drag any piece to try your own ideas with the coach.</div>`);
    return;
  }
  const showBest = mv.best_san && mv.best_move !== mv.uci;
  $("#feedback").innerHTML = coachSays(`
    <div class="fb-head">${icon(mv.classification, "lg")}<span class="t-${mv.classification}">${esc(moveLabel(mv))}</span>
      <span class="muted" style="font-weight:400;font-size:13px">${CLS[mv.classification].label}</span>
      <span class="eval-chip ${evalNum(mv.eval) < 0 ? "neg" : ""}">${esc(mv.eval_text)}</span></div>
    ${notesHtml(mv.explanation)}
    ${tipHtml(mv.tip)}
    ${showBest ? `<div class="fb-line"><b>Best: ${esc(mv.best_san)}</b> (${esc(mv.best_eval_text)}) <span class="line-moves">${esc(mv.best_line.join(" "))}</span></div>` : ""}
    ${actions([
      BAD.has(mv.classification) && ["retry", "↻ Retry this move", true],
      showBest && ["bestline", "▶ Show best line"],
      mv.next_line.length && ["nextline", "▶ What happens next"],
    ])}
    <div class="fb-meta"><span>Move accuracy ${mv.accuracy}%</span><span>Win% ${mv.win_before} → ${mv.win_after}</span>${mv.time_spent != null ? `<span>⏱ ${mv.time_spent}s</span>` : ""}<span>${mv.phase}</span></div>`);
}

$("#feedback").addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (!b || (state.coach && state.coach.busy)) return;
  const act = b.dataset.act;
  const r = state.review, mv = state.ply ? r.moves[state.ply - 1] : null;
  const c = state.coach;
  ({
    retry: () => startRetry(state.ply),
    bestline: () => startLine(mv.fen_before, mv.best_pv && mv.best_pv.length ? mv.best_pv : mv.best_line, `Best line instead of ${moveLabel(mv)}`, { from: mv.from, to: mv.to }),
    nextline: () => startLine(mv.fen, mv.next_line, `Engine continuation after ${moveLabel(mv)}`),
    exit: () => { leaveCoach(); goto(state.ply); },
    hint: () => { c.hint++; renderRetry(); },
    solution: showSolution,
    again: () => { c.status = "await"; c.res = null; renderRetry(); },
    skip: () => { recordPractice("skipped"); nextPractice(); },
    next: nextPractice,
    explore: () => {
      const fen0 = c.mv.fen_before;
      state.mode = "explore";
      state.coach = { history: [{ fen: fen0, res: null }, { fen: c.res.after.fen, res: c.res }], prevFlipped: c.prevFlipped };
      renderExplore();
    },
    solline: () => startLine(c.mv.fen_before, c.mv.best_pv && c.mv.best_pv.length ? c.mv.best_pv : c.mv.best_line, `Best line in move ${moveLabel(c.mv).split(" ")[0]}`),
    undo: () => { c.history.pop(); renderExplore(); },
    reply: () => {
      const res = c.history[c.history.length - 1].res;
      if (res && res.after.best.uci) playMove(res.after.best.uci);
    },
    lprev: () => { c.idx = Math.max(0, c.idx - 1); renderLine(); },
    lnext: () => { c.idx = Math.min(c.steps.length, c.idx + 1); renderLine(); },
    restart: () => startPractice(c.color),
  })[act]?.();
});

// ------------------------------------------------------------------ coach: board interaction
const legalCache = new Map();

function seedLegal(review) {
  if (review.start_legal) legalCache.set(review.start_fen, review.start_legal);
  for (const m of review.moves) if (m.legal) legalCache.set(m.fen, m.legal);
}

// Ask the server to analyse positions the user is likely to try next, while they think.
function prefetch(items) {
  items = items.filter((it) => it && it.fen);
  if (!items.length) return;
  fetch("/api/coach/prefetch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) }).catch(() => {});
}

// A retry of the move actually played needs no engine: the review already has everything.
function resFromReview(mv) {
  return {
    san: mv.san, uci: mv.uci, from: mv.from, to: mv.to, classification: mv.classification,
    correct: false, explanation: mv.explanation, tip: mv.tip,
    best: { uci: mv.best_move, san: mv.best_san, eval_text: mv.best_eval_text, line: mv.best_line },
    reply: { uci: mv.next_best, san: mv.next_line[0] },
    after: { fen: mv.fen, eval: mv.eval, eval_text: mv.eval_text, check: mv.check, legal: mv.legal, game_over: !mv.next_best,
             best: { uci: mv.next_best, san: mv.next_line[0], line: mv.next_line } },
  };
}

async function legalMoves(fen) {
  if (!legalCache.has(fen)) {
    const pos = await api("/api/coach/position", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fen, engine: false }) });
    legalCache.set(fen, pos.legal);
  }
  return legalCache.get(fen);
}

function canMove() {
  if (!state.review || $("#review").classList.contains("hidden")) return false;
  if (state.coach && state.coach.busy) return false;
  if (state.mode === "retry") return state.coach.status === "await";
  return true;
}

async function select(sq) {
  const view = state.boardView;
  view.selected = sq;
  view.targets = null;
  drawBoard(view);
  if (!sq) return;
  try {
    const legal = await legalMoves(view.fen);
    if (state.boardView !== view || view.selected !== sq) return;
    view.targets = [...new Set(legal.filter((u) => u.startsWith(sq)).map((u) => u.slice(2, 4)))];
    drawBoard(view);
  } catch (err) { toast(err.message); }
}

let drag = null;
$("#board").addEventListener("pointerdown", (e) => {
  const el = e.target.closest(".sq");
  if (!el || !canMove() || e.button > 0) return;
  const sq = el.dataset.sq, view = state.boardView;
  if (view.selected && view.targets && view.targets.includes(sq)) {
    e.preventDefault();
    playMove(view.selected + sq);
    return;
  }
  const { board, turn } = parseFen(view.fen);
  const p = board[sq];
  if (!p || (p === p.toUpperCase() ? "w" : "b") !== turn) { if (view.selected) select(null); return; }
  e.preventDefault();
  if (view.selected !== sq) select(sq);
  drag = { from: sq, x: e.clientX, y: e.clientY, ghost: null };
});
window.addEventListener("pointermove", (e) => {
  if (!drag) return;
  if (!drag.ghost) {
    if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
    const src = $(`.sq[data-sq="${drag.from}"] .piece`);
    if (!src) return;
    const size = $(".sq").getBoundingClientRect().width;
    drag.ghost = src.cloneNode(true);
    drag.ghost.classList.add("ghost");
    Object.assign(drag.ghost.style, { width: size + "px", height: size + "px", fontSize: size * 0.82 + "px" });
    document.body.appendChild(drag.ghost);
    src.classList.add("lifted");
  }
  drag.ghost.style.left = e.clientX + "px";
  drag.ghost.style.top = e.clientY + "px";
});
window.addEventListener("pointerup", (e) => {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (!d.ghost) return;
  d.ghost.remove();
  const to = document.elementFromPoint(e.clientX, e.clientY)?.closest(".sq")?.dataset.sq;
  const view = state.boardView;
  if (to && to !== d.from && view.targets && view.targets.includes(to)) playMove(d.from + to, { animate: false });
  else drawBoard(view);
});

async function playMove(uci, { animate = true } = {}) {
  const view = state.boardView;
  const fen = view.fen;
  const from = uci.slice(0, 2), to = uci.slice(2, 4);
  const { board } = parseFen(fen);
  if (board[from] && board[from].toLowerCase() === "p" && (to[1] === "8" || to[1] === "1") && uci.length === 4) uci += "q";
  if (state.mode === "review" || state.mode === "line") {
    state.mode = "explore";
    state.coach = { history: [{ fen, res: null }] };
  }
  const c = state.coach;
  if (state.mode === "retry" && c.mv && uci.slice(0, 4) === c.mv.uci.slice(0, 4)) {
    onRetryResult(resFromReview(c.mv), animate);
    return;
  }
  // Optimistic redraw while the coach thinks; the "thinking" note only appears if it takes a while.
  const opt = { ...board };
  opt[to] = opt[from];
  delete opt[from];
  drawBoard({ fen: boardToFen(opt, fen), last: { from, to }, arrows: [], animate });
  c.busy = true;
  const thinking = setTimeout(() => { if (c.busy && state.coach === c) renderCoachPanel(); }, 250);
  try {
    const res = await api("/api/coach/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fen, uci }) });
    legalCache.set(res.after.fen, res.after.legal);
    clearTimeout(thinking);
    c.busy = false;
    if (state.coach !== c) return;
    if (state.mode === "retry") onRetryResult(res);
    else {
      c.history.push({ fen: res.after.fen, res });
      renderExplore();
      if (!res.after.game_over) prefetch([{ fen: res.after.fen, moves: res.after.best.uci ? [res.after.best.uci] : [] }]);
    }
  } catch (err) {
    clearTimeout(thinking);
    c.busy = false;
    toast(err.message);
    drawBoard({ ...view, selected: null, targets: null });
    renderCoachPanel();
  }
}

function leaveCoach() {
  if (state.coach && state.coach.prevFlipped != null) state.flipped = state.coach.prevFlipped;
  state.mode = "review";
  state.coach = null;
  $(".side-card").classList.remove("coach-mode");
  $("#review").classList.remove("coaching");
}

function enterCoachUI() {
  $(".side-card").classList.add("coach-mode");
  $("#review").classList.add("coaching");
  renderPlayers();
}

function renderCoachPanel() {
  if (state.mode === "retry") renderRetry();
  else if (state.mode === "explore") renderExplore();
  else if (state.mode === "line") renderLine();
}

// ------------------------------------------------------------------ coach: retry a mistake
function startRetry(ply, practice = null) {
  const mv = state.review.moves[ply - 1];
  state.mode = "retry";
  const prevFlipped = state.coach && state.coach.prevFlipped != null ? state.coach.prevFlipped : state.flipped;
  state.coach = { ply, mv, attempts: 0, hint: 0, status: "await", res: null, practice, prevFlipped };
  state.flipped = mv.color === "black";
  if (!practice) prefetch([{ fen: mv.fen_before, moves: [mv.best_move] }]);
  renderRetry();
}

// The review's best move is the solution we show, so it always counts as solved, even if the
// live coach (searching at a different depth) slightly prefers another move.
function asSolution(res, mv) {
  if (res.uci.slice(0, 4) === (mv.best_move || "").slice(0, 4) && !res.correct) {
    res.correct = true;
    res.classification = "best";
    res.explanation = [`${res.san} is the best move.`, ...res.explanation.slice(1).filter((n) => !/^(Slightly stronger|Better) was/.test(n))];
  }
  return res;
}

function onRetryResult(res, animate = true) {
  const c = state.coach;
  asSolution(res, c.mv);
  c.animate = animate;
  c.res = res;
  c.attempts++;
  c.status = res.correct ? "correct" : res.classification === "good" ? "good" : "wrong";
  if (res.correct) recordPractice(c.attempts === 1 && c.hint === 0 ? "first" : "solved");
  renderRetry();
}

async function showSolution() {
  const c = state.coach;
  c.busy = true;
  renderRetry();
  try {
    const res = await api("/api/coach/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fen: c.mv.fen_before, uci: c.mv.best_move }) });
    c.res = asSolution(res, c.mv);
    c.status = "revealed";
    recordPractice("revealed");
  } catch (err) { toast(err.message); }
  c.busy = false;
  renderRetry();
}

// Strip sentences that would give away the solution during a retry.
const hideAnswer = (notes) => notes.filter((n) => !/^(Better was|Slightly stronger was|You had a forced mate)/.test(n));

function practiceHeader() {
  const p = state.coach.practice;
  if (!p) return "";
  const dots = p.queue.map((ply, i) => {
    const r = p.results[ply];
    return `<span class="pdot ${r || ""} ${i === p.idx ? "cur" : ""}" title="${moveLabel(state.review.moves[ply - 1])}"></span>`;
  }).join("");
  return `<div class="practice-head"><b>Practice</b> · mistake ${p.idx + 1} of ${p.queue.length}<span class="pdots">${dots}</span></div>`;
}

function renderRetry() {
  enterCoachUI();
  const c = state.coach, mv = c.mv;
  const prev = c.ply > 1 ? state.review.moves[c.ply - 2] : null;
  const who = sideName(mv.color);
  const busy = c.busy ? `<div class="thinking">Coach is thinking…</div>` : "";
  let html = practiceHeader(), mood = "";

  if (c.status === "await") {
    if (!c.busy) drawBoard({ fen: mv.fen_before, last: prev && { from: prev.from, to: prev.to }, check: prev ? prev.check : false, arrows: [], hintSq: c.hint >= 1 && mv.hint ? mv.hint.square : null });
    renderEvalBar(mv.eval_before);
    const hints = [];
    if (c.hint >= 1 && mv.hint) hints.push(mv.hint.piece);
    if (c.hint >= 2 && mv.hint) hints.push(mv.hint.idea);
    html += coachSays(`<div class="fb-head">${icon(mv.classification, "lg")} Fix ${esc(moveLabel(mv))}</div>
      <p>In the game, ${esc(who)} played <b>${esc(mv.san)}</b>, ${CLS[mv.classification].label.toLowerCase() === "miss" ? "missing a chance" : "which was " + (mv.classification === "inaccuracy" ? "an inaccuracy" : "a " + CLS[mv.classification].label.toLowerCase())}.
      ${c.attempts ? "Give it another go." : `Can you find a better move for ${esc(who)}?`} Drag or click a piece to answer.</p>
      ${hints.map((h) => `<div class="coach-hint">💡 ${esc(h)}</div>`).join("")}${busy}`);
    html += actions([
      mv.hint && c.hint < 2 && ["hint", c.hint ? "💡 Another hint" : "💡 Hint", false],
      ["solution", "👁 Show solution"],
      c.practice && ["skip", "Skip ⏭"],
      ["exit", "✕ Exit"],
    ]);
  } else {
    const res = c.res;
    // Never draw the solution while the user is still trying: a wrong answer shows the refutation.
    const arrows = !res.reply.uci || c.status === "good" ? [] : [arrow(res.reply.uci, c.status === "wrong" ? "bad" : "engine")];
    drawBoard({ fen: res.after.fen, last: { from: res.from, to: res.to, cls: res.classification }, check: res.after.check, arrows, animate: c.animate !== false });
    c.animate = true;
    renderEvalBar(res.after.eval);
    const last = c.practice && c.practice.idx === c.practice.queue.length - 1;
    const nextBtn = c.practice ? ["next", last ? "Finish ✓" : "Next mistake →", true] : ["exit", "Back to review", true];
    if (c.status === "correct") {
      mood = "good";
      html += coachSays(`<div class="fb-head">${icon(res.classification, "lg")} ${c.attempts === 1 && c.hint === 0 ? "Correct, first try!" : "Correct!"}</div>${notesHtml(res.explanation)}
        <p class="compare">In the game, ${esc(mv.san)} led to <b>${esc(mv.eval_text)}</b>. Your move keeps it at <b>${esc(res.after.eval_text)}</b>.</p>${tipHtml(res.tip)}`, mood);
      html += actions([nextBtn, ["explore", "🔍 Explore from here"], ["solline", "▶ Best line"], c.practice && ["exit", "✕ Exit"]]);
    } else if (c.status === "good") {
      html += coachSays(`<div class="fb-head">${icon("good", "lg")} Good, but there's something stronger.</div>${notesHtml(hideAnswer(res.explanation).slice(1))}${busy}`);
      html += actions([["again", "↻ Try again", true], ["solution", "👁 Show solution"], c.practice && ["skip", "Skip ⏭"], ["exit", "✕ Exit"]]);
    } else if (c.status === "wrong") {
      mood = "bad";
      const notes = hideAnswer(res.explanation);
      html += coachSays(`<div class="fb-head">${icon(res.classification, "lg")} Not quite. ${esc(res.san)} is ${res.classification === "inaccuracy" ? "an inaccuracy" : "a " + CLS[res.classification].label.toLowerCase()}.</div>${notesHtml(notes.slice(1))}${tipHtml(res.tip)}${busy}`, mood);
      html += actions([["again", "↻ Try again", true], mv.hint && c.hint < 2 && ["hint", "💡 Hint"], ["solution", "👁 Show solution"], ["exit", "✕ Exit"]]);
    } else {
      const why = mv.explanation.slice(1).filter((n) => !/^(Better was|White is|Black is|The position)/.test(n));
      html += coachSays(`<div class="fb-head">${icon(res.classification, "lg")} The best move was ${esc(res.san)}</div>${notesHtml(res.explanation.slice(1))}
        <div class="fb-line">Line: <span class="line-moves">${esc((mv.best_line || []).join(" "))}</span></div>
        ${why.length ? `<div class="section-title">Why ${esc(mv.san)} was ${mv.classification === "inaccuracy" ? "an inaccuracy" : "a " + CLS[mv.classification].label.toLowerCase()}</div>${notesHtml(why)}` : ""}${tipHtml(mv.tip)}`);
      html += actions([nextBtn, ["solline", "▶ Step through the line"], ["explore", "🔍 Explore from here"], c.practice && ["exit", "✕ Exit"]]);
    }
  }
  $("#feedback").innerHTML = html;
}

// ------------------------------------------------------------------ coach: practice all mistakes
function practiceColor() { return state.playerColor || (state.flipped ? "black" : "white"); }

function startPractice(color) {
  const queue = state.review.moves.filter((m) => m.color === color && BAD.has(m.classification) && m.best_move).map((m) => m.ply);
  if (!queue.length) return toast(`No mistakes to practice for ${sideName(color)}. Well played!`);
  prefetch(queue.map((ply) => { const m = state.review.moves[ply - 1]; return { fen: m.fen_before, moves: [m.best_move] }; }));
  startRetry(queue[0], { color, queue, idx: 0, results: {} });
}

function recordPractice(result) {
  const p = state.coach && state.coach.practice;
  if (p && !p.results[p.queue[p.idx]]) p.results[p.queue[p.idx]] = result;
}

function nextPractice() {
  const p = state.coach.practice;
  if (!p) { leaveCoach(); goto(state.ply); return; }
  if (p.idx + 1 < p.queue.length) { startRetry(p.queue[p.idx + 1], { ...p, idx: p.idx + 1 }); return; }
  const vals = Object.values(p.results);
  const n = (k) => vals.filter((v) => v === k).length;
  state.coach = { practice: { ...p, idx: p.queue.length }, color: p.color, prevFlipped: state.coach.prevFlipped };
  const first = n("first"), solved = n("solved"), revealed = n("revealed") + n("skipped");
  const score = Math.round((100 * (first + 0.5 * solved)) / p.queue.length);
  $("#feedback").innerHTML = coachSays(`<div class="fb-head">🏁 Practice complete</div>
    <p>${score >= 80 ? "Excellent work, you're spotting these ideas well." : score >= 50 ? "Nice progress. Review the ones you missed and try again." : "These were tough. Step through the best lines and try once more."}</p>
    <div class="practice-score"><div><b>${first}</b><span>first try</span></div><div><b>${solved}</b><span>with help</span></div><div><b>${revealed}</b><span>revealed / skipped</span></div></div>`, score >= 50 ? "good" : "")
    + actions([["restart", "↻ Practice again", true], ["exit", "Back to review"]]);
  state.coach.color = p.color;
}

// ------------------------------------------------------------------ coach: free exploration
function renderExplore() {
  enterCoachUI();
  const c = state.coach;
  const h = c.history[c.history.length - 1];
  const res = h.res;
  const arrows = [];
  if (res && !res.correct && res.best.uci) arrows.push(arrow(res.best.uci, "best"));
  if (res && $("#show-engine").checked && res.after.best.uci) arrows.push(arrow(res.after.best.uci, "engine"));
  if (!c.busy) drawBoard({ fen: h.fen, last: res && { from: res.from, to: res.to, cls: res.classification }, check: res ? res.after.check : false, arrows });
  if (res) renderEvalBar(res.after.eval);
  const chips = c.history.slice(1).map((x, i) => `<span class="line-chip ${i === c.history.length - 2 ? "cur" : ""}">${icon(x.res.classification)} ${esc(x.res.san)}</span>`).join("");
  let body;
  if (c.busy) body = `<div class="thinking">Coach is thinking…</div>`;
  else if (!res) body = `<p>Your turn: make a move for either side and I'll tell you what I think of it.</p>`;
  else {
    const over = res.after.game_over;
    body = `<div class="fb-head">${icon(res.classification, "lg")} <span class="t-${res.classification}">${esc(res.san)}</span>
      <span class="eval-chip ${evalNum(res.after.eval) < 0 ? "neg" : ""}">${esc(res.after.eval_text)}</span></div>
      ${notesHtml(res.explanation)}${tipHtml(res.tip)}
      ${!over && res.after.best.san ? `<div class="fb-line">Best reply now: <b>${esc(res.after.best.san)}</b> <span class="line-moves">${esc(res.after.best.line.slice(1).join(" "))}</span></div>` : ""}`;
  }
  $("#feedback").innerHTML = `<div class="practice-head"><b>Exploring</b>${chips ? `<span class="line-chips">${chips}</span>` : ""}</div>`
    + coachSays(body, res ? (res.correct ? "good" : BAD.has(res.classification) ? "bad" : "") : "")
    + actions([
      c.history.length > 1 && !c.busy && ["undo", "↶ Undo"],
      res && !c.busy && !res.after.game_over && res.after.best.uci && ["reply", `🤖 Play ${esc(res.after.best.san)}`],
      ["exit", "⟲ Back to game", true],
    ]);
}

// ------------------------------------------------------------------ coach: step through a line
async function startLine(fen, moves, title, mistake = null) {
  try {
    const steps = await api("/api/coach/line", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fen, moves }) });
    if (!steps.length) return toast("No line to show.");
    steps.forEach((st) => st.legal && legalCache.set(st.fen, st.legal));
    state.mode = "line";
    state.coach = { fen0: fen, steps, idx: 1, title, mistake, prevFlipped: state.coach ? state.coach.prevFlipped : null };
    renderLine();
  } catch (err) { toast(err.message); }
}

function renderLine() {
  enterCoachUI();
  const c = state.coach;
  const step = c.idx ? c.steps[c.idx - 1] : null;
  const next = c.steps[c.idx];
  const arrows = next ? [arrow(next.uci, "engine")] : [];
  if (!step && c.mistake) arrows.push(arrow(c.mistake.from + c.mistake.to, "bad"));
  drawBoard({ fen: step ? step.fen : c.fen0, last: step && { from: step.from, to: step.to }, check: step ? step.check : false, arrows });
  const chips = c.steps.map((s, i) => `<span class="line-chip ${i + 1 === c.idx ? "cur" : ""}" data-step="${i + 1}">${esc(s.san)}</span>`).join("");
  $("#feedback").innerHTML = `<div class="practice-head"><b>${esc(c.title)}</b></div>`
    + coachSays(`<div class="line-chips big">${chips}</div><p class="muted">Step with ◀ ▶ (or the arrow keys). The blue arrow shows the next move${c.mistake ? ", the red one the move that was played" : ""}. You can also drag pieces to explore your own ideas.</p>`)
    + actions([["lprev", "◀"], ["lnext", "▶", true], ["exit", "⟲ Back to game"]]);
  $$("#feedback .line-chip[data-step]").forEach((el) => el.addEventListener("click", () => { c.idx = +el.dataset.step; renderLine(); }));
}

// --- summary tab
function renderSummary() {
  const r = state.review, s = r.summary;
  const rowsHtml = TABLE_ORDER.map((c) => {
    const w = s.white.counts[c], b = s.black.counts[c];
    return `<tr class="${w + b === 0 ? "zero" : ""}"><td class="n t-${c}">${w}</td><td class="mid">${icon(c)} ${CLS[c].label}</td><td class="n t-${c}">${b}</td></tr>`;
  }).join("");
  const phase = (p) => `<tr><td>${s.white.phase_accuracy[p] ?? "–"}</td><td>${p[0].toUpperCase() + p.slice(1)}</td><td>${s.black.phase_accuracy[p] ?? "–"}</td></tr>`;
  const moments = s.key_moments.map((ply) => {
    const m = r.moves[ply - 1];
    return `<div class="key-moment" data-ply="${ply}">${icon(m.classification)}<b>${esc(moveLabel(m))}</b><span class="muted">${esc(m.explanation[1] || CLS[m.classification].label)}</span>${BAD.has(m.classification) && m.best_move ? `<button class="ghost mini" data-retry="${ply}" title="Retry this move">↻</button>` : ""}</div>`;
  }).join("");
  const mistakes = (c) => r.moves.filter((m) => m.color === c && BAD.has(m.classification) && m.best_move).length;
  const me = practiceColor(), other = me === "white" ? "black" : "white";
  const practice = `<div class="practice-card">
    <div class="coach-avatar">🎓</div>
    <div><b>Learn from this game</b><div class="muted">${mistakes(me) ? `Replay the ${mistakes(me)} position${mistakes(me) > 1 ? "s" : ""} where ${esc(r[me] || sideName(me))} went wrong and find the better move.` : `${esc(r[me] || sideName(me))} made no real mistakes. Great game!`}</div>
      <div class="coach-actions">
        ${mistakes(me) ? `<button data-practice="${me}">🎯 Practice ${esc(r[me] || sideName(me))}'s mistakes (${mistakes(me)})</button>` : ""}
        ${mistakes(other) ? `<button class="ghost" data-practice="${other}">${esc(r[other] || sideName(other))}'s (${mistakes(other)})</button>` : ""}
      </div></div></div>`;
  $("#tab-summary").innerHTML = `<div class="summary">
    <div class="opening-name">${r.eco ? `<b>${esc(r.eco)}</b> ` : ""}${esc(r.opening || "Unknown opening")} · ${esc(r.result || "")}
      ${r.termination ? `<br>${esc(r.termination)}` : ""}<br><span>Stockfish depth ${r.depth}</span></div>
    ${practice}
    <div class="players-acc">
      <div class="acc-box white"><div class="who">${esc(r.white)}</div><div class="num">${s.white.accuracy ?? "–"}</div><div class="lbl">Accuracy</div></div>
      <div class="acc-box black"><div class="who">${esc(r.black)}</div><div class="num">${s.black.accuracy ?? "–"}</div><div class="lbl">Accuracy</div></div>
    </div>
    <table class="cls-table">${rowsHtml}</table>
    <div class="section-title">Accuracy by phase</div>
    <table class="phase-table">${phase("opening")}${phase("middlegame")}${phase("endgame")}</table>
    ${moments ? `<div class="section-title">Key moments</div>${moments}` : ""}
  </div>`;
  $$(".key-moment").forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.closest("[data-retry]")) { state.ply = +el.dataset.ply; startRetry(state.ply); } else goto(+el.dataset.ply);
  }));
  $$("[data-practice]").forEach((b) => b.addEventListener("click", () => startPractice(b.dataset.practice)));
}

// --- move list
function renderMoveList() {
  const r = state.review;
  let html = `<div class="move-list">`;
  const firstBlack = r.moves.length && r.moves[0].color === "black";
  if (firstBlack) html += `<div class="num">${r.moves[0].move_number}.</div><div class="mv">…</div>`;
  for (const m of r.moves) {
    if (m.color === "white") html += `<div class="num">${m.move_number}.</div>`;
    html += `<div class="mv" data-ply="${m.ply}">${icon(m.classification)}<span class="t-${BAD.has(m.classification) || m.classification === "brilliant" || m.classification === "great" ? m.classification : ""}">${esc(m.san)}</span>${m.time_spent != null ? `<span class="t">${m.time_spent}s</span>` : ""}</div>`;
  }
  html += `</div>`;
  $("#tab-moves").innerHTML = html;
  $$(".move-list .mv[data-ply]").forEach((el) => el.addEventListener("click", () => goto(+el.dataset.ply)));
}

// --- eval graph
// The graph is rebuilt only when the review or its size changes; moving through the game just
// slides the cursor.
function renderGraph() {
  const r = state.review;
  const svg = $("#graph");
  const W = svg.clientWidth || 400, H = svg.clientHeight || 90;
  const n = r.moves.length || 1;
  const sig = `${state.reviewKey}|${W}|${H}|${n}`;
  if (svg._sig !== sig) {
    svg._sig = sig;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const pts = [r.start_eval, ...r.moves.map((m) => m.eval)].map((e) => (e.mate != null ? (evalNum(e) > 0 ? 100 : 0) : winPct(e.cp)));
    const x = (i) => (i / n) * W, y = (p) => H - (p / 100) * H;
    const area = `M0,${H} ` + pts.map((p, i) => `L${x(i).toFixed(1)},${y(p).toFixed(1)}`).join(" ") + ` L${W},${H} Z`;
    const dots = r.moves.map((m, i) => {
      if (!["brilliant", "great", "mistake", "miss", "blunder"].includes(m.classification)) return "";
      return `<circle cx="${x(i + 1)}" cy="${y(pts[i + 1])}" r="4" fill="var(--c-${m.classification})" stroke="#1f1e1b" stroke-width="1.5"/>`;
    }).join("");
    svg.innerHTML = `<rect width="${W}" height="${H}" fill="#403d39"/>
      <path d="${area}" fill="#fff"/>
      <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="#888" stroke-dasharray="2 3" stroke-width="1"/>
      <line id="graph-cursor" x1="0" x2="0" y1="0" y2="${H}" stroke="var(--accent)" stroke-width="2"/>
      ${dots}`;
  }
  const cx = (state.ply / n) * W;
  const cursor = svg.querySelector("#graph-cursor");
  cursor.setAttribute("x1", cx);
  cursor.setAttribute("x2", cx);
}

$("#graph").addEventListener("click", (e) => {
  const r = state.review;
  if (!r) return;
  const rect = e.currentTarget.getBoundingClientRect();
  goto(Math.round(((e.clientX - rect.left) / rect.width) * r.moves.length));
});
window.addEventListener("resize", () => { if (state.review && !$("#review").classList.contains("hidden")) renderGraph(); });

// --- controls
function stopPlay() {
  clearInterval(state.playTimer);
  state.playTimer = null;
  $("#play-btn")?.classList.remove("on");
}
function togglePlay() {
  if (state.playTimer) return stopPlay();
  if (state.ply >= state.review.moves.length) goto(0);
  $("#play-btn").classList.add("on");
  state.playTimer = setInterval(() => {
    if (state.ply >= state.review.moves.length) return stopPlay();
    goto(state.ply + 1);
  }, 1100);
}
function nav(action) {
  if (!state.review) return;
  if (action !== "play") stopPlay();
  if (state.mode !== "review" && action === "flip") {
    state.flipped = !state.flipped;
    renderPlayers();
    renderCoachPanel();
    return;
  }
  ({
    start: () => goto(0),
    prev: () => goto(state.ply - 1),
    next: () => goto(state.ply + 1),
    end: () => goto(state.review.moves.length),
    flip: () => { state.flipped = !state.flipped; goto(state.ply); },
    play: togglePlay,
  })[action]();
}
$$("[data-nav]").forEach((b) => b.addEventListener("click", () => nav(b.dataset.nav)));
$("#show-engine").addEventListener("change", () => (state.mode === "review" ? goto(state.ply) : renderCoachPanel()));

document.addEventListener("keydown", (e) => {
  if ($("#review").classList.contains("hidden") || e.target.matches("input, textarea, select")) return;
  if (state.mode !== "review") {
    const c = state.coach;
    if (e.key === "Escape") { leaveCoach(); goto(state.ply); return; }
    if (state.mode === "line" && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      e.preventDefault();
      c.idx = Math.max(0, Math.min(c.steps.length, c.idx + (e.key === "ArrowRight" ? 1 : -1)));
      renderLine();
      return;
    }
    if (state.mode === "explore" && e.key === "ArrowLeft" && c.history.length > 1 && !c.busy) {
      e.preventDefault();
      c.history.pop();
      renderExplore();
      return;
    }
    if (e.key === "f") nav("flip");
    if (e.key.startsWith("Arrow") || e.key === " ") e.preventDefault();
    return;
  }
  const map = { ArrowLeft: "prev", ArrowRight: "next", Home: "start", End: "end", f: "flip", " ": "play", ArrowUp: "start", ArrowDown: "end" };
  if (map[e.key]) { e.preventDefault(); nav(map[e.key]); }
});

// ------------------------------------------------------------------ share as PDF
const isLocalHost = () => /^(localhost|127\.|\[::1\]|0\.0\.0\.0)/.test(location.hostname);

function reviewLink() {
  const q = $("#share-perspective").value === "black" ? "?as=black" : "";
  return `${location.origin}/#/review/${state.reviewKey}${q}`;
}

$("#share-btn").addEventListener("click", () => {
  if (!state.reviewKey) return toast("This review can't be shared yet.");
  $("#share-perspective").value = state.flipped ? "black" : "white";
  $("#share-link").checked = !isLocalHost();
  $("#share-link-note").textContent = isLocalHost()
    ? "This server runs on localhost, so the link only works on this computer. The PDF works anywhere."
    : "";
  try { $("#share-sender").value = localStorage.getItem("share-sender") || ""; } catch {}
  // Only offer the system share sheet where the browser can share files.
  const probe = new File([""], "x.pdf", { type: "application/pdf" });
  $("#share-send").classList.toggle("hidden", !(navigator.canShare && navigator.canShare({ files: [probe] })));
  $("#share-dialog").showModal();
});

async function buildPdf() {
  const sender = $("#share-sender").value.trim();
  try { localStorage.setItem("share-sender", sender); } catch {}
  const body = {
    perspective: $("#share-perspective").value,
    sender,
    note: $("#share-note").value,
    puzzles: $("#share-puzzles").checked,
    moves: $("#share-moves").checked,
    link: $("#share-link").checked ? reviewLink() : null,
  };
  const res = await fetch(`/api/analysis/${state.reviewKey}/pdf`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : "Invalid options");
  }
  const name = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "chess-review.pdf";
  return new File([await res.blob()], name, { type: "application/pdf" });
}

async function withBusy(btn, fn) {
  const buttons = $$("#share-dialog button");
  const label = btn.textContent;
  buttons.forEach((b) => (b.disabled = true));
  btn.textContent = "Creating PDF…";
  try { await fn(); } catch (err) { if (err.name !== "AbortError") toast("Couldn't share: " + err.message); }
  buttons.forEach((b) => (b.disabled = false));
  btn.textContent = label;
}

$("#share-download").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  const file = await buildPdf();
  const url = URL.createObjectURL(file);
  const a = Object.assign(document.createElement("a"), { href: url, download: file.name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}));

$("#share-send").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  const file = await buildPdf();
  const r = state.review;
  await navigator.share({
    files: [file],
    title: `Game review: ${r.white} vs ${r.black}`,
    text: $("#share-note").value.trim() || `Check out this chess game review: ${r.white} vs ${r.black}`,
  });
  $("#share-dialog").close();
}));

$("#share-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(reviewLink());
    toast("Link copied to clipboard");
  } catch {
    prompt("Copy this link:", reviewLink());
  }
});

// ------------------------------------------------------------------ init
$("#legend-row").innerHTML = Object.keys(CLS).filter((c) => c !== "forced").map((c) => `<span class="legend-item">${icon(c)} ${CLS[c].label}</span>`).join("");
route();
