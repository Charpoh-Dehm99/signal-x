/* SIGNAL-X dashboard controller: live data plumbing, panels, controls.
   Observation tool only — G0 killed 2026-10-17; layers are visualizations. */

"use strict";

const $ = (id) => document.getElementById(id);
/* HTML-escape exchange/journal derived strings before any innerHTML sink.
   Data provenance is upstream APIs and the local journal; escaping at render
   keeps markup injection impossible even if a poisoned feed cracks both. */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
const STREAM_INTERVAL_S = 15;
const TAIL_MS = 2500;
const OVERLAY_MS = 25000;
const HOT_EVENTS = new Set(["RESISTANCE_TOUCHED", "SQUEEZE_GUARD", "BOS_DOWN"]);
const WARN_EVENTS = new Set(["NEAR_RESISTANCE", "BEARISH_DIVERGENCE", "BULLISH_DIVERGENCE", "MOMENTUM_DECAY", "FUNDING_EXTREME"]);
const EVENT_TEXT = {
  RESISTANCE_TOUCHED: "Resistance touched",
  NEAR_RESISTANCE: "Approaching resistance",
  SQUEEZE_GUARD: "Squeeze guard \u2014 expansion window",
  BOS_DOWN: "BOS down \u2014 structure break below",
  BEARISH_DIVERGENCE: "Bearish divergence \u2192 SHORT bias",
  BULLISH_DIVERGENCE: "Bullish divergence \u2192 LONG bias",
  MOMENTUM_DECAY: "Momentum decaying",
  FUNDING_EXTREME: "Funding extreme",
};
const eventText = (e) => EVENT_TEXT[e] || e;
const ST_ABBR = { TRIGGERED: "TRIG", IN_TRADE: "TRADE", ARMED: "ARM", PLATEAU_BUILDING: "PLAT", PUMP_DETECTED: "PUMP" };

const state = {
  sym: null, market: null, tf: localStorage.getItem("sx_tf") || "5m",
  mode: localStorage.getItem("sx_mode") || "candle",
  gen: 0, es: null, tailTimer: null, ovTimer: null, emaTimer: null, tailBusy: false,
  lastPrice: null, ov: null, candles: [],
};

const OVERLAY_DEFAULTS = { emas: true, pivots: true, structure: true, bos: true, zones: true, avwap: true, box: true, divs: true, rsi: true };

/* ------------------------------------------------ alert sounds (WebAudio, no assets)
   synthesized chimes: emaUp/emaDown (8/13/21 cross), cascade (cross propagation),
   stack (all timeframes aligned), top1 (coin enters the top 1% of gainers),
   signal (new journal entry), hot/warn (observation events).
   Browsers block audio until the first click/keypress — ensure() is wired to both. */
const Sound = (() => {
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx = null;
  let enabled = localStorage.getItem("sx_sound") !== "0";
  let onHit = null;
  let volume = (() => {
    const v = parseFloat(localStorage.getItem("sx_vol") || "");
    return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 1;
  })();
  const log = [];
  const lastAt = {};
  const COOLDOWN_MS = { hot: 300000, warn: 300000, signal: 60000, cascade: 45000, stack: 45000, top1: 300000 };

  function ensure() {
    if (!AC) return null;
    if (!ctx) { try { ctx = new AC(); } catch { return null; } }
    if (ctx.state === "suspended") { try { ctx.resume(); } catch { /* blocked */ } }
    return ctx;
  }

  function tone(c, f, t0, dur, type, gain) {
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = type || "sine";
    o.frequency.value = f;
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime((gain == null ? 0.07 : gain) * volume, t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0008, t0 + dur);
    o.connect(g).connect(c.destination);
    o.start(t0);
    o.stop(t0 + dur + 0.04);
  }

  const PATTERNS = {
    emaUp: (c, t) => { tone(c, 784, t, 0.16); tone(c, 1175, t + 0.13, 0.22); },
    emaDown: (c, t) => { tone(c, 622, t, 0.16); tone(c, 415, t + 0.13, 0.24); },
    cascade: (c, t) => { tone(c, 523, t, 0.14); tone(c, 659, t + 0.12, 0.14); tone(c, 880, t + 0.24, 0.26); },
    stack: (c, t) => { tone(c, 659, t, 0.12); tone(c, 880, t + 0.11, 0.12); tone(c, 1047, t + 0.22, 0.12); tone(c, 1319, t + 0.33, 0.3); },
    signal: (c, t) => { tone(c, 988, t, 0.1, "triangle", 0.1); tone(c, 988, t + 0.16, 0.1, "triangle", 0.1); tone(c, 1480, t + 0.32, 0.3, "triangle", 0.1); },
    top1: (c, t) => { tone(c, 392, t, 0.5, "triangle", 0.075); tone(c, 784, t + 0.02, 0.28, "triangle", 0.055); tone(c, 1175, t + 0.18, 0.2); tone(c, 1568, t + 0.34, 0.42); },
    hot: (c, t) => { tone(c, 740, t, 0.12, "square", 0.045); tone(c, 554, t + 0.14, 0.22, "square", 0.045); },
    warn: (c, t) => { tone(c, 1320, t, 0.07, "sine", 0.045); },
  };

  function play(kind, key, info) {
    if (!PATTERNS[kind]) return false;
    const now = performance.now();
    const ck = key || kind;
    if (lastAt[ck] && now - lastAt[ck] < (COOLDOWN_MS[kind] || 0)) return false;
    lastAt[ck] = now;
    log.push(kind);
    if (log.length > 60) log.shift();
    /* notifications record every alert that beats the cooldowns (audio may
       still be muted — the bell panel then stays the only channel) */
    if (onHit) { try { onHit(kind, info || null); } catch { /* notify hiccup */ } }
    if (!enabled) return false;
    const c = ensure();
    if (c && c.state === "running") {
      try { PATTERNS[kind](c, c.currentTime + 0.01); } catch { /* audio hiccup */ }
    }
    return true;
  }

  function updateBtn() {
    const b = $("sound");
    if (!b) return;
    if (!b.firstChild) {
      b.innerHTML =
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M4 9.6v4.8h3.1L12 18.2V5.8L7.1 9.6H4z"/>' +
        '<path class="w1" d="M15.3 9a4.4 4.4 0 0 1 0 6"/>' +
        '<path class="w2" d="M17.8 6.5a8 8 0 0 1 0 11"/>' +
        '<path class="mute" d="M15.5 9.5l5 5M20.5 9.5l-5 5"/>' +
        "</svg>";
    }
    b.classList.toggle("off", !enabled);
    b.title = enabled
      ? "Alert sounds: ON — EMA crosses, cascades, top-1% entries, signals, key events (click to mute)"
      : "Alert sounds: muted (click to unmute)";
  }

  function toggle() {
    enabled = !enabled;
    localStorage.setItem("sx_sound", enabled ? "1" : "0");
    updateBtn();
    if (enabled) play("warn", "sound-on", { msg: "Sound alerts on" });
  }

  function boot() {
    updateBtn();
    const b = $("sound");
    if (b) b.addEventListener("click", toggle);
    const v = $("vol");
    if (v) {
      v.value = String(Math.round(volume * 100));
      v.addEventListener("input", () => setVolume(Number(v.value) / 100));
      v.addEventListener("change", () => { ensure(); preview(); });
    }
    window.addEventListener("pointerdown", () => ensure(), { passive: true });
    window.addEventListener("keydown", () => ensure(), { passive: true });
  }

  function setOnHit(fn) { onHit = fn; }

  function setVolume(v) {
    v = Math.min(1, Math.max(0, Number(v) || 0));
    volume = v;
    try { localStorage.setItem("sx_vol", String(v)); } catch { /* private mode */ }
  }

  function preview() {
    const c = ensure();
    if (c && c.state === "running") {
      try { tone(c, 880, c.currentTime + 0.01, 0.16); tone(c, 1174, c.currentTime + 0.12, 0.22); } catch { /* audio hiccup */ }
    }
  }

  return { play, toggle, boot, log, setOnHit, setVolume, preview, get volume() { return volume; }, get enabled() { return enabled; } };
})();

/* ------------------------------------------------ notifications (bell panel)
   every chime that beats the sound cooldowns lands here with context: time,
   symbol and message; the bell badges unread, opening clears it, clicking an
   entry opens that symbol. Persisted to localStorage so reloads keep history;
   filter by kind chip or free-text search. The tab title + favicon get an
   unread count while the tab is in the background. */
const Notify = (() => {
  const LS_KEY = "sx_notify";
  const items = []; /* newest first */
  const MAX = 200;
  const GROUPS = { ema: ["emaUp", "emaDown"], chain: ["cascade", "stack"], top1: ["top1"], events: ["hot", "warn"], signal: ["signal"] };
  let unread = 0;
  let filter = "all";
  let q = "";
  const KIND_LABEL = {
    emaUp: "EMA \u25b2 up", emaDown: "EMA \u25bc down", cascade: "Cascade",
    stack: "Stack", top1: "Top 1%", signal: "Signal", hot: "Hot event", warn: "Warn event",
  };

  function save() {
    try { localStorage.setItem(LS_KEY, JSON.stringify({ items: items.slice(0, MAX), unread })); } catch { /* quota / private mode */ }
  }

  function load() {
    try {
      const d = JSON.parse(localStorage.getItem(LS_KEY) || "");
      if (!d || !Array.isArray(d.items)) return;
      for (const n of d.items.slice(0, MAX)) {
        if (!n || !n.t || !n.kind) continue;
        items.push({ t: n.t, kind: n.kind, sym: n.sym || null, msg: String(n.msg || KIND_LABEL[n.kind] || n.kind) });
      }
      unread = Math.min(Math.max(0, Number(d.unread) || 0), items.length);
    } catch { /* fresh start */ }
  }

  function match(n) {
    if (filter !== "all" && !(GROUPS[filter] || []).includes(n.kind)) return false;
    if (q) {
      const hay = ((n.sym || "") + " " + n.msg + " " + (KIND_LABEL[n.kind] || "")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function render() {
    const ul = $("notify-list");
    if (!ul) return;
    if (!items.length) {
      ul.innerHTML = '<li class="notify-item empty">nothing yet \u2014 alerts appear here as they chime</li>';
      return;
    }
    const rows = items.filter(match);
    if (!rows.length) {
      ul.innerHTML = '<li class="notify-item empty">no alerts match this filter</li>';
      return;
    }
    ul.innerHTML = rows.map((n) => {
      const label = esc(n.sym || KIND_LABEL[n.kind] || n.kind);
      return '<li class="notify-item k-' + esc(n.kind) + '"' + (n.sym ? ' data-sym="' + esc(n.sym) + '" title="open ' + esc(n.sym) + '"' : "") + ">" +
        '<span class="nt">' + new Date(n.t).toLocaleTimeString() + "</span>" +
        '<span class="nmsg"><b>' + label + "</b> \u00b7 " + esc(n.msg) + "</span>" +
        "</li>";
    }).join("");
  }

  let favState = null;
  function tabBadge() {
    document.title = unread ? "(" + (unread > 99 ? "99+" : unread) + ") SIGNAL-X \u00b7 charts" : "SIGNAL-X \u00b7 charts";
    const state = unread ? "u" : "n";
    if (state === favState) return;
    favState = state;
    try {
      let link = document.querySelector('link[rel="icon"]');
      if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.appendChild(link); }
      const c = document.createElement("canvas"); c.width = c.height = 64;
      const x = c.getContext("2d");
      const g = x.createLinearGradient(0, 0, 64, 64); g.addColorStop(0, "#2e8bff"); g.addColorStop(1, "#7a4dff");
      x.fillStyle = g;
      x.beginPath();
      if (x.roundRect) x.roundRect(3, 3, 58, 58, 16); else x.rect(3, 3, 58, 58);
      x.fill();
      x.fillStyle = "#fff"; x.font = "800 40px system-ui, sans-serif";
      x.textAlign = "center"; x.textBaseline = "middle"; x.fillText("S", 32, 36);
      if (unread) {
        x.fillStyle = "#f23645";
        x.beginPath(); x.arc(50, 13, 13, 0, Math.PI * 2); x.fill();
        x.lineWidth = 5; x.strokeStyle = "#101013"; x.stroke();
      }
      link.href = c.toDataURL("image/png");
    } catch { /* favicon is cosmetic */ }
  }

  function badge() {
    const b = $("notify");
    if (b) {
      let el = b.querySelector(".notify-badge");
      if (!el) { el = document.createElement("span"); el.className = "notify-badge"; b.appendChild(el); }
      el.hidden = !unread;
      el.textContent = unread > 9 ? "9+" : String(unread);
    }
    tabBadge();
  }

  function push(kind, info) {
    info = info || {};
    items.unshift({ t: Date.now(), kind, sym: info.sym || null, msg: info.msg || (KIND_LABEL[kind] || kind) });
    if (items.length > MAX) items.pop();
    unread++;
    render(); badge(); save();
  }

  function open(show) {
    const p = $("notify-panel");
    if (!p) return;
    p.hidden = show == null ? !p.hidden : !show;
    if (!p.hidden && unread) { unread = 0; badge(); save(); }
  }

  function boot() {
    load();
    const b = $("notify");
    if (b) {
      b.innerHTML =
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M12 4.2a4.8 4.8 0 0 0-4.8 4.8v3.1L5.9 14.9h12.2l-1.3-2.8V9A4.8 4.8 0 0 0 12 4.2z"/>' +
        '<path d="M10.2 17.6a1.9 1.9 0 0 0 3.6 0"/>' +
        "</svg>";
      b.addEventListener("click", (e) => { e.stopPropagation(); open(); });
    }
    const clear = $("notify-clear");
    if (clear) clear.addEventListener("click", (e) => {
      e.stopPropagation();
      items.length = 0;
      render(); save();
    });
    const chips = $("notify-chips");
    if (chips) chips.addEventListener("click", (e) => {
      const c = e.target.closest("button[data-f]");
      if (!c) return;
      filter = c.dataset.f;
      chips.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === c));
      render();
    });
    const s = $("notify-search");
    if (s) s.addEventListener("input", () => { q = s.value.trim().toLowerCase(); render(); });
    const ul = $("notify-list");
    if (ul) ul.addEventListener("click", (e) => {
      const li = e.target.closest(".notify-item[data-sym]");
      if (!li) return;
      open(false);
      selectSymbol(li.dataset.sym);
    });
    document.addEventListener("click", (e) => {
      const p = $("notify-panel");
      if (!p || p.hidden) return;
      const bb = $("notify");
      if (!p.contains(e.target) && !(bb && bb.contains(e.target))) open(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const p = $("notify-panel");
      if (p && !p.hidden) open(false);
    });
    render(); badge();
  }

  return { push, boot, open, get items() { return items; }, get unread() { return unread; }, get filter() { return filter; } };
})();

/* ------------------------------------------------ formatting */
function fmtPrice(p) {
  if (p == null || !isFinite(p)) return "\u2014";
  if (p >= 1000) return p.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (p >= 1) return p.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return p.toPrecision(4);
}
function fmtPct(x) {
  if (x == null || !isFinite(x)) return "\u2014";
  return (x >= 0 ? "+" : "") + x.toFixed(1) + "%";
}
function fmtQv(v) {
  if (v == null || !isFinite(v)) return "\u2014";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
  return "$" + v.toFixed(0);
}
function cell(label, value, cls) {
  return '<div class="cell"><div class="k">' + label + '</div><div class="v ' + (cls || "") + '">' + value + "</div></div>";
}
function setStatus(msg) {
  const el = $("chart-status");
  el.textContent = msg || "";
  el.classList.toggle("show", !!msg);
}
function numberFmt(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(Math.round(n));
}

/* ------------------------------------------------ rings */
const RINGS = {};
function buildRings() {
  document.querySelectorAll("[data-ring]").forEach((card, idx) => {
    const key = card.dataset.ring;
    const body = card.querySelector(".ring-body");
    const c1 = body.dataset.c1, c2 = body.dataset.c2, R = 46, C = 2 * Math.PI * R, id = "rg" + idx;
    body.insertAdjacentHTML("beforeend",
      '<div class="donut" style="--glow:' + c1 + '88">' +
      '<svg viewBox="0 0 120 120"><defs><linearGradient id="' + id + '" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="' + c1 + '"/><stop offset="1" stop-color="' + c2 + '"/></linearGradient></defs>' +
      '<circle class="t" cx="60" cy="60" r="' + R + '"/>' +
      '<circle class="v" cx="60" cy="60" r="' + R + '" stroke="url(#' + id + ')" stroke-dasharray="' + C + '" stroke-dashoffset="' + C + '"/></svg>' +
      "<b>\u2014</b></div>" +
      '<div class="legend"><span style="--c:' + c1 + '">&nbsp;</span><span style="--c:#58585f">&nbsp;</span></div>');
    RINGS[key] = { v: body.querySelector(".v"), b: body.querySelector("b"), lg: body.querySelectorAll(".legend span"), C };
  });
}
function updateRing(key, value, label, sub) {
  const r = RINGS[key];
  if (!r) return;
  const pct = Math.max(0, Math.min(100, value == null ? 0 : value));
  r.v.style.strokeDashoffset = r.C * (1 - pct / 100);
  r.b.textContent = label == null ? "\u2014" : label;
  r.lg[0].textContent = sub[0];
  r.lg[1].textContent = sub[1];
}

/* ------------------------------------------------ volume bars (last 7) */
function buildBars() {
  const host = $("bars");
  host.innerHTML = "";
  for (let i = 0; i < 7; i++) {
    host.insertAdjacentHTML("beforeend", '<div class="col"><div class="track"><div class="fill"></div></div><small></small></div>');
  }
}
function renderBars(candles) {
  const tail = candles.slice(-7);
  if (!tail.length) return;
  const max = Math.max.apply(null, tail.map((k) => k.v)) || 1;
  const totalQv = tail.reduce((a, k) => a + (k.qv || 0), 0);
  $("vol7-sum").textContent = fmtQv(totalQv);
  const cols = document.querySelectorAll("#bars .col");
  tail.forEach((k, i) => {
    const fill = cols[i].querySelector(".fill");
    const up = k.c >= k.o;
    const c = up ? ["#14c9a8", "#089981"] : ["#ff5a68", "#f23645"];
    const d = new Date(k.t);
    cols[i].querySelector("small").textContent =
      state.tf === "1m" || state.tf === "5m" || state.tf === "15m"
        ? String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0")
        : d.getDate() + " " + ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
    fill.style.background = "linear-gradient(180deg," + c[0] + "," + c[1] + ")";
    fill.style.setProperty("--g", c[1]);
    fill.style.height = Math.max(8, (k.v / max) * 100) + "%";
  });
}

/* ------------------------------------------------ layers panel */
function renderLayers(ov) {
  const host = $("layers");
  if (!ov) {
    host.innerHTML = cell("layers", "unavailable for this symbol", "warn") + cell("hint", "new listing / short history", "");
    return;
  }
  const lastStruct = ov.structure && ov.structure.length ? ov.structure[ov.structure.length - 1] : null;
  const anchorAge = ov.avwap ? ov.n - 1 - ov.avwap.anchor_i : null;
  const vwapLast = ov.avwap ? ov.avwap.line[ov.n - 1] : null;
  const chops = ov.breaks.filter((e) => e.type === "choch");
  const bosN = ov.breaks.length - chops.length;
  const lastDown = ov.breaks.filter((e) => e.dir === "down").pop();
  const lastChoch = chops[chops.length - 1];
  const emaVals = ov.emas ? [ov.emas["8"][ov.n - 1], ov.emas["13"][ov.n - 1], ov.emas["21"][ov.n - 1]] : [null, null, null];
  const emaCls = emaVals[0] == null ? ""
    : emaVals[0] > emaVals[1] && emaVals[1] > emaVals[2] ? "ok"
      : emaVals[0] < emaVals[1] && emaVals[1] < emaVals[2] ? "warn" : "";
  const lastEmaCross = ov.ema_crosses && ov.ema_crosses.length ? ov.ema_crosses[ov.ema_crosses.length - 1] : null;
  const cells = [
    cell("ATR (14)", fmtPrice(ov.atr)),
    cell("EMA 8/13/21", emaVals[0] == null ? "\u2014"
      : fmtPrice(emaVals[0]) + " / " + fmtPrice(emaVals[1]) + " / " + fmtPrice(emaVals[2])
        + (lastEmaCross ? " \u00b7 " + (lastEmaCross.dir === "up" ? "\u25b2" : "\u25bc") + " x" + lastEmaCross.i : ""),
      emaCls),
    cell("zones", ov.zones.length + (ov.zones.length ? " \u00b7 top \u00d7" + ov.zones[0].touches : "")),
    cell("plateau", ov.box ? ov.box.height_atr.toFixed(1) + "\u00d7ATR \u00b7 " + Math.round(ov.box.containment * 100) + "%" : "\u2014",
      ov.box && ov.box.qualifies ? "ok" : ""),
    cell("breaks", ov.breaks.length + " \u00b7 " + bosN + " BOS \u00b7 " + chops.length + " CHoCH",
      lastDown && state.lastPrice != null && state.lastPrice < lastDown.level ? "hot" : ""),
    cell("divergence", ov.divs.length + "\u2193 " + ov.budivs.length + "\u2191",
      ov.divs.length ? "warn" : ov.budivs.length ? "ok" : ""),
    cell("structure", lastStruct ? lastStruct.label + (lastChoch ? " \u00b7 CHoCH" + (lastChoch.dir === "up" ? "\u2191" : "\u2193") : "") : "\u2014",
      lastChoch ? (lastChoch.dir === "up" ? "ok" : "warn") : ""),
    cell("AVWAP", vwapLast != null ? fmtPrice(vwapLast) : "\u2014"),
    cell("AVWAP anchor", anchorAge != null ? anchorAge + " bars ago" : "\u2014"),
  ];
  host.innerHTML = cells.join("");
}

/* ------------------------------------------------ EMA cross cascade (5m -> 4h) */
let emaSeen = {};
let emaReady = false;
let cascadeSig = null;
let stackSig = undefined;
const EMA_FRESH_MS = 120000;

function fmtAge(ms) {
  if (ms == null || !isFinite(ms)) return "\u2014";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return s + "s";
  const m = Math.round(s / 60);
  if (m < 90) return m + "m";
  if (m < 60 * 48) return (m / 60).toFixed(1) + "h";
  return (m / 1440).toFixed(1) + "d";
}

function renderEmaCascade(d) {
  let newest = null;
  $("ema-rows").innerHTML = d.tfs.map((t) => {
    const last = t.crosses.length ? t.crosses[t.crosses.length - 1] : null;
    const ageMs = last ? d.now_ms - (last.t + t.step_ms) : null;
    const fresh = ageMs != null && ageMs >= 0 && ageMs < EMA_FRESH_MS;
    if (last && (!newest || last.t > newest.t)) newest = { dir: last.dir, tf: t.tf, ageMs };
    const st = t.state === "up" ? "up" : t.state === "down" ? "down" : "flat";
    const arrow = t.state === "up" ? "\u25b2" : t.state === "down" ? "\u25bc" : "\u25cf";
    const crossTxt = last ? (last.dir === "up" ? "\u25b2" : "\u25bc") + " " + fmtAge(ageMs) + " ago" : "\u2014";
    const vals = t.emas["8"] != null
      ? fmtPrice(t.emas["8"]) + " / " + fmtPrice(t.emas["13"]) + " / " + fmtPrice(t.emas["21"])
      : "warming up";
    return '<div class="ema-row' + (fresh ? " fresh" : "") + (t.tf === state.tf ? " active" : "") + '">' +
      '<span class="tf">' + t.tf + "</span>" +
      '<span class="st ' + st + '">' + arrow + " " + (t.state ? t.state.toUpperCase() : "\u2014") + "</span>" +
      '<span class="age">' + crossTxt + "</span>" +
      '<span class="vals">' + vals + "</span></div>";
  }).join("");

  const p = d.pattern || {};
  const pat = $("ema-pattern");
  const arrow = p.direction === "up" ? "\u25b2 up" : "\u25bc down";
  if (p.direction && p.chain.length > 1) {
    const parts = p.chain.map((tf, i) =>
      tf + (i && p.intervals_ms[i - 1] != null ? " +" + fmtAge(p.intervals_ms[i - 1]) : ""));
    pat.textContent = arrow + " cascade: " + parts.join(" \u2192 ");
    pat.className = "ema-pattern " + p.direction;
  } else if (p.direction) {
    pat.textContent = arrow + " cross on " + p.chain[0] + " \u00b7 no propagation yet";
    pat.className = "ema-pattern " + p.direction;
  } else {
    pat.textContent = "no recent cross cascade";
    pat.className = "ema-pattern";
  }
  if (p.full_stack) pat.textContent += " \u00b7 full " + (p.full_stack === "up" ? "bull" : "bear") + " stack";

  const banner = $("ema-alert");
  let alert = null;
  for (const t of d.tfs) {
    const last = t.crosses.length ? t.crosses[t.crosses.length - 1] : null;
    if (!last) continue;
    const key = last.t + ":" + last.dir;
    if (emaSeen[t.tf] !== key) {
      emaSeen[t.tf] = key;
      if (emaReady) alert = { tf: t.tf, dir: last.dir };
    }
  }
  if (alert) {
    banner.textContent = "EMA 8/13/21 crossed " + (alert.dir === "up" ? "\u25b2 UP" : "\u25bc DOWN") +
      " on " + alert.tf + " \u00b7 " + new Date().toLocaleTimeString();
    banner.className = "ema-alert show " + alert.dir;
    Sound.play(alert.dir === "up" ? "emaUp" : "emaDown", alert.tf + alert.dir, {
      sym: state.sym,
      msg: "EMA 8/13/21 crossed " + (alert.dir === "up" ? "\u25b2 UP" : "\u25bc DOWN") + " on " + alert.tf,
    });
  } else if (newest && newest.ageMs < EMA_FRESH_MS) {
    banner.textContent = "fresh " + (newest.dir === "up" ? "\u25b2 up" : "\u25bc down") +
      " cross on " + newest.tf + " \u00b7 " + fmtAge(newest.ageMs) + " ago";
    banner.className = "ema-alert show " + newest.dir;
  } else {
    banner.textContent = "";
    banner.className = "ema-alert";
  }
  /* cascade propagation + full-stack chimes (only after the panel is seeded) */
  const sig = p.direction ? p.direction + ":" + p.chain.join(">") : null;
  const fs = p.full_stack || null;
  if (emaReady) {
    if (sig !== cascadeSig && sig && p.chain.length > 1) {
      Sound.play(fs ? "stack" : "cascade", "p" + sig, {
        sym: state.sym,
        msg: (fs ? "Full " + (fs === "up" ? "bull" : "bear") + " stack" : "Cascade " + (p.direction === "up" ? "\u25b2 up" : "\u25bc down")) +
          " \u00b7 " + p.chain.join(" \u2192 "),
      });
    } else if (fs && fs !== stackSig) {
      Sound.play("stack", "fs" + fs, { sym: state.sym, msg: "Full " + (fs === "up" ? "bull" : "bear") + " stack across timeframes" });
    }
  }
  cascadeSig = sig;
  stackSig = fs;

  emaReady = true;
  $("ema-upd").textContent = "updated " + new Date().toLocaleTimeString();
  const errs = d.errors ? Object.keys(d.errors) : [];
  $("ema-errors").textContent = errs.length ? "no data for: " + errs.join(", ") : "";
}

async function loadEmaCascade(sym, gen) {
  if (!sym) return;
  try {
    const r = await fetch("/api/ema-cascade/" + sym + "?market=" + encodeURIComponent(state.market || "auto"));
    if (!r.ok || gen !== state.gen) return;
    renderEmaCascade(await r.json());
  } catch { /* transient */ }
}

/* ------------------------------------------------ observation panel (SSE) */
let prevEvents = null;

function renderObservation(s) {
  const b = $("state");
  b.textContent = s.state || "\u2014";
  b.className = "badge " + (s.state || "");
  state.lastPrice = s.price;
  $("mkt").textContent = s.market + " \u00b7 " + s.tf;
  const chg = $("chg");
  chg.textContent = fmtPct(s.change_24h_pct) + " 24h";
  chg.className = "chg " + (s.change_24h_pct >= 0 ? "up" : "down");

  const cm = s.compression;
  const r = s.resistance;
  $("meta").innerHTML = [
    cell("vol z (1h)", s.vol_z == null ? "\u2014" : s.vol_z.toFixed(1), s.vol_z >= 3 ? "warn" : ""),
    cell("plateau", cm ? "h/atr " + cm.height_atr.toFixed(1) + " \u00b7 " + (cm.containment * 100).toFixed(0) + "%" : "\u2014",
      cm && cm.qualifies ? "ok" : ""),
    cell("resistance", r ? fmtPrice(r.low) + "\u2013" + fmtPrice(r.high) : "none"),
    cell("dist to res.", r ? (r.dist_pct * 100).toFixed(1) + "% \u00b7 " + r.dist_atr.toFixed(1) + " ATR" : "\u2014",
      r && r.dist_atr <= 0.5 ? "warn" : ""),
    cell("swing / BOS",
      s.swing_low == null ? "\u2014"
        : fmtPrice(s.swing_low) + "\u2013" + (s.swing_high == null ? "\u2014" : fmtPrice(s.swing_high))
          + " \u00b7 " + (s.bos_down ? "BOS\u2193" : s.bos_up ? "BOS\u2191" : "no BOS"),
      s.bos_down ? "hot" : s.bos_up ? "ok" : ""),
    cell("RSI \u00b7 div \u00b7 decay",
      (s.rsi == null ? "\u2014" : s.rsi.toFixed(0)) +
        " \u00b7 " + (s.has_bearish_div ? "bear\u2193 short" : s.has_bullish_div ? "bull\u2191 long" : "no div") +
        " \u00b7 " + (s.momentum_decay ? "decay" : "flat"),
      s.has_bearish_div ? "warn" : ""),
    cell("funding", s.funding_now == null ? "n/a (spot)"
      : (s.funding_now * 100).toFixed(4) + "% \u00b7 p" + (s.funding_pctile == null ? "\u2014" : Math.round(s.funding_pctile)),
      s.funding_pctile >= 90 ? "hot" : ""),
    cell("OI regime", s.oi_regime || "n/a"),
    cell("squeeze guard", s.squeeze_active === null ? "unknown" : (s.squeeze_active ? "BLOCK" : "clear"), s.squeeze_active ? "hot" : ""),
    cell("zones", String(s.n_zones)),
  ].join("");

  const chips = $("events");
  chips.innerHTML = !s.events || !s.events.length
    ? '<span class="ev">no events</span>'
    : s.events.map((e) => {
        const cls = HOT_EVENTS.has(e) ? "hot" : WARN_EVENTS.has(e) ? "warn" : "ok";
        return '<span class="ev ' + cls + '">' + esc(e) + "</span>";
      }).join("");

  /* chime for newly appeared critical / warning events (first snapshot seeds silently) */
  const evSet = new Set(s.events || []);
  if (prevEvents) {
    for (const e of evSet) {
      if (prevEvents.has(e)) continue;
      if (HOT_EVENTS.has(e)) Sound.play("hot", e, { sym: state.sym, msg: eventText(e) });
      else if (WARN_EVENTS.has(e)) Sound.play("warn", e, { sym: state.sym, msg: eventText(e) });
    }
  }
  prevEvents = evSet;

  const sq = $("squeeze-badge");
  sq.textContent = "squeeze guard: " + (s.squeeze_active === null ? "unknown" : s.squeeze_active ? "BLOCK" : "clear");
  sq.classList.toggle("hot", !!s.squeeze_active);

  updateRing("volz", s.vol_z == null ? 0 : Math.max(0, Math.min(100, (s.vol_z / 6) * 100)),
    s.vol_z == null ? "\u2014" : s.vol_z.toFixed(1),
    ["z \u2265 3 = pump-grade", "vs prior 7d"]);
  updateRing("funding", s.funding_pctile == null ? 0 : s.funding_pctile,
    s.funding_pctile == null ? "\u2014" : Math.round(s.funding_pctile),
    [s.funding_now == null ? "no perp data" : (s.funding_now * 100).toFixed(4) + "% now", "\u2265 P90 = extreme"]);
  updateRing("plateau", cm ? cm.score * 100 : 0,
    cm ? Math.round(cm.score * 100) : "\u2014",
    [cm ? cm.height_atr.toFixed(1) + "\u00d7ATR height" : "no box yet", cm && cm.qualifies ? "qualifies" : "not qualifying"]);
}

/* ------------------------------------------------ coin icons */
let logoMap = {};

function baseOf(sym) {
  return String(sym || "").replace(/USDT$/, "").toUpperCase();
}
function logoFor(sym) {
  return logoMap[baseOf(sym)] || null;
}
function logoBadge(sym) {
  const base = baseOf(sym);
  const url = logoFor(sym);
  return '<span class="coin-logo" data-l="' + esc(base.slice(0, 1)) + '">' +
    (url ? '<img class="logo-img" src="' + esc(url) + '" alt="" loading="lazy">' : "") +
    "</span>";
}
function paintCoinIcon(sym) {
  const el = $("coin-icon");
  const base = baseOf(sym);
  const url = logoFor(sym);
  el.classList.toggle("has-img", !!url);
  el.innerHTML = "";
  if (!url) { el.textContent = base.slice(0, 3); return; }
  const img = document.createElement("img");
  img.alt = base + " icon";
  img.loading = "lazy";
  img.onerror = () => { el.classList.remove("has-img"); el.textContent = base.slice(0, 3); };
  img.src = url;
  el.appendChild(img);
}
async function loadLogos(symbols) {
  try {
    const bases = [...new Set((symbols || []).map(baseOf).filter(Boolean))];
    if (!bases.length) return;
    const r = await fetch("/api/logos?symbols=" + encodeURIComponent(bases.join(",")));
    if (!r.ok) return;
    const d = await r.json();
    Object.assign(logoMap, d.symbols || {});
  } catch { return; } /* cosmetic: letter fallbacks remain */
  if (state.sym) paintCoinIcon(state.sym);
  if (lastGainers.length) renderGainers();
}

/* ------------------------------------------------ watchlist */
let lastGainers = [];
let latestStates = {};
let signalKeys = new Set();
let signalsReady = false;
let top1Universe = 0;
let top1Count = 1;
let top1Set = new Set();
let top1Ready = false;
const top1Watch = { busy: false, emaSeen: new Map(), evSeen: new Map() };

function pruneTop1Watch() {
  for (const m of [top1Watch.emaSeen, top1Watch.evSeen]) {
    for (const k of m.keys()) {
      if (!top1Set.has(k.split("|")[0])) m.delete(k);
    }
  }
}

async function loadGainers() {
  try {
    const r = await fetch("/api/gainers?top=40");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    lastGainers = data.gainers;
    top1Universe = data.universe || 0;
    top1Count = data.top1_count || Math.max(1, Math.ceil(top1Universe / 100));
    const next = new Set(lastGainers.slice(0, top1Count).map((g) => g.symbol));
    if (top1Ready) {
      for (const s of next) {
        if (top1Set.has(s)) continue;
        const row = lastGainers.find((g) => g.symbol === s);
        Sound.play("top1", "t1" + s, {
          sym: s,
          msg: "Entered the top 1% gainers" + (row ? " \u00b7 " + fmtPct(row.change_pct) + " \u00b7 " + fmtQv(row.quote_volume) : ""),
        });
      }
    } else if (next.size) {
      top1Ready = true; /* first poll only seeds — no chime for the current elite */
    }
    top1Set = next;
    pruneTop1Watch();
    renderGainers();
    loadLogos(lastGainers.map((g) => g.symbol));
    $("gainers-upd").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("gainers").innerHTML = '<li class="empty">failed to load: ' + esc(e.message) + "</li>";
  }
}

function renderGainers() {
  const ul = $("gainers");
  ul.innerHTML = "";
  if (!lastGainers.length) {
    ul.innerHTML = '<li class="empty">no gainers above threshold</li>';
    return;
  }
  let rank = 0;
  for (const g of lastGainers) {
    const li = document.createElement("li");
    li.dataset.sym = g.symbol;
    if (g.symbol === state.sym) li.classList.add("active");
    const isTop1 = rank++ < top1Count;
    if (isTop1) {
      li.classList.add("top1");
      li.title = "top 1% of liquid perps \u00b7 EMA + event sounds fire even when this coin is not selected";
    }
    const st = latestStates[g.symbol];
    let chip = "";
    if (st && st.state && st.state !== "IDLE") {
      const sig = st.event === "SIGNAL";
      chip = ' <span class="st ' + esc(st.state) + (sig ? " sig" : "") + '">' +
        (sig ? "TRIG" : esc(ST_ABBR[st.state] || st.state)) + "</span>";
    }
    li.innerHTML = logoBadge(g.symbol) + '<span class="s">' + esc(g.symbol) + chip + "</span>" +
      '<span class="c ' + (g.change_pct >= 0 ? "pos" : "neg") + '">' + fmtPct(g.change_pct) + "</span>" +
      '<span class="q">' + fmtQv(g.quote_volume) + "</span>";
    li.addEventListener("click", () => selectSymbol(g.symbol));
    ul.appendChild(li);
  }
}

/* background watcher for the top-1% elite: EMA crosses and hot/warn events ring
   even while another symbol is on the chart. The selected symbol is skipped
   (its panel + stream already ring). First sight of a symbol/timeframe or a
   symbol's event set seeds silently; only later changes chime. Symbols are
   fetched in parallel (futures-pinned) so one sweep stays well under cadence. */
function t1fetch(url) {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), 20000);
  return fetch(url, { signal: ctl.signal }).finally(() => clearTimeout(to));
}

async function checkTop1Symbol(sym) {
  const [rEma, rSnap] = await Promise.all([
    t1fetch("/api/ema-cascade/" + sym + "?market=futures").catch(() => null),
    t1fetch("/api/snapshot/" + sym + "?market=futures&tf=5m").catch(() => null),
  ]);
  if (rEma && rEma.ok) {
    try {
      const d = await rEma.json();
      for (const t of d.tfs || []) {
        const last = t.crosses && t.crosses.length ? t.crosses[t.crosses.length - 1] : null;
        if (!last) continue;
        const key = sym + "|" + t.tf;
        const sig = last.t + ":" + last.dir;
        if (top1Watch.emaSeen.get(key) === sig) continue;
        const known = top1Watch.emaSeen.has(key);
        top1Watch.emaSeen.set(key, sig);
        const ageMs = d.now_ms != null ? d.now_ms - (last.t + t.step_ms) : null;
        if (known && ageMs != null && ageMs >= 0 && ageMs < 10 * 60000) {
          Sound.play(last.dir === "up" ? "emaUp" : "emaDown", "t1" + key + sig, {
            sym,
            msg: "EMA 8/13/21 crossed " + (last.dir === "up" ? "\u25b2 UP" : "\u25bc DOWN") + " on " + t.tf + " \u00b7 top 1%",
          });
        }
      }
    } catch { /* malformed payload */ }
  }
  if (rSnap && rSnap.ok) {
    try {
      const s = await rSnap.json();
      const evSet = new Set(s.events || []);
      const prev = top1Watch.evSeen.get(sym);
      if (prev) {
        for (const e of evSet) {
          if (prev.has(e)) continue;
          if (HOT_EVENTS.has(e)) Sound.play("hot", "t1" + sym + e, { sym, msg: eventText(e) + " \u00b7 top 1%" });
          else if (WARN_EVENTS.has(e)) Sound.play("warn", "t1" + sym + e, { sym, msg: eventText(e) + " \u00b7 top 1%" });
        }
      }
      top1Watch.evSeen.set(sym, evSet);
    } catch { /* malformed payload */ }
  }
}

async function watchTop1() {
  if (top1Watch.busy || document.hidden || !top1Set.size) return;
  top1Watch.busy = true;
  try {
    const jobs = [];
    for (const sym of top1Set) {
      if (sym === state.sym) continue;
      jobs.push(checkTop1Symbol(sym));
    }
    await Promise.all(jobs);
  } finally {
    top1Watch.busy = false;
  }
}

function highlight(sym) {
  document.querySelectorAll("#gainers li").forEach((li) => li.classList.toggle("active", li.dataset.sym === sym));
}

/* ------------------------------------------------ signal journal */
async function loadSignals() {
  const tb = $("signals-table").querySelector("tbody");
  try {
    const r = await fetch("/api/signals?limit=100");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    latestStates = data.latest || {};
    renderSignals(data);
    if (lastGainers.length) renderGainers();
  } catch (e) {
    tb.innerHTML = '<tr><td colspan="9" class="empty">failed to load: ' + esc(e.message) + "</td></tr>";
  }
}

function fmtTime(ms) {
  return ms == null ? "\u2014" : new Date(ms).toLocaleTimeString();
}

function renderSignals(data) {
  const tb = $("signals-table").querySelector("tbody");
  const rows = data.rows || [];
  $("signals-upd").textContent = "updated " + new Date().toLocaleTimeString();
  const triggered = rows.filter((r) => r.event === "SIGNAL");
  /* chime once per new journal entry (first render seeds silently) */
  const fresh = triggered.filter((r) => !signalKeys.has(r.ts_ms + "|" + r.symbol));
  for (const r of fresh) signalKeys.add(r.ts_ms + "|" + r.symbol);
  if (signalsReady && fresh.length) {
    Sound.play("signal", fresh[0].symbol + "|" + fresh[0].ts_ms, {
      sym: fresh[0].symbol,
      msg: "Journal signal" + (fresh[0].state ? " \u00b7 " + fresh[0].state : "") + " \u00b7 " + new Date(fresh[0].ts_ms).toLocaleTimeString(),
    });
  }
  signalsReady = true;
  $("signals-triggered").innerHTML = triggered.length
    ? triggered.map((r) => '<span class="ev hot">' + esc(r.symbol) + " \u00b7 " + fmtTime(r.ts_ms) + "</span>").join("")
    : '<span class="ev">no short-entry signals yet</span>';
  $("signals-sum").textContent = data.total + " journal rows \u00b7 " + triggered.length + " signals";
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="9" class="empty">journal empty \u2014 start scripts/sentinel.py to populate</td></tr>';
    return;
  }
  tb.innerHTML = rows.map((r) => {
    const sig = r.event === "SIGNAL";
    const st = sig ? "TRIGGERED" : (r.prev_state ? r.prev_state + " \u2192 " + r.state : r.state);
    return "<tr" + (sig ? ' class="signal"' : "") + ">" +
      "<td>" + fmtTime(r.ts_ms) + "</td>" +
      '<td class="s">' + esc(r.symbol) + "</td>" +
      '<td><span class="st ' + esc(r.state) + (sig ? " sig" : "") + '">' + esc(st) + "</span></td>" +
      "<td>" + fmtPrice(r.entry) + "</td>" +
      "<td>" + fmtPrice(r.stop) + "</td>" +
      "<td>" + fmtPrice(r.tp1) + "</td>" +
      "<td>" + fmtPrice(r.tp2) + "</td>" +
      "<td>" + (r.score == null ? "\u2014" : r.score.toFixed(2)) + "</td>" +
      '<td class="why">' + esc(r.reasons || "\u2014") + "</td></tr>";
  }).join("");
}

/* ------------------------------------------------ live chart plumbing */
function overlayQuery() {
  const m = state.market || "auto";
  return "market=" + encodeURIComponent(m) + "&tf=" + encodeURIComponent(state.tf);
}

function showHeaderPrice(cs) {
  if (!cs.length) return;
  const close = cs[cs.length - 1].c;
  const el = $("price");
  if (state.lastPrice !== null && close !== state.lastPrice) {
    el.classList.remove("fu", "fd");
    void el.offsetWidth;
    el.classList.add(close >= state.lastPrice ? "fu" : "fd");
  }
  state.lastPrice = close;
  el.textContent = fmtPrice(close);
  $("price-note").textContent = "close \u00b7 " + state.tf;
}

function stopLive() {
  if (state.es) { state.es.close(); state.es = null; }
  if (state.tailTimer) { clearInterval(state.tailTimer); state.tailTimer = null; }
  if (state.ovTimer) { clearInterval(state.ovTimer); state.ovTimer = null; }
  if (state.emaTimer) { clearInterval(state.emaTimer); state.emaTimer = null; }
}

async function loadChart(sym, gen, keepView) {
  setStatus("loading candles\u2026");
  const [kr, or_] = await Promise.all([
    fetch("/api/klines/" + sym + "?" + overlayQuery() + "&limit=300"),
    fetch("/api/overlays/" + sym + "?" + overlayQuery() + "&limit=300").catch(() => null),
  ]);
  if (gen !== state.gen) return;
  if (!kr.ok) {
    let detail = "HTTP " + kr.status;
    try { detail = (await kr.json()).detail || detail; } catch { /* ignore */ }
    setStatus("chart failed: " + detail);
    return;
  }
  const kd = await kr.json();
  state.market = kd.market;
  state.candles = kd.candles;
  $("mkt").textContent = kd.market + " \u00b7 " + kd.tf;
  let ov = null;
  if (or_ && or_.ok) {
    ov = await or_.json();
    setStatus(null);
  } else {
    let detail = "insufficient history for layers";
    if (or_) { try { detail = (await or_.json()).detail || detail; } catch { /* ignore */ } }
    setStatus("algorithm layers unavailable: " + detail);
  }
  state.ov = ov;
  ChartX.setData(state.candles, ov, state.tf, { keepView: !!keepView });
  showHeaderPrice(state.candles);
  renderBars(state.candles);
  renderLayers(ov);
  loadEmaCascade(sym, gen);
  const foot = state.candles.length + " \u00d7 " + state.tf + " candles \u00b7 " + kd.market + " \u00b7 updated " + new Date().toLocaleTimeString();
  $("chart-foot").textContent = foot;
  startLive(sym, gen);
}

function startLive(sym, gen) {
  state.tailTimer = setInterval(async () => {
    if (gen !== state.gen || state.tailBusy || document.hidden) return;
    state.tailBusy = true;
    try {
      const r = await fetch("/api/klines/" + sym + "?" + overlayQuery() + "&limit=3");
      if (gen !== state.gen || !r.ok) return;
      const d = await r.json();
      const fresh = d.candles || [];
      if (!fresh.length) return;
      const cs = ChartX.getCandles();
      const lastT = cs.length ? cs[cs.length - 1].t : 0;
      const step = d.step_ms;
      if (lastT && fresh[0].t - lastT > step) {
        loadChart(sym, gen, true);
        return;
      }
      ChartX.updateTail(fresh);
      state.candles = ChartX.getCandles();
      showHeaderPrice(state.candles);
      renderBars(state.candles);
      $("chart-foot").textContent = state.candles.length + " \u00d7 " + state.tf + " candles \u00b7 " +
        d.market + " \u00b7 updated " + new Date().toLocaleTimeString();
    } catch { /* transient */ } finally {
      state.tailBusy = false;
    }
  }, TAIL_MS);

  state.ovTimer = setInterval(async () => {
    if (gen !== state.gen || document.hidden) return;
    try {
      const r = await fetch("/api/overlays/" + sym + "?" + overlayQuery() + "&limit=300");
      if (gen !== state.gen || !r.ok) return;
      const ov = await r.json();
      state.ov = ov;
      ChartX.setOverlays(ov);
      renderLayers(ov);
    } catch { /* transient */ }
  }, OVERLAY_MS);

  state.emaTimer = setInterval(() => {
    if (gen !== state.gen || document.hidden) return;
    loadEmaCascade(sym, gen);
  }, 30000);

  const es = new EventSource("/api/stream/" + sym + "?market=" + encodeURIComponent(state.market || "auto") +
    "&tf=" + encodeURIComponent(state.tf) + "&interval=" + STREAM_INTERVAL_S);
  state.es = es;
  es.onmessage = (ev) => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch { return; }
    if (payload.ok) {
      renderObservation(payload.snapshot);
      $("stream-status").textContent = "live \u00b7 updated " + new Date().toLocaleTimeString();
    } else {
      $("stream-status").textContent = "upstream: " + payload.error;
    }
  };
  es.onerror = () => { $("stream-status").textContent = "connection lost \u2014 reconnecting\u2026"; };
}

/* ------------------------------------------------ selection */
function selectSymbol(sym) {
  sym = (sym || "").toUpperCase();
  if (!/^[A-Z0-9]{4,20}$/.test(sym)) {
    $("search").classList.add("invalid");
    return;
  }
  $("search").classList.remove("invalid");
  localStorage.setItem("sx_sym", sym);
  state.gen++;
  const gen = state.gen;
  stopLive();
  state.sym = sym;
  state.market = null;
  state.lastPrice = null;
  $("sym").textContent = sym;
  paintCoinIcon(sym);
  loadLogos([sym]);
  $("price").textContent = "\u2014";
  $("chg").textContent = "\u2014";
  $("chg").className = "chg";
  $("mkt").textContent = "\u2014";
  $("meta").innerHTML = "";
  $("events").innerHTML = '<span class="ev">waiting for snapshot\u2026</span>';
  $("stream-status").textContent = "connecting\u2026";
  emaSeen = {};
  emaReady = false;
  prevEvents = null;
  $("ema-alert").className = "ema-alert";
  $("ema-alert").textContent = "";
  $("ema-rows").innerHTML = '<div class="muted small">loading\u2026</div>';
  $("ema-pattern").textContent = "";
  $("ema-pattern").className = "ema-pattern";
  $("ema-errors").textContent = "";
  highlight(sym);
  loadChart(sym, gen).catch((e) => { if (gen === state.gen) setStatus("chart failed: " + e.message); });
}

/* ------------------------------------------------ controls */
function bindSeg(id, attr, cb) {
  const host = $(id.replace(/^#/, ""));
  host.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    host.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    cb(b.dataset[attr]);
  });
}

function currentToggles() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("sx_layers") || "{}"); } catch { saved = {}; }
  return Object.assign({}, OVERLAY_DEFAULTS, saved);
}
function applyToggles(next, persist) {
  ChartX.setToggles(next);
  document.querySelectorAll("#overlay-bar .chip[data-ov]").forEach((c) => c.classList.toggle("on", !!next[c.dataset.ov]));
  if (persist) localStorage.setItem("sx_layers", JSON.stringify(next));
}

/* ------------------------------------------------ big chart mode (b)
   hides both side rails so the candles get the full row width; the chart's
   ResizeObserver picks the new wrap size up automatically. */
const BIG_KEY = "sx_big";
function applyBig(on, persist) {
  document.body.classList.toggle("big-chart", !!on);
  const b = $("bigchart");
  if (b) {
    b.classList.toggle("on", !!on);
    b.title = on ? "Big chart mode: ON \u2014 click to show the side rails (b)" : "Big chart mode: hide side rails for full-width candles (b)";
  }
  if (persist) { try { localStorage.setItem(BIG_KEY, on ? "1" : "0"); } catch { /* private mode */ } }
}
function toggleBig() { applyBig(!document.body.classList.contains("big-chart"), true); }

/* ------------------------------------------------ keyboard shortcuts
   n = bell panel \u00b7 b = big chart \u00b7 1–5 = timeframe \u00b7 / = symbol search \u00b7
   Esc = close panel / blur input / reset the chart view */
function bindShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") {
      if (typing) { t.blur(); return; }
      const p = $("notify-panel");
      if (p && !p.hidden) return; /* Notify's own handler closes the panel */
      if (document.activeElement !== $("chart")) ChartX.resetView();
      return;
    }
    if (typing) return;
    if (e.key === "n" || e.key === "N") { Notify.open(); e.preventDefault(); }
    else if (e.key === "b" || e.key === "B") toggleBig();
    else if (e.key === "/") { const s = $("search"); if (s) { s.focus(); e.preventDefault(); } }
    else if (e.key >= "1" && e.key <= "5") {
      const b = document.querySelectorAll("#tf button")[Number(e.key) - 1];
      if (b && !b.classList.contains("on")) b.click();
    }
  });
}

/* ------------------------------------------------ boot */
function init() {
  buildRings();
  buildBars();
  /* CSP-safe coin-logo fallback: capture-phase img error listener replaces
     the old inline onerror attribute (script-src 'self' blocks markup handlers). */
  document.addEventListener("error", (e) => {
    const t = e.target;
    if (t && t.tagName === "IMG" && t.classList.contains("logo-img")) t.remove();
  }, true);
  Sound.setOnHit((kind, info) => {
    Notify.push(kind, info);
    if (typeof BulletLogo !== "undefined") BulletLogo.pulse();
  });
  Sound.boot();
  Notify.boot();
  bindShortcuts();
  const big = $("bigchart");
  if (big) big.addEventListener("click", toggleBig);
  applyBig(localStorage.getItem(BIG_KEY) === "1", false);
  ChartX.init($("chart"), $("tip"), $("chart-status"));
  const toggles = currentToggles();
  applyToggles(toggles, false);
  document.querySelectorAll("#overlay-bar .chip[data-ov]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const next = Object.assign(currentToggles(), { [chip.dataset.ov]: !chip.classList.contains("on") });
      applyToggles(next, true);
    });
  });

  document.querySelectorAll("#mode button").forEach((b) => b.classList.toggle("on", b.dataset.m === state.mode));
  ChartX.setMode(state.mode);
  bindSeg("#mode", "m", (m) => { state.mode = m; localStorage.setItem("sx_mode", m); ChartX.setMode(m); });

  document.querySelectorAll("#tf button").forEach((b) => b.classList.toggle("on", b.dataset.t === state.tf));
  bindSeg("#tf", "t", (t) => {
    state.tf = t;
    localStorage.setItem("sx_tf", t);
    if (state.sym) selectSymbol(state.sym);
  });

  $("search-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    selectSymbol($("search").value.trim());
  });

  async function health() {
    try {
      const r = await fetch("/api/health");
      $("health").className = "dot " + (r.ok ? "ok" : "err");
    } catch {
      $("health").className = "dot err";
    }
  }
  health();
  setInterval(health, 30000);
  loadGainers();
  setInterval(loadGainers, 60000);
  setTimeout(watchTop1, 5000);
  setInterval(watchTop1, 30000);
  loadSignals();
  setInterval(loadSignals, 15000);

  const last = localStorage.getItem("sx_sym");
  if (last) {
    selectSymbol(last);
  } else {
    fetch("/api/gainers?top=1").then((r) => r.json()).then((d) => {
      if (d.gainers && d.gainers.length) selectSymbol(d.gainers[0].symbol);
    }).catch(() => {});
  }
}

init();
