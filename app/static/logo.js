/* ------------------------------------------------ bullet logo */
const BulletLogo = (() => {
  const el = document.getElementById("logo");
  if (!el || !el.getContext) return { fire() {}, pulse() {}, state: "off" };

  const c2d = el.getContext("2d");
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  const TEXT = "SIGNAL-X";
  const CALM = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rnd = (a, b) => a + Math.random() * (b - a);

  let W = 138, H = 32, CX = 96, CY = 16, RX = 35, RY = 9, TF = 10;

  function mk(col, size) {
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    const gr = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gr.addColorStop(0, `rgba(${col[0]},${col[1]},${col[2]},1)`);
    gr.addColorStop(.4, `rgba(${col[0]},${col[1]},${col[2]},.55)`);
    gr.addColorStop(1, `rgba(${col[0]},${col[1]},${col[2]},0)`);
    g.fillStyle = gr;
    g.fillRect(0, 0, size, size);
    return c;
  }

  const RAMP = [[255, 255, 240], [255, 222, 110], [255, 140, 35], [215, 45, 12], [70, 12, 6]];
  const FIRE = RAMP.map((c) => mk(c, 44));
  const CORE = mk([255, 250, 230], 40);
  const SMOKE = mk([150, 150, 160], 44);
  const GLOW = mk([255, 205, 90], 64);
  const WHITE = mk([225, 238, 255], 56);

  const particles = [], rings = [];
  let state = "idle", flyT = 0, hit = 0, lock = 0, shake = 0, slow = 0, settleT = 0;
  let lastFire = -1e9, nextAuto = 1400, last = 0, lastDraw = 0, T = 0;

  function mix(a, b, t) {
    const r = Math.round(a[0] + (b[0] - a[0]) * t);
    const g = Math.round(a[1] + (b[1] - a[1]) * t);
    const bl = Math.round(a[2] + (b[2] - a[2]) * t);
    return `rgb(${r},${g},${bl})`;
  }
  const heat = () => Math.min(1, lock * .75 + hit * .5);

  function fitText() {
    let size = Math.max(9.5, Math.min(13, H * .3));
    const maxW = RX * 2 - 10;
    for (let i = 0; i < 14; i++) {
      c2d.font = `700 ${size}px Georgia, "Times New Roman", serif`;
      if (c2d.measureText(TEXT).width <= maxW || size <= 9) break;
      size -= .4;
    }
    TF = size;
  }

  function layout() {
    const r = el.getBoundingClientRect();
    W = Math.max(120, Math.round(r.width) || 190);
    H = Math.max(28, Math.round(r.height) || 44);
    if (el.width !== Math.round(W * DPR)) el.width = Math.round(W * DPR);
    if (el.height !== Math.round(H * DPR)) el.height = Math.round(H * DPR);
    c2d.setTransform(DPR, 0, 0, DPR, 0, 0);
    RY = Math.min(12.5, H * .284);
    RX = Math.min(RY * 3.84, W * .3);
    CX = W - RX - 7;
    CY = H / 2;
    fitText();
  }

  function fire() {
    const now = performance.now();
    if (state === "fly" || now - lastFire < 900) return;
    lastFire = now;
    nextAuto = 20000;
    state = "fly";
    flyT = 0;
    lock = 1;
  }

  function bulletPos() {
    if (state !== "fly") return null;
    const e = Math.pow(flyT, 1.5);
    return { x: -40 + e * (CX + 46), y: CY };
  }

  function emitTrail() {
    const bp = bulletPos();
    for (let i = 0; i < 2; i++)
      particles.push({
        t: "f", x: bp.x - 12 - rnd(0, 6), y: bp.y + rnd(-3, 3),
        vx: -50 - Math.random() * 40, vy: rnd(-12, 12),
        life: 1, ttl: .2 + Math.random() * .18, r: 3.4 + Math.random() * 2.4, drag: 2.2,
      });
    if ((T * 60 | 0) % 3 === 0)
      particles.push({
        t: "k", x: bp.x - 14, y: bp.y + rnd(-2, 2), vx: -30, vy: -8,
        life: 1, ttl: .5, r: 3.6, drag: 1.4, g: -12,
      });
    if (particles.length > 260) particles.splice(0, particles.length - 260);
  }

  function impact() {
    state = "settle";
    settleT = 0;
    hit = 1;
    lock = 1;
    if (!CALM) { shake = 1; slow = 1; }
    rings.length = 0;
    rings.push({ life: 1, delay: 0, col: "rgba(255,232,180,.9)", w: 2.2 });
    rings.push({ life: 1, delay: .07, col: "rgba(255,120,60,.75)", w: 3 });
    if (!CALM) {
      for (let i = 0; i < 26; i++) {
        const a = Math.random() * Math.PI * 2, sp = 140 + Math.random() * 340;
        particles.push({
          t: "s", x: CX, y: CY, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp * .55,
          life: 1, ttl: .3 + Math.random() * .5, drag: 2.4,
        });
      }
      for (let i = 0; i < 10; i++) {
        const a = Math.random() * Math.PI * 2, sp = 40 + Math.random() * 130;
        particles.push({
          t: "f", x: CX, y: CY, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp * .6 - 30,
          life: 1, ttl: .5 + Math.random() * .7, r: 3 + Math.random() * 4, drag: 1.6, g: 210,
        });
      }
      for (let i = 0; i < 4; i++)
        particles.push({
          t: "k", x: CX + rnd(-6, 6), y: CY + rnd(-3, 3), vx: rnd(-14, 14), vy: -16 - Math.random() * 18,
          life: 1, ttl: .8 + Math.random() * .7, r: 6 + Math.random() * 5, drag: 1.1,
        });
    }
    for (let i = 0; i < 3; i++)
      particles.push({ t: "x", x: CX, y: CY, vx: 0, vy: 0, life: 1, ttl: .16 + i * .05, r: 26 + i * 14, drag: 0 });
  }

  function pulse() {
    hit = Math.max(hit, .6);
    lock = Math.max(lock, .35);
    if (!CALM) shake = Math.max(shake, .3);
  }

  function step(dt) {
    T += dt;
    hit = Math.max(0, hit - dt * 1.7);
    lock = Math.max(0, lock - dt * 2.1);
    shake = Math.max(0, shake - dt * 2.6);
    if (slow > 0) slow = Math.max(0, slow - dt / .6);

    if (state === "fly") {
      flyT = Math.min(1, flyT + dt / .48);
      emitTrail();
      if (flyT >= 1) impact();
    } else if (state === "settle") {
      settleT += dt;
      if (settleT > 1.1 && !particles.length && !rings.length) state = "idle";
    }

    const ts = slow > 0 && !CALM ? .55 : 1;
    const pdt = dt * ts;
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.life -= pdt / p.ttl;
      if (p.life <= 0) { particles.splice(i, 1); continue; }
      p.x += p.vx * pdt;
      p.y += p.vy * pdt;
      if (p.g) p.vy += p.g * pdt;
      const d = 1 - Math.min(.95, p.drag * pdt);
      p.vx *= d;
      p.vy *= d;
    }
    for (let i = rings.length - 1; i >= 0; i--) {
      const r = rings[i];
      if (r.delay > 0) { r.delay -= dt; continue; }
      r.life -= dt / .62;
      if (r.life <= 0) rings.splice(i, 1);
    }
  }

  function roundRect(x, y, w, h, r) {
    c2d.beginPath();
    c2d.moveTo(x + r, y);
    c2d.arcTo(x + w, y, x + w, y + h, r);
    c2d.arcTo(x + w, y + h, x, y + h, r);
    c2d.arcTo(x, y + h, x, y, r);
    c2d.arcTo(x, y, x + w, y, r);
    c2d.closePath();
  }

  function drawBullet(x, y) {
    const L = 22, bh = 7;
    c2d.save();
    c2d.translate(x, y);
    const fl = 1 + Math.sin(T * 40) * .15;
    c2d.globalAlpha = .9;
    c2d.drawImage(FIRE[2], -L - 10 - rnd(0, 4), -8 * fl, 24 * fl, 16 * fl);
    c2d.drawImage(CORE, -L - 4, -5 * fl, 12, 10);
    c2d.globalAlpha = 1;
    const g = c2d.createLinearGradient(0, -bh / 2, 0, bh / 2);
    g.addColorStop(0, "#eceef4");
    g.addColorStop(.42, "#a8aebc");
    g.addColorStop(.6, "#454b58");
    g.addColorStop(1, "#15171c");
    c2d.fillStyle = g;
    roundRect(-L / 2 + 5, -bh / 2, L - 9, bh, 2.2);
    c2d.fill();
    const tg = c2d.createLinearGradient(-L / 2 + 5, 0, -L / 2 - 4, 0);
    tg.addColorStop(0, "#f2c48e");
    tg.addColorStop(.55, "#c9803f");
    tg.addColorStop(1, "#6e421c");
    c2d.fillStyle = tg;
    c2d.beginPath();
    c2d.moveTo(-L / 2 + 5.5, -bh / 2 + .5);
    c2d.lineTo(-L / 2 - 3.5, 0);
    c2d.lineTo(-L / 2 + 5.5, bh / 2 - .5);
    c2d.closePath();
    c2d.fill();
    c2d.fillStyle = "rgba(255,255,255,.30)";
    c2d.fillRect(-L / 2 + 7, -bh / 2 + .9, L - 12, 1.1);
    c2d.fillStyle = "rgba(0,0,0,.35)";
    c2d.fillRect(-L / 2 + 4.4, -bh / 2, 1.1, bh);
    c2d.restore();
  }

  function drawTarget() {
    const h = heat();
    const main = mix([255, 205, 90], [255, 84, 60], h);

    c2d.globalAlpha = .22 + h * .3;
    c2d.strokeStyle = main;
    c2d.lineWidth = 1;
    c2d.beginPath();
    c2d.moveTo(4, CY);
    c2d.lineTo(CX + RX + 10, CY);
    c2d.stroke();
    c2d.beginPath();
    c2d.moveTo(CX, 3);
    c2d.lineTo(CX, H - 3);
    c2d.stroke();
    c2d.globalAlpha = 1;

    if (h > .04) {
      c2d.globalAlpha = h * .35;
      c2d.drawImage(GLOW, CX - RX * .7, CY - RX * .7, RX * 1.4, RX * 1.4);
      c2d.globalAlpha = 1;
    }

    c2d.save();
    c2d.globalAlpha = .3 + h * .35;
    c2d.strokeStyle = main;
    c2d.lineWidth = 1.1;
    c2d.setLineDash([3, 4]);
    c2d.lineDashOffset = -(T * 9);
    c2d.beginPath();
    c2d.ellipse(CX, CY, RX * .82, RY * .82, 0, 0, Math.PI * 2);
    c2d.stroke();
    c2d.restore();

    c2d.globalAlpha = .5 + h * .4;
    c2d.strokeStyle = main;
    c2d.lineWidth = 1.5;
    c2d.beginPath();
    c2d.ellipse(CX, CY, RX, RY, 0, 0, Math.PI * 2);
    c2d.stroke();
    c2d.globalAlpha = 1;

    const vd = RY + Math.min(6, H * .14);
    for (const [dx, dy] of [[CX - RX - 5, CY], [CX + RX + 5, CY], [CX, CY - vd], [CX, CY + vd]]) {
      c2d.save();
      c2d.translate(dx, dy);
      c2d.rotate(Math.PI / 4);
      c2d.fillStyle = main;
      c2d.globalAlpha = .65 + h * .35;
      c2d.fillRect(-1.8, -1.8, 3.6, 3.6);
      c2d.restore();
    }

    const g = c2d.createLinearGradient(0, CY - RY, 0, CY + RY);
    g.addColorStop(0, mix([255, 243, 196], [255, 84, 60], h));
    g.addColorStop(.5, mix([255, 205, 80], [255, 84, 60], h));
    g.addColorStop(1, mix([190, 125, 25], [255, 84, 60], h));
    c2d.font = `700 ${TF}px Georgia, "Times New Roman", serif`;
    c2d.textAlign = "center";
    c2d.textBaseline = "middle";
    c2d.lineWidth = Math.max(1.6, TF * .2);
    c2d.strokeStyle = `rgba(255,190,90,${.16 + h * .3 + Math.sin(T * 3) * .03})`;
    c2d.strokeText(TEXT, CX, CY + .5);
    c2d.fillStyle = g;
    c2d.fillText(TEXT, CX, CY + .5);
  }

  function drawParticles() {
    for (const p of particles) {
      if (p.t === "s") {
        c2d.globalAlpha = p.life * .9;
        c2d.strokeStyle = "rgba(255,226,150,1)";
        c2d.lineWidth = 1.3;
        c2d.beginPath();
        c2d.moveTo(p.x, p.y);
        c2d.lineTo(p.x - p.vx * .05, p.y - p.vy * .05);
        c2d.stroke();
      } else if (p.t === "f") {
        const idx = Math.min(FIRE.length - 1, ((1 - p.life) * FIRE.length) | 0);
        const s = p.r * (1.7 - p.life * .7) * 2;
        c2d.globalAlpha = Math.min(1, p.life * 1.4);
        c2d.drawImage(FIRE[idx], p.x - s / 2, p.y - s / 2, s, s);
      } else if (p.t === "k") {
        const s = p.r * (2.2 - p.life * .8);
        c2d.globalAlpha = p.life * .3;
        c2d.drawImage(SMOKE, p.x - s / 2, p.y - s / 2, s, s);
      } else {
        const s = p.r * (1.6 - p.life * .6);
        c2d.globalAlpha = p.life * .8;
        c2d.drawImage(WHITE, p.x - s / 2, p.y - s / 2, s, s);
      }
    }
    c2d.globalAlpha = 1;
  }

  function drawRings() {
    for (const r of rings) {
      if (r.delay > 0) continue;
      const e = 1 - Math.pow(1 - (1 - r.life), 2.2);
      const hr = 5 + e * (RX + 14);
      const vr = hr * (RY / RX) * .78;
      c2d.globalAlpha = r.life * .8;
      c2d.strokeStyle = r.col;
      c2d.lineWidth = r.w * r.life + .4;
      c2d.beginPath();
      c2d.ellipse(CX, CY, hr, vr, 0, 0, Math.PI * 2);
      c2d.stroke();
    }
    c2d.globalAlpha = 1;
  }

  function draw() {
    c2d.clearRect(0, 0, W, H);
    c2d.save();
    if (shake > 0) c2d.translate(rnd(-1, 1) * shake * 2.6, rnd(-1, 1) * shake * 1.6);
    drawTarget();
    drawRings();
    drawParticles();
    const bp = bulletPos();
    if (bp) drawBullet(bp.x, bp.y);
    c2d.restore();
  }

  function loop(now) {
    const dt = Math.min(.05, (now - last) / 1000 || 0);
    last = now;

    if (!CALM) {
      nextAuto -= dt * 1000;
      if (nextAuto <= 0 && document.visibilityState === "visible") fire();
    }
    step(dt);

    const busy = state !== "idle" || particles.length || rings.length || hit > .01 || shake > .01;
    if (busy || now - lastDraw > (CALM ? 240 : 80)) {
      lastDraw = now;
      draw();
    }
    requestAnimationFrame(loop);
  }

  function boot() {
    layout();
    draw();
    el.addEventListener("pointerdown", fire);
    window.addEventListener("resize", layout);
    requestAnimationFrame((now) => {
      last = now;
      requestAnimationFrame(loop);
    });
  }

  boot();
  return { fire, pulse, get state() { return state; } };
})();
