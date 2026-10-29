// 审阅页的纯逻辑：时间映射、播放列表、边界移动、撤销栈。不碰 DOM，可以直接用 `node --test` 测。
// 规则和 src/autocut/plan.py 保持一致（move_boundary、set_action、Output.timeline 等）；
// 前端只做即时反馈，保存时由服务端用 Plan.from_dict 再校验一遍。

export const UNCERTAIN_BELOW = 0.75;
export const MIN_SEG = 0.01; // 片段最短 10ms，和 plan.move_boundary 一致
const JOIN_EPS = 1e-6;

export const REASONS = {
  speech: "说话",
  pause: "自然停顿",
  silence: "长停顿",
  retake: "重说",
  filler: "口头禅",
  action: "无解说但有看点",
  idle: "无解说也无看点",
  highlight: "高光",
  lead_in: "铺垫",
  beat: "卡点",
  syllable: "音节",
  shot: "镜头",
  image: "配图",
  manual: "手动",
};

export class PlanError extends Error {}

// 报错文字要和 plan.py 逐字一致（用 repr 风格的单引号），所以这里模仿 Python 的 repr / {:.3f}
const pyRepr = (x) => (Array.isArray(x) ? `[${x.map(pyRepr).join(", ")}]` : typeof x === "string" ? `'${x}'` : String(x));
const f3 = (x) => (Number.isFinite(x) ? x.toFixed(3) : x > 0 ? "inf" : "-inf");

// ---------- 规范化 ----------

const SEGMENT_DEFAULTS = {
  action: "keep",
  reason: "speech",
  confidence: 1,
  detail: "",
  text: "",
  locked: false,
  slot: null,
  speed: 1,
  pitch: 0,
  reverse: false,
  mute: false,
};

/** 服务端的 to_dict() 会去掉空字段；这里补回默认值，之后的代码就不用到处写 `?? `。深拷贝，不改入参。 */
export function normalizePlan(raw) {
  const plan = JSON.parse(JSON.stringify(raw));
  plan.options ??= {};
  plan.notes ??= [];
  plan.sources ??= {};
  for (const [id, s] of Object.entries(plan.sources)) s.id ??= id;
  for (const o of plan.outputs ?? []) {
    o.segments ??= [];
    o.selected ??= true;
    o.title ??= "";
    o.reasons ??= [];
    o.score ??= null;
    for (const s of o.segments) {
      for (const [k, v] of Object.entries(SEGMENT_DEFAULTS)) s[k] ??= v;
      s.evidence ??= {};
    }
  }
  return plan;
}

export function isUncertain(seg) {
  return seg.confidence < UNCERTAIN_BELOW;
}

/** "决定" = 计划偏离素材原样的地方：剪掉的、拿不准的、理由不是普通说话/自然停顿的保留段。 */
export function isDecision(seg) {
  return seg.action === "cut" || isUncertain(seg) || (seg.reason !== "speech" && seg.reason !== "pause");
}

// ---------- 时间轴 ----------

export function segOutDuration(seg) {
  if (seg.slot != null) return seg.slot;
  return (seg.end - seg.start) / (seg.speed || 1);
}

export function keptSegments(output) {
  return output.segments.filter((s) => s.action === "keep");
}

/** 保留片段在输出时间轴上的位置：[{seg, o0, o1}]，对应 Output.timeline()。 */
export function timeline(output) {
  const out = [];
  let t = 0;
  for (const seg of keptSegments(output)) {
    const d = segOutDuration(seg);
    out.push({ seg, o0: t, o1: t + d });
    t += d;
  }
  return out;
}

export function outputDuration(output) {
  return keptSegments(output).reduce((a, s) => a + segOutDuration(s), 0);
}

/**
 * 素材时间 → 输出时间。落在保留片段里返回精确值；落在被剪掉的部分时，返回播放恢复处的输出时间并标 kept=false。
 * 对应 plan.to_output_time（那个函数对剪掉的部分返回 None）。
 */
export function mapSourceTime(output, sourceId, t) {
  const tl = timeline(output);
  let next = null;
  for (const { seg, o0, o1 } of tl) {
    if (seg.source !== sourceId) continue;
    if (t >= seg.start && t <= seg.end) {
      return { out: Math.min(o0 + (t - seg.start) / (seg.speed || 1), o1), kept: true };
    }
    if (seg.start > t && (next === null || seg.start < next.start)) next = { start: seg.start, out: o0 };
  }
  if (next) return { out: next.out, kept: false };
  return { out: tl.length ? tl[tl.length - 1].o1 : 0, kept: false };
}

export function toOutputTime(output, sourceId, t) {
  const m = mapSourceTime(output, sourceId, t);
  return m.kept ? m.out : null;
}

/** 输出时间 → 素材位置：{seg, source, t, o0, o1}；超出范围返回 null（恰好等于总时长算最后一个片段的末尾）。 */
export function fromOutputTime(output, ot) {
  const tl = timeline(output);
  if (!tl.length || ot < 0) return null;
  for (let i = 0; i < tl.length; i++) {
    const { seg, o0, o1 } = tl[i];
    const last = i === tl.length - 1;
    if (ot < o1 || (last && ot <= o1 + 1e-9)) {
      const t = Math.min(seg.start + (ot - o0) * (seg.speed || 1), seg.end);
      return { seg, source: seg.source, t, o0, o1 };
    }
  }
  return null;
}

// ---------- 播放列表与跳过列表 ----------

/**
 * 按方案播放用的播放列表：保留片段按输出顺序排列，首尾相接（同素材、同速度）的合并成一项，这样连续播放不会 seek。
 * 图片素材没法用 <video> 播放，跳过（输出时间仍然按 timeline 计，所以 o0/o1 保持完整）。
 */
export function buildPlaylist(output, sources = {}) {
  const entries = [];
  for (const { seg, o0, o1 } of timeline(output)) {
    if (sources[seg.source]?.kind === "image") continue;
    const end = seg.slot != null ? Math.min(seg.end, seg.start + seg.slot * (seg.speed || 1)) : seg.end;
    const prev = entries[entries.length - 1];
    const plain = seg.slot == null && !seg.reverse;
    if (
      prev && prev.plain && plain && prev.source === seg.source && Math.abs(prev.end - seg.start) < 1e-3 &&
      prev.speed === seg.speed && prev.mute === seg.mute
    ) {
      prev.end = end;
      prev.o1 = o1;
      prev.segIds.push(seg.id);
      continue;
    }
    entries.push({
      source: seg.source, start: seg.start, end, o0, o1,
      speed: seg.speed || 1, mute: !!seg.mute, plain, segIds: [seg.id],
    });
  }
  return entries;
}

/** 播放时需要 seek 的位置：到 `at`（素材时间）时跳到 `to`。相邻保留段首尾相接的不会出现在这里。 */
export function skipList(output, sources = {}) {
  const pl = buildPlaylist(output, sources);
  const jumps = [];
  for (let i = 0; i + 1 < pl.length; i++) {
    const a = pl[i], b = pl[i + 1];
    if (a.source === b.source && Math.abs(a.end - b.start) < 1e-3) continue;
    jumps.push({ source: a.source, at: a.end, to: b.start, toSource: b.source, outTime: a.o1 });
  }
  return jumps;
}

/**
 * 素材时间 t 对应播放列表的哪一项。优先包含 t 的项（有多项时取 hint 之后最近的）；
 * t 落在被剪掉的部分时返回下一项（调用方应该 seek 到它的 start）；t 在所有项之后返回 -1。
 */
export function locatePlaylist(playlist, source, t, hint = 0) {
  let best = -1, bestDist = Infinity;
  for (let i = 0; i < playlist.length; i++) {
    const e = playlist[i];
    if (e.source !== source) continue;
    if (t >= e.start && t < e.end) {
      const dist = i >= hint ? i - hint : playlist.length + (hint - i);
      if (dist < bestDist) { best = i; bestDist = dist; }
    }
  }
  if (best >= 0) return { index: best, inside: true };
  let next = -1, nextStart = Infinity;
  for (let i = 0; i < playlist.length; i++) {
    const e = playlist[i];
    if (e.source === source && e.start >= t && e.start < nextStart) { next = i; nextStart = e.start; }
  }
  return { index: next, inside: false };
}

// ---------- 修改（和 plan.py 的 set_action / move_boundary 一致）----------

const SEG_FIELDS = ["action", "confidence", "locked", "start", "end", "evidence"];

function snap(seg, fields) {
  const o = {};
  for (const f of fields) o[f] = JSON.parse(JSON.stringify(seg[f]));
  return o;
}

export function findSegment(output, segId) {
  const i = output.segments.findIndex((s) => s.id === segId);
  if (i < 0) {
    const ids = output.segments.slice(0, 5).map((s) => s.id);
    throw new PlanError(`输出 ${pyRepr(output.id)} 里没有 id 为 ${pyRepr(segId)} 的片段；可用的 id 例如 ${pyRepr(ids)}`);
  }
  return [i, output.segments[i]];
}

/** 一个 change 是一次可撤销的最小改动：{output, seg, before, after}；seg 为 null 时改的是输出本身。 */
function change(outputId, segId, before, after) {
  return { output: outputId, seg: segId, before, after };
}

export function setAction(output, segId, action) {
  if (action !== "keep" && action !== "cut") throw new PlanError(`action 只能是 ('keep', 'cut')，收到 ${pyRepr(action)}`);
  const [, seg] = findSegment(output, segId);
  const before = snap(seg, ["action", "confidence", "evidence"]);
  seg.action = action; // reason 保留机器当初的判断，方便事后对照
  seg.confidence = 1; // 人确认过的决定不再算"拿不准"
  seg.evidence.edited = true;
  return [change(output.id, segId, before, snap(seg, ["action", "confidence", "evidence"]))];
}

export function setLocked(output, segId, locked) {
  const [, seg] = findSegment(output, segId);
  const before = snap(seg, ["locked"]);
  seg.locked = !!locked;
  return [change(output.id, segId, before, snap(seg, ["locked"]))];
}

export function setSelected(output, selected) {
  const before = { selected: output.selected };
  output.selected = !!selected;
  return [change(output.id, null, before, { selected: output.selected })];
}

/** 移动边界的合法范围，以及相邻片段是否与它首尾相接（相接则一起动）。规则逐行对应 plan.move_boundary。 */
export function boundaryRange(output, sources, segId, edge) {
  if (edge !== "start" && edge !== "end") throw new PlanError(`edge 只能是 'start' 或 'end'，收到 ${pyRepr(edge)}`);
  const [i, seg] = findSegment(output, segId);
  const nbIndex = edge === "start" ? i - 1 : i + 1;
  const nb = nbIndex >= 0 && nbIndex < output.segments.length ? output.segments[nbIndex] : null;
  const old = seg[edge];
  const joined = nb !== null && nb.source === seg.source && Math.abs((edge === "start" ? nb.end : nb.start) - old) < JOIN_EPS;
  let lo, hi;
  if (edge === "start") {
    lo = joined ? nb.start + MIN_SEG : 0;
    hi = seg.end - MIN_SEG;
  } else {
    lo = seg.start + MIN_SEG;
    hi = joined ? nb.end - MIN_SEG : sources[seg.source]?.duration || Infinity;
  }
  return { lo, hi, joined, seg, nb };
}

export function round4(t) {
  return Number(t.toFixed(4));
}

export function moveBoundary(output, sources, segId, edge, t) {
  const { lo, hi, joined, seg, nb } = boundaryRange(output, sources, segId, edge);
  if (!(lo <= t && t <= hi)) {
    throw new PlanError(`${segId}.${edge}=${f3(t)} 超出允许范围 [${f3(lo)}, ${f3(hi)}]`);
  }
  const nbEdge = edge === "start" ? "end" : "start";
  const changes = [];
  const before = snap(seg, [edge, "evidence"]);
  const nbBefore = joined ? snap(nb, [nbEdge]) : null;
  seg[edge] = round4(t);
  if (joined) nb[nbEdge] = round4(t);
  seg.evidence.edited = true;
  changes.push(change(output.id, segId, before, snap(seg, [edge, "evidence"])));
  if (joined) changes.push(change(output.id, nb.id, nbBefore, snap(nb, [nbEdge])));
  return changes;
}

/** 拖动时用：把目标时间夹进合法范围再移动（Python 那边是报错，交互上夹住更顺手）。 */
export function moveBoundaryClamped(output, sources, segId, edge, t) {
  const { lo, hi } = boundaryRange(output, sources, segId, edge);
  return moveBoundary(output, sources, segId, edge, Math.min(Math.max(t, lo), hi));
}

// ---------- 撤销栈 ----------

export function applyChange(plan, ch, direction) {
  const out = plan.outputs.find((o) => o.id === ch.output);
  if (!out) return;
  const target = ch.seg === null ? out : out.segments.find((s) => s.id === ch.seg);
  if (!target) return;
  const values = JSON.parse(JSON.stringify(ch[direction]));
  Object.assign(target, values);
}

/** 把后一次改动合并进前一次（连续微调、拖动）：每个 (output, seg) 保留最早的 before 和最新的 after。 */
function mergeChanges(prev, next) {
  const merged = prev.map((c) => ({ ...c, before: { ...c.before }, after: { ...c.after } }));
  for (const n of next) {
    const hit = merged.find((c) => c.output === n.output && c.seg === n.seg);
    if (hit) {
      for (const k of Object.keys(n.before)) if (!(k in hit.before)) hit.before[k] = n.before[k];
      Object.assign(hit.after, n.after);
    } else {
      merged.push({ ...n, before: { ...n.before }, after: { ...n.after } });
    }
  }
  return merged;
}

export class UndoStack {
  constructor({ limit = 500, now = () => Date.now() } = {}) {
    this.limit = limit;
    this.now = now;
    this.past = [];
    this.future = [];
  }

  get canUndo() { return this.past.length > 0; }
  get canRedo() { return this.future.length > 0; }

  /** key + windowMs：同一个 key 在窗口内连续 push 会合并成一步（按住 `]` 连续微调只算一次撤销）。 */
  push(label, changes, { key = null, windowMs = 900 } = {}) {
    if (!changes.length) return;
    const t = this.now();
    const top = this.past[this.past.length - 1];
    if (key && top && top.key === key && t - top.t < windowMs && this.future.length === 0) {
      top.changes = mergeChanges(top.changes, changes);
      top.t = t;
      return;
    }
    this.past.push({ label, changes, key, t });
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
  }

  /** 撤销：对 plan 应用最近一步的 before；返回那一步（调用方据此刷新界面），没有可撤销的返回 null。 */
  undo(plan) {
    const e = this.past.pop();
    if (!e) return null;
    for (const c of [...e.changes].reverse()) applyChange(plan, c, "before");
    this.future.push(e);
    return e;
  }

  redo(plan) {
    const e = this.future.pop();
    if (!e) return null;
    for (const c of e.changes) applyChange(plan, c, "after");
    this.past.push({ ...e, key: null }); // 重做之后不再和后续操作合并
    return e;
  }

  clear() {
    this.past = [];
    this.future = [];
  }
}

// ---------- 比较与统计 ----------

const DIFF_FIELDS = ["action", "start", "end", "locked", "speed", "pitch", "mute", "reverse", "slot"];

/** 相对基线改了多少处：片段（按 id 对应）任一关键字段不同算一处，输出的"是否选中"变了算一处。 */
export function diffCount(base, cur) {
  let n = 0;
  for (const o of cur.outputs) {
    const bo = base.outputs.find((x) => x.id === o.id);
    if (!bo) continue;
    if (bo.selected !== o.selected) n++;
    const bsegs = new Map(bo.segments.map((s) => [s.id, s]));
    for (const s of o.segments) {
      const b = bsegs.get(s.id);
      if (!b) { n++; continue; }
      if (DIFF_FIELDS.some((f) => b[f] !== s[f])) n++;
    }
  }
  return n;
}

export function planStats(plan, output) {
  const cut = output.segments.filter((s) => s.action === "cut");
  const srcIds = new Set(output.segments.map((s) => s.source));
  const spanFromSegs = output.segments.reduce((a, s) => a + (s.end - s.start), 0);
  let span = spanFromSegs;
  if (srcIds.size === 1) {
    const d = plan.sources[[...srcIds][0]]?.duration;
    if (d && cut.length) span = d; // 剪辑类输出的 segments 覆盖整段素材，用素材时长更直观
  }
  const removed = cut.reduce((a, s) => a + (s.end - s.start), 0);
  const dur = outputDuration(output);
  return {
    span, duration: dur, removed,
    removedPct: span > 0 ? removed / span : 0,
    cutCount: cut.length,
    uncertain: output.segments.filter(isUncertain).length,
    cutByReason: cut.reduce((m, s) => ((m[s.reason] = (m[s.reason] || 0) + 1), m), {}),
  };
}

/** 方案是否应该用候选清单模式：有多个输出，或者任一输出带 score。 */
export function isCandidateMode(plan) {
  return plan.outputs.length > 1 || plan.outputs.some((o) => o.score != null);
}

// ---------- 竖屏裁切 ----------

/** reframe 在输出时间 ot 处的裁切中心 {cx, cy}（0–1）。mode=center 或没有关键帧时取正中；关键帧线性插值。 */
export function reframeCenter(reframe, ot) {
  const kf = reframe?.keyframes ?? [];
  if (!reframe || reframe.mode === "center" || !kf.length) return { cx: 0.5, cy: 0.5 };
  const k = [...kf].sort((a, b) => a[0] - b[0]);
  if (ot <= k[0][0]) return { cx: k[0][1], cy: k[0][2] };
  const last = k[k.length - 1];
  if (ot >= last[0]) return { cx: last[1], cy: last[2] };
  for (let i = 0; i + 1 < k.length; i++) {
    const [t0, x0, y0] = k[i], [t1, x1, y1] = k[i + 1];
    if (ot >= t0 && ot <= t1) {
      const f = t1 === t0 ? 0 : (ot - t0) / (t1 - t0);
      return { cx: x0 + (x1 - x0) * f, cy: y0 + (y1 - y0) * f };
    }
  }
  return { cx: last[1], cy: last[2] };
}

/** 从 srcW×srcH 的画面里按输出宽高比裁出的矩形（相对坐标 0–1）：{x, y, w, h}。 */
export function cropRect(srcW, srcH, outAspect, cx = 0.5, cy = 0.5) {
  const srcAspect = srcW / srcH;
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  if (outAspect < srcAspect) {
    const w = outAspect / srcAspect;
    return { x: clamp(cx - w / 2, 0, 1 - w), y: 0, w, h: 1 };
  }
  const h = srcAspect / outAspect;
  return { x: 0, y: clamp(cy - h / 2, 0, 1 - h), w: 1, h };
}

// ---------- 候选的开头/结尾微调 ----------

/** 候选的开头 = 第一个保留片段的起点，结尾 = 最后一个保留片段的终点。 */
export function trimOutput(output, sources, which, delta) {
  const kept = keptSegments(output);
  if (!kept.length) throw new PlanError(`输出 ${pyRepr(output.id)} 没有保留的片段，没法调整${which === "start" ? "开头" : "结尾"}`);
  const seg = which === "start" ? kept[0] : kept[kept.length - 1];
  const edge = which === "start" ? "start" : "end";
  return moveBoundaryClamped(output, sources, seg.id, edge, seg[edge] + delta);
}

// ---------- 其它 ----------

/** 时间码：< 1 小时 mm:ss，否则 h:mm:ss；ms=true 时带一位或三位小数。 */
export function fmtTime(t, { ms = 0 } = {}) {
  if (!Number.isFinite(t)) return "--:--";
  const neg = t < 0;
  let x = Math.abs(t);
  const scale = 10 ** ms;
  x = Math.round(x * scale) / scale;
  const h = Math.floor(x / 3600);
  const m = Math.floor((x % 3600) / 60);
  const s = x - h * 3600 - m * 60;
  const sec = ms ? s.toFixed(ms).padStart(3 + ms, "0") : String(Math.floor(s)).padStart(2, "0");
  const body = h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${String(m).padStart(2, "0")}:${sec}`;
  return neg ? "-" + body : body;
}

/** 二分查找：words 按 start 升序，返回第一个 end > t 的下标。 */
export function wordIndexAt(words, t) {
  let lo = 0, hi = words.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].end > t) hi = mid; else lo = mid + 1;
  }
  return lo;
}

const CJK = /[　-鿿＀-￯]/;

/** 把落在 [start, end) 内的词拼成文本。中文不加空格，英文词之间加空格。 */
export function wordsText(words) {
  let out = "";
  for (const w of words) {
    if (out && !CJK.test(out.slice(-1)) && !CJK.test(w.text[0] ?? "") && !/^[,.!?;:，。！？；：、)\]]/.test(w.text)) out += " ";
    out += w.text;
  }
  return out;
}

/** 节奏档位：recipe-cut 的取值表。第一项是显示名，第二项是命令行取值。 */
export const PACE_LEVELS = [
  { id: "compact", label: "紧凑" },
  { id: "natural", label: "自然" },
  { id: "relaxed", label: "从容" },
];
const PACE_ALIASES = { tight: "compact", fast: "compact", 紧凑: "compact", 自然: "natural", normal: "natural", loose: "relaxed", slow: "relaxed", 从容: "relaxed" };

export function paceId(value) {
  const v = String(value ?? "").toLowerCase();
  return PACE_ALIASES[v] ?? v;
}

/** 点击波形/峰值数据的降采样：把 peaks（每秒 per_sec 个）压到 width 个桶，每桶取最大值。 */
export function bucketPeaks(peaks, perSec, t0, t1, width) {
  const out = new Float32Array(width);
  if (width <= 0 || t1 <= t0) return out;
  const span = (t1 - t0) * perSec;
  const i0 = t0 * perSec;
  for (let x = 0; x < width; x++) {
    const a = Math.max(0, Math.floor(i0 + (x / width) * span));
    const b = Math.min(peaks.length, Math.max(a + 1, Math.ceil(i0 + ((x + 1) / width) * span)));
    let m = 0;
    for (let i = a; i < b; i++) if (peaks[i] > m) m = peaks[i];
    out[x] = m;
  }
  return out;
}
