// 审阅页主程序：状态、保存、快捷键、两种模式（单条时间线 / 候选清单）的组装。
// 纯逻辑在 model.js（有 node --test），这里只负责把它们接到 DOM 和服务器上。

import { api, ApiError } from "./api.js";
import {
  PACE_LEVELS, UndoStack, applyChanges, boundaryRange, captionLabel, captionMode, checkActions, clone, confirmSegment, cropRect,
  describeConflictSide, describeFields, diffCount, diffPlans, fmtTime, isCandidateMode, isDecision, isUncertain, keepMoreLess,
  keptSegments, mergePlans, moveBoundary, moveBoundaryClamped, normalizePlan, normalizeReport, outputDuration, paceId, planStats,
  reframeCenter, resolveConflict, ensureReframe, setAction, setCaptionMode, setLocked, setOutputSize, setSelected, sizeForPreset, sizePresetOf,
  spotCheckOrder, trimOutput, findCue, REASONS, round4,
} from "./model.js";
import { PlanPlayer } from "./player.js";
import { Timeline } from "./timeline.js";
import { Transcript } from "./transcript.js";
import { $, $$, GLYPH, announce, basename, copyText, debounce, fill, h, menu, safeStorage, toast } from "./ui.js";

const store = safeStorage();
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const MOD = IS_MAC ? "⌘" : "Ctrl+";
const SAVE_DELAY = 800;
const NUDGE = 0.05;
const RECIPE_LABEL = { cut: "去停顿 + 去重说", highlights: "直播高光", montage: "混剪镜头", ytpmv: "鬼畜材料", narrate: "配音成片", manual: "手动" };
const FORMAT_LABEL = {
  fcpxml: ["Final Cut Pro / Resolve", "fcpxml"], xmeml: ["Premiere Pro", "xmeml"], edl: ["剪切列表", "edl"],
  srt: ["字幕", "srt"], markers: ["标记（节拍/决定）", "markers"],
};

// ---------------------------------------------------------------- 状态
const state = {
  plan: null, base: null, synced: null, meta: null, revision: 0, etag: "",
  marks: { label: "", segs: new Map(), outputs: new Map(), removed: [] }, // agent / 节奏改过的行：{key: 说明}
  conflictSegs: new Set(), // 待选冲突涉及的片段（"输出:片段"）
  spot: null, seen: new Set(), // 抽查：{order, i}；看过的片段
  renderOpts: { loudnorm: null }, cues: {}, drawer: null, baseMap: new Map(), uncertainStart: 0,
  mode: "timeline", outputId: null, selSeg: null, edge: "start",
  filter: { kind: "all", reason: null },
  playMode: "plan", follow: true, previewAll: false,
  dirty: false, editSeq: 0, saving: false, conflict: null,
  save: { status: "idle", text: "" },
  undo: new UndoStack(),
  words: {}, peaks: {}, peaksLoading: new Set(),
  job: null, check: null,
  auditionNudge: store.getItem("autocut.review.auditionNudge") !== "0",
  dismissedNotes: false,
};

const $app = $("#app");
let player, timeline, transcript;
let videoEl;
const out = () => state.plan.outputs.find((o) => o.id === state.outputId) ?? state.plan.outputs[0];
const sources = () => state.meta.sources;
const selectedSeg = () => out().segments.find((s) => s.id === state.selSeg) ?? null;
const mainSource = (o = out()) => (keptSegments(o)[0] ?? o.segments[0])?.source ?? Object.keys(state.plan.sources)[0];

// ---------------------------------------------------------------- 启动
async function boot() {
  let data;
  try {
    data = await api.plan();
  } catch (e) {
    fill($("#main"), errorPanel("打不开方案", e.message, e.code === "offline" ? null : "检查方案文件是否存在、是否是合法的 autocut 方案（autocut plan summary 可以验证）。"));
    return;
  }
  adoptServerPlan(data);
  state.mode = isCandidateMode(state.plan) ? "candidates" : "timeline";
  state.outputId = state.mode === "candidates"
    ? (rankedOutputs().find((o) => o.selected) ?? rankedOutputs()[0]).id
    : state.plan.outputs[0].id;
  state.uncertainStart = uncertainCount();
  document.title = `${basename(state.plan.sources[mainSource()]?.path) || state.meta.name} · autocut 审阅`;
  $app.dataset.mode = state.mode;
  mountMain();
  bindChrome();
  refreshHeader();
  renderHints();
  setupBanners();
  await loadOutput({ first: true });
  setInterval(pollExternal, 2500);
}

function adoptServerPlan(data) {
  state.plan = normalizePlan(data.plan);
  state.base = normalizePlan(data.plan);
  state.synced = clone(state.plan);
  state.revision = data.revision;
  state.etag = data.etag;
  state.meta = { sources: data.sources, name: data.name, path: data.path, capabilities: data.capabilities, formats: data.formats, summary: data.summary };
}

function errorPanel(title, message, hint) {
  return h("div", { class: "fatal", role: "alert" }, h("h2", {}, title), h("p", {}, message), hint && h("p", { class: "muted" }, hint));
}

// ---------------------------------------------------------------- 主区域
function mountMain() {
  const main = $("#main");
  fill(main, $(`#tpl-${state.mode}`).content.cloneNode(true));
  for (const [slot, tpl] of [["transport", "tpl-transport"], ["timeline", "tpl-timeline-block"], ["transcript", "tpl-transcript-block"]]) {
    for (const el of $$(`[data-slot="${slot}"]`, main)) el.replaceWith($(`#${tpl}`).content.cloneNode(true));
  }
  videoEl = $("#video");
  player = new PlanPlayer(videoEl);
  timeline = new Timeline({
    canvas: $("#timeline"), overview: $("#overview"), tooltip: $("#tl-tip"),
    callbacks: {
      onSeek: (t, seg) => { player.seekSource(timeline.sourceId, t); if (!seg) clearSelection(); },
      onSelect: (id) => selectSeg(id, { seek: false, scroll: true }),
      onDrag: onTimelineDrag,
      describe: (seg) => `${seg.action === "cut" ? GLYPH.cut + " 剪掉" : "保留"} · ${REASONS[seg.reason] ?? seg.reason} · ${fmtTime(seg.start, { ms: 1 })}–${fmtTime(seg.end, { ms: 1 })}${seg.text ? " · " + seg.text.slice(0, 18) : ""}`,
    },
  });
  transcript = new Transcript({
    list: $("#rows"), scroller: $("#doc-scroll"),
    handlers: {
      onSelect: (id, o) => selectSeg(id, { seek: o.seek, focus: true }), onAction: rowAction, onWord: (id, t) => player.seekSource(selectedSeg()?.source ?? mainSource(), t),
      baseSeg: (id) => state.baseMap.get(id),
      mark: (id) => state.marks.segs.get(`${state.outputId}:${id}`) ?? null,
      conflict: (id) => state.conflictSegs.has(`${state.outputId}:${id}`),
      seen: (id) => state.seen.has(`${state.outputId}:${id}`),
      candidate: () => candidateMode(),
    },
  });
  $("#doc-scroll").addEventListener("wheel", stopFollow, { passive: true });
  $("#doc-scroll").addEventListener("touchmove", stopFollow, { passive: true });
  $("#follow-btn").addEventListener("click", () => { state.follow = true; $("#follow-btn").hidden = true; const r = transcript.rowFor(transcript.now); if (r) transcript.ensureVisible(r, "center"); });
  bindPlayer();
  bindTransport();
  if (state.mode === "candidates") mountCandidates();
  else mountFilters();
}

function stopFollow() {
  if (!state.follow) return;
  state.follow = false;
  $("#follow-btn").hidden = false;
}

async function loadOutput({ first = false } = {}) {
  lastSegNow = null;
  const o = out();
  const src = mainSource(o);
  const hasText = o.segments.some((s) => s.text);
  rebuildBaseMap();
  transcript.build(o, { sources: sources(), words: state.words, hasText });
  $("#rows").classList.toggle("no-transcript", !hasText);
  $("#no-text-note").hidden = hasText;
  timeline.setData({ output: o, sources: sources(), sourceId: src, duration: sources()[src]?.duration });
  timeline.setPeaks(state.peaks[src]?.peaks ?? null, state.peaks[src]?.per_sec ?? 50);
  if (state.mode === "candidates") fitCandidateTimeline(o);
  player.setOutput(o, sources());
  updateFilterCounts();
  refreshHeader();
  renderMarksBar();
  renderSpotBar();
  loadCues();

  const info = sources()[src];
  const msg = $("#player-msg");
  if (info && info.exists) {
    msg.hidden = true;
    await player.load(src);
    ensureFrameLayout();
  } else if (info && info.kind !== "image") {
    fill(msg, h("strong", {}, "找不到素材文件，没法预览"), h("span", {}, info.abs_path), h("span", {}, "方案和文字稿仍然可以审阅和修改；在页面上方“重新定位”之后就能播放。"));
    msg.hidden = false;
  }
  if (first) {
    const firstDecision = o.segments.find((s) => isUncertain(s)) ?? o.segments.find(isDecision) ?? o.segments[0];
    if (firstDecision) selectSeg(firstDecision.id, { seek: false, scroll: false });
    if (info?.exists) player.seekSource(src, o.segments.find((s) => s.action === "keep")?.start ?? 0);
    ensurePeaks(src);
    ensureWords(src);
  } else if (state.selSeg && o.segments.some((s) => s.id === state.selSeg)) {
    transcript.setSelected(state.selSeg, { scroll: false });
    timeline.setSelection(state.selSeg, state.edge);
  } else {
    const first = o.segments[0];
    if (first) selectSeg(first.id, { seek: false, scroll: false });
  }
  syncPlayModeButtons();
}

function rebuildBaseMap() {
  const bo = state.base.outputs.find((o) => o.id === out().id);
  state.baseMap = new Map((bo?.segments ?? []).map((s) => [s.id, s]));
}

function ensurePeaks(sid) {
  const info = sources()[sid];
  if (!info?.exists || state.peaks[sid] || state.peaksLoading.has(sid) || info.kind === "image") return;
  state.peaksLoading.add(sid);
  $("#wave-state").textContent = "波形加载中…";
  api.peaks(sid, 50).then((d) => {
    state.peaks[sid] = d;
    if (timeline.sourceId === sid) timeline.setPeaks(d.peaks, d.per_sec);
    $("#wave-state").textContent = "";
  }).catch((e) => {
    $("#wave-state").textContent = "没有波形";
    $("#wave-state").title = e.message;
  }).finally(() => state.peaksLoading.delete(sid));
}

function ensureWords(sid) {
  if (state.words[sid] !== undefined || !sources()[sid]?.has_words || !out().segments.some((s) => s.text)) return;
  state.words[sid] = null;
  api.words(sid).then((d) => {
    state.words[sid] = d.words;
    transcript.words = state.words;
    transcript.updateAll();
  }).catch(() => { /* 没有逐词文字稿是正常情况：按片段文字显示 */ });
}

// ---------------------------------------------------------------- 顶栏
function refreshHeader() {
  const o = out();
  const plan = state.plan;
  const src = plan.sources[mainSource(o)];
  $("#doc-title").textContent = basename(src?.path);
  $("#doc-title").title = state.meta.path;
  $("#doc-recipe").textContent = RECIPE_LABEL[plan.recipe] ?? plan.recipe;
  const stats = $("#stats");
  if (candidateMode()) {
    const sel = plan.outputs.filter((x) => x.selected);
    const opt = plan.options ?? {};
    const len = Array.isArray(opt.length) ? ` · 每条 ${opt.length[0]}–${opt.length[1]}s` : "";
    const aspect = opt.aspect ? ` · ${opt.aspect}` : "";
    fill(stats, h("span", { class: "mono" }, `${plan.outputs.length} 个候选 · 已选 ${sel.length}${len}${aspect}`));
    $("#render-label").textContent = `出片：${sel.length} 条`;
    $("#render-btn").disabled = sel.length === 0 || !state.meta.capabilities.render;
  } else {
    const s = planStats(plan, o);
    const parts = [h("span", { class: "mono" }, o.segments.some((x) => x.action === "cut")
      ? `${fmtTime(s.span)} → ${fmtTime(s.duration)}` : `成片 ${fmtTime(s.duration)}`)];
    if (s.cutCount) parts.push(h("span", { class: "mono" }, ` · 剪掉 ${fmtTime(s.removed)} (${Math.round(s.removedPct * 100)}%)`));
    fill(stats, ...parts);
    $("#render-label").textContent = "出片";
    $("#render-btn").disabled = !state.meta.capabilities.render;
  }
  const missing = Object.values(sources()).filter((x) => x.kind !== "image" && !x.exists);
  const why = (ok, missingMsg, off) => (missing.length ? missingMsg : ok ? "" : off);
  const gone = "找不到素材文件：先在页面上方“重新定位”素材";
  $("#render-btn").disabled = $("#render-btn").disabled || missing.length > 0;
  $("#render-btn").title = why(state.meta.capabilities.render, gone, "出片模块还没装好，暂时不能出片（审阅和保存不受影响）");
  const unc = uncertainCount();
  const ub = $("#uncertain-btn");
  const spotN = candidateMode() ? 0 : spotCheckOrder(o).length;
  ub.hidden = unc === 0 && spotN === 0;
  ub.classList.toggle("btn-warn", unc > 0);
  if (unc > 0) {
    fill(ub, GLYPH.warn, ` ${unc} 处拿不准`);
    ub.setAttribute("aria-label", `${unc} 处拿不准，点击跳到第一处`);
    ub.title = "";
  } else {
    fill(ub, GLYPH.check, " 没有拿不准的 · 抽查 ▸");
    ub.setAttribute("aria-label", "没有拿不准的地方；点击开始抽查，按把握度从低到高看");
    ub.title = "没有拿不准，不等于都对：按把握度从低到高抽查（⇧J 下一处）";
  }
  $("#cand-auth").hidden = !candidateMode();
  $("#check-btn").disabled = !state.meta.capabilities.check || missing.length > 0;
  $("#check-btn").title = why(state.meta.capabilities.check, gone, "体检模块还没装好");
  $("#export-btn").disabled = !state.meta.capabilities.export || missing.length > 0;
  $("#export-btn").title = why(state.meta.capabilities.export, gone, "导出模块还没装好");
  $("#redo-btn").disabled = !state.undo.canRedo;
  refreshCaptionUI();
  refreshSaveState();
  refreshPace();
}

const candidateMode = () => state.mode === "candidates";

/** 底栏快捷键提示：按模式给（候选清单里真正常用的是 ↑↓ 和 S）。J = 下一个，K = 上一个，和实际行为一致。 */
function renderHints() {
  const k = (...keys) => h("kbd", {}, ...keys);
  const items = candidateMode()
    ? [[k("空格"), " 播放/暂停"], [k("↑"), "/", k("↓"), " 上/下一个候选"], [k("S"), " 勾选/取消"], [k("J"), "/", k("K"), " 下/上一个决定"], [k("X"), " 剪/留"], [k("["), k("]"), " 微调边界（", k("⇧"), " 为 500ms）"], [k(MOD + "Z"), " 撤销"], [k("?"), " 全部快捷键"]]
    : [[k("空格"), " 播放/暂停"], [k("J"), "/", k("K"), " 下/上一个决定"], [k("⇧J"), "/", k("⇧K"), " 只在拿不准之间跳"], [k("X"), " 剪/留切换"], [k("L"), " 锁定"], [k("["), k("]"), " 微调边界 50ms（", k("⇧"), " 500ms）"], [k("A"), " 看 agent 改了什么"], [k(MOD + "Z"), " 撤销"], [k("?"), " 全部快捷键"]];
  fill($("#hints"), ...items.map((n) => h("span", {}, ...n)), h("span", { class: "tb-spacer" }), h("span", { id: "footer-save", class: "muted" }));
  $("#footer-save").textContent = `自动保存到 ${state.meta.name}`;
}

function uncertainCount() {
  return candidateMode()
    ? state.plan.outputs.filter((o) => o.selected).reduce((a, o) => a + o.segments.filter(isUncertain).length, 0)
    : out().segments.filter(isUncertain).length;
}

function refreshSaveState() {
  const el = $("#save-state");
  const n = diffCount(state.base, state.plan);
  const changes = n ? ` · 改动 ${n} 处` : "";
  const map = {
    idle: ["", "✓ 已保存" + changes], dirty: ["", "● 未保存" + changes], saving: ["", "保存中…" + changes],
    saved: ["", "✓ 已保存" + changes], error: ["is-error", GLYPH.warn + " 没能保存"], conflict: ["is-error", GLYPH.warn + " 方案被外部修改"],
  };
  const [cls, text] = map[state.save.status] ?? map.idle;
  el.className = `save-state ${cls}`;
  el.textContent = text;
  $("#undo-btn").disabled = !state.undo.canUndo;
  $("#redo-btn").disabled = !state.undo.canRedo;
}

function refreshPace() {
  const wrap = $("#pace");
  const pace = state.plan.options?.pace;
  wrap.hidden = pace == null;
  if (pace == null) return;
  const cur = paceId(pace);
  const keep = state.plan.options?.keep_pause;
  wrap.title = `${keep != null ? `现在每个停顿保留 ${keep}s。` : ""}点另一档会在页面里重新划分停顿；你手改过或锁定的片段不会动。`;
  for (const b of $$("[data-pace]", wrap)) {
    const on = b.dataset.pace === cur;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on || cur === null ? 0 : -1;
  }
  $("#pace-custom").hidden = cur !== null;
  $("#pace-custom").textContent = typeof pace === "number" ? `自定义 ${pace}s` : String(pace);
}

// ---------------------------------------------------------------- 横幅
const banners = new Map();
function setBanner(key, { kind = "info", title, body, actions = [], dismiss = null }) {
  banners.set(key, { kind, title, body, actions, dismiss });
  renderBanners();
}
function clearBanner(key) { if (banners.delete(key)) renderBanners(); }
function renderBanners() {
  const host = $("#banners");
  fill(host, ...[...banners.entries()].map(([key, b]) => h("div", { class: `banner banner-${b.kind}`, role: b.kind === "error" ? "alert" : "region", "aria-label": b.title },
    h("div", { class: "banner-text" }, b.title && h("strong", {}, b.title, " "), b.body),
    h("div", { class: "banner-actions" }, ...b.actions.map((a) => h("button", { class: "btn btn-sm", onclick: a.onclick }, a.label)),
      b.dismiss !== false && h("button", { class: "icon-btn", "aria-label": "关闭这条提示", onclick: () => { b.dismiss?.(); clearBanner(key); } }, "×")),
  )));
}

function setupBanners() {
  const introKey = "autocut.review.intro.v1";
  if (!store.getItem(introKey)) {
    const nU = uncertainCount();
    const cutsRecipe = state.plan.outputs.some((o) => o.segments.some((s) => s.action === "cut"));
    setBanner("intro", {
      kind: "tip",
      body: candidateMode()
        ? "候选按分数从高到低排好了；勾选要出片的，右边写着为什么入选。满意就点“出片”。"
        : cutsRecipe
          ? `${GLYPH.cut} 是建议剪掉的地方。${nU ? `按 ⇧J 逐个看拿不准的 ${nU} 处；` : "没有拿不准的地方，不等于都对：点“抽查”按把握度从低到高看；"}满意就点“出片”。把鼠标移到一行上能看到“剪掉 / 多留 / 试听”。`
          : "这是机器排好的第一版材料：每一段都可以试听、改边界；要重新编排，用“导出工程”到剪辑软件里继续。",
      actions: [{ label: "知道了", onclick: () => { store.setItem(introKey, "1"); clearBanner("intro"); } }],
      dismiss: () => store.setItem(introKey, "1"),
    });
  }
  showNotes();
  for (const [sid, info] of Object.entries(sources())) {
    if (info.kind !== "image" && !info.exists) showMissing(sid, info);
    for (const w of info.warnings ?? []) setBanner(`warn:${sid}:${w}`, { kind: "warn", title: `素材 ${basename(info.path)}`, body: w });
  }
}

let notesExpanded = false;
function showNotes() {
  const notes = state.plan.notes ?? [];
  if (!notes.length || state.dismissedNotes) return clearBanner("notes");
  const show = notesExpanded || notes.length <= 2 ? notes : notes.slice(0, 2);
  setBanner("notes", {
    kind: "warn", title: "方案备注",
    body: h("span", {}, ...show.flatMap((n, i) => [i ? h("br") : null, n])),
    actions: notes.length > 2 ? [{ label: notesExpanded ? "收起" : `展开全部（${notes.length} 条）`, onclick: () => { notesExpanded = !notesExpanded; showNotes(); } }] : [],
    dismiss: () => { state.dismissedNotes = true; },
  });
}

function showMissing(sid, info) {
  const input = h("input", { class: "input", type: "text", placeholder: "新的素材路径，例如 /Users/…/talk.mp4", "aria-label": "新的素材路径", size: 36 });
  const go = async () => {
    try {
      await flushSave();
      const d = await api.relocate(sid, input.value, state.revision, state.etag);
      state.meta.sources = d.sources;
      state.plan.sources[sid].path = d.plan.sources[sid].path;
      state.base.sources[sid].path = d.plan.sources[sid].path;
      state.revision = d.revision; state.etag = d.etag;
      clearBanner(`missing:${sid}`);
      $("#player-msg").hidden = true;
      toast("素材已重新定位", { kind: "ok" });
      player.sourceId = null; // 之前没加载成功，强制重新加载
      player.setOutput(out(), sources());
      await player.load(sid);
      ensureFrameLayout();
      ensurePeaks(sid);
      ensureWords(sid);
      refreshHeader();
    } catch (e) { toast(e.message, { kind: "error", timeout: 9000 }); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  setBanner(`missing:${sid}`, {
    kind: "error", title: `找不到素材 ${basename(info.path)}`,
    body: h("span", {}, `方案里记的路径：${info.abs_path}。文件被移动或改名了吗？ `, input),
    actions: [{ label: "重新定位", onclick: go }], dismiss: false,
  });
}

// ---------------------------------------------------------------- 选中与导航
function selectSeg(segId, { seek = true, scroll = true, focus = false, edge = null } = {}) {
  const o = out();
  const seg = o.segments.find((s) => s.id === segId);
  if (!seg) return;
  state.selSeg = segId;
  state.edge = edge ?? "start";
  transcript.setSelected(segId, { focus, scroll });
  timeline.setSelection(segId, state.edge);
  if (scroll) timeline.ensureVisible(seg.start, 0.1);
  if (seek) player.seekSource(seg.source, seg.start);
  if (!focus) announce(transcript.rowFor(segId)?.getAttribute("aria-label") ?? "");
}

function clearSelection() { /* 点在空白处：保留当前选中，只移动播放头 */ }

function stepDecision(dir, uncertainOnly = false) {
  if (uncertainOnly && !candidateMode() && uncertainCount() === 0) { // 没有拿不准：⇧J/⇧K 改成按把握度抽查
    if (!state.spot) startSpotCheck(); else stepSpot(dir);
    return;
  }
  const segs = out().segments;
  const cur = segs.findIndex((s) => s.id === state.selSeg);
  const ok = (s) => (uncertainOnly ? isUncertain(s) : isDecision(s)) && transcript.isVisible(s.id);
  for (let i = cur + dir; i >= 0 && i < segs.length; i += dir) {
    if (ok(segs[i])) { selectSeg(segs[i].id, { seek: true, focus: true }); return; }
  }
  const total = segs.filter(ok).length;
  toast(uncertainOnly
    ? (total ? `${dir > 0 ? "后面" : "前面"}没有拿不准的了（共 ${total} 处）` : "没有拿不准的地方了")
    : `已经是${dir > 0 ? "最后" : "第"}一个决定`, { timeout: 2200 });
}

// ---- 抽查：没有"拿不准"时，按把握度从低到高看一遍（"没有拿不准"不等于"都对"）
function startSpotCheck() {
  const order = spotCheckOrder(out());
  if (!order.length) { toast("没有可以抽查的决定了", { timeout: 2200 }); return; }
  state.spot = { order, i: -1 };
  stepSpot(1);
}

function stepSpot(dir) {
  const sp = state.spot;
  if (!sp) return;
  const next = sp.i + dir;
  if (next < 0) { toast("已经是抽查的第一处", { timeout: 1800 }); return; }
  if (next >= sp.order.length) { toast(`抽查完了（共 ${sp.order.length} 处）。没发现问题也不等于都对，出片后再听一遍。`, { timeout: 4500 }); return; }
  sp.i = next;
  const id = sp.order[next];
  state.seen.add(`${state.outputId}:${id}`);
  selectSeg(id, { seek: true, focus: true });
  transcript.update(id);
  renderSpotBar();
}

function exitSpot() {
  state.spot = null;
  renderSpotBar();
}

function renderSpotBar() {
  const bar = $("#spot-bar");
  if (!bar) return;
  bar.hidden = !state.spot;
  if (!state.spot) return;
  const { order, i } = state.spot;
  const seg = out().segments.find((s) => s.id === order[i]);
  fill(bar,
    h("span", { class: "bar-text" }, h("strong", {}, "抽查："), "没有拿不准的地方，不等于都对。按把握度从低到高看，",
      h("span", { class: "mono" }, ` ${Math.max(i + 1, 0)}/${order.length}`), seg ? `，这一处 ${Math.round(seg.confidence * 100)}%` : ""),
    h("span", { class: "bar-actions" },
      h("button", { class: "btn btn-sm", onclick: () => stepSpot(-1) }, "◂ 上一处"),
      h("button", { class: "btn btn-sm", onclick: () => stepSpot(1) }, "下一处 ▸"),
      h("button", { class: "btn btn-sm btn-quiet", onclick: exitSpot }, "退出抽查")));
}

function jumpToFirstUncertain() {
  if (!candidateMode() && uncertainCount() === 0) { startSpotCheck(); return; }
  if (candidateMode()) {
    const o = rankedOutputs().find((x) => x.selected && x.segments.some(isUncertain)) ?? rankedOutputs().find((x) => x.segments.some(isUncertain));
    if (!o) return;
    if (o.id !== state.outputId) switchOutput(o.id).then(() => selectSeg(out().segments.find(isUncertain).id, { focus: true }));
    else selectSeg(o.segments.find(isUncertain).id, { focus: true });
    return;
  }
  const first = out().segments.find((s) => isUncertain(s) && transcript.isVisible(s.id)) ?? out().segments.find(isUncertain);
  if (first) selectSeg(first.id, { focus: true });
}

// ---------------------------------------------------------------- 修改
function applyEdit(label, changes, opts) {
  if (!changes.length) return;
  state.undo.push(label, changes, opts);
  afterChange(changes);
}

function afterChange(changes) {
  state.editSeq++;
  const o = out();
  if (changes.some((c) => c.op && c.output === o.id)) { // 增删片段、改顺序：整份文字稿重建
    loadOutput().then(() => { if (candidateMode()) renderCandidateList(); refreshHeader(); });
    markDirty();
    return;
  }
  const touched = new Set(changes.filter((c) => c.seg && c.output === o.id).map((c) => c.seg));
  for (const id of touched) transcript.update(id);
  player.setOutput(o, sources());
  timeline.setData({ output: o, sources: sources(), sourceId: timeline.sourceId, duration: timeline.duration });
  updateFilterCounts();
  if (candidateMode()) { renderCandidateList(); updateFrameCrop(player.state().outTime ?? 0); }
  refreshHeader();
  markDirty();
}

const snippet = (seg) => (seg.text ? `“${seg.text.slice(0, 14)}”` : `${REASONS[seg.reason] ?? seg.reason} ${fmtTime(seg.start, { ms: 1 })}`);

function setSegAction(segId, action) {
  const seg = out().segments.find((s) => s.id === segId);
  if (!seg || (seg.action === action && !isUncertain(seg))) return;
  const wasUncertain = isUncertain(seg);
  const changes = setAction(out(), segId, action);
  applyEdit(`${action === "cut" ? "剪掉" : "保留"} ${snippet(seg)}`, changes);
  announce(`${action === "cut" ? "已剪掉" : "已改回保留"}：${snippet(seg)}`);
  if (wasUncertain) afterResolved(segId);
}

/** "就这样"：确认机器的判断，不改动作和边界。 */
function confirmSeg(segId) {
  const seg = out().segments.find((s) => s.id === segId);
  if (!seg) return;
  const wasUncertain = isUncertain(seg);
  applyEdit(`确认 ${snippet(seg)}`, confirmSegment(out(), segId));
  announce(`已确认：${snippet(seg)}`);
  if (wasUncertain) afterResolved(segId);
}

/**
 * 处理完一处"拿不准"之后（#8）：在"拿不准"筛选里，这一行留在视图里（变淡，标"已确认"），
 * 选中和焦点移到下一处拿不准，键盘用户不会丢掉位置；其它视图里焦点留在这一行。
 */
function afterResolved(segId) {
  if (state.filter.kind === "uncertain") {
    transcript.pin(segId);
    transcript.update(segId);
    const segs = out().segments;
    const i = segs.findIndex((s) => s.id === segId);
    const next = segs.slice(i + 1).find((s) => isUncertain(s)) ?? segs.slice(0, i).reverse().find((s) => isUncertain(s));
    if (next) { selectSeg(next.id, { seek: true, focus: true }); return; }
    announce("拿不准的都处理完了");
    toast("拿不准的都处理完了。想再检查，用“抽查”按把握度从低到高看。", { timeout: 4500 });
  }
  transcript.rowFor(segId)?.focus({ preventScroll: true });
}

function toggleSelected() {
  const seg = selectedSeg();
  if (seg) setSegAction(seg.id, seg.action === "cut" ? "keep" : "cut");
}

function toggleLock(segId = state.selSeg) {
  const seg = out().segments.find((s) => s.id === segId);
  if (!seg) return;
  applyEdit(seg.locked ? "解锁" : "锁定", setLocked(out(), segId, !seg.locked));
  announce(seg.locked ? "已锁定：改节奏时这一段不会被动" : "已解锁");
}

function rowAction(segId, act) {
  if (segId !== state.selSeg) selectSeg(segId, { seek: false, scroll: false });
  const wasUncertain = isUncertain(out().segments.find((s) => s.id === segId) ?? {});
  if (act === "keep" || act === "cut") setSegAction(segId, act);
  else if (act === "toggle") setSegAction(segId, out().segments.find((s) => s.id === segId).action === "cut" ? "keep" : "cut");
  else if (act === "lock") toggleLock(segId);
  else if (act === "audition") auditionSegment(segId);
  else if (act === "confirm") confirmSeg(segId);
  else if (act === "more" || act === "less") keepMore(segId, act === "more" ? 0.5 : -0.5);
  else if (act.startsWith("edge:")) {
    const [, edge, what] = act.split(":");
    if (what === "audition") auditionEdge(segId, edge);
    else edgeMoreLess(segId, edge, what === "more" ? 1 : -1, wasUncertain);
  }
  // afterResolved 已经把焦点交给下一处时不要再抢回来
  if (document.activeElement?.closest?.(".row")?.dataset.seg === segId || document.activeElement === document.body) transcript.rowFor(segId)?.focus({ preventScroll: true });
}

/** 多留 / 少留（秒）：动剪掉片段上和保留部分相接的那条边。 */
function keepMore(segId, delta) {
  try {
    const { changes, edge } = keepMoreLess(out(), sources(), segId, delta);
    if (!changes.length) { toast("到头了：再移就会碰到相邻片段", { timeout: 2200 }); return; }
    applyEdit(delta > 0 ? "多留一点" : "少留一点", changes, { key: `more:${segId}` });
    const seg = out().segments.find((s) => s.id === segId);
    announce(`${delta > 0 ? "多留" : "少留"} ${Math.abs(delta)} 秒，边界 ${fmtTime(seg[edge], { ms: 2 })}`);
    if (state.auditionNudge) auditionEdge(segId, edge);
  } catch (e) { toast(e.message, { kind: "error" }); }
}

/** 切点拿不准：开头 / 结尾 多留 / 少留 delta 秒（+1 多留，−1 少留）；动过边界就算确认。 */
function edgeMoreLess(segId, edge, dir, wasUncertain) {
  const seg = out().segments.find((s) => s.id === segId);
  if (!seg) return;
  const sign = edge === "start" ? -dir : dir; // 开头往前、结尾往后 = 多留
  const { lo, hi } = boundaryRange(out(), sources(), segId, edge);
  const target = Math.min(Math.max(round4(seg[edge] + sign * 1), lo), hi);
  const what = `${edge === "start" ? "开头" : "结尾"}${dir > 0 ? "多留" : "少留"} 1s`;
  if (Math.abs(target - seg[edge]) < 1e-9) { toast("到头了：再移会超出素材范围或吃掉整段", { timeout: 2200 }); return; }
  const changes = moveBoundary(out(), sources(), segId, edge, target);
  if (wasUncertain) changes.push(...confirmSegment(out(), segId));
  applyEdit(what, changes);
  announce(`${what}，${edge === "start" ? "开头" : "结尾"}改到 ${fmtTime(target, { ms: 1 })}`);
  fitCandidateTimelineIfNeeded();
  auditionEdge(segId, edge);
  if (wasUncertain) afterResolved(segId);
}

function fitCandidateTimelineIfNeeded() {
  if (candidateMode()) { renderWhy(); fitCandidateTimeline(out()); }
}

/** 试听某一条边附近：开头听进入的地方，结尾听收住的地方。 */
function auditionEdge(segId, edge) {
  const seg = out().segments.find((s) => s.id === segId);
  if (!seg) return;
  const t = seg[edge];
  player.auditionRange(seg.source, edge === "start" ? t - 0.8 : t - 2.2, edge === "start" ? t + 2.2 : t + 0.8);
  announce(`试听${edge === "start" ? "开头" : "结尾"}切点附近`);
}

function auditionSegment(segId = state.selSeg) {
  const seg = out().segments.find((s) => s.id === segId);
  if (!seg) return;
  player.auditionRange(seg.source, seg.start - 1, seg.end + 1);
  announce("试听：前后各 1 秒");
}

function nudge(dir, big = false) {
  const seg = selectedSeg();
  if (!seg) return;
  const edge = state.edge;
  const { lo, hi } = boundaryRange(out(), sources(), seg.id, edge);
  const target = Math.min(Math.max(round4(seg[edge] + dir * (big ? 0.5 : NUDGE)), lo), hi);
  if (Math.abs(target - seg[edge]) < 1e-9) { toast(`${edge === "start" ? "起点" : "终点"}已经到头了：再移就会碰到相邻片段或素材边缘`, { timeout: 2200 }); return; }
  applyEdit("微调边界", moveBoundary(out(), sources(), seg.id, edge, target), { key: `nudge:${seg.id}:${edge}` });
  announce(`${edge === "start" ? "起点" : "终点"} ${fmtTime(target, { ms: 2 })}`);
  timeline.ensureVisible(target, 0.1);
  if (state.auditionNudge) {
    const t = seg[edge];
    player.auditionRange(seg.source, edge === "start" ? t - 0.5 : t - 0.8, edge === "start" ? t + 0.8 : t + 0.5);
  }
}

function switchEdge() {
  state.edge = state.edge === "start" ? "end" : "start";
  timeline.setSelection(state.selSeg, state.edge);
  const seg = selectedSeg();
  if (seg) announce(`现在微调${state.edge === "start" ? "起点" : "终点"} ${fmtTime(seg[state.edge], { ms: 2 })}`);
  toast(`[ ] 现在微调${state.edge === "start" ? "起点" : "终点"}（按 E 切换）`, { timeout: 1800 });
}

let dragSeq = 0;
function onTimelineDrag({ segId, edge, t, phase }) {
  if (phase === "start") dragSeq++;
  try {
    const changes = moveBoundaryClamped(out(), sources(), segId, edge, t);
    state.undo.push("拖动边界", changes, { key: `drag:${dragSeq}`, windowMs: 1e9 });
    state.editSeq++;
    for (const c of changes) transcript.update(c.seg);
    timeline.requestDraw();
    markDirty();
    if (phase === "end") afterChange(changes);
  } catch (e) { toast(e.message, { kind: "error" }); }
  if (phase === "start") { state.selSeg = segId; state.edge = edge; transcript.setSelected(segId, { scroll: false }); timeline.setSelection(segId, edge); }
}

function undo(redo = false) {
  const e = redo ? state.undo.redo(state.plan) : state.undo.undo(state.plan);
  if (!e) { toast(redo ? "没有可重做的操作" : "没有可撤销的操作", { timeout: 1800 }); return; }
  if (e.meta?.base) { applyChanges(state.base, e.changes, redo ? "after" : "before"); rebuildBaseMap(); } // 节奏调整不算“我改的”
  const changes = e.changes;
  const outId = changes.find((c) => c.output !== null)?.output ?? state.outputId; // 方案级的改动（options）不属于某个输出
  if (outId !== state.outputId) { switchOutput(outId).then(() => afterChange(changes)); }
  else {
    afterChange(changes);
    const segId = changes.find((c) => c.seg)?.seg;
    if (segId) selectSeg(segId, { seek: false });
  }
  announce(`${redo ? "已重做" : "已撤销"}：${e.label}`);
  toast(`${redo ? "已重做" : "已撤销"}：${e.label}`, { timeout: 1800 });
}

// ---------------------------------------------------------------- 保存与外部修改
function markDirty() {
  state.dirty = true;
  if (state.save.status !== "conflict") state.save.status = "dirty";
  clearBanner("save-error");
  refreshSaveState();
  autosave();
}
const autosave = debounce(() => save(), SAVE_DELAY);

async function save() {
  if (!state.dirty || state.conflict) return;
  if (state.saving) { autosave(); return; }
  state.saving = true;
  state.save.status = "saving";
  refreshSaveState();
  const seq = state.editSeq;
  const sent = clone(state.plan);
  try {
    const r = await api.save(state.revision, state.etag, sent);
    state.revision = r.revision;
    state.etag = r.etag;
    state.synced = sent; // 和服务器同步过的版本：之后三方合并的"共同祖先"
    state.meta.summary = r.summary;
    if (state.editSeq === seq) { state.dirty = false; state.save.status = "saved"; } else { state.save.status = "dirty"; autosave(); }
    loadCuesSoon();
  } catch (e) {
    if (e.status === 409) { await absorbDisk(e.body.disk); }
    else if (e.status === 400) {
      state.save.status = "error";
      setBanner("save-error", { kind: "error", title: "方案没能保存：", body: h("span", { class: "pre" }, e.message), actions: [{ label: "撤销上一步", onclick: () => { undo(); clearBanner("save-error"); } }], dismiss: false });
    } else {
      state.save.status = "error";
      setBanner("save-error", { kind: "error", title: "保存失败：", body: e.message, actions: [{ label: "重试", onclick: () => save() }], dismiss: false });
      setTimeout(() => { if (state.dirty && !state.conflict) save(); }, 5000);
    }
  } finally {
    state.saving = false;
    refreshSaveState();
  }
}

async function flushSave() {
  autosave.cancel();
  for (let i = 0; i < 20 && (state.dirty || state.saving); i++) {
    if (state.conflict) throw new Error("有改动和 agent 的修改冲突，先在右侧“方案冲突”面板里逐条选一边");
    if (state.save.status === "error") throw new Error("方案还没保存成功，先处理页面上方的提示");
    if (!state.saving) await save();
    else await new Promise((r) => setTimeout(r, 50));
  }
}

// ---- 外部修改：标出改了哪里、保留撤销历史、按片段合并（#4 #5）
/**
 * 磁盘上的方案变了（agent、编辑器，或者保存时返回 409）。
 * - 我没有未保存的改动：把 agent 的改动逐处套到当前方案上，记成一条可撤销的历史（⌘Z 就是拒绝 agent 的改动），撤销栈保留；
 * - 我有未保存的改动：按片段三方合并（共同祖先 = 上次同步的版本）。不冲突的自动合并，真冲突的先保留我的值，
 *   在右侧面板里逐条选；选完之前不保存，期间我的编辑一律保留。
 * 两种情况都会把 agent 改过的行标出来，可以筛选、逐处跳转。
 */
async function absorbDisk(data) {
  const theirs = normalizePlan(data.plan);
  const prev = state.synced;
  const theirsDiff = diffPlans(prev, theirs);
  const hadLocal = state.dirty || !!state.conflict;
  applyChanges(state.base, theirsDiff.changes); // base 跟着走：“改动 N 处”“已改”只反映我自己的修改
  let conflicts = [];
  let merged = 0;
  if (!hadLocal) {
    const d = diffPlans(state.plan, theirs);
    if (d.changes.length) {
      applyChanges(state.plan, d.changes);
      state.undo.push(`agent 的改动（${theirsDiff.items.length} 处）`, d.changes);
    }
  } else {
    const r = mergePlans(prev, state.plan, theirs);
    const d = diffPlans(state.plan, r.plan);
    if (d.changes.length) {
      applyChanges(state.plan, d.changes);
      state.undo.push("合并 agent 的改动", d.changes);
    }
    conflicts = r.conflicts;
    merged = r.applied;
  }
  state.revision = data.revision;
  state.etag = data.etag;
  state.synced = theirs;
  state.meta.sources = data.sources;
  state.meta.summary = data.summary;
  state.outputId = state.plan.outputs.some((o) => o.id === state.outputId) ? state.outputId : state.plan.outputs[0].id;
  if (theirsDiff.items.length) setMarks(theirsDiff.items, "agent 改过");
  showNotes();
  await loadOutput();
  if (candidateMode()) renderCandidateList();
  const n = theirsDiff.items.filter((x) => x.output !== null).length;
  if (conflicts.length) {
    state.conflict = { groups: conflicts };
    state.dirty = true;
    state.save.status = "conflict";
    state.conflictSegs = conflictSegSet(conflicts);
    transcript.updateAll();
    setBanner("conflict", {
      kind: "error", title: "方案在外部被修改了：",
      body: `agent 改了 ${n} 处${merged ? `，其中 ${merged} 处已经合并进来` : ""}；有 ${conflicts.length} 处和你改的是同一个地方，需要你选一边。你的改动都还在，选完之前不会保存。`,
      actions: [{ label: "逐条处理", onclick: () => { openDrawer("conflict"); renderConflictPanel(); } }], dismiss: false,
    });
    openDrawer("conflict");
    renderConflictPanel();
  } else if (hadLocal) {
    markDirty();
    toast(`已把 agent 的 ${n} 处改动合并进来，你的改动也都保留了`, { timeout: 5000 });
  } else if (n) {
    toast(`agent 改了 ${n} 处，已载入并标出（⌘Z 可以拒绝）`, { timeout: 6000 });
  }
  refreshHeader();
}

function conflictSegSet(groups) {
  return new Set(groups.flatMap((g) => g.segIds.map((id) => `${g.output}:${id}`)));
}

// 冲突面板（右侧抽屉）
function renderConflictPanel() {
  const c = state.conflict;
  const body = $("#drawer-body");
  if (!c) { fill(body, h("p", {}, "没有待处理的冲突。")); return; }
  const snip = (g) => {
    const seg = state.plan.outputs.find((o) => o.id === g.output)?.segments.find((s) => s.id === g.segIds[0]);
    if (!g.segIds.length) return `输出「${g.output}」的设置`;
    return seg ? `${seg.text ? "“" + seg.text.slice(0, 18) + "”" : (REASONS[seg.reason] ?? seg.reason)} · ${fmtTime(seg.start, { ms: 1 })}` : g.segIds.join("、");
  };
  fill(body,
    h("p", {}, h("strong", {}, `${c.groups.length} 处冲突`), "：agent 和你改了同一个地方。你的改动先保留着，没有丢；逐条选一边。选完之前不会保存，也可以先去改别的地方。"),
    h("div", { class: "conflict-bulk" },
      h("button", { class: "btn btn-sm", onclick: () => resolveAll("mine") }, "全部用我的"),
      h("button", { class: "btn btn-sm", onclick: () => resolveAll("theirs") }, "全部用 agent 的")),
    h("ol", { class: "conflict-list" }, ...c.groups.map((g) => h("li", { class: "conflict-card" },
      h("div", { class: "conflict-head" }, h("strong", {}, snip(g)),
        g.segIds.length ? h("button", { class: "btn btn-sm btn-quiet", onclick: () => selectSeg(g.segIds[0], { focus: true }) }, "跳到这里") : null),
      h("div", { class: "conflict-sides" },
        h("div", {}, h("span", { class: "lbl" }, "你"), describeConflictSide(g, "mine")),
        h("div", {}, h("span", { class: "lbl lbl-agent" }, "agent"), describeConflictSide(g, "theirs"))),
      h("div", { class: "conflict-actions" },
        h("button", { class: "btn btn-sm", onclick: () => resolveGroup(g.key, "mine") }, "用我的"),
        h("button", { class: "btn btn-sm", onclick: () => resolveGroup(g.key, "theirs") }, "用 agent 的"))))));
}

function resolveGroup(key, side, { quiet = false } = {}) {
  const c = state.conflict;
  const g = c?.groups.find((x) => x.key === key);
  if (!g) return;
  const changes = resolveConflict(state.plan, g, side);
  c.groups = c.groups.filter((x) => x !== g);
  state.conflictSegs = conflictSegSet(c.groups);
  if (changes.length) {
    state.undo.push(side === "theirs" ? "采用 agent 的版本" : "保留我的版本", changes);
    afterChange(changes);
  }
  for (const id of g.segIds) transcript.update(id);
  if (!quiet) finishConflictIfDone();
}

function resolveAll(side) {
  const c = state.conflict;
  if (!c) return;
  for (const g of [...c.groups]) resolveGroup(g.key, side, { quiet: true });
  finishConflictIfDone();
}

function finishConflictIfDone() {
  const c = state.conflict;
  if (!c) return;
  if (c.groups.length) { renderConflictPanel(); refreshSaveState(); return; }
  state.conflict = null;
  state.conflictSegs = new Set();
  state.save.status = "dirty";
  clearBanner("conflict");
  closeDrawer({ focus: false });
  markDirty();
  toast("冲突都处理完了，正在保存合并后的方案", { kind: "ok", timeout: 3500 });
}

async function pollExternal() {
  if (document.hidden || state.saving || state.conflict) return;
  let s;
  try { s = await api.status(); } catch { return; }
  if (s.revision === state.revision && s.etag === state.etag) return;
  if (state.saving || state.conflict) return;
  let data;
  try { data = await api.plan(); } catch { return; }
  await absorbDisk(data);
}

// ---- 被标出的改动（agent / 节奏）
const markKey = (o, s) => `${o}:${s}`;
const markedIds = () => out().segments.filter((s) => state.marks.segs.has(markKey(state.outputId, s.id))).map((s) => s.id);

function setMarks(items, label) {
  const old = new Set(state.marks.segs.keys());
  const marks = { label, segs: new Map(), outputs: new Map(), removed: [] };
  for (const it of items) {
    if (it.output === null) continue;
    if (it.seg === null) { marks.outputs.set(it.output, `${label}：${describeFields(it.fields)}`); continue; }
    if (it.op === "remove") { marks.removed.push({ output: it.output, seg: it.seg, text: it.data?.text || `${REASONS[it.data?.reason] ?? ""} ${fmtTime(it.data?.start ?? 0, { ms: 1 })}` }); continue; }
    if (it.op === "insert") { marks.segs.set(markKey(it.output, it.seg), `${label}：新增`); continue; }
    // 节奏调整一定会顺带改说明、把握度，只看动作和边界；agent 的改动则什么字段都算
    const quiet = label.startsWith("节奏") ? ["evidence", "confidence", "detail", "reason", "text"] : ["evidence", "confidence"];
    const fields = Object.fromEntries(Object.entries(it.fields).filter(([k]) => !quiet.includes(k)));
    if (!Object.keys(fields).length) continue;
    marks.segs.set(markKey(it.output, it.seg), `${label}：${describeFields(fields)}`);
  }
  state.marks = marks;
  if (transcript?.output) for (const k of new Set([...old, ...marks.segs.keys()])) { const [o, id] = [k.slice(0, k.indexOf(":")), k.slice(k.indexOf(":") + 1)]; if (o === state.outputId) transcript.update(id); }
  renderMarksBar();
  if (candidateMode()) renderCandidateList();
  updateFilterCounts();
}

function clearMarks() {
  if (state.filter.kind === "marked") setFilter({ kind: "all", reason: null });
  setMarks([], "");
}

function renderMarksBar() {
  const bar = $("#marks-bar");
  if (!bar) return;
  const ids = markedIds();
  const nRem = state.marks.removed.length;
  bar.hidden = !(ids.length || nRem || state.marks.outputs.size);
  if (bar.hidden) return;
  const label = state.marks.label || "改过";
  fill(bar,
    h("span", { class: "bar-text" }, h("strong", {}, `${label}：${ids.length} 处`),
      nRem ? `，删除了 ${nRem} 个片段（${state.marks.removed.slice(0, 2).map((r) => r.text.slice(0, 10)).join("、")}${nRem > 2 ? "…" : ""}）` : "",
      state.marks.outputs.size ? `，${[...state.marks.outputs.values()].join("；")}` : ""),
    h("span", { class: "bar-actions" },
      ids.length ? h("button", { class: "btn btn-sm", onclick: () => stepMark(-1) }, "◂ 上一处") : null,
      ids.length ? h("button", { class: "btn btn-sm", onclick: () => stepMark(1) }, "下一处 ▸") : null,
      ids.length ? h("button", { class: "btn btn-sm", "aria-pressed": String(state.filter.kind === "marked"), onclick: () => setFilter(state.filter.kind === "marked" ? { kind: "all", reason: null } : { kind: "marked", reason: null }) }, "只看这些") : null,
      h("button", { class: "btn btn-sm btn-quiet", onclick: clearMarks }, "知道了")));
}

function stepMark(dir) {
  const ids = markedIds();
  if (!ids.length) return;
  const segs = out().segments.map((s) => s.id);
  const cur = segs.indexOf(state.selSeg);
  const order = ids.map((id) => segs.indexOf(id));
  const next = dir > 0 ? order.find((i) => i > cur) ?? order[0] : [...order].reverse().find((i) => i < cur) ?? order[order.length - 1];
  selectSeg(segs[next], { seek: true, focus: true });
}

// ---------------------------------------------------------------- 播放
function bindPlayer() {
  player.addEventListener("tick", (e) => onTick(e.detail));
  player.addEventListener("state", syncTransport);
  player.addEventListener("error", (e) => {
    setBanner("video-error", { kind: "error", title: "视频播放出错：", body: e.detail.message, dismiss: () => {} });
  });
  player.addEventListener("loaded", () => { clearBanner("video-error"); ensureFrameLayout(); });
  player.addEventListener("ended", () => {
    if (candidateMode() && state.previewAll) playNextSelected();
  });
  let chipTimer = null;
  player.addEventListener("skip", (e) => {
    const { from, to, source } = e.detail;
    const cutSegs = out().segments.filter((s) => s.action === "cut" && s.source === source && s.start >= from - 0.01 && s.end <= to + 0.01);
    const label = cutSegs.length === 1 ? REASONS[cutSegs[0].reason] : cutSegs.length ? `${cutSegs.length} 处` : "";
    const chip = $("#skip-chip");
    chip.textContent = `${GLYPH.cut} 跳过 ${(to - from).toFixed(1)}s${label ? " · " + label : ""}`;
    chip.hidden = false;
    clearTimeout(chipTimer);
    chipTimer = setTimeout(() => { chip.hidden = true; }, 1100);
  });
  videoEl.addEventListener("click", () => player.toggle());
}

let lastSegNow = null;
function onTick(s) {
  if (s.sourceId !== timeline.sourceId) return;
  timeline.setPlayhead(s.srcTime);
  const o = out();
  let seg = lastSegNow && lastSegNow.source === s.sourceId && s.srcTime >= lastSegNow.start && s.srcTime < lastSegNow.end ? lastSegNow : null;
  if (!seg) seg = o.segments.find((x) => x.source === s.sourceId && s.srcTime >= x.start && s.srcTime < x.end) ?? null;
  const changed = seg?.id !== lastSegNow?.id;
  lastSegNow = seg;
  transcript.setNow(seg?.id ?? null);
  if (seg) transcript.setNowWord(seg.id, s.srcTime);
  if (changed && seg && s.playing && state.follow) {
    const row = transcript.rowFor(seg.id);
    if (row && !row.hidden) transcript.ensureVisible(row, "center");
  }
  // 读数
  const inCut = seg?.action === "cut" && s.mode === "plan";
  $("#t-out").textContent = s.outTime != null ? fmtTime(s.outTime, { ms: 1 }) : "—";
  $("#t-out-total").textContent = fmtTime(s.outDuration);
  $("#t-src").textContent = fmtTime(s.srcTime, { ms: 1 });
  $("#t-cutflag").hidden = !(seg?.action === "cut");
  const scrub = $("#scrub");
  if (!scrub.dataset.dragging) {
    const orig = s.mode === "orig";
    const total = orig ? s.duration : s.outDuration;
    scrub.max = String(total || 1);
    scrub.value = String((orig ? s.srcTime : s.outTime ?? scrub.value) || 0);
    scrub.setAttribute("aria-valuetext", `成片 ${fmtTime(s.outTime ?? 0)}，共 ${fmtTime(s.outDuration)}；原片 ${fmtTime(s.srcTime)}`);
  }
  void inCut;
  updateCaption(s.outTime);
  if (candidateMode()) updateFrameCrop(s.outTime ?? 0);
}

function bindTransport() {
  $("#play-btn").addEventListener("click", () => player.toggle());
  const scrub = $("#scrub");
  scrub.addEventListener("pointerdown", () => { scrub.dataset.dragging = "1"; });
  scrub.addEventListener("pointerup", () => { delete scrub.dataset.dragging; });
  scrub.addEventListener("input", () => {
    const v = Number(scrub.value);
    if (player.mode === "plan") player.seekOutput(v); else player.seekSource(player.sourceId, v);
  });
  scrub.addEventListener("change", () => { delete scrub.dataset.dragging; });
  $("#rate-btn").addEventListener("click", () => {
    const rates = [1, 1.25, 1.5, 2, 0.75];
    const next = rates[(rates.indexOf(player.rate) + 1) % rates.length];
    player.setRate(next);
    $("#rate-btn").textContent = `${next}×`;
  });
  $("#zoom-in").addEventListener("click", () => timeline.zoomBy(1.6));
  $("#zoom-out").addEventListener("click", () => timeline.zoomBy(1 / 1.6));
  $("#zoom-fit").addEventListener("click", () => (candidateMode() ? fitCandidateTimeline(out()) : timeline.fitAll()));
}

function syncTransport() {
  const playing = !videoEl.paused;
  const b = $("#play-btn");
  fill(b, playing ? GLYPH.pause : GLYPH.play);
  b.setAttribute("aria-label", playing ? "暂停（空格）" : "播放（空格）");
  b.setAttribute("aria-pressed", String(playing));
}

function setPlayMode(mode) {
  state.playMode = mode;
  player.setMode(mode);
  syncPlayModeButtons();
  announce(mode === "plan" ? "按方案播放：会跳过剪掉的部分" : "播放原片：不跳过");
}

function syncPlayModeButtons() {
  const plan = $("#mode-plan"), orig = $("#mode-orig");
  if (!plan) return;
  plan.setAttribute("aria-pressed", String(state.playMode === "plan"));
  orig.setAttribute("aria-pressed", String(state.playMode === "orig"));
  $("#mode-note").textContent = state.playMode === "plan" ? "播放时跳过剪掉的部分" : "原片，不跳过";
}

// ---------------------------------------------------------------- 筛选（单条时间线）
function mountFilters() {
  const bar = $("#filter-bar");
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (b) setFilter({ kind: b.dataset.filter, reason: null });
  });
  $("#reason-chips").addEventListener("click", (e) => {
    const r = e.target.closest("[data-reason]");
    if (r) setFilter({ kind: "all", reason: state.filter.reason === r.dataset.reason ? null : r.dataset.reason });
  });
  bar.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const btns = $$("[data-filter]", bar);
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const nb = btns[(i + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length];
    nb.focus();
    nb.click();
  });
}

function setFilter(f) {
  state.filter = f;
  transcript.applyFilter(f);
  updateFilterCounts();
  renderMarksBar();
  const n = out().segments.filter((s) => transcript.isVisible(s.id)).length;
  announce(`筛选后显示 ${n} 条`);
  const sel = transcript.rowFor(state.selSeg);
  if (sel && !sel.hidden) transcript.ensureVisible(sel, "nearest");
}

function updateFilterCounts() {
  const bar = $("#filter-bar");
  if (!bar) return;
  const segs = out().segments;
  const nMarked = markedIds().length + segs.filter((s) => state.conflictSegs.has(markKey(state.outputId, s.id))).length;
  const counts = { all: segs.length, uncertain: segs.filter(isUncertain).length, cut: segs.filter((s) => s.action === "cut").length, keep: segs.filter((s) => s.action === "keep").length, marked: nMarked };
  const resolved = Math.max(0, state.uncertainStart - counts.uncertain);
  for (const b of $$("[data-filter]", bar)) {
    const on = b.dataset.filter === state.filter.kind && !state.filter.reason;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
    b.hidden = b.dataset.filter === "marked" && nMarked === 0 && !on;
    const label = b.querySelector(".count");
    label.textContent = b.dataset.filter === "uncertain" && resolved > 0 ? `${counts.uncertain} · 已处理 ${resolved}` : String(counts[b.dataset.filter]);
  }
  // 按理由统计被剪掉的（同时也是筛选器）
  const byReason = {};
  for (const s of segs) if (s.action === "cut") byReason[s.reason] = (byReason[s.reason] ?? 0) + 1;
  const chips = $("#reason-chips");
  fill(chips, ...Object.entries(byReason).sort((a, b) => b[1] - a[1]).map(([r, n]) =>
    h("button", { class: "chip", dataset: { reason: r }, "aria-pressed": String(state.filter.reason === r), title: `只看被剪掉的“${REASONS[r] ?? r}”` },
      GLYPH.cut, ` ${REASONS[r] ?? r} `, h("span", { class: "mono" }, String(n)))));
}

// ---------------------------------------------------------------- 候选清单模式
const rankedOutputs = () => state.plan.outputs.map((o, i) => [o, i]).sort((a, b) => (b[0].score ?? -1) - (a[0].score ?? -1) || a[1] - b[1]).map((x) => x[0]);

function mountCandidates() {
  $("#cand-list").addEventListener("click", (e) => {
    const cb = e.target.closest("input[type=checkbox]");
    const item = e.target.closest(".cand");
    if (!item) return;
    if (cb) { toggleCandidate(item.dataset.out, cb.checked); return; }
    switchOutput(item.dataset.out, { focus: false });
  });
  $("#preview-all").addEventListener("click", previewSelected);
  $("#cand-select").addEventListener("change", (e) => toggleCandidate(state.outputId, e.target.checked));
  for (const b of $$("[data-trim]", $("#cand-why"))) {
    b.addEventListener("click", () => {
      const [which, d] = b.dataset.trim.split(":");
      trimCandidate(which, Number(d));
    });
  }
  for (const b of $$("[data-crop]", $("#cand-preview"))) b.addEventListener("click", () => moveCrop(b.dataset.crop));
  bindCropDrag();
  new ResizeObserver(() => ensureFrameLayout()).observe($("#frame"));
  renderCandidateList();
}

function candRange(o) {
  const k = keptSegments(o);
  if (!k.length) return [0, 0];
  return [Math.min(...k.map((s) => s.start)), Math.max(...k.map((s) => s.end))];
}

function renderCandidateList() {
  const list = $("#cand-list");
  const focusId = document.activeElement?.closest?.(".cand")?.dataset.out;
  const ranked = rankedOutputs();
  fill(list, ...ranked.map((o, i) => {
    const [a, b] = candRange(o);
    const reason = o.reasons[0] ?? "";
    const unc = o.segments.filter(isUncertain).length;
    const current = o.id === state.outputId;
    return h("li", { class: `cand${current ? " is-current" : ""}${o.selected ? " is-selected" : " is-unselected"}`, dataset: { out: o.id }, "aria-current": current ? "true" : false },
      h("label", { class: "cand-check" },
        h("input", { type: "checkbox", checked: o.selected, "aria-label": `选中第 ${i + 1} 名「${o.title || o.id}」出片` })),
      h("button", { class: "cand-main", type: "button", "aria-label": `查看第 ${i + 1} 名：${o.title || o.id}，${o.score ?? "无"} 分，${o.selected ? "已选" : "没选"}` },
        h("span", { class: "cand-top" },
          h("span", { class: "cand-rank mono" }, `#${i + 1}`),
          h("span", { class: "cand-range mono" }, `${fmtTime(a)}–${fmtTime(b)}`),
          o.score != null && h("span", { class: "cand-score mono" }, `${o.score} 分`)),
        h("span", { class: "cand-title" }, o.title || o.id),
        h("span", { class: "cand-sub" },
          !o.selected && h("span", { class: "tag tag-skip" }, "没选"),
          unc > 0 && h("span", { class: "tag tag-warn" }, GLYPH.warn, ` ${unc} 处拿不准`),
          (state.marks.outputs.has(o.id) || [...state.marks.segs.keys()].some((k) => k.startsWith(`${o.id}:`))) && h("span", { class: "tag tag-agent" }, state.marks.label || "改过"),
          reason)),
    );
  }));
  if (focusId) list.querySelector(`.cand[data-out="${CSS.escape(focusId)}"] .cand-main`)?.focus({ preventScroll: true });
  renderWhy();
}

function renderWhy() {
  const o = out();
  const k = keptSegments(o);
  const box = $("#why-body");
  const first = k[0], last = k[k.length - 1];
  fill(box, 
    h("h2", { class: "why-title" }, o.title || o.id),
    h("p", { class: "why-meta mono" }, o.score != null ? `${o.score} 分 · ` : "", `${fmtTime(outputDuration(o))} 长`, !o.selected ? " · 没选" : ""),
    h("h3", {}, o.selected ? "为什么入选" : "为什么没选"),
    o.reasons.length ? h("ul", { class: "why-list" }, ...o.reasons.map((r) => h("li", {}, r))) : h("p", { class: "muted" }, "方案没有给出理由。"),
    first && h("p", { class: "why-edge" }, h("strong", {}, "开头："), first.detail ? ` ${first.detail}` : first.reason === "lead_in" ? ` 铺垫 ${(first.end - first.start).toFixed(1)}s 后进入。` : ` 从 ${fmtTime(first.start, { ms: 1 })} 开始。`),
    last && h("p", { class: "why-edge" }, h("strong", {}, "结尾："), last.detail ? ` ${last.detail}` : ` 在 ${fmtTime(last.end, { ms: 1 })}。`),
  );
  $("#cand-select-label").textContent = o.selected ? "已选中，会出片" : "没选，不会出片";
  const cb = $("#cand-select");
  cb.checked = o.selected;
}

async function switchOutput(id, { focus = false } = {}) {
  if (id === state.outputId) return;
  player.pause();
  state.outputId = id;
  state.selSeg = null;
  lastSegNow = null;
  await loadOutput();
  renderCandidateList();
  const f = keptSegments(out())[0] ?? out().segments[0];
  if (f) player.seekSource(f.source, f.start);
  if (focus) transcript.rowFor(state.selSeg)?.focus({ preventScroll: true });
}

function toggleCandidate(id, on) {
  const o = state.plan.outputs.find((x) => x.id === id);
  if (!o || o.selected === on) return;
  applyEdit(on ? `选中 ${o.title || o.id}` : `取消选中 ${o.title || o.id}`, setSelected(o, on));
  announce(`${o.title || o.id}：${on ? "已选中" : "已取消选中"}，共选 ${state.plan.outputs.filter((x) => x.selected).length} 条`);
}

function trimCandidate(which, delta) {
  try {
    const before = keptSegments(out());
    const edgeSeg = which === "start" ? before[0] : before[before.length - 1];
    const old = which === "start" ? edgeSeg?.start : edgeSeg?.end;
    const changes = trimOutput(out(), sources(), which, delta);
    const now = which === "start" ? edgeSeg.start : edgeSeg.end;
    if (Math.abs(now - old) < 1e-9) { toast("到头了：再移会超出素材范围或吃掉整段", { timeout: 2200 }); return; }
    applyEdit(`${which === "start" ? "开头" : "结尾"} ${delta > 0 ? "+" : "−"}${Math.abs(delta)}s`, changes, { key: `trim:${out().id}:${which}` });
    if (candidateMode()) { renderWhy(); fitCandidateTimeline(out()); }
    player.seekSource(edgeSeg.source, which === "start" ? edgeSeg.start : Math.max(edgeSeg.start, edgeSeg.end - 3));
    announce(`${which === "start" ? "开头" : "结尾"}改到 ${fmtTime(now, { ms: 1 })}，现在 ${fmtTime(outputDuration(out()))} 长`);
    return true;
  } catch (e) { toast(e.message, { kind: "error" }); }
}

function fitCandidateTimeline(o) {
  const [a, b] = candRange(o);
  timeline.fitRange(a, b, Math.max(2, (b - a) * 0.12));
}

function previewSelected() {
  const sel = rankedOutputs().filter((o) => o.selected);
  if (!sel.length) { toast("还没有勾选候选", { timeout: 2000 }); return; }
  state.previewAll = true;
  setPlayMode("plan");
  const go = async () => { await switchOutput(sel[0].id); await player.seekOutput(0); player.play(); };
  if (sel.some((o) => o.id === state.outputId)) { player.seekOutput(0).then(() => player.play()); } else go();
}

async function playNextSelected() {
  const sel = rankedOutputs().filter((o) => o.selected);
  const i = sel.findIndex((o) => o.id === state.outputId);
  const next = sel[i + 1];
  if (!next) { state.previewAll = false; toast("所选候选预览完了", { timeout: 2200 }); return; }
  await switchOutput(next.id);
  await player.seekOutput(0);
  player.play();
}

// 9:16 预览框：用 CSS 模拟裁切（和出片时 reframe 的裁切框一致）
function frameAspect() {
  const o = out();
  if (o.width && o.height) return o.width / o.height;
  const m = String(state.plan.options?.aspect ?? "").match(/^(\d+):(\d+)$/);
  if (m) return Number(m[1]) / Number(m[2]);
  const s = sources()[mainSource(o)];
  return s?.width && s?.height ? s.width / s.height : 16 / 9;
}

let frameState = null;
function ensureFrameLayout() {
  const frame = $("#frame");
  if (!frame) return;
  const aspect = frameAspect();
  frame.style.aspectRatio = String(aspect);
  const info = sources()[mainSource()] ?? {};
  const sw = videoEl.videoWidth || info.width || 1920, sh = videoEl.videoHeight || info.height || 1080;
  frameState = { sw, sh, aspect, cw: frame.clientWidth, ch: frame.clientHeight };
  const crop = !!out().reframe;
  videoEl.classList.toggle("cropped", crop);
  $("#crop-tools").hidden = !crop;
  updateFrameCrop(player.state().outTime ?? 0);
}

function updateFrameCrop(outTime) {
  if (!frameState || !candidateMode()) return;
  const { sw, sh, aspect, cw, ch } = frameState;
  if (!out().reframe) {
    videoEl.style.cssText = "";
    return;
  }
  const { cx, cy } = reframeCenter(out().reframe, outTime);
  const r = cropRect(sw, sh, aspect, cx, cy);
  const scale = cw / (r.w * sw);
  videoEl.style.width = `${sw * scale}px`;
  videoEl.style.height = `${sh * scale}px`;
  videoEl.style.transform = `translate(${-r.x * sw * scale}px, ${-r.y * sh * scale}px)`;
  void ch;
}

function setReframe(cx, cy) {
  const o = out();
  const before = { reframe: o.reframe ? JSON.parse(JSON.stringify(o.reframe)) : null };
  const k = o.reframe?.keyframes ?? [];
  const keyframes = k.length > 1 ? k : [[0, cx, cy]];
  if (k.length > 1) { // 有多个关键帧：整体平移
    const cur = reframeCenter(o.reframe, player.state().outTime ?? 0);
    const dx = cx - cur.cx, dy = cy - cur.cy;
    o.reframe = { mode: "track", keyframes: k.map(([t, x, y]) => [t, Math.min(Math.max(x + dx, 0), 1), Math.min(Math.max(y + dy, 0), 1)]) };
  } else {
    o.reframe = { mode: "track", keyframes };
  }
  state.undo.push("调整裁切框", [{ output: o.id, seg: null, before, after: { reframe: JSON.parse(JSON.stringify(o.reframe)) } }], { key: `crop:${o.id}`, windowMs: 1200 });
  state.editSeq++;
  updateFrameCrop(player.state().outTime ?? 0);
  refreshHeader();
  markDirty();
}

function moveCrop(dir) {
  if (!out().reframe) return;
  const cur = reframeCenter(out().reframe, player.state().outTime ?? 0);
  if (dir === "center") setReframe(0.5, 0.5);
  else setReframe(Math.min(Math.max(cur.cx + (dir === "left" ? -0.05 : 0.05), 0), 1), cur.cy);
}

function bindCropDrag() {
  const frame = $("#frame");
  let drag = null;
  frame.addEventListener("pointerdown", (e) => {
    if (!out().reframe || !frameState) return;
    const cur = reframeCenter(out().reframe, player.state().outTime ?? 0);
    drag = { x: e.clientX, y: e.clientY, cx: cur.cx, cy: cur.cy };
    frame.setPointerCapture(e.pointerId);
  });
  frame.addEventListener("pointermove", (e) => {
    if (!drag || !frameState) return;
    const { sw, sh, aspect, cw } = frameState;
    const r = cropRect(sw, sh, aspect, drag.cx, drag.cy);
    const scale = cw / (r.w * sw);
    setReframe(Math.min(Math.max(drag.cx - (e.clientX - drag.x) / (sw * scale), 0), 1), Math.min(Math.max(drag.cy - (e.clientY - drag.y) / (sh * scale), 0), 1));
  });
  frame.addEventListener("pointerup", () => { drag = null; });
}

// ---------------------------------------------------------------- 出片 / 体检 / 导出
async function requestRender() {
  if (!state.meta.capabilities.render) return;
  try { await flushSave(); } catch (e) { toast(e.message, { kind: "error" }); return; }
  const ids = candidateMode() ? rankedOutputs().filter((o) => o.selected).map((o) => o.id) : [out().id];
  const unc = ids.reduce((a, id) => a + state.plan.outputs.find((o) => o.id === id).segments.filter(isUncertain).length, 0);
  if (unc === 0) return startRender(ids);
  const dlg = $("#confirm");
  fill($("#confirm-body"), 
    h("p", {}, `还有 ${unc} 处“拿不准”没有确认。直接出片的话，它们按机器当时的判断处理（${GLYPH.cut} 的剪掉，其余保留）。`),
    candidateMode() && h("p", { class: "muted" }, "发布前请确认已获得主播授权。"));
  $("#confirm-go").onclick = () => { dlg.close(); startRender(ids); };
  $("#confirm-review").onclick = () => { dlg.close(); jumpToFirstUncertain(); };
  dlg.showModal();
}

async function startRender(ids) {
  clearBanner("render-error");
  let r;
  try { r = await api.render(ids, { loudnorm: state.renderOpts.loudnorm }); } catch (e) {
    setBanner("render-error", { kind: "error", title: "没能开始出片：", body: e.message, actions: [{ label: "重试", onclick: () => startRender(ids) }] });
    return;
  }
  state.job = { id: r.job, ids, status: "running", fraction: 0, stage: "准备", eta: null, captionMode: captionMode(out()), loudnorm: state.renderOpts.loudnorm };
  renderJobBar();
  announce("开始出片");
  pollJob(r.job, ids);
}

async function pollJob(id, ids) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 500));
    if (state.job?.id !== id) return;
    let j;
    try { j = await api.job(id); } catch (e) {
      state.job = { ...state.job, status: "error", error: e.message };
      renderJobBar();
      return;
    }
    state.job = { ...state.job, ...j, ids };
    renderJobBar();
    if (j.status !== "running") {
      if (j.status === "error") setBanner("render-error", { kind: "error", title: "出片失败：", body: h("span", {}, h("span", { class: "pre" }, j.error), h("br"), h("span", { class: "muted" }, "方案已经保存，没有丢。按上面的说明处理后点“重试”；需要帮助时点“复制错误”发给 agent。")), actions: [{ label: "重试", onclick: () => startRender(ids) }, { label: "复制错误", onclick: () => copyText(j.error).then(() => toast("已复制")) }] });
      announce(j.status === "done" ? "出片完成" : j.status === "cancelled" ? "已取消出片" : "出片失败");
      return;
    }
  }
}

function resultsOf(job) {
  const r = job.result;
  const arr = Array.isArray(r) ? r : r?.results ?? r?.outputs ?? (r ? [r] : []);
  return arr.map((x) => {
    const path = x.path ?? x.output ?? x.file;
    const dur = x.duration ?? x.duration_s ?? x.expected_duration;
    const drift = x.av_drift ?? x.drift;
    const lufs = x.loudness?.target ?? (typeof x.loudness === "number" ? x.loudness : x.lufs);
    return { id: x.output_id ?? x.id, path, name: basename(path), dur, drift, lufs, srt: x.srt_path ?? null, warnings: x.warnings ?? [] };
  });
}

function renderJobBar() {
  const bar = $("#jobbar");
  const j = state.job;
  bar.hidden = !j;
  if (!j) return;
  if (j.status === "running") {
    const pct = Math.round((j.fraction ?? 0) * 100);
    const eta = j.eta != null ? ` · 约剩 ${fmtTime(j.eta)}` : "";
    bar.className = "jobbar is-running";
    fill(bar, 
      h("progress", { max: 100, value: pct, "aria-label": "出片进度" }),
      h("span", { class: "job-text" }, `出片中 · ${j.stage} `, h("span", { class: "mono" }, `${pct}%${eta}`)),
      h("button", { class: "btn btn-sm", onclick: () => api.cancelJob(j.id).catch(() => {}) }, "取消"));
  } else if (j.status === "done") {
    const rs = resultsOf(j);
    bar.className = "jobbar is-done";
    const line = (r) => {
      const sync = r.drift == null ? null : r.drift <= 0.05 ? `音画同步 ✓` : `${GLYPH.warn} 音画相差 ${(r.drift * 1000).toFixed(0)}ms`;
      const text = [r.name, r.dur != null && fmtTime(r.dur), sync, r.lufs != null ? `响度 ${Number(r.lufs).toFixed(1)} LUFS` : "响度未标准化"].filter(Boolean).join(" · ");
      const cap = j.captionMode === "burn" ? "字幕已烧进画面" : r.srt ? `字幕：${basename(r.srt)}（单独文件，没有烧进画面）` : j.captionMode === "off" ? "没有字幕" : null;
      return h("span", { title: r.path }, text, cap ? h("span", { class: "job-cap" }, ` · ${cap}`) : null); // 完整路径放在悬停提示里
    };
    const warns = rs.flatMap((r) => r.warnings.map((w) => `${r.id ? r.id + "：" : ""}${w}`));
    fill(bar, 
      h("span", { class: "job-text" }, h("strong", {}, rs.length === 1 ? "成片：" : `已出片 ${rs.length} 条`),
        rs.length === 1 ? line(rs[0]) : h("span", {}, ` · 共 ${fmtTime(rs.reduce((a, r) => a + (r.dur ?? 0), 0))}${rs.every((r) => r.drift != null && r.drift <= 0.05) ? " · 音画同步 ✓" : ""}`),
        rs.length > 1 && h("details", {}, h("summary", {}, "每个文件"), rs.map((r) => h("div", { class: "job-line" }, line(r)))),
        warns.length ? h("div", { class: "job-warn" }, GLYPH.warn, " ", warns.join("；")) : null),
      rs[0]?.path && h("span", { class: "job-dir mono", title: rs[0].path }, `目录：${rs[0].path.replace(/[^/]*$/, "")}`),
      rs[0]?.path && h("button", { class: "btn btn-sm", onclick: () => api.reveal(rs[0].path).catch((e) => toast(e.message, { kind: "error" })) }, "在 Finder 中显示"),
      h("button", { class: "icon-btn", "aria-label": "收起这条结果", onclick: () => { state.job = null; renderJobBar(); } }, "×"));
  } else {
    bar.className = "jobbar is-failed";
    fill(bar, h("span", { class: "job-text" }, j.status === "cancelled" ? "已取消出片。" : `${GLYPH.warn} 出片没有完成，原因见页面上方。`),
      h("button", { class: "icon-btn", "aria-label": "收起", onclick: () => { state.job = null; renderJobBar(); } }, "×"));
  }
}

// 体检 ---------------------------------------------------------------
const SEV = {
  risk: ["▲", "风险"], high: ["▲", "风险"], critical: ["▲", "风险"], error: ["▲", "风险"],
  warn: ["◆", "提醒"], warning: ["◆", "提醒"], medium: ["◆", "提醒"],
  info: ["●", "说明"], low: ["●", "说明"],
};

async function runCheck() {
  openDrawer("check");
  const body = $("#drawer-body");
  fill(body, h("p", { class: "muted", role: "status" }, `正在体检${candidateMode() ? "「" + (out().title || out().id) + "」" : ""}…按方案估算，不渲染；没有文字稿时会先识别语音，第一次可能要几分钟。`));
  try { await flushSave(); } catch (e) { fill(body, errorPanel("体检前需要先保存", e.message)); return; }
  try {
    state.check = normalizeReport(await api.check(out().id));
    renderCheck();
  } catch (e) {
    fill(body, errorPanel(e.status === 503 ? "体检模块不可用" : "体检没有完成", e.message, "方案和审阅不受影响。"));
  }
}

function checkCtx() {
  return { duration: state.check?.duration || outputDuration(out()), captionMode: captionMode(out()), sourcePath: state.plan.sources[mainSource()]?.path ?? "素材.mp4" };
}

function renderCheck() {
  const rep = state.check;
  if (!rep || state.drawer !== "check") return;
  const body = $("#drawer-body");
  const dur = rep.duration || outputDuration(out()) || 1;
  const track = h("div", { class: "risk-track", role: "group", "aria-label": "风险时间线（成片时间；整条成片的问题不在这里）" },
    ...rep.timed.map((x) => {
      const [glyph, label] = SEV[x.severity] ?? SEV.info;
      return h("button", { class: `risk-pin sev-${x.severity}`, style: { left: `${Math.min(100, (x.t / dur) * 100)}%` }, "aria-label": `${label} ${fmtTime(x.t)} ${x.title}`, title: `${fmtTime(x.t)} ${x.title}`, onclick: () => jumpToCheck(x) }, glyph);
    }));
  const card = (x) => {
    const [glyph, label] = SEV[x.severity] ?? SEV.info;
    const acts = checkActions(x, checkCtx());
    return h("li", { class: `risk-card sev-${x.severity}` },
      h("div", { class: "risk-sev" }, h("span", { "aria-hidden": "true" }, glyph), " ", label,
        x.whole ? h("span", { class: "risk-time" }, " · 整条成片") : x.t != null ? h("button", { class: "linklike mono risk-time", onclick: () => jumpToCheck(x), "aria-label": `跳到 ${fmtTime(x.t)}` }, ` · ${fmtTime(x.t)}`) : null),
      h("strong", {}, x.title),
      x.message && h("span", { class: "risk-msg" }, x.message),
      x.advice && h("span", { class: "risk-advice" }, "建议：", x.advice),
      acts.length ? h("div", { class: "risk-actions" }, ...acts.map((act) => h("button", { class: "btn btn-sm", onclick: () => runCheckAction(act, x) }, act.label))) : null);
  };
  fill(body,
    rep.summary && h("p", { class: "check-summary" }, String(rep.summary)),
    rep.timed.length ? h("div", { class: "risk-wrap" }, track, h("div", { class: "risk-axis mono" }, h("span", {}, "00:00"), h("span", {}, fmtTime(dur)))) : null,
    rep.timed.length ? h("ol", { class: "risk-list" }, ...rep.timed.map(card)) : null,
    rep.whole.length ? h("h3", { class: "risk-group" }, "整条成片的问题", h("span", { class: "muted small" }, "（不在某一秒，所以不上时间线）")) : null,
    rep.whole.length ? h("ol", { class: "risk-list" }, ...rep.whole.map(card)) : null,
    !rep.items.length ? h("p", {}, "没有发现需要注意的信号。") : null,
    h("p", { class: "disclaimer" }, rep.disclaimer),
  );
}

/** 体检建议 → 页面里的动作。做不到的（挪句子顺序、加画面变化）写明"在剪辑软件里做"，并直接给导出入口。 */
function runCheckAction(act, item) {
  switch (act.kind) {
    case "jump": jumpToCheck(item); break;
    case "jump-edge": {
      const k = keptSegments(out());
      const seg = act.which === "start" ? k[0] : k[k.length - 1];
      if (seg) { selectSeg(seg.id, { seek: true, focus: true, edge: act.which }); player.seekSource(seg.source, act.which === "start" ? seg.start : Math.max(seg.start, seg.end - 2)); }
      toast(`已选中${act.which === "start" ? "开头" : "结尾"}那一段：用“多留 / 少留”或 [ ] 调整切点`, { timeout: 4500 });
      break;
    }
    case "trim":
      if (trimCandidate(act.which, act.delta)) { toast(`${act.label}：已改，可以 ${MOD}Z 撤销。重新体检看看`, { timeout: 4500 }); }
      break;
    case "loudnorm":
      state.renderOpts.loudnorm = true;
      refreshRenderOpts();
      toast("出片时会把响度标准化到平台常用值（-14 LUFS）", { kind: "ok", timeout: 4500 });
      break;
    case "captions": setCaptions(act.mode); toast("字幕会烧进画面", { kind: "ok", timeout: 3500 }); break;
    case "aspect": case "size": setSize(act.preset, { withReframe: act.kind === "aspect" }); break;
    case "filter-silence": setFilter({ kind: "all", reason: "silence" }); toast("已筛出被剪掉的长停顿：逐条看哪些还能再缩或改回保留", { timeout: 4500 }); break;
    case "export": $("#export-btn").click(); break;
    case "copy": copyText(act.text).then(() => toast("命令已复制", { timeout: 1800 })); break;
    default: break;
  }
  renderCheck();
}

function jumpToCheck(x) {
  if (x.t == null) return;
  if (x.space === "source") player.seekSource(mainSource(), x.t); else player.seekOutput(x.t);
  setPlayMode("plan");
  const at = player.state();
  const seg = out().segments.find((s) => s.source === (at.sourceId ?? mainSource()) && at.srcTime >= s.start && at.srcTime < s.end);
  if (seg) selectSeg(seg.id, { seek: false });
}

function openDrawer(kind) {
  state.drawer = kind;
  $("#drawer").hidden = false;
  $app.classList.add("with-drawer");
  $("#drawer-title").textContent = kind === "conflict" ? "方案冲突" : "发布前体检";
  $("#drawer").setAttribute("aria-label", kind === "conflict" ? "方案冲突" : "发布前体检");
  $("#drawer-rerun").hidden = kind !== "check";
}
function closeDrawer({ focus = true } = {}) {
  const kind = state.drawer;
  state.drawer = null;
  $("#drawer").hidden = true;
  $app.classList.remove("with-drawer");
  if (focus) (kind === "conflict" ? $("#uncertain-btn") : $("#check-btn")).focus();
}

// 字幕 ---------------------------------------------------------------
const CAP_NAME = { off: "关", srt: "单独 srt", burn: "烧进画面" };

/** mode: off | srt | burn。候选清单模式下一次改所有候选（批量出片，字幕设置应该一致）。 */
function setCaptions(mode) {
  const targets = candidateMode() ? state.plan.outputs : [out()];
  if (targets.every((o) => captionMode(o) === mode)) return;
  applyEdit(captionLabel(mode), targets.flatMap((o) => setCaptionMode(o, mode)));
  announce(captionLabel(mode));
}

async function loadCues() {
  if (!state.meta.capabilities.captions) return;
  const id = out().id;
  try { state.cues[id] = await api.captions(id); } catch (e) { state.cues[id] = { cues: [], has_words: false, reason: e.message, error: true }; }
  refreshCaptionUI();
}
const loadCuesSoon = debounce(() => loadCues(), 400);

function refreshCaptionUI() {
  const btn = $("#cap-btn");
  if (!btn) return;
  const mode = captionMode(out());
  btn.textContent = `字幕：${CAP_NAME[mode]} ▾`;
  btn.disabled = !state.meta.capabilities.captions;
  btn.title = state.meta.capabilities.captions ? "预览里叠加字幕；选“烧进画面”出片时字幕会画在视频上，选“单独 srt”会另外写一个 .srt 文件" : "字幕模块还没装好";
  const badge = $("#cap-badge");
  badge.hidden = mode === "off";
  badge.textContent = mode === "burn" ? "字幕：烧进画面" : "字幕：单独 srt（不烧进画面）";
  const info = state.cues[out().id];
  $("#cap-note")?.toggleAttribute("hidden", !(mode !== "off" && info && !info.has_words));
  for (const r of $$("input[name=opt-cap]")) r.checked = r.value === mode;
  updateCaption(player?.state().outTime);
}

let lastCap = null;
function updateCaption(ot) {
  const el = $("#cap-overlay");
  if (!el) return;
  const mode = captionMode(out());
  const info = state.cues[out().id];
  const cue = mode !== "off" && info && ot != null ? findCue(info.cues, ot) : null;
  el.hidden = !cue;
  if (cue?.text !== lastCap) { lastCap = cue?.text ?? null; el.firstElementChild.textContent = cue?.text ?? ""; }
  el.className = `cap-overlay cap-${mode}`;
}

// 成片尺寸 / 出片选项 ---------------------------------------------------
/** preset: source / vertical / horizontal / 720p / 1080p；候选清单模式一次改所有候选。 */
function setSize(preset, { withReframe = false } = {}) {
  const targets = candidateMode() ? state.plan.outputs : [out()];
  const changes = targets.flatMap((o) => {
    const size = sizeForPreset(o, sources(), preset);
    return [...setOutputSize(o, size), ...(withReframe ? ensureReframeFor(o) : [])];
  });
  applyEdit(`成片尺寸：${preset}`, changes);
  if (candidateMode()) ensureFrameLayout();
  toast("成片尺寸已改，可以 " + MOD + "Z 撤销。重新体检看看", { kind: "ok", timeout: 4500 });
  refreshRenderOpts();
}

function ensureReframeFor(o) { return ensureReframe(o); }

function refreshRenderOpts() {
  const ln = state.renderOpts.loudnorm;
  for (const r of $$("input[name=opt-ln]")) r.checked = r.value === (ln === null ? "default" : ln === false ? "off" : "on");
  const p = sizePresetOf(out());
  for (const r of $$("input[name=opt-size]")) r.checked = r.value === p;
  $("#opts-btn")?.classList.toggle("has-opts", ln !== null);
}

// 导出 ---------------------------------------------------------------
async function doExport(fmt) {
  try { await flushSave(); } catch (e) { toast(e.message, { kind: "error" }); return; }
  const t = toast(`正在导出 ${fmt}${candidateMode() ? "（当前候选）" : ""}…`, { timeout: 0 });
  try {
    const r = await api.exportPlan(fmt, out().id);
    t.remove();
    const p = r.paths?.[0];
    toast(p ? `已导出 ${fmt}：${basename(p)}` : `已导出 ${fmt}`, { kind: "ok", timeout: 9000, action: p ? { label: "在 Finder 中显示", onclick: () => api.reveal(p).catch((e) => toast(e.message, { kind: "error" })) } : null });
  } catch (e) {
    t.remove();
    toast(e.message, { kind: "error", timeout: 10000 });
  }
}

// ---------------------------------------------------------------- 外壳：按钮、菜单、对话框、快捷键
function bindChrome() {
  // “按方案播放”/“原片”既是模式开关也是播放键：已经在这个模式里就当暂停/播放用
  const playIn = (mode) => { if (state.playMode === mode) player.toggle(); else { setPlayMode(mode); player.play(); } };
  $("#mode-plan").addEventListener("click", () => playIn("plan"));
  $("#mode-orig").addEventListener("click", () => playIn("orig"));
  $("#uncertain-btn").addEventListener("click", jumpToFirstUncertain);
  $("#render-btn").addEventListener("click", requestRender);
  $("#check-btn").addEventListener("click", runCheck);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-rerun").addEventListener("click", runCheck);
  $("#undo-btn").addEventListener("click", () => undo());
  $("#redo-btn").addEventListener("click", () => undo(true));
  menu($("#export-btn"), () => [
    ...state.meta.formats.map((f) => ({ label: FORMAT_LABEL[f]?.[0] ?? f, hint: f, onselect: () => doExport(f) })),
    { note: "剪映：这几种格式能否直接导入没有验证过；稳妥的做法是用“出片”得到的 mp4，再加“字幕 srt”。" },
  ]);
  menu($("#cap-btn"), () => ["off", "srt", "burn"].map((m) => ({ label: `${captionMode(out()) === m ? "✓ " : ""}${CAP_NAME[m]}`, hint: m === "burn" ? "画在视频上" : m === "srt" ? "另存 .srt" : "", onselect: () => setCaptions(m) })));
  const optsDlg = $("#render-opts");
  $("#opts-btn").addEventListener("click", () => { refreshRenderOpts(); refreshCaptionUI(); optsDlg.showModal(); });
  $("#opts-close").addEventListener("click", () => optsDlg.close());
  for (const r of $$("input[name=opt-cap]")) r.addEventListener("change", () => setCaptions(r.value));
  for (const r of $$("input[name=opt-ln]")) r.addEventListener("change", () => { state.renderOpts.loudnorm = r.value === "default" ? null : r.value === "on" ? true : false; refreshRenderOpts(); });
  for (const r of $$("input[name=opt-size]")) r.addEventListener("change", () => setSize(r.value, { withReframe: r.value === "vertical" }));

  // 节奏档位
  const pace = $("#pace");
  pace.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pace]");
    if (!b || b.getAttribute("aria-checked") === "true") return;
    changePace(b.dataset.pace);
  });
  pace.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const btns = $$("[data-pace]", pace);
    const i = btns.indexOf(document.activeElement);
    if (i >= 0) { e.preventDefault(); btns[(i + (e.key === "ArrowRight" ? 1 : -1) + btns.length) % btns.length].focus(); }
  });

  // 帮助、字号
  const help = $("#help");
  $("#help-btn").addEventListener("click", () => help.showModal());
  $("#help-close").addEventListener("click", () => help.close());
  $("#opt-audition").checked = state.auditionNudge;
  $("#opt-audition").addEventListener("change", (e) => { state.auditionNudge = e.target.checked; store.setItem("autocut.review.auditionNudge", e.target.checked ? "1" : "0"); });
  $$("[data-mod]").forEach((el) => { el.textContent = MOD; });
  const sizes = ["", "lg", "xl", "xxl"];
  const applySize = (s) => { document.documentElement.dataset.size = s; store.setItem("autocut.review.size", s); };
  applySize(store.getItem("autocut.review.size") ?? "");
  $("#size-btn").addEventListener("click", () => applySize(sizes[(sizes.indexOf(document.documentElement.dataset.size ?? "") + 1) % sizes.length]));

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => timeline.refreshTheme());

  document.addEventListener("keydown", onKey);
  // 用鼠标点过的按钮松开焦点，否则之后按空格会再次“点击”那个按钮而不是播放
  document.addEventListener("click", (e) => {
    const b = e.target.closest?.("button, [role=radio]");
    if (b && e.detail > 0 && !b.closest("dialog, .menu, .row-actions, #filter-bar, #pace, .cand")) b.blur();
  });
  window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
  window.addEventListener("pagehide", () => { if (state.dirty && !state.conflict) api.save(state.revision, state.etag, state.plan, { keepalive: true }).catch(() => {}); });
}

/**
 * 改节奏：服务器端调用 recipes.cut.repace 重新划分每个停顿保留多长（跳过手改过和锁定的片段），保存后返回新方案。
 * 页面把差异套到当前方案上，记成一条可撤销的历史，并把变了的行标出来（#1）。前端不重算。
 */
async function changePace(id) {
  const label = PACE_LEVELS.find((l) => l.id === id)?.label ?? id;
  if (!state.meta.capabilities.repace) { showPaceFallback(id, label); return; }
  try { await flushSave(); } catch (e) { toast(e.message, { kind: "error" }); return; }
  const t = toast(`正在按“${label}”重新划分停顿…`, { timeout: 0 });
  let r;
  try { r = await api.repace(id, state.revision, state.etag, out().id); } catch (e) {
    t.remove();
    if (e.status === 409) { await absorbDisk(e.body.disk); toast("方案刚被外部修改过，已合并；请再点一次节奏档位", { timeout: 5000 }); return; }
    toast(e.message, { kind: "error", timeout: 9000 });
    return;
  }
  t.remove();
  const theirs = normalizePlan(r.plan);
  const d = diffPlans(state.plan, theirs);
  const skipped = out().segments.filter((s) => (s.locked || s.evidence.edited) && (s.reason === "pause" || s.reason === "silence")).length;
  if (d.changes.length) {
    applyChanges(state.plan, d.changes);
    applyChanges(state.base, d.changes); // 节奏调整不算“我改的”
    state.undo.push(`节奏改成“${label}”`, d.changes, { meta: { base: true } }); // 撤销/重做时 base 也跟着走
  }
  state.revision = r.revision;
  state.etag = r.etag;
  state.synced = clone(theirs);
  state.editSeq++;
  if (d.items.length) setMarks(d.items, "节奏已调");
  await loadOutput();
  refreshHeader();
  toast(r.changed
    ? `节奏改成“${label}”：${r.changed} 个片段变了${skipped ? `；你手改过或锁定的 ${skipped} 个停顿没动` : ""}`
    : `节奏已是“${label}”：没有片段需要改${skipped ? `（手改过或锁定的 ${skipped} 个停顿本来就不动）` : ""}`,
  { kind: "ok", timeout: 7000, action: d.changes.length ? { label: "撤销", onclick: () => undo() } : null });
  announce(`节奏改成${label}，${r.changed} 个片段变了`);
}

/** 节奏重算模块没装好时的退路：给终端命令，不假装能改。 */
function showPaceFallback(id, label) {
  const src = state.plan.sources[mainSource()]?.path ?? "素材.mp4";
  const cmd = `autocut cut "${src}" --pace ${id}`;
  setBanner("pace", {
    kind: "info", title: `想用“${label}”节奏：`,
    body: h("span", {}, "页面里的节奏重算模块还没装好，现在只能重新生成方案（会覆盖你手改过的部分，改之前先备份方案文件）。终端命令：", h("code", { class: "cmd" }, cmd)),
    actions: [{ label: "复制命令", onclick: () => copyText(cmd).then(() => toast("命令已复制", { timeout: 1800 })) }],
  });
}

const isTyping = (t) => t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && t.type !== "checkbox" && t.type !== "range");

function onKey(e) {
  if (e.defaultPrevented || e.isComposing) return;
  if (document.querySelector("dialog[open]")) return;
  if (isTyping(e.target)) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.code === "KeyZ") { e.preventDefault(); undo(e.shiftKey); return; }
  if (mod && e.code === "KeyY") { e.preventDefault(); undo(true); return; }
  if (mod || e.altKey) return;
  const onButton = e.target.closest?.("button, a, summary, [role=menuitem], [role=radio], input") && !e.target.closest?.(".cand-main"); // 候选按钮上空格播放、↑↓ 切换候选
  switch (e.code) {
    case "Space":
      if (onButton) return; // 焦点在按钮上时，空格留给按钮本身
      e.preventDefault(); player.toggle(); return;
    case "KeyJ": e.preventDefault(); stepDecision(1, e.shiftKey); return;
    case "KeyK": e.preventDefault(); stepDecision(-1, e.shiftKey); return;
    case "KeyX": e.preventDefault(); toggleSelected(); return;
    case "KeyL": e.preventDefault(); toggleLock(); return;
    case "KeyE": e.preventDefault(); switchEdge(); return;
    case "KeyA": e.preventDefault(); stepMark(e.shiftKey ? -1 : 1); return;
    case "KeyP": e.preventDefault(); setPlayMode(state.playMode === "plan" ? "orig" : "plan"); return;
    case "KeyS": if (candidateMode()) { e.preventDefault(); toggleCandidate(state.outputId, !out().selected); } return;
    case "Enter": if (!onButton) { e.preventDefault(); auditionSegment(); } return;
    case "BracketLeft": e.preventDefault(); nudge(-1, e.shiftKey); return;
    case "BracketRight": e.preventDefault(); nudge(1, e.shiftKey); return;
    case "ArrowLeft": if (!onButton || e.target === $("#scrub")) { e.preventDefault(); player.seekRelative(e.shiftKey ? -5 : -1); } return;
    case "ArrowRight": if (!onButton || e.target === $("#scrub")) { e.preventDefault(); player.seekRelative(e.shiftKey ? 5 : 1); } return;
    case "ArrowUp": case "ArrowDown":
      if (candidateMode() && !onButton) {
        e.preventDefault();
        const ids = rankedOutputs().map((o) => o.id);
        const i = ids.indexOf(state.outputId) + (e.code === "ArrowDown" ? 1 : -1);
        if (ids[i]) switchOutput(ids[i]);
      }
      return;
    case "Escape":
      if (!$("#drawer").hidden) closeDrawer();
      return;
    default:
  }
  if (e.key === "?") { e.preventDefault(); $("#help").showModal(); }
}

window.__autocut = { state, get player() { return player; }, get timeline() { return timeline; }, get transcript() { return transcript; } }; // 便于在控制台和自动化里检查
boot();
