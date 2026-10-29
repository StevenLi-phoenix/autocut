// 文字稿栏：按片段渲染，像一份带修订标记的稿子。
// 保留的正常显示；剪掉的画删除线并写明 理由 · 把握度 · detail；拿不准的加 ⚠ 和 [保留]/[剪掉]。
// 状态同时用形状（✂ ⚠）、线条（删除线/虚线）和文字表达，不只靠颜色。

import { REASONS, fmtTime, isUncertain, wordIndexAt, wordsText } from "./model.js";
import { GLYPH, h, reducedMotion } from "./ui.js";

const CJK = /[　-鿿＀-￯]/;

export class Transcript {
  /**
   * handlers: onSelect(segId, {seek}), onAction(segId, "keep"|"cut"|"toggle"|"audition"|"lock"), onWord(segId, t)
   */
  constructor({ list, scroller, handlers }) {
    this.list = list;
    this.scroller = scroller;
    this.h = handlers;
    this.rows = new Map();
    this.output = null;
    this.words = {};
    this.selected = null;
    this.now = null;
    this.nowWordEl = null;
    this.filter = { kind: "all", reason: null };
    list.addEventListener("click", (e) => this._click(e));
    list.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.classList?.contains("row")) { e.preventDefault(); this.h.onAction(e.target.dataset.seg, "audition"); }
    });
  }

  build(output, { sources, words, hasText }) {
    this.output = output;
    this.sources = sources;
    this.words = words;
    this.hasText = hasText;
    this.rows.clear();
    this.list.replaceChildren();
    const frag = document.createDocumentFragment();
    output.segments.forEach((seg, i) => {
      const row = h("li", { class: "row", dataset: { seg: seg.id }, tabindex: -1, role: "group" });
      this.rows.set(seg.id, row);
      this._fill(row, seg, i);
      frag.append(row);
    });
    this.list.append(frag);
    if (!output.segments.length) this.list.append(h("li", { class: "empty" }, "这个输出里还没有片段。"));
    this.selected = null;
    this.now = null;
    this.applyFilter(this.filter);
  }

  rowFor(segId) { return this.rows.get(segId); }

  update(segId) {
    const row = this.rows.get(segId);
    if (!row) return;
    const i = this.output.segments.findIndex((s) => s.id === segId);
    this._fill(row, this.output.segments[i], i);
    this._applyStateClasses(row, segId);
    this._passFilter(row, this.output.segments[i]);
  }

  updateAll() {
    this.output.segments.forEach((s) => this.update(s.id));
  }

  _applyStateClasses(row, segId) {
    row.classList.toggle("is-selected", segId === this.selected);
    row.classList.toggle("is-now", segId === this.now);
    row.tabIndex = segId === this.selected ? 0 : -1;
    for (const b of row.querySelectorAll(".row-actions button")) b.tabIndex = segId === this.selected ? 0 : -1;
  }

  setSelected(segId, { focus = false, scroll = true } = {}) {
    const prev = this.selected;
    this.selected = segId;
    if (prev && prev !== segId && this.rows.has(prev)) this._applyStateClasses(this.rows.get(prev), prev);
    const row = this.rows.get(segId);
    if (!row) return;
    this._applyStateClasses(row, segId);
    if (focus) row.focus({ preventScroll: true });
    if (scroll) this.ensureVisible(row, "nearest");
  }

  setNow(segId) {
    if (segId === this.now) return;
    const prev = this.now;
    this.now = segId;
    if (prev && this.rows.has(prev)) this.rows.get(prev).classList.remove("is-now");
    this.rows.get(segId)?.classList.add("is-now");
    this.setNowWord(null, null);
  }

  /** 播放时高亮当前正在说的词。 */
  setNowWord(segId, t) {
    const old = this.nowWordEl;
    let el = null;
    if (segId && t != null) {
      const spans = this.rows.get(segId)?._wordEls;
      if (spans?.length) {
        let lo = 0, hi = spans.length - 1;
        while (lo < hi) { const m = (lo + hi + 1) >> 1; if (spans[m].start <= t) lo = m; else hi = m - 1; }
        if (spans[lo].start <= t) el = spans[lo].el;
      }
    }
    if (el === old) return;
    old?.classList.remove("is-word-now");
    el?.classList.add("is-word-now");
    this.nowWordEl = el;
  }

  ensureVisible(row, mode = "nearest") {
    const s = this.scroller, r = row.getBoundingClientRect(), b = s.getBoundingClientRect();
    let top = null;
    if (mode === "center") top = s.scrollTop + (r.top - b.top) - (b.height / 2 - r.height / 2);
    else if (r.top < b.top + 8) top = s.scrollTop - (b.top + 8 - r.top) - 24;
    else if (r.bottom > b.bottom - 8) top = s.scrollTop + (r.bottom - b.bottom + 8) + 24;
    if (top != null) s.scrollTo({ top: Math.max(0, top), behavior: reducedMotion() ? "auto" : "smooth" });
  }

  // ---- 筛选 ----
  applyFilter(filter) {
    this.filter = filter;
    if (!this.output) return 0;
    let n = 0;
    this.output.segments.forEach((seg) => { if (this._passFilter(this.rows.get(seg.id), seg)) n++; });
    return n;
  }

  _passFilter(row, seg) {
    const { kind, reason } = this.filter;
    let ok = true;
    if (kind === "uncertain") ok = isUncertain(seg);
    else if (kind === "cut") ok = seg.action === "cut";
    else if (kind === "keep") ok = seg.action === "keep";
    if (ok && reason) ok = seg.reason === reason && seg.action === "cut";
    row.hidden = !ok;
    return ok;
  }

  isVisible(segId) { return !this.rows.get(segId)?.hidden; }

  // ---- 事件 ----
  _click(e) {
    const row = e.target.closest(".row");
    if (!row) return;
    const segId = row.dataset.seg;
    const btn = e.target.closest("button[data-act]");
    if (btn) {
      e.stopPropagation();
      this.h.onAction(segId, btn.dataset.act);
      return;
    }
    const w = e.target.closest(".w");
    if (w) {
      this.h.onSelect(segId, { seek: false });
      this.h.onWord(segId, Number(w.dataset.s));
      return;
    }
    this.h.onSelect(segId, { seek: true });
  }

  // ---- 一行 ----
  _wordsFor(seg) {
    const words = this.words[seg.source];
    if (!words?.length) return null;
    const out = [];
    for (let i = wordIndexAt(words, seg.start - 0.05); i < words.length; i++) {
      const w = words[i];
      const mid = (w.start + w.end) / 2;
      if (w.start === w.end && out.length) { out.push(w); continue; } // 标点跟着前一个词走
      if (mid < seg.start) continue;
      if (mid >= seg.end) break;
      out.push(w);
    }
    return out.length ? out : null;
  }

  _fill(row, seg, index) {
    const cut = seg.action === "cut";
    const unc = isUncertain(seg);
    const hasText = !!seg.text;
    const plain = !cut && !unc && seg.reason === "speech";
    row.className = ["row", cut ? "is-cut" : "is-keep", unc && "is-uncertain", seg.locked && "is-locked", hasText ? "has-text" : "no-text", plain && "is-plain"]
      .filter(Boolean).join(" ");
    const dur = seg.end - seg.start;
    const label = hasText ? seg.text : `${REASONS[seg.reason] ?? seg.reason} ${dur.toFixed(dur < 10 ? 2 : 1)}s`;
    const pct = Math.round(seg.confidence * 100);

    row.setAttribute("aria-label", [
      `第 ${index + 1} 条`, `${fmtTime(seg.start)} 到 ${fmtTime(seg.end)}`,
      cut ? "建议剪掉" : "保留", unc && "拿不准", REASONS[seg.reason], (cut || unc) && `把握度 ${pct}%`,
      seg.locked && "已锁定", hasText ? seg.text : "无文字",
    ].filter(Boolean).join("，"));

    // 文字
    const text = h("p", { class: "row-text" });
    row._wordEls = null;
    const ws = hasText ? this._wordsFor(seg) : null;
    if (ws) {
      row._wordEls = [];
      let prevText = "";
      for (const w of ws) {
        if (prevText && !CJK.test(prevText.slice(-1)) && !CJK.test(w.text[0] ?? "") && !/^[,.!?;:，。！？；：、)\]]/.test(w.text)) text.append(" ");
        const el = h("span", { class: "w", dataset: { s: w.start.toFixed(3) } }, w.text);
        text.append(el);
        row._wordEls.push({ start: w.start, el });
        prevText = w.text;
      }
    } else {
      text.textContent = label;
    }

    // 说明行：理由 · 把握度 · detail
    const meta = [];
    if (cut) meta.push(h("span", { class: "tag tag-cut" }, GLYPH.cut, " 剪掉"));
    if (unc) meta.push(h("span", { class: "tag tag-warn" }, GLYPH.warn, " 拿不准"));
    if (!plain && hasText) meta.push(h("span", { class: "reason" }, REASONS[seg.reason] ?? seg.reason)); // 没有文字时，上面那行标签已经是理由
    if ((cut || unc) && seg.confidence < 1) meta.push(h("span", { class: "mono conf", title: "机器对这个决定的把握度" }, `${pct}%`));
    if (seg.detail && !plain) meta.push(h("span", { class: "detail" }, seg.detail)); // 普通说话的 detail（“说话 0.76s”）只是噪声
    if (seg.locked) meta.push(h("span", { class: "tag tag-lock" }, GLYPH.lock, " 已锁定"));
    if (seg.evidence?.edited) meta.push(h("span", { class: "tag tag-edit" }, "已改"));
    const metaEl = meta.length ? h("p", { class: "row-meta" }, ...meta.flatMap((m, i) => (i ? [h("span", { class: "dot", "aria-hidden": "true" }, "·"), m] : [m]))) : null;

    // 时间
    const time = h("div", { class: "row-time mono" },
      h("time", {}, fmtTime(seg.start, { ms: hasText ? 0 : 1 })),
    );

    // 操作
    const actions = h("div", { class: "row-actions" });
    if (unc) {
      actions.append(
        h("button", { class: "btn btn-sm", dataset: { act: "keep" }, "aria-pressed": String(!cut), "aria-label": "保留这一段" }, "保留"),
        h("button", { class: "btn btn-sm", dataset: { act: "cut" }, "aria-pressed": String(cut), "aria-label": "剪掉这一段" }, "剪掉"),
      );
    } else {
      actions.append(h("button", { class: "btn btn-sm", dataset: { act: "toggle" }, "aria-label": cut ? "改回保留（X）" : "剪掉这一段（X）", title: "X" }, cut ? "改回保留" : "剪掉"));
    }
    actions.append(
      h("button", { class: "btn btn-sm btn-quiet", dataset: { act: "audition" }, "aria-label": "试听这一段及前后各 1 秒（Enter）", title: "Enter" }, GLYPH.play, " 试听"),
      h("button", { class: "btn btn-sm btn-quiet", dataset: { act: "lock" }, "aria-pressed": String(!!seg.locked), "aria-label": seg.locked ? "取消锁定（L）" : "锁定：重新生成方案时保持不变（L）", title: "L" }, seg.locked ? "解锁" : "锁定"),
    );

    row.replaceChildren(time, h("div", { class: "row-bar", "aria-hidden": "true" }), h("div", { class: "row-body" }, text, metaEl), actions);
  }
}
