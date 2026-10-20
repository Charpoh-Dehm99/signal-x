/* SIGNAL-X canvas chart engine: candles/line + volume + RSI panes and the
   signalx algorithm overlays (pivots, structure, BOS/CHoCH breaks, zones,
   AVWAP, box, divergence). No dependencies, no build step.
   All overlay layers are observations (G0 killed 2026-10-17). */

"use strict";

const ChartX = (() => {
  const UP = "#089981", DOWN = "#f23645", UP_L = "#14c9a8", DOWN_L = "#ff5a68";
  const BLUE = "#2e8bff", CYAN = "#35c2ff", ORANGE = "#ffa42e", GOLD = "#ffd23f";
  const PURPLE = "#b48bff", MUTED = "#8b8b92", GRID = "#1b1b1e", AXIS = "#6f6f76";
  const PAD = { l: 8, r: 62, t: 10, b: 22 };
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  let cv, ctx, wrap, tipEl, statusEl, liveEl;
  let W = 0, H = 0;
  let candles = [];
  let ov = null;
  let stepMs = 300000;
  let tf = "5m";
  let mode = "candle";
  let show = { pivots: true, structure: true, bos: true, zones: true, avwap: true, box: true, divs: true, rsi: true };
  let timeIdx = new Map();      // open_time -> candle index
  let geom = null;              // layout of the last draw
  let hoverI = null, hoverY = null;
  let raf = 0;
  let animRaf = 0;
  const MIN_BARS = 24;          // hard zoom-in limit
  const MIN_BAR_PX = 4;         // candle-mode fit: never cram bars into <4px slots
  let view = { start: 0, count: 0 };  // visible window over candles (count 0 = all)
  let drag = null;
  let vScrollPx = 0;            // manual vertical (price) scroll offset in px
  let touches = new Map();      // active touch pointers (pointerId -> {x,y})
  let pinch = null;             // two-finger gesture baseline {cx, cy, dist}
  let wheelBurst = null;        // { t, mode } classifies wheel vs trackpad bursts
  let lastTap = null;           // { t, x, y } last single-finger tap (double-tap zoom)
  let tapZoomAt = 0;            // suppress the synthesized dblclick after a touch double-tap

  /* ------------------------------------------------ view window */
  function viewCount() {
    const n = candles.length || 1;
    return clamp(view.count > 0 ? view.count : n, Math.min(MIN_BARS, n), n);
  }
  /* max bars that still render readable candles: phones get ~60-70 bars
     instead of smearing 300 into a solid wall of stacked 1px bars */
  function maxViewCount() {
    const n = candles.length || 1;
    const minC = Math.min(MIN_BARS, n);
    if (mode !== "candle" || !W) return n;
    return clamp(Math.floor(plotWidth() / MIN_BAR_PX), minC, n);
  }
  function fitView() {
    const n = candles.length;
    view.count = n ? maxViewCount() : 0;
    view.start = Math.max(0, n - view.count);
    vScrollPx = 0;
  }
  function clampView() {
    const n = candles.length || 1;
    view.count = clamp(view.count || n, Math.min(MIN_BARS, n), maxViewCount());
    view.start = clamp(view.start, 0, Math.max(0, n - view.count));
  }
  function resetView() { fitView(); pinch = null; schedule(); }
  function atRightEdge() { return view.start + viewCount() >= candles.length - 0.01; }
  function getView() { return { start: view.start, count: viewCount(), vScroll: vScrollPx }; }

  /* ------------------------------------------------ helpers */
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const fmtP = (v) => {
    if (v == null || !isFinite(v)) return "\u2014";
    if (v >= 1000) return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (v >= 1) return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 4 });
    return "$" + v.toPrecision(4);
  };
  const fmtV = (v) => {
    if (v == null || !isFinite(v)) return "\u2014";
    if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(2);
  };
  const hhmm = (d) => String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  const tLabel = (ms) => {
    const d = new Date(ms);
    return stepMs < 3600e3 ? hhmm(d) : d.getDate() + " " + MON[d.getMonth()];
  };
  const tFull = (ms) => {
    const d = new Date(ms);
    return MON[d.getMonth()] + " " + d.getDate() + " " + d.getFullYear() + " " + hhmm(d);
  };
  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const m = raw / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }
  function setStatus(msg) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("show", !!msg);
  }

  /* ------------------------------------------------ pivot glow */
  const REDUCE_MOTION = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const pulsePhase = () => (REDUCE_MOTION ? 0.55 : (Math.sin(performance.now() * 0.0042) + 1) / 2);
  let dotSprite = null;
  function glowDot(x, y, ph) {
    const S = 9;
    if (!dotSprite) {
      dotSprite = document.createElement("canvas");
      dotSprite.width = dotSprite.height = S * 6;
      const c = dotSprite.getContext("2d");
      c.setTransform(3, 0, 0, 3, 0, 0);
      const g = c.createRadialGradient(S, S, 0, S, S, S);
      g.addColorStop(0, "rgba(255,235,160,1)");
      g.addColorStop(0.34, "rgba(255,210,63,0.92)");
      g.addColorStop(0.5, "rgba(255,210,63,0.22)");
      g.addColorStop(0.75, "rgba(255,196,40,0.06)");
      g.addColorStop(1, "rgba(255,196,40,0)");
      c.fillStyle = g;
      c.beginPath(); c.arc(S, S, S, 0, Math.PI * 2); c.fill();
    }
    ctx.globalAlpha = 0.72 + 0.28 * ph;
    ctx.drawImage(dotSprite, x - S, y - S, S * 2, S * 2);
    ctx.globalAlpha = 1;
  }

  /* ------------------------------------------------ data */
  function indexByTime() {
    timeIdx = new Map();
    candles.forEach((k, i) => timeIdx.set(k.t, i));
  }

  function setData(cs, overlays, interval, opts) {
    const keep = !!(opts && opts.keepView) && interval === tf && candles.length > 0;
    const anchorT = keep ? candles[clamp(Math.round(view.start), 0, candles.length - 1)].t : null;
    candles = Array.isArray(cs) ? cs : [];
    ov = overlays || null;
    tf = interval || tf;
    if (candles.length > 1) stepMs = candles[1].t - candles[0].t;
    hoverI = null;
    indexByTime();
    if (anchorT != null && timeIdx.has(anchorT)) {
      view.start = timeIdx.get(anchorT);   // same window, shifted data
    } else {
      fitView();                           // fresh symbol/timeframe: right edge, readable
    }
    clampView();
    draw();
    return candles.length;
  }

  function setOverlays(next) { ov = next || null; draw(); }

  function updateTail(fresh) {
    if (!fresh || !fresh.length) return;
    const wasRight = atRightEdge();
    let added = 0;
    for (const k of fresh) {
      const i = timeIdx.get(k.t);
      if (i == null) {
        candles.push(k);
        timeIdx.set(k.t, candles.length - 1);
        added++;
      } else {
        candles[i] = k;
      }
    }
    let dropped = 0;
    while (candles.length > 720) {
      const gone = candles.shift();
      timeIdx.delete(gone.t);
      timeIdx.forEach((v, key) => timeIdx.set(key, v - 1));
      dropped++;
    }
    view.start -= dropped;
    if (wasRight) view.start += added;   // stay glued to the live edge
    clampView();
    draw();
  }

  function setMode(m) {
    const wasRight = atRightEdge();
    mode = m;
    if (wasRight) fitView();   // stay glued to the live edge at the new density
    draw();
  }
  function setToggles(next) { show = Object.assign({}, show, next); draw(); }
  function getCandles() { return candles; }
  function getOverlays() { return ov; }

  /* ------------------------------------------------ layout */
  function layout() {
    const n = candles.length || 1;
    const vc = viewCount();
    const i0 = clamp(Math.floor(view.start), 0, n - 1);
    const i1 = clamp(Math.ceil(view.start + vc) - 1, 0, n - 1);
    const plotW = W - PAD.l - PAD.r;
    const plotH = H - PAD.t - PAD.b;
    const rsiOn = !!(show.rsi && ov && ov.rsi && ov.rsi.length);
    const rsiH = rsiOn ? clamp(plotH * 0.17, 72, 122) : 0;
    const volH = clamp(plotH * 0.11, 34, 76);
    const gap = 8;
    const mainH = plotH - volH - rsiH - gap * (rsiOn ? 2 : 1);
    const main = { y: PAD.t, h: Math.max(60, mainH) };
    const vol = { y: main.y + main.h + gap, h: volH };
    const rsi = rsiOn ? { y: vol.y + vol.h + gap, h: rsiH } : null;
    return { n, vc, i0, i1, plotW, plotH, main, vol, rsi, gap, rowH: plotW / vc };
  }

  /* ------------------------------------------------ draw */
  function draw() {
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = "500 10px Manrope, system-ui, sans-serif";
    ctx.textBaseline = "middle";

    const n = candles.length;
    if (!n) { geom = null; return; }
    clampView();
    const g = geom = layout();
    const { plotW, i0, i1 } = g;
    const x = (i) => PAD.l + (i - view.start + 0.5) * g.rowH;

    /* price / volume scales over the visible window only */
    let lo = Infinity, hi = -Infinity, vmax = 0;
    for (let i = i0; i <= i1; i++) {
      const k = candles[i];
      if (k.l < lo) lo = k.l;
      if (k.h > hi) hi = k.h;
      if (k.v > vmax) vmax = k.v;
    }
    if (!isFinite(lo) || hi <= lo) { lo = lo || 1; hi = lo * 1.001 + 0.001; }
    const span = hi - lo, padv = span * 0.06;
    lo -= padv; hi += padv;
    /* two-finger vertical scroll: shift the auto-scaled price range */
    if (vScrollPx) {
      const vs = vScrollPx * (hi - lo) / g.main.h;
      lo -= vs; hi -= vs;
    }
    const yP = (v) => g.main.y + (1 - (v - lo) / (hi - lo)) * g.main.h;
    const yV = (v) => g.vol.y + g.vol.h - (vmax > 0 ? v / vmax : 0) * g.vol.h;
    const yR = (v) => (g.rsi ? g.rsi.y + (1 - v / 100) * g.rsi.h : 0);

    /* overlay index (closed bars) -> chart index by open_time */
    const oi = (i) => (ov ? timeIdx.get(ov.t0 + i * stepMs) : undefined);

    /* live-edge button visibility */
    if (liveEl) liveEl.hidden = atRightEdge();

    /* ---------------- grid + axes */
    ctx.strokeStyle = GRID;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 6]);
    const pstep = niceStep((hi - lo) / 5);
    for (let p = Math.ceil(lo / pstep) * pstep; p <= hi; p += pstep) {
      const y = yP(p);
      ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(PAD.l + plotW, y); ctx.stroke();
    }
    ctx.setLineDash([]);
    const lastC = candles[n - 1].c;
    ctx.fillStyle = AXIS;
    ctx.textAlign = "left";
    for (let p = Math.ceil(lo / pstep) * pstep; p <= hi; p += pstep) {
      const y = yP(p);
      if (Math.abs(y - yP(lastC)) < 12) continue;
      ctx.fillText(fmtP(p), PAD.l + plotW + 8, y);
    }
    ctx.textAlign = "center";
    for (let k = 0; k <= 5; k++) {
      const i = clamp(Math.round(view.start + k * (g.vc - 1) / 5), i0, i1);
      ctx.fillText(tLabel(candles[i].t), clamp(x(i), 22, W - PAD.r - 6), H - PAD.b + 12);
    }
    if (g.rsi) {
      ctx.strokeStyle = GRID; ctx.setLineDash([3, 5]);
      ctx.beginPath(); ctx.moveTo(PAD.l, g.rsi.y - 4); ctx.lineTo(PAD.l + plotW, g.rsi.y - 4); ctx.stroke();
      ctx.setLineDash([]);
    }
    /* volume baseline */
    ctx.strokeStyle = GRID;
    ctx.beginPath(); ctx.moveTo(PAD.l, g.vol.y + g.vol.h); ctx.lineTo(PAD.l + plotW, g.vol.y + g.vol.h); ctx.stroke();

    ctx.save();
    ctx.beginPath(); ctx.rect(PAD.l, g.main.y, plotW, g.main.h); ctx.clip();

    /* ---------------- resistance zones (behind price) */
    if (ov && show.zones && ov.zones) {
      for (const z of ov.zones) {
        const y1 = yP(z.hi), y2 = yP(z.lo);
        if (y2 < g.main.y - 20 || y1 > g.main.y + g.main.h + 20) continue;
        const grad = ctx.createLinearGradient(0, y1, 0, y2);
        const a = 0.05 + 0.11 * z.w;
        grad.addColorStop(0, "rgba(46,139,255," + a.toFixed(3) + ")");
        grad.addColorStop(1, "rgba(46,139,255," + (a * 0.4).toFixed(3) + ")");
        ctx.fillStyle = grad;
        ctx.fillRect(PAD.l, y1, plotW, Math.max(1, y2 - y1));
        ctx.strokeStyle = "rgba(46,139,255," + (0.25 + 0.25 * z.w).toFixed(3) + ")";
        ctx.beginPath(); ctx.moveTo(PAD.l, y1); ctx.lineTo(PAD.l + plotW, y1); ctx.stroke();
        if (y2 - y1 > 15) {
          ctx.fillStyle = "rgba(46,139,255,.75)";
          ctx.textAlign = "left";
          ctx.fillText("zone \u00d7" + z.touches, PAD.l + 6, y1 + 9);
        }
      }
    }

    /* ---------------- plateau box */
    if (ov && show.box && ov.box) {
      const b = ov.box;
      const i0 = oi(b.start_i), i1 = oi(ov.n - 1);
      if (i0 != null && i1 != null) {
        const x1 = x(Math.max(0, i0)) - g.rowH / 2, x2 = x(i1) + g.rowH / 2;
        const y1 = yP(b.hi), y2 = yP(b.lo);
        ctx.fillStyle = b.qualifies ? "rgba(255,210,63,.06)" : "rgba(255,164,46,.05)";
        ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = b.qualifies ? "rgba(255,210,63,.6)" : "rgba(255,164,46,.35)";
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
        ctx.setLineDash([]);
        if (y2 - y1 > 16) {
          ctx.fillStyle = b.qualifies ? GOLD : ORANGE;
          ctx.textAlign = "left";
          ctx.fillText("plateau " + b.height_atr.toFixed(1) + "\u00d7ATR \u00b7 " + Math.round(b.containment * 100) + "% in", x1 + 6, y2 - 9);
        }
      }
    }

    /* ---------------- AVWAP band + line */
    if (ov && show.avwap && ov.avwap) {
      const A = ov.avwap;
      const pts = (arr) => {
        const out = [];
        for (let i = 0; i < arr.length; i++) {
          const ci = oi(i);
          if (ci == null || arr[i] == null) continue;
          out.push([x(ci), yP(arr[i])]);
        }
        return out;
      };
      const up = pts(A.upper), dn = pts(A.lower), ln = pts(A.line);
      if (up.length > 1 && dn.length > 1) {
        ctx.beginPath();
        up.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
        for (let i = dn.length - 1; i >= 0; i--) ctx.lineTo(dn[i][0], dn[i][1]);
        ctx.closePath();
        ctx.fillStyle = "rgba(180,139,255,.07)";
        ctx.fill();
      }
      if (ln.length > 1) {
        ctx.beginPath();
        ln.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
        ctx.strokeStyle = PURPLE;
        ctx.lineWidth = 1.6;
        ctx.shadowColor = "rgba(180,139,255,.55)";
        ctx.shadowBlur = 7;
        ctx.stroke();
        ctx.shadowBlur = 0;
      }
      const ai = oi(A.anchor_i);
      if (ai != null && A.line[A.anchor_i] != null) {
        ctx.fillStyle = PURPLE;
        ctx.beginPath(); ctx.arc(x(ai), yP(A.line[A.anchor_i]), 3, 0, Math.PI * 2); ctx.fill();
        ctx.textAlign = "left";
        ctx.fillText("AVWAP \u00b7 display", x(ai) + 6, yP(A.line[A.anchor_i]) - 10);
      }
    }

    /* ---------------- candles / line */
    const lastDirUp = n > 1 ? candles[n - 1].c >= candles[n - 1].o : true;
    const col = (k) => (k.c >= k.o ? UP : DOWN);
    if (mode === "candle") {
      /* pixel-snapped candles: integer slot widths + crisp 1px wicks so bars
         stay individually readable at TradingView-like densities */
      const slot = Math.max(1, Math.round(g.rowH));
      const bw = Math.max(1, slot - (slot >= 5 ? 2 : 1));
      const wick = g.rowH > 18 ? 2 : 1;
      for (let i = Math.max(0, i0 - 1); i <= Math.min(n - 1, i1 + 1); i++) {
        const k = candles[i];
        const cx = Math.floor(x(i)) + 0.5;
        const c = col(k);
        const live = i === n - 1;
        ctx.globalAlpha = live ? 0.82 : 1;
        ctx.strokeStyle = c; ctx.lineWidth = wick;
        ctx.beginPath();
        ctx.moveTo(cx, yP(k.h)); ctx.lineTo(cx, yP(k.l)); ctx.stroke();
        const yo = yP(k.o), yc = yP(k.c);
        ctx.fillStyle = c;
        ctx.fillRect(Math.round(cx - bw / 2), Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
      }
      ctx.globalAlpha = 1;
      if (n - 1 >= i0 && n - 1 <= i1) {
        const xl = x(n - 1), yl = yP(candles[n - 1].c);
        ctx.shadowColor = lastDirUp ? "rgba(8,153,129,.9)" : "rgba(242,54,69,.9)";
        ctx.shadowBlur = 8;
        ctx.fillStyle = "#fff";
        ctx.beginPath(); ctx.arc(xl, yl, 2.6, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
      }
    } else {
      ctx.beginPath();
      for (let i = i0; i <= i1; i++) {
        const px = x(i), py = yP(candles[i].c);
        if (i === i0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.lineTo(x(i1), g.main.y + g.main.h);
      ctx.lineTo(x(i0), g.main.y + g.main.h);
      ctx.closePath();
      const trendUp = candles[i1].c >= candles[i0].c;
      const lc0 = trendUp ? UP : DOWN;
      const grad = ctx.createLinearGradient(0, g.main.y, 0, g.main.y + g.main.h);
      grad.addColorStop(0, trendUp ? "rgba(8,153,129,.16)" : "rgba(242,54,69,.15)");
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      for (let i = i0; i <= i1; i++) {
        const px = x(i), py = yP(candles[i].c);
        if (i === i0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.strokeStyle = lc0;
      ctx.lineWidth = 1.8;
      ctx.shadowColor = trendUp ? "rgba(8,153,129,.7)" : "rgba(242,54,69,.7)";
      ctx.shadowBlur = 6;
      ctx.stroke();
      ctx.shadowBlur = 0;
      if (n - 1 >= i0 && n - 1 <= i1) {
        ctx.fillStyle = "#fff";
        ctx.beginPath(); ctx.arc(x(n - 1), yP(candles[n - 1].c), 2.8, 0, Math.PI * 2); ctx.fill();
      }
    }

    /* ---------------- EMA 8 / 13 / 21 triple stack + cross markers */
    if (ov && show.emas && ov.emas) {
      const series = [
        { arr: ov.emas["8"], c: CYAN, w: 1.4 },
        { arr: ov.emas["13"], c: GOLD, w: 1.4 },
        { arr: ov.emas["21"], c: ORANGE, w: 1.6 },
      ];
      for (const s of series) {
        if (!s.arr) continue;
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < s.arr.length; i++) {
          const ci = oi(i);
          const v = s.arr[i];
          if (ci == null || v == null) continue;
          if (started) ctx.lineTo(x(ci), yP(v));
          else { ctx.moveTo(x(ci), yP(v)); started = true; }
        }
        if (!started) continue;
        ctx.strokeStyle = s.c;
        ctx.lineWidth = s.w;
        ctx.globalAlpha = 0.95;
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
      /* crosses: triangle under the break-up bar, over the break-down bar */
      for (const cx of ov.ema_crosses || []) {
        const ci = oi(cx.i);
        const v = ov.emas["8"][cx.i];
        if (ci == null || v == null) continue;
        const up = cx.dir === "up";
        const y = yP(v);
        ctx.fillStyle = up ? UP_L : DOWN_L;
        ctx.beginPath();
        if (up) {
          ctx.moveTo(x(ci), y + 6); ctx.lineTo(x(ci) - 4, y + 12); ctx.lineTo(x(ci) + 4, y + 12);
        } else {
          ctx.moveTo(x(ci), y - 6); ctx.lineTo(x(ci) - 4, y - 12); ctx.lineTo(x(ci) + 4, y - 12);
        }
        ctx.closePath(); ctx.fill();
      }
    }

    /* ---------------- swing structure (zigzag) */
    const ph = pulsePhase();
    /* a dot that shows in both layers (structure ∩ pivots) turns gold; the rest keep defaults */
    const pivotSet = show.pivots && ov && ov.pivots ? new Set(ov.pivots.map((p) => p.i)) : null;
    if (ov && show.structure && ov.structure && ov.structure.length > 1) {
      const pts = [];
      for (const s of ov.structure) {
        const ci = oi(s.i);
        if (ci == null) continue;
        pts.push({ x: x(ci), y: yP(s.p), s });
      }
      ctx.setLineDash([3, 5]);
      ctx.strokeStyle = "rgba(255,255,255,.25)";
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.stroke();
      ctx.setLineDash([]);
      let lastLabelX = -Infinity;
      for (const p of pts) {
        const up = p.s.k === "H";
        if (pivotSet && pivotSet.has(p.s.i)) {
          glowDot(p.x, p.y, ph);
        } else {
          ctx.fillStyle = up ? DOWN_L : UP_L;
          ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, Math.PI * 2); ctx.fill();
        }
        if (p.x - lastLabelX < 42) continue;   // adaptive density: keep it readable
        ctx.fillStyle = p.s.label === "HH" || p.s.label === "HL"
          ? "rgba(20,201,168,.8)"
          : p.s.label === "LH" || p.s.label === "LL"
            ? "rgba(255,90,104,.8)"
            : "rgba(255,255,255,.62)";
        ctx.textAlign = "center";
        ctx.fillText(p.s.label, p.x, p.y + (up ? -13 : 15));
        lastLabelX = p.x;
      }
    }

    /* ---------------- swing pivots (fractal) */
    if (ov && show.pivots && ov.pivots) {
      /* overlapping dots were already drawn gold by the structure layer */
      const structSet = show.structure && ov.structure ? new Set(ov.structure.map((s) => s.i)) : null;
      for (const p of ov.pivots) {
        const ci = oi(p.i);
        if (ci == null) continue;
        if (structSet && structSet.has(p.i)) continue;
        ctx.fillStyle = p.k === "H" ? "rgba(46,139,255,.85)" : "rgba(53,194,255,.7)";
        ctx.beginPath(); ctx.arc(x(ci), yP(p.p), 2.1, 0, Math.PI * 2); ctx.fill();
      }
    }

    /* ---------------- structure breaks: BOS (continuation) / CHoCH (reversal)
       teal = broke a swing high, red = broke a swing low */
    if (ov && show.bos && ov.breaks) {
      const placed = []; /* label rects, to stack overlaps instead of printing on top */
      for (const ev of ov.breaks) {
        const i0 = oi(ev.swing_i), i1 = oi(ev.break_i);
        if (i0 == null || i1 == null) continue;
        const up = ev.dir === "up";
        const chop = ev.type === "choch";
        const text = chop ? "CHoCH" : "BOS";
        const y = yP(ev.level);
        ctx.strokeStyle = up
          ? (chop ? "rgba(20,201,168,.95)" : "rgba(20,201,168,.7)")
          : (chop ? "rgba(255,90,104,.95)" : "rgba(242,54,69,.7)");
        ctx.setLineDash(chop ? [2, 3] : [4, 4]);
        ctx.lineWidth = chop ? 1.5 : 1.2;
        ctx.beginPath(); ctx.moveTo(x(i0), y); ctx.lineTo(x(i1), y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = up ? UP_L : DOWN_L;
        ctx.beginPath();
        if (up) {
          ctx.moveTo(x(i1), y); ctx.lineTo(x(i1) - 4, y + 6); ctx.lineTo(x(i1) + 4, y + 6);
        } else {
          ctx.moveTo(x(i1), y); ctx.lineTo(x(i1) - 4, y - 6); ctx.lineTo(x(i1) + 4, y - 6);
        }
        ctx.closePath(); ctx.fill();
        ctx.textAlign = "left";
        const lx = x(i1) + 6;
        const tw = ctx.measureText(text).width;
        let ly = up ? y + 18 : y - 12;
        for (let guard = 0; guard < 8; guard++) {
          const clash = placed.some((r) =>
            !(lx + tw < r.x0 || lx > r.x1 || ly + 3 < r.y0 || ly - 9 > r.y1));
          if (!clash) break;
          ly += up ? 13 : -13;
        }
        placed.push({ x0: lx, x1: lx + tw, y0: ly - 9, y1: ly + 3 });
        ctx.fillText(text, lx, ly);
      }
    }

    /* ---------------- divergence connectors (red = bearish → short bias, teal = bullish → long bias) */
    if (ov && show.divs && ov.divs) {
      ov.divs.forEach((d, di) => {
        const i1 = oi(d.p1i), i2 = oi(d.p2i);
        if (i1 == null || i2 == null) return;
        ctx.strokeStyle = "rgba(242,54,69,.9)";
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1.3;
        ctx.beginPath(); ctx.moveTo(x(i1), yP(d.p1p)); ctx.lineTo(x(i2), yP(d.p2p)); ctx.stroke();
        ctx.setLineDash([]);
        for (const [ii, pp] of [[i1, d.p1p], [i2, d.p2p]]) {
          ctx.fillStyle = DOWN_L;
          ctx.beginPath(); ctx.arc(x(ii), yP(pp), 2.6, 0, Math.PI * 2); ctx.fill();
        }
        ctx.fillStyle = "rgba(255,90,104,.9)";
        ctx.textAlign = "center";
        ctx.fillText("div \u2193 short", x(i2), yP(d.p2p) - 12 - (di % 2) * 13);
      });
    }
    if (ov && show.divs && ov.budivs) {
      ov.budivs.forEach((d, di) => {
        const i1 = oi(d.p1i), i2 = oi(d.p2i);
        if (i1 == null || i2 == null) return;
        ctx.strokeStyle = "rgba(20,201,168,.9)";
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1.3;
        ctx.beginPath(); ctx.moveTo(x(i1), yP(d.p1p)); ctx.lineTo(x(i2), yP(d.p2p)); ctx.stroke();
        ctx.setLineDash([]);
        for (const [ii, pp] of [[i1, d.p1p], [i2, d.p2p]]) {
          ctx.fillStyle = UP_L;
          ctx.beginPath(); ctx.arc(x(ii), yP(pp), 2.6, 0, Math.PI * 2); ctx.fill();
        }
        ctx.fillStyle = "rgba(20,201,168,.9)";
        ctx.textAlign = "center";
        ctx.fillText("div \u2191 long", x(i2), yP(d.p2p) + 20 + (di % 2) * 14);
      });
    }

    ctx.restore(); /* end main clip */

    /* ---------------- volume pane */
    ctx.save();
    ctx.beginPath(); ctx.rect(PAD.l, g.vol.y, plotW, g.vol.h); ctx.clip();
    const bw = Math.max(1.5, g.rowH * 0.62);
    for (let i = Math.max(0, i0 - 1); i <= Math.min(n - 1, i1 + 1); i++) {
      const k = candles[i];
      const y = yV(k.v);
      ctx.fillStyle = col(k) === UP ? "rgba(8,153,129,.5)" : "rgba(242,54,69,.5)";
      ctx.fillRect(x(i) - bw / 2, y, bw, g.vol.y + g.vol.h - y);
    }
    ctx.restore();

    /* ---------------- RSI pane */
    if (g.rsi) {
      ctx.save();
      ctx.beginPath(); ctx.rect(PAD.l, g.rsi.y, plotW, g.rsi.h); ctx.clip();
      ctx.fillStyle = "rgba(242,54,69,.08)";
      ctx.fillRect(PAD.l, yR(100), plotW, yR(70) - yR(100));
      ctx.fillStyle = "rgba(20,201,168,.08)";
      ctx.fillRect(PAD.l, yR(30), plotW, yR(0) - yR(30));
      ctx.setLineDash([3, 5]);
      ctx.strokeStyle = "rgba(255,255,255,.08)";
      ctx.beginPath(); ctx.moveTo(PAD.l, yR(50)); ctx.lineTo(PAD.l + plotW, yR(50)); ctx.stroke();
      ctx.strokeStyle = "rgba(242,54,69,.35)";
      ctx.beginPath(); ctx.moveTo(PAD.l, yR(70)); ctx.lineTo(PAD.l + plotW, yR(70)); ctx.stroke();
      ctx.strokeStyle = "rgba(20,201,168,.35)";
      ctx.beginPath(); ctx.moveTo(PAD.l, yR(30)); ctx.lineTo(PAD.l + plotW, yR(30)); ctx.stroke();
      ctx.setLineDash([]);
      if (ov && ov.rsi) {
        /* zone-colored line: red while overbought (>=70), green while oversold
           (<=30), yellow in between — segments are split exactly where the
           line crosses a threshold so the color flips on the crossing */
        const zoneCol = (v) => (v >= 70 ? DOWN_L : v <= 30 ? UP_L : GOLD);
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        let pathCol = null;
        const flush = () => { if (pathCol) { ctx.stroke(); pathCol = null; } };
        const seg = (a, b) => {
          const c = zoneCol((a.v + b.v) / 2);
          if (c !== pathCol) {
            flush();
            pathCol = c;
            ctx.strokeStyle = c;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
          }
          ctx.lineTo(b.x, b.y);
        };
        let prev = null;
        for (let i = i0; i <= i1; i++) {
          const k = candles[i];
          const oIdx = Math.round((k.t - ov.t0) / stepMs);
          const v = ov.rsi[oIdx];
          if (v == null) { prev = null; flush(); continue; }
          const p = { x: x(i), y: yR(v), v };
          if (prev) {
            const cuts = [];
            for (const lev of [70, 30]) {
              const d = p.v - prev.v;
              if (Math.abs(d) < 1e-9) continue;
              const t = (lev - prev.v) / d;
              if (t > 0 && t < 1) cuts.push(t);
            }
            cuts.sort((m, n) => m - n);
            let a = prev;
            for (const t of cuts) {
              const m = { x: prev.x + (p.x - prev.x) * t, y: prev.y + (p.y - prev.y) * t, v: prev.v + (p.v - prev.v) * t };
              seg(a, m);
              a = m;
            }
            seg(a, p);
          }
          prev = p;
        }
        flush();
        const last = ov.rsi[ov.n - 1];
        if (last != null) {
          const li = oi(ov.n - 1);
          if (li != null && li >= i0 && li <= i1) {
            ctx.fillStyle = "#fff";
            ctx.shadowColor = zoneCol(last); ctx.shadowBlur = 8;
            ctx.beginPath(); ctx.arc(x(li), yR(last), 2.6, 0, Math.PI * 2); ctx.fill();
            ctx.shadowBlur = 0;
          }
        }
        if (show.divs) {
          for (const [list, col] of [
            [ov.divs, "rgba(242,54,69,.75)"],
            [ov.budivs, "rgba(20,201,168,.75)"],
          ]) {
            for (const d of list || []) {
              const r1 = ov.rsi[d.r1i], r2 = ov.rsi[d.r2i];
              if (r1 == null || r2 == null) continue;
              const i1 = oi(d.r1i), i2 = oi(d.r2i);
              if (i1 == null || i2 == null) continue;
              ctx.strokeStyle = col;
              ctx.setLineDash([3, 3]);
              ctx.beginPath(); ctx.moveTo(x(i1), yR(r1)); ctx.lineTo(x(i2), yR(r2)); ctx.stroke();
              ctx.setLineDash([]);
            }
          }
        }
      }
      /* level numbers inside the pane, right-aligned by the last bar */
      ctx.font = "600 10px Manrope, system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.fillStyle = "rgba(255,90,104,.9)";
      ctx.fillText("70", PAD.l + plotW - 6, yR(70) + 3);
      ctx.fillStyle = "rgba(255,255,255,.35)";
      ctx.fillText("50", PAD.l + plotW - 6, yR(50) + 3);
      ctx.fillStyle = "rgba(20,201,168,.9)";
      ctx.fillText("30", PAD.l + plotW - 6, yR(30) + 3);
      ctx.fillStyle = "rgba(255,255,255,.4)";
      ctx.textAlign = "left";
      ctx.font = "500 10px Manrope, system-ui, sans-serif";
      ctx.fillText("RSI 14", PAD.l + 6, g.rsi.y + 9);
      ctx.restore();

      /* zone-colored live RSI value tag (outside the pane clip) */
      if (ov && ov.rsi) {
        const last = ov.rsi[ov.n - 1];
        if (last != null) {
          const ly = clamp(yR(last), g.rsi.y + 9, g.rsi.y + g.rsi.h - 9);
          tag(PAD.l + plotW + 6, ly, last.toFixed(0),
            last >= 70 ? DOWN : last <= 30 ? UP : "#2c2c31");
        }
      }
    }

    /* ---------------- last price tag */
    const yLast = clamp(yP(lastC), g.main.y + 9, g.main.y + g.main.h - 9);
    const lc = lastDirUp ? UP : DOWN;
    if (yP(lastC) >= g.main.y && yP(lastC) <= g.main.y + g.main.h) {
      ctx.setLineDash([2, 4]);
      ctx.strokeStyle = lc;
      ctx.globalAlpha = 0.45;
      ctx.beginPath(); ctx.moveTo(PAD.l, yP(lastC)); ctx.lineTo(PAD.l + plotW, yP(lastC)); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
    }
    tag(PAD.l + plotW + 6, yLast, fmtP(lastC), lc);

    /* ---------------- crosshair + tooltip */
    if (hoverI != null && hoverI >= i0 && hoverI <= i1) {
      const k = candles[hoverI];
      const xc = x(hoverI);
      const yy = clamp(hoverY == null ? yP(k.c) : hoverY, g.main.y, g.main.y + g.main.h);
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = "rgba(120,120,128,.8)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xc, g.main.y); ctx.lineTo(xc, g.vol.y + g.vol.h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(PAD.l, yy); ctx.lineTo(PAD.l + plotW, yy); ctx.stroke();
      ctx.setLineDash([]);
      const cHover = col(k);
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = cHover;
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(xc, yP(k.c), 4.2, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      tag(PAD.l + plotW + 6, yy, fmtP(lo + (1 - (yy - g.main.y) / g.main.h) * (hi - lo)), "#2c2c31");
      tagTime(clamp(xc, PAD.l + 42, PAD.l + plotW - 42), tLabel(k.t));
      tip(k, xc, yP(k.c));
    } else if (tipEl) {
      tipEl.classList.remove("show");
    }

    /* keep the dual-layer gold glow pulsing while both overlays are visible */
    if (!REDUCE_MOTION && !animRaf && candles.length && show.pivots && show.structure) {
      animRaf = requestAnimationFrame(() => { animRaf = 0; draw(); });
    }

    function tag(tx, ty, text, bg) {
      ctx.font = "600 10.5px Manrope, system-ui, sans-serif";
      const w = ctx.measureText(text).width + 12;
      ctx.fillStyle = bg;
      ctx.beginPath();
      ctx.roundRect(tx, ty - 9, w, 18, 5);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.textAlign = "center";
      ctx.fillText(text, tx + w / 2, ty + 0.5);
    }
    function tagTime(tx, text) {
      ctx.font = "600 10.5px Manrope, system-ui, sans-serif";
      const w = ctx.measureText(text).width + 12;
      ctx.fillStyle = "#2c2c31";
      ctx.beginPath();
      ctx.roundRect(tx - w / 2, H - PAD.b + 3, w, 17, 5);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.textAlign = "center";
      ctx.fillText(text, tx, H - PAD.b + 12);
    }
    function tip(k, tx, ty) {
      if (!tipEl) return;
      const up = k.c >= k.o, prev = hoverI > 0 ? candles[hoverI - 1].c : k.o;
      const chg = prev > 0 ? (k.c - prev) / prev * 100 : 0;
      const oIdx = ov ? Math.round((k.t - ov.t0) / stepMs) : -1;
      const rv = ov && ov.rsi && oIdx >= 0 ? ov.rsi[oIdx] : null;
      const rows = [
        '<div>' + tFull(k.t) + '</div>',
        '<div><span>Open</span>' + fmtP(k.o) + '</div>',
        '<div><span>High</span>' + fmtP(k.h) + '</div>',
        '<div><span>Low</span>' + fmtP(k.l) + '</div>',
        '<div><span>Close</span><b style="color:' + (up ? UP_L : DOWN_L) + '">' + fmtP(k.c) + '</b></div>',
        '<div><span>Chg</span>' + (chg >= 0 ? "+" : "") + chg.toFixed(2) + '%</div>',
        '<div><span>Vol</span>' + fmtV(k.v) + '</div>',
      ];
      if (rv != null) rows.push('<div><span>RSI 14</span>' + rv.toFixed(1) + '</div>');
      tipEl.innerHTML = rows.join("");
      tipEl.style.left = clamp(tx, 95, W - 95) + "px";
      tipEl.style.top = ty + "px";
      tipEl.style.transform = ty < 190 ? "translate(-50%, 26px)" : "translate(-50%, -112%)";
      tipEl.classList.add("show");
    }
  }

  /* ------------------------------------------------ events */
  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; draw(); });
  }

  function pointer(e) {
    if (!geom || !candles.length) return;
    const r = cv.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    if (px < PAD.l || px > PAD.l + geom.plotW) { hoverI = null; schedule(); return; }
    const i = clamp(Math.floor(view.start + (px - PAD.l) / geom.rowH), geom.i0, geom.i1);
    hoverI = i;
    hoverY = clamp(py, geom.main.y, geom.main.y + geom.main.h);
    schedule();
  }

  function plotWidth() { return W - PAD.l - PAD.r; }

  function zoomAt(px, factor) {
    const n = candles.length;
    if (!n || n <= MIN_BARS) return;
    const vc = viewCount();
    const rowH = plotWidth() / vc;
    const pxClamped = clamp(px, PAD.l, PAD.l + plotWidth());
    const anchor = view.start + (pxClamped - PAD.l) / rowH;
    const next = clamp(vc * factor, Math.min(MIN_BARS, n), maxViewCount());
    view.count = next;
    view.start = anchor - (pxClamped - PAD.l) / (plotWidth() / next);
    clampView();
    schedule();
  }

  /* magnifier-style focus zoom at a plot x: zoom in, fit the axis; at the
     deep-zoom limit or on the price axis it jumps back to the live edge */
  function focusZoom(px) {
    if (!candles.length) return;
    if (!geom || px > PAD.l + geom.plotW) { resetView(); return; }
    if (viewCount() <= Math.min(MIN_BARS, candles.length) + 2) { resetView(); return; }
    zoomAt(px, 1 / 1.8);
  }

  function panByPixels(dx) {
    const vc = viewCount();
    const rowH = plotWidth() / vc;
    view.start -= dx / rowH;
    clampView();
    hoverI = null;
    schedule();
  }

  function vScrollBy(dy) {
    if (!geom) return;
    const cap = 3 * geom.main.h;
    vScrollPx = clamp(vScrollPx + dy, -cap, cap);
    schedule();
  }

  /* two-finger touch: centroid delta pans (time + price), distance ratio zooms */
  function touchGesture() {
    const pts = Array.from(touches.values());
    if (pts.length < 2) return;
    const cx = (pts[0].x + pts[1].x) / 2, cy = (pts[0].y + pts[1].y) / 2;
    const dist = Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y));
    if (!pinch) { pinch = { cx, cy, dist }; hoverI = null; schedule(); return; }
    const r = cv.getBoundingClientRect();
    const rowH = plotWidth() / viewCount();
    view.start -= (cx - pinch.cx) / rowH;   // grab-pan: horizontal = time, vertical = price
    clampView();
    vScrollBy(-(cy - pinch.cy));
    if (Math.abs(dist - pinch.dist) > 5) zoomAt(cx - r.left, pinch.dist / dist);
    pinch = { cx, cy, dist };
    hoverI = null;
    schedule();
  }

  function init(canvas, tip, status) {
    cv = canvas; tipEl = tip; statusEl = status;
    ctx = cv.getContext("2d");
    if (!ctx.roundRect) {
      CanvasRenderingContext2D.prototype.roundRect = function (x, y, w, h) {
        this.rect(x, y, w, h);
        return this;
      };
    }
    wrap = cv.parentElement;
    liveEl = wrap.querySelector(".chart-live");
    if (liveEl) liveEl.addEventListener("click", resetView);
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const wasFit = candles.length > 0 && viewCount() >= maxViewCount() - 0.01;
      W = wrap.clientWidth;
      H = wrap.clientHeight;
      cv.width = Math.max(1, Math.round(W * dpr));
      cv.height = Math.max(1, Math.round(H * dpr));
      /* rotation/resize: a fitted window re-fits to the new width and stays
         pinned to the live edge instead of leaving bars stacked */
      if (wasFit) fitView();
      draw();
    };
    new ResizeObserver(resize).observe(wrap);
    resize();
    cv.addEventListener("pointermove", (e) => {
      if (e.pointerType === "touch") {
        if (!touches.has(e.pointerId)) return;
        touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (touches.size >= 2) touchGesture();
        else pointer(e);
        return;
      }
      if (drag) {
        const dx = e.clientX - drag.x;
        if (Math.abs(dx) > 2) drag.moved = true;
        panByPixels(dx - drag.lastDx);
        drag.lastDx = dx;
        return;
      }
      pointer(e);
    });
    cv.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "touch") {
        const wasSingle = touches.size === 0;
        touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (e.isPrimary && wasSingle) {
          /* double-tap = focus zoom (same as dblclick), 320ms / 18px window;
             only tracked when the previous finger fully lifted, so pinch
             starts can never read as a double-tap */
          const now = performance.now();
          if (lastTap && now - lastTap.t < 320 && Math.abs(e.clientX - lastTap.x) < 18 && Math.abs(e.clientY - lastTap.y) < 18) {
            tapZoomAt = now;
            lastTap = null;
            const r = cv.getBoundingClientRect();
            focusZoom(e.clientX - r.left);
          } else {
            lastTap = { t: now, x: e.clientX, y: e.clientY };
          }
        }
        if (touches.size === 1) pointer(e);
        else { pinch = null; lastTap = null; hoverI = null; schedule(); }
        try { cv.setPointerCapture(e.pointerId); } catch { /* synthetic events */ }
        return;
      }
      if (e.button !== 0) return;
      drag = { x: e.clientX, lastDx: 0, moved: false };
      cv.classList.add("dragging");
      try { cv.setPointerCapture(e.pointerId); } catch { /* synthetic events */ }
      pointer(e);
    });
    const endPointer = (e) => {
      if (e.pointerType === "touch") {
        touches.delete(e.pointerId);
        if (touches.size < 2) pinch = null;
        try { cv.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
        return;
      }
      if (!drag) return;
      drag = null;
      cv.classList.remove("dragging");
      try { cv.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
    };
    cv.addEventListener("pointerup", endPointer);
    cv.addEventListener("pointercancel", endPointer);
    cv.addEventListener("pointerleave", (e) => {
      if (!drag && e.pointerType !== "touch") { hoverI = null; schedule(); }
    });
    cv.addEventListener("blur", () => { hoverI = null; touches.clear(); pinch = null; schedule(); });
    /* double-click: magnifier-style focus zoom at the pointer (x1.8 per click),
       time-anchored on the pointed candle with the price axis auto-fitting the
       zoomed window; on the price axis it fits, and at the deep-zoom limit too.
       Touch double-taps are handled in pointerdown (tapZoomAt guards the
       browser-synthesized dblclick that follows them). */
    cv.addEventListener("dblclick", (e) => {
      if (performance.now() - tapZoomAt < 600) return;
      const r = cv.getBoundingClientRect();
      focusZoom(e.clientX - r.left);
    });
    cv.addEventListener("wheel", (e) => {
      if (!candles.length) return;
      e.preventDefault();
      /* classify per gesture burst: pinch (ctrl+wheel) or a mouse notch = zoom,
         small continuous deltas (two-finger trackpad scroll) = pan both axes */
      const now = performance.now();
      const notch = e.deltaMode === 1 || (Math.abs(e.deltaY) >= 50 && Math.abs(e.deltaX) < 2);
      if (!wheelBurst || now - wheelBurst.t > 250) {
        wheelBurst = { t: now, mode: e.ctrlKey || e.metaKey ? "zoom" : notch ? "zoom" : "scroll" };
      } else {
        wheelBurst.t = now;
        if (e.ctrlKey || e.metaKey) wheelBurst.mode = "zoom";
      }
      if (wheelBurst.mode === "zoom") {
        const r = cv.getBoundingClientRect();
        zoomAt(e.clientX - r.left, Math.pow(1.0018, clamp(e.deltaY, -400, 400)));
        return;
      }
      if (e.deltaX) panByPixels(-e.deltaX);
      if (e.deltaY) vScrollBy(e.deltaY);
    }, { passive: false });
    cv.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { resetView(); e.preventDefault(); return; }
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (hoverI == null) hoverI = Math.round(view.start + viewCount() - 1);
      hoverI = clamp(hoverI + (e.key === "ArrowRight" ? 1 : -1), 0, candles.length - 1);
      hoverY = null;
      if (hoverI < view.start || hoverI > view.start + viewCount() - 1) {
        view.start = hoverI - Math.floor(viewCount() / 2);
        clampView();
      }
      schedule();
      e.preventDefault();
    });
  }

  return { init, setData, setOverlays, updateTail, setMode, setToggles, getCandles, getOverlays, setStatus, getView, resetView };
})();
