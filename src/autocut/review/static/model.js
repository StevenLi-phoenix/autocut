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
    o.captions ??= { enabled: false, burn: false, max_chars: 16 };
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

export const clone = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)));

export function deepEqual(a, b) {
  if (a === b) return true;
  if (a == null || b == null || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => k in b && deepEqual(a[k], b[k]));
}

/**
 * 应用（direction="after"）或撤回（"before"）一个 change。
 * 字段改动：{output, seg, before, after}；output 为 null 时改的是方案本身（options、notes），seg 为 null 时改的是输出。
 * 结构改动：op="insert" / "remove"（带 data 和 index）、op="order"（before.ids / after.ids）。
 */
export function applyChange(plan, ch, direction) {
  const out = ch.output === null ? null : plan.outputs.find((o) => o.id === ch.output);
  if (ch.output !== null && !out) return;
  if (ch.op === "order") {
    const ids = ch[direction].ids;
    const rank = new Map(ids.map((id, i) => [id, i]));
    out.segments.sort((a, b) => (rank.get(a.id) ?? 1e9) - (rank.get(b.id) ?? 1e9)); // 稳定排序：不在列表里的保持原有相对顺序，排在最后
    return;
  }
  if (ch.op === "insert" || ch.op === "remove") {
    const insert = (ch.op === "insert") === (direction === "after");
    const segs = out.segments;
    const i = segs.findIndex((s) => s.id === ch.seg);
    if (insert) { if (i < 0) segs.splice(Math.min(ch.index, segs.length), 0, clone(ch.data)); }
    else if (i >= 0) segs.splice(i, 1);
    return;
  }
  const target = ch.output === null ? plan : ch.seg === null ? out : out.segments.find((s) => s.id === ch.seg);
  if (!target) return;
  Object.assign(target, clone(ch[direction]));
}

export function applyChanges(plan, changes, direction = "after") {
  const list = direction === "after" ? changes : [...changes].reverse();
  for (const c of list) applyChange(plan, c, direction);
}

/** 把后一次改动合并进前一次（连续微调、拖动）：每个 (output, seg) 保留最早的 before 和最新的 after。 */
function mergeChanges(prev, next) {
  const merged = prev.map((c) => ({ ...c, before: { ...c.before }, after: { ...c.after } }));
  for (const n of next) {
    if (n.op) { merged.push(n); continue; }
    const hit = merged.find((c) => c.output === n.output && c.seg === n.seg && !c.op);
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
  push(label, changes, { key = null, windowMs = 900, meta = null } = {}) {
    if (!changes.length) return;
    const t = this.now();
    const top = this.past[this.past.length - 1];
    if (key && top && top.key === key && t - top.t < windowMs && this.future.length === 0) {
      top.changes = mergeChanges(top.changes, changes);
      top.t = t;
      return;
    }
    this.past.push({ label, changes, key, t, meta });
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

/** 节奏档位：和 recipes/cut.py 的 PACES 一致（每个停顿保留 0.15 / 0.4 / 0.8 秒）。pace 也可能是一个秒数。 */
export const PACE_LEVELS = [
  { id: "tight", label: "紧凑", seconds: 0.15 },
  { id: "natural", label: "自然", seconds: 0.4 },
  { id: "relaxed", label: "从容", seconds: 0.8 },
];
const PACE_ALIASES = { compact: "tight", fast: "tight", 紧凑: "tight", 自然: "natural", normal: "natural", loose: "relaxed", slow: "relaxed", 从容: "relaxed" };

/** options.pace → 档位 id；给的是秒数或者不认识的值时返回 null（界面不选中任何一档）。 */
export function paceId(value) {
  const v = String(value ?? "").trim().toLowerCase();
  const id = PACE_ALIASES[v] ?? v;
  return PACE_LEVELS.some((l) => l.id === id) ? id : null;
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


// ================================================================ 确认、抽查、多留/少留

/** "就这样"：确认机器的判断，不改动作和边界。和 setAction 的区别：证据里记 confirmed 而不是 edited（#36：确认不等于改过）。 */
export function confirmSegment(output, segId) {
  const [, seg] = findSegment(output, segId);
  const before = snap(seg, ["confidence", "evidence"]);
  seg.confidence = 1;
  seg.evidence.confirmed = true;
  return [change(output.id, segId, before, snap(seg, ["confidence", "evidence"]))];
}

/**
 * 拿不准的类型：决定页面给什么操作。
 * - boundary：切点拿不准（硬切、只取了峰值附近），给"试听 / 多留 / 少留 / 就这样"，不给"保留/剪掉整段"。edges 是该动的边。
 * - content：整段是不是值得留的证据偏弱（候选里的高光等），只给"就这样"；要不要这一条由"选中"决定。
 * - action：剪/留本身拿不准（去停顿、去重说），给"保留 / 剪掉"。
 */
export function uncertaintyKind(seg, { candidate = false } = {}) {
  const d = seg.detail || "";
  const edges = [];
  if (/起点[^；。]{0,12}硬切/.test(d)) edges.push("start");
  if (/结尾[^；。]{0,12}硬切/.test(d) || /超过了最长时长/.test(d)) edges.push("end");
  if (!edges.length && /找不到不打断句子的位置|只能在电平最低处硬切/.test(d)) edges.push("start", "end");
  if (!edges.length && seg.reason === "lead_in") edges.push("start");
  if (edges.length) return { kind: "boundary", edges: [...new Set(edges)] };
  if (candidate || ["highlight", "lead_in", "shot", "beat", "syllable", "image"].includes(seg.reason)) return { kind: "content", edges: [] };
  return { kind: "action", edges: [] };
}

/** 没有"拿不准"时的抽查顺序：还没确认过的决定，按把握度从低到高；一样低的，长的在前。 */
export function spotCheckOrder(output) {
  return output.segments
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => isDecision(s) && !isUncertain(s) && !s.evidence.edited && !s.evidence.confirmed)
    .sort((a, b) => a.s.confidence - b.s.confidence || (b.s.end - b.s.start) - (a.s.end - a.s.start) || a.i - b.i)
    .map(({ s }) => s.id);
}

/**
 * 相对本次打开时的版本，这一段是"已改"（动作/边界变了）还是"已确认"（只是确认了机器的判断）。
 * base 是打开时的同一片段；没有就返回 null。
 */
export function editState(seg, base) {
  if (!base) return null;
  if (["action", "start", "end", "speed", "mute", "reverse", "slot"].some((f) => seg[f] !== base[f])) return "changed";
  const confirmed = (isUncertain(base) && !isUncertain(seg)) || (!base.evidence.confirmed && seg.evidence.confirmed) || (!base.evidence.edited && seg.evidence.edited);
  return confirmed ? "confirmed" : null;
}

/**
 * "多留 / 少留"：delta > 0 多留、< 0 少留（秒）。对剪掉的片段，动的是和相邻保留片段相接的那条边：
 * 前一个是保留且相接 → 动这段的起点；否则后一个是保留且相接 → 动这段的终点。
 */
export function keepMoreLess(output, sources, segId, delta) {
  const [i, seg] = findSegment(output, segId);
  const joined = (a, b) => a && b && a.source === b.source && Math.abs(a.end - b.start) < JOIN_EPS;
  const prev = output.segments[i - 1], next = output.segments[i + 1];
  let edge, sign;
  if (seg.action === "cut" && prev?.action === "keep" && joined(prev, seg)) { edge = "start"; sign = 1; }
  else if (seg.action === "cut" && next?.action === "keep" && joined(seg, next)) { edge = "end"; sign = -1; }
  else throw new PlanError(`${segId} 不是和保留片段相接的剪掉片段，没法“多留/少留”`);
  const { lo, hi } = boundaryRange(output, sources, segId, edge);
  const target = Math.min(Math.max(round4(seg[edge] + sign * delta), lo), hi);
  if (Math.abs(target - seg[edge]) < 1e-9) return { changes: [], edge };
  return { changes: moveBoundary(output, sources, segId, edge, target), edge };
}

/** 这一段能不能"多留/少留"（行上要不要显示这两个按钮）。 */
export function canKeepMoreLess(output, segId) {
  const i = output.segments.findIndex((s) => s.id === segId);
  const seg = output.segments[i];
  if (!seg || seg.action !== "cut") return false;
  const joined = (a, b) => a && b && a.source === b.source && Math.abs(a.end - b.start) < JOIN_EPS;
  const prev = output.segments[i - 1], next = output.segments[i + 1];
  return (prev?.action === "keep" && joined(prev, seg)) || (next?.action === "keep" && joined(seg, next));
}

// ================================================================ 字幕、成片尺寸

export function captionMode(output) {
  const c = output.captions ?? {};
  return !c.enabled ? "off" : c.burn ? "burn" : "srt";
}

/** mode: off | srt | burn → output.captions.enabled / burn（整个 captions 对象作为一条可撤销的改动）。 */
export function setCaptionMode(output, mode) {
  if (!["off", "srt", "burn"].includes(mode)) throw new PlanError(`字幕模式只能是 off / srt / burn，收到 ${pyRepr(mode)}`);
  const before = { captions: clone(output.captions) };
  output.captions = { ...output.captions, enabled: mode !== "off", burn: mode === "burn" };
  return [change(output.id, null, before, { captions: clone(output.captions) })];
}

/** 这一刻的实际字幕动作：渲染时会不会烧进画面、会不会另外写 srt（出片完成行用）。 */
export function captionLabel(mode) {
  return { off: "字幕：关", srt: "字幕：单独 srt（不烧进画面）", burn: "字幕：烧进画面" }[mode];
}

/**
 * 成片尺寸预设 → {width, height}。source = 跟随素材（清空）；vertical / horizontal = 1080×1920 / 1920×1080；
 * 720p / 1080p = 保持当前方向（竖/横），短边取 720 / 1080，长边按当前（或素材）比例，取偶数。
 */
export function sizeForPreset(output, sources, preset) {
  if (preset === "source") return { width: null, height: null };
  if (preset === "vertical") return { width: 1080, height: 1920 };
  if (preset === "horizontal") return { width: 1920, height: 1080 };
  if (preset !== "720p" && preset !== "1080p") throw new PlanError(`不认识的尺寸预设 ${pyRepr(preset)}`);
  const short = preset === "720p" ? 720 : 1080;
  const src = sources[(keptSegments(output)[0] ?? output.segments[0])?.source];
  let w = output.width ?? src?.width ?? 1920, h = output.height ?? src?.height ?? 1080;
  if (output.width && !output.height && src?.width) h = Math.round(output.width * src.height / src.width);
  const ratio = Math.max(w, h) / Math.min(w, h);
  const long = Math.round((short * ratio) / 2) * 2;
  return h > w ? { width: short, height: long } : { width: long, height: short };
}

export function setOutputSize(output, size) {
  const before = { width: output.width ?? null, height: output.height ?? null };
  output.width = size.width;
  output.height = size.height;
  return [change(output.id, null, before, { width: output.width, height: output.height })];
}

/** 用哪种尺寸预设能描述这个输出的现状（对话框里高亮用）；对不上返回 null。 */
export function sizePresetOf(output) {
  const { width: w, height: h } = output;
  if (!w && !h) return "source";
  if (w === 1080 && h === 1920) return "vertical";
  if (w === 1920 && h === 1080) return "horizontal";
  if (Math.min(w, h) === 720) return "720p";
  if (Math.min(w, h) === 1080) return "1080p";
  return null;
}

// ================================================================ 差异、三方合并

const SEG_STRUCT = "segments";

function recordDiff(a, b, skip = []) {
  const before = {}, after = {};
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (skip.includes(k)) continue;
    if (!deepEqual(a[k], b[k])) { before[k] = clone(a[k]); after[k] = clone(b[k]); }
  }
  return Object.keys(after).length ? { before, after } : null;
}

/**
 * 两份方案之间的差异。changes 可以用 applyChanges 把 a 变成 b（也能撤回）；
 * items 是给人看的明细：{output, seg, op?, fields: {字段: [前, 后]}}。
 * 只比较输出和片段（以及 options、notes）；素材路径等由其它流程处理。
 */
export function diffPlans(a, b) {
  const changes = [], items = [];
  const pd = recordDiff({ options: a.options, notes: a.notes }, { options: b.options, notes: b.notes });
  if (pd) {
    changes.push({ output: null, seg: null, before: pd.before, after: pd.after });
    items.push({ output: null, seg: null, fields: Object.fromEntries(Object.keys(pd.after).map((k) => [k, [pd.before[k], pd.after[k]]])) });
  }
  for (const bo of b.outputs) {
    const ao = a.outputs.find((o) => o.id === bo.id);
    if (!ao) continue;
    const od = recordDiff(ao, bo, [SEG_STRUCT]);
    if (od) {
      changes.push({ output: bo.id, seg: null, before: od.before, after: od.after });
      items.push({ output: bo.id, seg: null, fields: Object.fromEntries(Object.keys(od.after).map((k) => [k, [od.before[k], od.after[k]]])) });
    }
    const amap = new Map(ao.segments.map((s) => [s.id, s]));
    const bmap = new Map(bo.segments.map((s) => [s.id, s]));
    for (const bs of bo.segments) {
      const as = amap.get(bs.id);
      if (!as) continue;
      const d = recordDiff(as, bs);
      if (d) {
        changes.push({ output: bo.id, seg: bs.id, before: d.before, after: d.after });
        items.push({ output: bo.id, seg: bs.id, fields: Object.fromEntries(Object.keys(d.after).map((k) => [k, [d.before[k], d.after[k]]])) });
      }
    }
    // 删除：从后往前，撤回时（反向）正好从前往后插回去，位置不会错
    const removed = ao.segments.map((s, i) => [s, i]).filter(([s]) => !bmap.has(s.id)).reverse();
    for (const [s, i] of removed) {
      changes.push({ output: bo.id, seg: s.id, op: "remove", index: i, data: clone(s) });
      items.push({ output: bo.id, seg: s.id, op: "remove", fields: {}, data: s });
    }
    const added = bo.segments.map((s, i) => [s, i]).filter(([s]) => !amap.has(s.id));
    for (const [s, i] of added) {
      changes.push({ output: bo.id, seg: s.id, op: "insert", index: i, data: clone(s) });
      items.push({ output: bo.id, seg: s.id, op: "insert", fields: {}, data: s });
    }
    const aCommon = ao.segments.filter((s) => bmap.has(s.id)).map((s) => s.id);
    const bIds = bo.segments.map((s) => s.id);
    const bCommon = bIds.filter((id) => amap.has(id));
    if (!deepEqual(aCommon, bCommon)) {
      changes.push({ output: bo.id, seg: null, op: "order", before: { ids: ao.segments.map((s) => s.id) }, after: { ids: bIds } });
    }
  }
  return { changes, items };
}

const FIELD_NAME = { action: "动作", start: "起点", end: "终点", locked: "锁定", text: "文字", detail: "说明", reason: "理由", confidence: "把握度", speed: "速度", selected: "是否选中", reframe: "裁切框", captions: "字幕", width: "宽", height: "高", title: "标题", score: "分数", slot: "时长" };
const ACTION_NAME = { keep: "保留", cut: "剪掉" };

/** 一个字段改动的人话：动作 保留 → 剪掉；起点 3.00 → 3.50。 */
export function describeFieldChange(field, [before, after]) {
  if (field === "action") return `${ACTION_NAME[before] ?? before} → ${ACTION_NAME[after] ?? after}`;
  if ((field === "start" || field === "end") && typeof before === "number" && typeof after === "number") return `${FIELD_NAME[field]} ${before.toFixed(2)} → ${after.toFixed(2)}`;
  if (field === "locked") return after ? "锁定" : "解锁";
  if (field === "selected") return after ? "选中" : "取消选中";
  if (field === "evidence") return null; // 证据里的 edited/confirmed 标记不单独讲
  if (field === "confidence") return null;
  return `${FIELD_NAME[field] ?? field}改了`;
}

export function describeFields(fields) {
  return Object.entries(fields).map(([f, v]) => describeFieldChange(f, v)).filter(Boolean).join("，") || "改了";
}

// ---- 三方合并 ----

function merge3(b, m, t) {
  if (deepEqual(m, t)) return { value: m };
  if (deepEqual(b, m)) return { value: t, theirs: true };
  if (deepEqual(b, t)) return { value: m };
  return { value: m, conflict: true };
}

/** 相对 base，这条记录有没有被明确地"决定"过（edited / confirmed 标记新出现）。 */
function decidedSince(b, x) {
  const flag = (r, k) => !!r.evidence?.[k];
  return (!flag(b, "edited") && flag(x, "edited")) || (!flag(b, "confirmed") && flag(x, "confirmed"));
}

/** 合并一条记录（片段或输出）：evidence 往下按键合并。返回 {value, conflicts: [{path, base, mine, theirs}], theirsApplied}。 */
function mergeRecord(b, m, t, skip = []) {
  const value = clone(m);
  const conflicts = [];
  let theirsApplied = false;
  const keys = new Set([...Object.keys(b), ...Object.keys(m), ...Object.keys(t)]);
  for (const k of keys) {
    if (skip.includes(k)) continue;
    if (k === "evidence") {
      const sub = new Set([...Object.keys(b.evidence ?? {}), ...Object.keys(m.evidence ?? {}), ...Object.keys(t.evidence ?? {})]);
      value.evidence ??= {};
      for (const e of sub) {
        const r = merge3(b.evidence?.[e], m.evidence?.[e], t.evidence?.[e]);
        if (r.conflict) conflicts.push({ path: `evidence.${e}`, base: b.evidence?.[e], mine: m.evidence?.[e], theirs: t.evidence?.[e] });
        else if (r.theirs) { value.evidence[e] = clone(r.value); theirsApplied = true; }
      }
      continue;
    }
    let r = merge3(b[k], m[k], t[k]);
    // 动作只有"保留/剪掉"两个值，单看值几乎不会冲突；但两边都明确地重新决定过（edited/confirmed）而且结论相反，就是真冲突：
    // 比如我把一处改回保留，agent 同时把它标成"剪掉（已确认）"。
    if (k === "action" && !r.conflict && m.action !== t.action && decidedSince(b, m) && decidedSince(b, t)) r = { value: m, conflict: true };
    if (r.conflict) conflicts.push({ path: k, base: b[k], mine: m[k], theirs: t[k] });
    else if (r.theirs) { value[k] = clone(r.value); theirsApplied = true; }
  }
  return { value, conflicts, theirsApplied };
}

/**
 * 按片段三方合并：base 是我上次和服务器同步时的版本，mine 是我现在的（含没保存的改动），theirs 是磁盘上的新版本。
 * - 两边改了不同的片段/字段 → 自动合并，两边的改动都保留；
 * - 同一片段的同一字段两边改得不一样 → 冲突：合并结果里先保留我的值（我的改动不会丢），由人逐条选；
 * - agent 删了我改过的片段 → 冲突；agent 新增的片段 → 加进来。
 * 返回 {plan, conflicts, applied}：applied 是被合并进来的 agent 改动数（片段/输出个数）。
 * conflicts 的每一项（一组相邻的冲突片段算一项）：{key, output, segIds, kind: "edit"|"removed"|"output", items: [{seg, path, base, mine, theirs}]}。
 */
export function mergePlans(base, mine, theirs) {
  const plan = clone(mine);
  const conflicts = [];
  let applied = 0;

  // 方案级（options、notes）：只有对方改了就采用；两边都改了以对方为准（页面不编辑它们）
  for (const k of ["options", "notes"]) {
    const r = merge3(base[k], mine[k], theirs[k]);
    if (r.theirs || r.conflict) { plan[k] = clone(theirs[k]); applied++; }
  }

  for (const to of theirs.outputs) {
    const mo = plan.outputs.find((o) => o.id === to.id);
    const bo = base.outputs.find((o) => o.id === to.id);
    if (!mo) { plan.outputs.push(clone(to)); applied++; continue; }
    if (!bo) continue;
    // 输出自身的字段（是否选中、裁切框、字幕设置……）
    const strip = ({ segments: _s, ...rest }) => rest;
    const om = mergeRecord(strip(bo), strip(mo), strip(to));
    if (om.theirsApplied) applied++;
    Object.assign(mo, om.value);
    if (om.conflicts.length) conflicts.push({ key: `${to.id}:output`, output: to.id, segIds: [], kind: "output", items: om.conflicts.map((c) => ({ seg: null, ...c })) });

    const bmap = new Map(bo.segments.map((s) => [s.id, s]));
    const mmap = new Map(mo.segments.map((s) => [s.id, s]));
    const tmap = new Map(to.segments.map((s) => [s.id, s]));
    const merged = new Map();
    const segConflicts = new Map();
    for (const ts of to.segments) {
      const bs = bmap.get(ts.id), ms = mmap.get(ts.id);
      if (!ms) { merged.set(ts.id, clone(ts)); if (!bs) applied++; continue; } // agent 新增（或我没有但 agent 有）
      if (!bs) { merged.set(ts.id, ms); continue; }
      const r = mergeRecord(bs, ms, ts);
      merged.set(ts.id, r.value);
      if (r.theirsApplied) applied++;
      if (r.conflicts.length) segConflicts.set(ts.id, { kind: "edit", items: r.conflicts.map((c) => ({ seg: ts.id, ...c })) });
    }
    for (const ms of mo.segments) {
      if (tmap.has(ms.id)) continue;
      const bs = bmap.get(ms.id);
      if (bs && deepEqual(bs, ms)) { applied++; continue; } // agent 删了、我没动：跟着删
      merged.set(ms.id, ms); // 我新增的，或者我改过而 agent 删了：保留我的
      if (bs) segConflicts.set(ms.id, { kind: "removed", items: [{ seg: ms.id, path: "(整段)", base: bs, mine: ms, theirs: null }] });
    }
    // 顺序：以 agent 的为准；只有我这边有的片段，插在它在我这边的前一个片段后面
    const order = to.segments.map((s) => s.id);
    mo.segments.forEach((s, i) => {
      if (tmap.has(s.id) || !merged.has(s.id)) return;
      let at = 0;
      for (let k = i - 1; k >= 0; k--) { const p = order.indexOf(mo.segments[k].id); if (p >= 0) { at = p + 1; break; } }
      order.splice(at, 0, s.id);
    });
    mo.segments = order.map((id) => merged.get(id));

    // 相邻的冲突片段合成一组（一次剪口的两边，要选就一起选）
    let group = null;
    mo.segments.forEach((s) => {
      const c = segConflicts.get(s.id);
      if (!c) { group = null; return; }
      if (group && group.kind === c.kind) { group.segIds.push(s.id); group.items.push(...c.items); }
      else { group = { key: `${to.id}:${s.id}`, output: to.id, segIds: [s.id], kind: c.kind, items: [...c.items] }; conflicts.push(group); }
    });
  }
  return { plan, conflicts, applied };
}

/** 解决一组冲突：side="mine" 什么都不用做；side="theirs" 把对方的值写进 plan，返回可撤销的 changes。 */
export function resolveConflict(plan, group, side) {
  if (side !== "theirs") return [];
  const out = plan.outputs.find((o) => o.id === group.output);
  if (!out) return [];
  const changes = [];
  if (group.kind === "removed") {
    for (const id of group.segIds) {
      const i = out.segments.findIndex((s) => s.id === id);
      if (i >= 0) { changes.push({ output: out.id, seg: id, op: "remove", index: i, data: clone(out.segments[i]) }); out.segments.splice(i, 1); }
    }
    return changes;
  }
  const bySeg = new Map();
  for (const it of group.items) (bySeg.get(it.seg) ?? bySeg.set(it.seg, []).get(it.seg)).push(it);
  for (const [segId, items] of bySeg) {
    const target = segId === null ? out : out.segments.find((s) => s.id === segId);
    if (!target) continue;
    const before = {}, after = {};
    for (const it of items) {
      const [top, sub] = it.path.split(".");
      before[top] ??= clone(target[top]);
      if (sub) { target[top] ??= {}; if (it.theirs === undefined) delete target[top][sub]; else target[top][sub] = clone(it.theirs); }
      else target[top] = clone(it.theirs);
      after[top] = clone(target[top]);
    }
    changes.push({ output: out.id, seg: segId, before, after });
  }
  return changes;
}

/** 一组冲突里"我这边 / agent 那边"各自的一句话：用在冲突面板上。 */
export function describeConflictSide(group, side) {
  if (group.kind === "removed") return side === "theirs" ? "删除了这一段" : "保留（你改过它）";
  const pick = (it) => (side === "mine" ? it.mine : it.theirs);
  const fields = {};
  const edges = group.items.filter((it) => it.path === "start" || it.path === "end");
  const joined = edges.length >= 2 && edges.every((it) => pick(it) === pick(edges[0]) && it.base === edges[0].base); // 相接的两条边一起动：写成一个"剪口"
  for (const it of group.items) {
    const [top] = it.path.split(".");
    if (top === "evidence" || top === "confidence") continue;
    if (joined && (top === "start" || top === "end")) { fields.cut = [edges[0].base, pick(edges[0])]; continue; }
    fields[top] = [it.base, pick(it)];
  }
  const text = Object.entries(fields).map(([f, v]) => (f === "cut" ? `剪口 ${v[0].toFixed(2)} → ${v[1].toFixed(2)}` : describeFieldChange(f, v))).filter(Boolean).join("，");
  return text || "（只是确认/标记不同）";
}

// ================================================================ 体检建议 → 页面里能做的动作

/** 覆盖整段成片的体检项（响度、画幅、字幕……check.py 把它们标成 0 到时长）。它们没有"哪一秒"，不上风险时间线。 */
export function isWholeVideoItem(item, duration) {
  return item.t != null && item.t <= 0.01 && item.end != null && duration > 0 && Math.abs(item.end - duration) < 0.06;
}

/**
 * 给一条体检建议配动作。返回 [{kind, label, ...参数}]；kind：
 * jump（跳到某处）、jump-edge（选中开头/结尾那一段，边已选好）、trim（开头/结尾多留 delta 秒）、loudnorm、captions、
 * size（尺寸预设）、aspect（竖屏 9:16 / 横屏 16:9）、filter-silence、export（打开导出菜单，提示这一步在剪辑软件里做）、copy（复制命令）。
 */
export function checkActions(item, { duration = 0, captionMode: cm = "off", sourcePath = "素材.mp4" } = {}) {
  const jump = item.t != null && !isWholeVideoItem(item, duration) ? [{ kind: "jump", label: "跳到这一处", t: item.t }] : [];
  const external = [{ kind: "export", label: "在剪辑软件里做：导出工程 ▾" }];
  switch (item.code) {
    case "start_midspeech": return [{ kind: "trim", which: "start", delta: -0.3, label: "开头多留 0.3s" }, { kind: "jump-edge", which: "start", label: "去调开头" }];
    case "start_midsentence": return [{ kind: "trim", which: "start", delta: -1, label: "开头多留 1s" }, { kind: "jump-edge", which: "start", label: "去调开头" }];
    case "end_midspeech": return [{ kind: "trim", which: "end", delta: 0.4, label: "结尾多留 0.4s" }, { kind: "jump-edge", which: "end", label: "去调结尾" }];
    case "end_midsentence": return [{ kind: "trim", which: "end", delta: 1, label: "结尾多留 1s" }, { kind: "jump-edge", which: "end", label: "去调结尾" }];
    case "hook_late": case "hook_empty": return [{ kind: "jump-edge", which: "start", label: "去看开头" }, ...external];
    case "dead_air": return [...jump, { kind: "filter-silence", label: "只看被剪掉的长停顿" }];
    case "loudness_low": case "loudness_high": case "true_peak": return [{ kind: "loudnorm", label: "出片时标准化响度" }];
    case "captions_sidecar": case "captions_missing": case "captions_unknown":
      return cm === "burn" ? [] : [{ kind: "captions", mode: "burn", label: "字幕烧进画面" }];
    case "aspect": return [{ kind: "aspect", label: /竖屏平台|9:16/.test(item.message) ? "成片改成竖屏 1080×1920" : "成片改成横屏 1920×1080", preset: /竖屏平台|9:16/.test(item.message) ? "vertical" : "horizontal" }];
    case "resolution": return [{ kind: "size", preset: "720p", label: "成片尺寸设为 720p" }, { kind: "size", preset: "1080p", label: "设为 1080p" }];
    case "duration": return [{ kind: "filter-silence", label: "看哪些停顿还能再缩" }];
    case "pacing": case "static_long": return [...jump, ...external];
    case "no_audio": return external;
    case "no_words": return [{ kind: "copy", label: "复制命令", text: `autocut analyze "${sourcePath}"` }];
    default: return jump;
  }
}

/** 把 check.py 的 to_dict() 整理成页面用的结构：条目按严重度、时间排序；全片问题单独一组。 */
export function normalizeReport(rep, { fallbackDisclaimer = "这些是留存风险信号，不是爆款保证。" } = {}) {
  const arr = ["items", "findings", "risks", "issues", "signals", "checks"].map((k) => rep?.[k]).find(Array.isArray) ?? [];
  const order = { risk: 0, high: 0, critical: 0, error: 0, warn: 1, warning: 1, medium: 1, info: 2, low: 2 };
  const duration = typeof rep?.duration === "number" ? rep.duration : 0;
  const items = arr.map((x, i) => {
    const t = [x.at, x.time, x.t, x.start, x.position, x.out_start].find((v) => typeof v === "number");
    const space = x.source_time != null || x.src_start != null ? "source" : "output";
    const tt = x.source_time ?? x.src_start ?? t;
    const message = x.message ?? x.detail ?? x.text ?? x.why ?? "";
    const it = {
      i, t: typeof tt === "number" ? tt : null, end: [x.end, x.out_end, x.src_end].find((v) => typeof v === "number") ?? null, space,
      severity: String(x.severity ?? x.level ?? "info").toLowerCase(),
      code: x.id ?? x.kind ?? x.rule ?? null,
      title: x.title ?? message ?? "提示",
      message: x.title ? message : "",
      advice: x.fix ?? x.suggestion ?? x.advice ?? x.action ?? "",
    };
    it.whole = isWholeVideoItem(it, duration);
    return it;
  }).sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3) || Number(a.whole) - Number(b.whole) || (a.t ?? 1e9) - (b.t ?? 1e9));
  const disclaimer = rep?.disclaimer ?? rep?.note ?? fallbackDisclaimer;
  const summary = String(rep?.summary ?? rep?.headline ?? "").replace(disclaimer, "").trim();
  return { items, timed: items.filter((x) => x.t != null && !x.whole), whole: items.filter((x) => x.whole), summary, disclaimer, duration, raw: rep };
}


/** 成片时间 t 上正在显示的字幕（cues 按 start 升序、互不重叠）；没有返回 null。 */
export function findCue(cues, t) {
  let lo = 0, hi = cues.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cues[mid].start <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return best >= 0 && t < cues[best].end ? cues[best] : null;
}

/** 改成和平台画幅不同的尺寸时，输出需要裁切框（reframe）；没有就补一个居中的。 */
export function ensureReframe(output) {
  if (output.reframe) return [];
  const before = { reframe: null };
  output.reframe = { mode: "center", keyframes: [] };
  return [change(output.id, null, before, { reframe: clone(output.reframe) })];
}
