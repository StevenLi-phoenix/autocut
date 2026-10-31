// 文字稿栏：按片段渲染，像一份带修订标记的稿子。
// 保留的正常显示；剪掉的画删除线并写明 理由 · 把握度 · detail；拿不准的加 ⚠，并按"拿不准的是什么"给不同的操作：
//   动作拿不准 → [保留]/[剪掉]；切点拿不准 → [试听 / 多留 / 少留 / 就这样]；整段证据弱 → [就这样]。
// 状态同时用形状（✂ ⚠）、线条（删除线/虚线）和文字表达，不只靠颜色。

import { REASONS, canKeepMoreLess, editState, fmtTime, isUncertain, uncertaintyKind, wordIndexAt } from "./model.js";
import { GLYPH, h, reducedMotion } from "./ui.js";

const CJK = /[　-鿿＀-￯]/;
const EDGE_NAME = { start: "开头", end: "结尾" };

export class Transcript {
  /**
   * handlers:
   *   onSelect(segId, {seek})、onAction(segId, act)、onWord(segId, t)
   *   baseSeg(segId) 本次打开时的同一片段；mark(segId) agent/节奏改过的说明（没有返回 null）；
   *   conflict(segId) 是否在待解决的冲突里；seen(segId) 抽查时是否看过；candidate() 是否候选清单模式
   * act：keep / cut / toggle / audition / lock / confirm / more / less / edge:<start|end>:<audition|more|less>
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
    this.pinned = new Set(); // 在"拿不准"筛选下刚处理过的行：留在当前视图里（变淡，标"已确认"），直到换筛选
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
    this.pinned.clear();
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
    if (i < 0) return;
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

  /**
   * 让行出现在可见范围内。窄屏/大字号下文字稿跟着页面滚动（不做嵌套滚动），
   * 这时用 scrollIntoView，顶部被固定的小窗挡住的部分由 CSS 的 scroll-margin-top 让出来。
   */
  ensureVisible(row, mode = "nearest") {
    const s = this.scroller;
    const behavior = reducedMotion() ? "auto" : "smooth";
    if (getComputedStyle(s).overflowY === "visible") {
      row.scrollIntoView({ block: mode === "center" ? "center" : "nearest", behavior });
      return;
    }
    const r = row.getBoundingClientRect(), b = s.getBoundingClientRect();
    let top = null;
    if (mode === "center") top = s.scrollTop + (r.top - b.top) - (b.height / 2 - r.height / 2);
    else if (r.top < b.top + 8) top = s.scrollTop - (b.top + 8 - r.top) - 24;
    else if (r.bottom > b.bottom - 8) top = s.scrollTop + (r.bottom - b.bottom + 8) + 24;
    if (top != null) s.scrollTo({ top: Math.max(0, top), behavior });
  }

  // ---- 筛选 ----
  applyFilter(filter) {
    if (filter.kind !== this.filter.kind || filter.reason !== this.filter.reason) this.pinned.clear();
    this.filter = filter;
    if (!this.output) return 0;
    let n = 0;
    this.output.segments.forEach((seg) => { if (this._passFilter(this.rows.get(seg.id), seg)) n++; });
    return n;
  }

  /** 刚处理过的行：在"拿不准"筛选下留在视图里（#8：不要让刚改的那一行消失）。 */
  pin(segId) {
    if (this.filter.kind === "uncertain") this.pinned.add(segId);
  }

  _passFilter(row, seg) {
    const { kind, reason } = this.filter;
    let ok = true;
    if (kind === "uncertain") ok = isUncertain(seg);
    else if (kind === "cut") ok = seg.action === "cut";
    else if (kind === "keep") ok = seg.action === "keep";
    else if (kind === "marked") ok = !!this.h.mark?.(seg.id) || !!this.h.conflict?.(seg.id);
    if (ok && reason) ok = seg.reason === reason && seg.action === "cut";
    const pinned = !ok && this.pinned.has(seg.id);
    row.hidden = !ok && !pinned;
    row.classList.toggle("is-resolved", pinned);
    return ok || pinned;
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
    const base = this.h.baseSeg?.(seg.id);
    const es = editState(seg, base); // changed | confirmed | null
    const mark = this.h.mark?.(seg.id);
    const conflict = this.h.conflict?.(seg.id);
    const seen = this.h.seen?.(seg.id);
    const candidate = !!this.h.candidate?.();
    const uk = unc ? uncertaintyKind(seg, { candidate }) : null;
    row.className = ["row", cut ? "is-cut" : "is-keep", unc && "is-uncertain", seg.locked && "is-locked", hasText ? "has-text" : "no-text", plain && "is-plain", mark && "is-marked", mark && mark.startsWith("节奏") && "is-marked-pace", conflict && "is-conflict"]
      .filter(Boolean).join(" ");
    const dur = seg.end - seg.start;
    const label = hasText ? seg.text : `${REASONS[seg.reason] ?? seg.reason} ${dur.toFixed(dur < 10 ? 2 : 1)}s`;
    const pct = Math.round(seg.confidence * 100);
    const basePct = base ? Math.round(base.confidence * 100) : null;

    row.setAttribute("aria-label", [
      `第 ${index + 1} 条`, `从 ${fmtTime(seg.start, { ms: 1 })} 起，时长 ${dur.toFixed(1)} 秒`,
      cut ? "建议剪掉" : "保留", unc && "拿不准", REASONS[seg.reason], (cut || unc) && `把握度 ${pct}%`,
      es === "changed" && "你改过", es === "confirmed" && "你确认过", mark && mark, conflict && "和 agent 的修改冲突，待选择",
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

    // 说明行：状态标签 · 理由 · 把握度 · detail（改过以后：当前值 + 机器原判）
    const meta = [];
    if (cut) meta.push(h("span", { class: "tag tag-cut" }, GLYPH.cut, " 剪掉"));
    if (unc) meta.push(h("span", { class: "tag tag-warn" }, GLYPH.warn, " 拿不准"));
    if (es === "changed") meta.push(h("span", { class: "tag tag-edit" }, "已改"));
    else if (es === "confirmed") meta.push(h("span", { class: "tag tag-ok" }, GLYPH.check, " 已确认"));
    if (mark) meta.push(h("span", { class: "tag tag-agent", title: mark }, mark));
    if (conflict) meta.push(h("span", { class: "tag tag-conflict" }, GLYPH.warn, " 待选：你和 agent 都改了"));
    if (seen) meta.push(h("span", { class: "tag tag-seen" }, GLYPH.check, " 已抽查"));
    if (!plain && hasText) meta.push(h("span", { class: "reason" }, REASONS[seg.reason] ?? seg.reason));
    if ((cut || unc) && seg.confidence < 1) meta.push(h("span", { class: "mono conf", title: "机器对这个决定的把握度" }, `${pct}%`));
    else if (es && basePct != null && base.confidence < 1) meta.push(h("span", { class: "mono conf muted", title: "机器当初的把握度" }, `机器原判 ${basePct}%`));
    if (es === "changed") meta.push(h("span", { class: "now" }, `现在：${cut ? "剪掉" : "保留"} ${dur.toFixed(2)}s`));
    if (seg.detail && !plain) meta.push(h("span", { class: es ? "detail muted" : "detail" }, es === "changed" ? `机器原判：${seg.detail}` : seg.detail));
    if (seg.locked) meta.push(h("span", { class: "tag tag-lock" }, GLYPH.lock, " 已锁定"));
    const metaEl = meta.length ? h("p", { class: "row-meta" }, ...meta) : null;

    // 时间：整列统一一位小数，不会出现 00:14.1 → 00:15 → 00:14.7 这样看着像倒退的写法
    const time = h("div", { class: "row-time mono" }, h("time", {}, fmtTime(seg.start, { ms: 1 })));

    // 操作
    const actions = h("div", { class: "row-actions" });
    const btn = (act, label, aria, extra = {}) => h("button", { class: `btn btn-sm${extra.quiet ? " btn-quiet" : ""}`, dataset: { act }, "aria-label": aria ?? label, title: extra.title }, label);
    if (unc && uk.kind === "action") {
      actions.append(
        h("span", { class: "lean" }, `机器倾向：${cut ? "剪掉" : "保留"}`),
        btn("keep", "保留", "保留这一段"), btn("cut", "剪掉", "剪掉这一段"),
      );
    } else if (unc && uk.kind === "boundary") {
      for (const edge of uk.edges) {
        // 多留/少留按"成片里多了还是少了"命名：开头往前、结尾往后是多留
        actions.append(h("span", { class: "edge-group", role: "group", "aria-label": `${EDGE_NAME[edge]}切点` },
          h("span", { class: "lean" }, EDGE_NAME[edge]),
          btn(`edge:${edge}:audition`, `${GLYPH.play} 试听`, `试听${EDGE_NAME[edge]}切点前后`),
          btn(`edge:${edge}:more`, "多留 1s", `${EDGE_NAME[edge]}多留 1 秒`),
          btn(`edge:${edge}:less`, "少留 1s", `${EDGE_NAME[edge]}少留 1 秒`)));
      }
      actions.append(btn("confirm", "就这样", "切点就这样，确认这一处", { title: "确认这一处，不再算拿不准" }));
    } else if (unc) {
      actions.append(h("span", { class: "lean" }, "整段的依据偏弱"), btn("confirm", "就这样", "就这样，确认这一处"));
      if (candidate) actions.append(h("span", { class: "muted small" }, "不想要这一条：在右边取消选中"));
    } else {
      // 候选里的高光主体不能"剪掉整段"：不要这一条，用右边的“选中”取消
      if (!(candidate && seg.reason === "highlight")) actions.append(btn("toggle", cut ? "改回保留" : "剪掉", cut ? "改回保留（X）" : "剪掉这一段（X）", { title: "X" }));
      if (canKeepMoreLess(this.output, seg.id)) {
        actions.append(btn("more", "多留 0.5s", "多留 0.5 秒：把和前后保留部分相接的剪口挪开"), btn("less", "少留 0.5s", "少留 0.5 秒"));
      }
    }
    actions.append(
      btn("audition", `${GLYPH.play} 试听`, "试听这一段及前后各 1 秒（Enter）", { quiet: true, title: "Enter" }),
      h("button", { class: "btn btn-sm btn-quiet", dataset: { act: "lock" }, "aria-pressed": String(!!seg.locked), "aria-label": seg.locked ? "取消锁定（L）" : "锁定：改节奏时这一段不会被动（L）", title: "锁定后，改节奏时这一段不会被动（L）" }, seg.locked ? "解锁" : "锁定"),
    );

    row.replaceChildren(time, h("div", { class: "row-bar", "aria-hidden": "true" }), h("div", { class: "row-body" }, text, metaEl), actions);
  }
}
