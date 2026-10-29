// 时间线：canvas 画波形和片段条，支持缩放、平移、拖边界、点击定位。
// 状态不靠颜色区分：保留 = 实心；剪掉 = 斜纹；拿不准 = 顶部加一条带点的“标签”和 ▲ 记号。

import { bucketPeaks, fmtTime, isUncertain } from "./model.js";

const RULER_H = 20;
const BAND_Y = 24;
const BAND_H = 30;
const WAVE_Y = 60;
const HIT_PX = 7;
const MIN_PPS_FIT = 0.02;
const MAX_PPS = 600;

export class Timeline {
  /**
   * callbacks: onSeek(t, seg|null)、onSelect(segId)、onDrag({segId, edge, t, phase})、describe(seg) → 提示文字
   */
  constructor({ canvas, overview, tooltip, callbacks }) {
    this.canvas = canvas;
    this.overview = overview;
    this.tooltip = tooltip;
    this.cb = callbacks;
    this.ctx = canvas.getContext("2d");
    this.octx = overview?.getContext("2d");
    this.output = null;
    this.sources = {};
    this.sourceId = null;
    this.duration = 0;
    this.segs = [];
    this.peaks = null;
    this.perSec = 50;
    this.playhead = 0;
    this.selSeg = null;
    this.edge = "start";
    this.view = { t0: 0, pps: 10 };
    this.followUntil = 0;
    this.drag = null;
    this._raf = null;
    this._theme = null;
    this._pat = null;
    this._w = 0;
    this._h = 0;
    this._bindEvents();
    this._ro = new ResizeObserver(() => { this.resize(); });
    this._ro.observe(canvas.parentElement);
    this.refreshTheme();
  }

  // ---- 数据 ----
  setData({ output, sources, sourceId, duration }) {
    this.output = output;
    this.sources = sources;
    const changed = sourceId !== this.sourceId;
    this.sourceId = sourceId;
    this.duration = duration || sources[sourceId]?.duration || Math.max(0, ...output.segments.map((s) => s.end));
    this.segs = output.segments.filter((s) => s.source === sourceId).sort((a, b) => a.start - b.start);
    if (changed) this.peaks = null;
    this.resize();
  }

  setPeaks(peaks, perSec) {
    this.peaks = peaks;
    this.perSec = perSec;
    this.requestDraw();
  }

  setPlayhead(t) {
    this.playhead = t;
    if (performance.now() > this.followUntil) this._follow();
    this.requestDraw();
  }

  setSelection(segId, edge = this.edge) {
    this.selSeg = segId;
    this.edge = edge;
    this.requestDraw();
  }

  refreshTheme() {
    const cs = getComputedStyle(this.canvas);
    const v = (n) => cs.getPropertyValue(n).trim();
    this._theme = {
      bg: v("--tl-bg"), keep: v("--tl-keep"), cut: v("--tl-cut"), cutBg: v("--tl-cut-bg"), warn: v("--tl-warn"),
      wave: v("--tl-wave"), waveDim: v("--tl-wave-dim"), grid: v("--tl-grid"), text: v("--tl-text"),
      playhead: v("--tl-playhead"), sel: v("--tl-sel"), font: v("--font-mono") || "ui-monospace, monospace",
    };
    this._pat = null;
    this.requestDraw();
  }

  // ---- 视图 ----
  resize() {
    const dpr = window.devicePixelRatio || 1;
    const p = this.canvas.parentElement;
    const w = Math.max(100, p.clientWidth);
    const h = this.canvas.clientHeight || 140;
    if (w === this._w && h === this._h && dpr === this._dpr) { this.requestDraw(); return; }
    this._w = w; this._h = h; this._dpr = dpr;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    if (this.overview) {
      this.overview.width = Math.round(w * dpr);
      this.overview.height = Math.round((this.overview.clientHeight || 28) * dpr);
    }
    this._pat = null;
    if (!this._fitted && this.duration) this.fitInitial();
    this._clampView();
    this.requestDraw();
  }

  /** 短素材整段显示；长素材先看一个约 60 秒的窗口（整段在概览条里看）。 */
  fitInitial() {
    this._fitted = true;
    const w = this._w;
    if (this.duration <= 120) this.view = { t0: 0, pps: w / Math.max(this.duration, 1) };
    else this.view = { t0: 0, pps: w / 60 };
  }

  fitAll() { this.view = { t0: 0, pps: this._w / Math.max(this.duration, 1) }; this._clampView(); this.requestDraw(); }

  /** 把 [a, b] 放满窗口（候选模式：只看这一条的范围）。 */
  fitRange(a, b, pad = 2) {
    a = Math.max(0, a - pad); b = Math.min(this.duration || b, b + pad);
    this._fitted = true;
    this.view = { t0: a, pps: this._w / Math.max(b - a, 1) };
    this._clampView();
    this.requestDraw();
  }

  zoomBy(factor, anchorX = this._w / 2) {
    const tAnchor = this.xToT(anchorX);
    this.view.pps *= factor;
    this._clampView(false);
    this.view.t0 = tAnchor - anchorX / this.view.pps;
    this._clampView();
    this.followUntil = performance.now() + 4000;
    this.requestDraw();
  }

  panBy(dt) {
    this.view.t0 += dt;
    this._clampView();
    this.followUntil = performance.now() + 4000;
    this.requestDraw();
  }

  /** 让 t 出现在窗口内（选中某一决定时用）；已经可见就不动。 */
  ensureVisible(t, margin = 0.15) {
    const span = this._w / this.view.pps;
    if (t < this.view.t0 + span * margin || t > this.view.t0 + span * (1 - margin)) {
      this.view.t0 = t - span * 0.35;
      this._clampView();
      this.requestDraw();
    }
  }

  _follow() {
    if (!this.drag) this.ensureVisible(this.playhead, 0.06);
  }

  _clampView(clampPps = true) {
    const minPps = Math.max(MIN_PPS_FIT, this._w / Math.max(this.duration, 1));
    if (clampPps) this.view.pps = Math.min(Math.max(this.view.pps, minPps), MAX_PPS);
    else this.view.pps = Math.min(Math.max(this.view.pps, minPps), MAX_PPS);
    const span = this._w / this.view.pps;
    this.view.t0 = Math.min(Math.max(this.view.t0, 0), Math.max(0, this.duration - span));
  }

  tToX(t) { return (t - this.view.t0) * this.view.pps; }
  xToT(x) { return this.view.t0 + x / this.view.pps; }

  // ---- 交互 ----
  _bindEvents() {
    const c = this.canvas;
    c.addEventListener("pointerdown", (e) => this._down(e));
    c.addEventListener("pointermove", (e) => this._move(e));
    c.addEventListener("pointerup", (e) => this._up(e));
    c.addEventListener("pointercancel", () => { this.drag = null; });
    c.addEventListener("pointerleave", () => { this._hideTip(); });
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = c.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) this.zoomBy(Math.exp(-e.deltaY * 0.01), e.clientX - rect.left);
      else this.panBy(((Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) / this.view.pps));
    }, { passive: false });

    const ov = this.overview;
    if (ov) {
      let down = false;
      const goto = (e) => {
        const r = ov.getBoundingClientRect();
        const t = ((e.clientX - r.left) / r.width) * this.duration;
        const span = this._w / this.view.pps;
        this.view.t0 = t - span / 2;
        this._clampView();
        this.followUntil = performance.now() + 4000;
        this.requestDraw();
      };
      ov.addEventListener("pointerdown", (e) => { down = true; ov.setPointerCapture(e.pointerId); goto(e); });
      ov.addEventListener("pointermove", (e) => { if (down) goto(e); });
      ov.addEventListener("pointerup", () => { down = false; });
    }
  }

  _edges() {
    // 可拖的边界。相接的两个片段共用一条边界（拖它等于 move_boundary(左边.end)，右边的起点一起动）。
    const edges = [];
    for (let i = 0; i < this.segs.length; i++) {
      const s = this.segs[i], prev = this.segs[i - 1];
      const joined = prev && Math.abs(prev.end - s.start) < 1e-6;
      if (!joined) edges.push({ seg: s, edge: "start", t: s.start });
      edges.push({ seg: s, edge: "end", t: s.end });
    }
    return edges;
  }

  _hitEdge(x) {
    let best = null, bestD = HIT_PX + 0.01;
    for (const e of this._edges()) {
      const d = Math.abs(this.tToX(e.t) - x);
      if (d < bestD) { best = e; bestD = d; }
    }
    return best;
  }

  segAt(t) {
    for (const s of this.segs) if (t >= s.start && t < s.end) return s;
    return null;
  }

  _local(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  _down(e) {
    if (e.button !== 0) return;
    const { x, y } = this._local(e);
    this.canvas.focus({ preventScroll: true });
    const hit = y >= RULER_H ? this._hitEdge(x) : null;
    this.canvas.setPointerCapture(e.pointerId);
    if (hit) this.drag = { ...hit, startX: x, moved: false };
    else this.drag = { seek: true, startX: x, moved: false };
  }

  _move(e) {
    const { x, y } = this._local(e);
    if (this.drag) {
      if (Math.abs(x - this.drag.startX) > 3) this.drag.moved = true;
      if (this.drag.seek || !this.drag.moved) return;
      this.cb.onDrag({ segId: this.drag.seg.id, edge: this.drag.edge, t: this.xToT(x), phase: this.drag.started ? "move" : "start" });
      this.drag.started = true;
      this.showTip(x, y, `${this.drag.edge === "start" ? "起点" : "终点"} ${fmtTime(this.xToT(x), { ms: 2 })}`);
      return;
    }
    const hit = y >= RULER_H ? this._hitEdge(x) : null;
    this.canvas.style.cursor = hit ? "col-resize" : "pointer";
    const seg = this.segAt(this.xToT(x));
    if (seg && y >= BAND_Y - 4 && y <= BAND_Y + BAND_H + 4) this.showTip(x, y, this.cb.describe(seg));
    else this._hideTip();
  }

  _up(e) {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    const { x } = this._local(e);
    if (!d.seek && d.moved && d.started) {
      this.cb.onDrag({ segId: d.seg.id, edge: d.edge, t: this.xToT(x), phase: "end" });
      this._hideTip();
      return;
    }
    // 没拖动 → 当作点击：选中并定位
    const t = Math.min(Math.max(this.xToT(x), 0), this.duration);
    const seg = this.segAt(t);
    if (seg) this.cb.onSelect(seg.id);
    this.cb.onSeek(t, seg);
  }

  showTip(x, y, text) {
    if (!this.tooltip || !text) return;
    this.tooltip.textContent = text;
    this.tooltip.hidden = false;
    const w = this.tooltip.offsetWidth;
    this.tooltip.style.left = `${Math.min(Math.max(x - w / 2, 0), this._w - w)}px`;
    this.tooltip.style.top = `${Math.max(0, y - 34)}px`;
  }

  _hideTip() { if (this.tooltip) this.tooltip.hidden = true; }

  // ---- 绘制 ----
  requestDraw() {
    if (this._raf != null) return;
    this._raf = requestAnimationFrame(() => { this._raf = null; this._paint(); });
  }

  _patterns() {
    if (this._pat) return this._pat;
    const dpr = this._dpr || 1;
    const mk = (draw) => {
      const c = document.createElement("canvas");
      c.width = c.height = Math.round(8 * dpr);
      const g = c.getContext("2d");
      g.scale(dpr, dpr);
      draw(g);
      const p = this.ctx.createPattern(c, "repeat");
      p.setTransform(new DOMMatrix().scale(1 / dpr));
      return p;
    };
    const t = this._theme;
    this._pat = {
      stripe: mk((g) => { g.strokeStyle = t.cut; g.lineWidth = 1.6; g.beginPath(); g.moveTo(-2, 10); g.lineTo(10, -2); g.moveTo(-2, 6); g.lineTo(6, -2); g.moveTo(2, 10); g.lineTo(10, 2); g.stroke(); }),
      dots: mk((g) => { g.fillStyle = t.warn; g.beginPath(); g.arc(2, 2, 1.3, 0, 7); g.arc(6, 6, 1.3, 0, 7); g.fill(); }),
    };
    return this._pat;
  }

  _paint() {
    if (!this._theme || !this._w) return;
    const g = this.ctx, t = this._theme, W = this._w, H = this._h, dpr = this._dpr || 1;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.fillStyle = t.bg;
    g.fillRect(0, 0, W, H);
    const pat = this._patterns();
    const { t0, pps } = this.view;
    const t1 = t0 + W / pps;

    this._ruler(g, t0, t1);

    // 片段条
    const visible = this.segs.filter((s) => s.end >= t0 && s.start <= t1);
    for (const s of visible) {
      const x0 = this.tToX(s.start), x1 = this.tToX(s.end);
      const w = Math.max(1, x1 - x0);
      if (s.action === "keep") {
        g.fillStyle = t.keep;
        g.fillRect(x0, BAND_Y, w, BAND_H);
      } else {
        g.fillStyle = t.cutBg;
        g.fillRect(x0, BAND_Y, w, BAND_H);
        g.fillStyle = pat.stripe;
        g.fillRect(x0, BAND_Y, w, BAND_H);
        g.strokeStyle = t.cut;
        g.lineWidth = 1;
        g.strokeRect(x0 + 0.5, BAND_Y + 0.5, Math.max(0, w - 1), BAND_H - 1);
      }
      if (isUncertain(s)) {
        g.fillStyle = t.bg;
        g.fillRect(x0, BAND_Y, w, 9);
        g.fillStyle = t.warn;
        g.globalAlpha = 0.25;
        g.fillRect(x0, BAND_Y, w, 9);
        g.globalAlpha = 1;
        g.fillStyle = pat.dots;
        g.fillRect(x0, BAND_Y, w, 9);
        g.strokeStyle = t.warn;
        g.lineWidth = 1.5;
        g.strokeRect(x0 + 0.75, BAND_Y + 0.75, Math.max(0, w - 1.5), BAND_H - 1.5);
        if (w >= 7) { // ▲ 记号
          const cx = x0 + w / 2;
          g.fillStyle = t.warn;
          g.beginPath(); g.moveTo(cx, BAND_Y - 6); g.lineTo(cx - 4, BAND_Y - 1); g.lineTo(cx + 4, BAND_Y - 1); g.closePath(); g.fill();
        }
      }
      if (s.locked && w >= 12) { g.fillStyle = t.text; g.font = `10px ${t.font}`; g.fillText("L", x0 + 3, BAND_Y + BAND_H - 4); }
    }
    // 片段之间 1px 缝，相邻保留段不会糊成一块
    g.fillStyle = t.bg;
    for (const s of visible) { const x = this.tToX(s.start); if (pps > 3) g.fillRect(Math.round(x), BAND_Y, 1, BAND_H); }

    this._wave(g, visible, t0, t1);

    // 选中的片段：描边 + 两端把手（当前可用 [ ] 微调的那一端更醒目）
    const sel = this.segs.find((s) => s.id === this.selSeg);
    if (sel) {
      const x0 = this.tToX(sel.start), x1 = this.tToX(sel.end);
      g.strokeStyle = t.sel; g.lineWidth = 2;
      g.strokeRect(x0 + 1, BAND_Y - 1, Math.max(2, x1 - x0 - 2), BAND_H + 2);
      g.fillStyle = t.sel; g.globalAlpha = 0.08;
      g.fillRect(x0, WAVE_Y, Math.max(1, x1 - x0), H - WAVE_Y);
      g.globalAlpha = 1;
      for (const [edge, x] of [["start", x0], ["end", x1]]) {
        const active = edge === this.edge;
        g.fillStyle = t.sel;
        g.fillRect(x - (active ? 3 : 2), BAND_Y - 3, active ? 6 : 4, BAND_H + 6);
      }
    }

    // 播放头
    const px = this.tToX(this.playhead);
    if (px >= -2 && px <= W + 2) {
      g.fillStyle = t.playhead;
      g.fillRect(Math.round(px) - 1, RULER_H - 2, 2, H - RULER_H + 2);
      g.beginPath(); g.moveTo(px - 5, RULER_H - 4); g.lineTo(px + 5, RULER_H - 4); g.lineTo(px, RULER_H + 3); g.closePath(); g.fill();
    }
    this._paintOverview();
  }

  _ruler(g, t0, t1) {
    const t = this._theme;
    const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const step = steps.find((s) => s * this.view.pps >= 70) ?? 3600;
    g.fillStyle = t.text; g.strokeStyle = t.grid; g.lineWidth = 1;
    g.font = `11px ${t.font}`; g.textBaseline = "top";
    for (let s = Math.floor(t0 / step) * step; s <= t1 + step; s += step) {
      const x = Math.round(this.tToX(s)) + 0.5;
      g.beginPath(); g.moveTo(x, RULER_H - 6); g.lineTo(x, RULER_H); g.stroke();
      g.fillText(fmtTime(s, { ms: step < 1 ? 1 : 0 }), x + 3, 3);
    }
    g.beginPath(); g.moveTo(0, RULER_H + 0.5); g.lineTo(this._w, RULER_H + 0.5); g.stroke();
  }

  _wave(g, visible, t0, t1) {
    const t = this._theme, W = this._w, H = this._h;
    const top = WAVE_Y, h = H - WAVE_Y - 4, mid = top + h / 2;
    g.strokeStyle = t.grid; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, mid + 0.5); g.lineTo(W, mid + 0.5); g.stroke();
    if (!this.peaks?.length) return;
    const cols = Math.floor(W);
    const buckets = bucketPeaks(this.peaks, this.perSec, t0, t1, cols);
    // 哪些列属于保留的片段（亮），其余变淡
    const keepMask = new Uint8Array(cols);
    const lit = this.output.segments.some((s) => s.action === "keep" && s.source === this.sourceId)
      ? visible.filter((s) => s.action === "keep") : [];
    for (const s of lit) {
      const a = Math.max(0, Math.floor(this.tToX(s.start))), b = Math.min(cols, Math.ceil(this.tToX(s.end)));
      for (let x = a; x < b; x++) keepMask[x] = 1;
    }
    const draw = (color, want) => {
      g.fillStyle = color;
      for (let x = 0; x < cols; x++) {
        if (keepMask[x] !== want) continue;
        const a = Math.max(0.02, Math.pow(buckets[x], 0.6)) * (h / 2);
        g.fillRect(x, mid - a, 1, a * 2);
      }
    };
    draw(t.waveDim, 0);
    draw(t.wave, 1);
  }

  _paintOverview() {
    if (!this.octx || !this.duration) return;
    const g = this.octx, t = this._theme, dpr = this._dpr || 1;
    const W = this._w, H = this.overview.clientHeight || 28;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.fillStyle = t.bg; g.fillRect(0, 0, W, H);
    const pat = this._patterns();
    const k = W / this.duration;
    for (const s of this.segs) {
      const x = s.start * k, w = Math.max(1, (s.end - s.start) * k);
      if (s.action === "keep") { g.fillStyle = t.keep; g.fillRect(x, 6, w, H - 12); }
      else { g.fillStyle = t.cutBg; g.fillRect(x, 6, w, H - 12); g.fillStyle = pat.stripe; g.fillRect(x, 6, w, H - 12); }
      if (isUncertain(s)) { g.fillStyle = t.warn; g.fillRect(x, 1, Math.max(2, w), 4); }
    }
    const span = W / this.view.pps;
    g.strokeStyle = t.sel; g.lineWidth = 2;
    g.strokeRect(this.view.t0 * k + 1, 1, Math.max(4, span * k - 2), H - 2);
    g.fillStyle = t.playhead;
    g.fillRect(Math.round(this.playhead * k) - 1, 0, 2, H);
  }
}
