// 外部修改的差异标记、三方合并、冲突解决、撤销；以及"拿不准"类型、抽查顺序、体检动作。
// 运行：cd src/autocut/review/static && node --test
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  UndoStack, applyChange, applyChanges, canKeepMoreLess, captionMode, checkActions, clone, confirmSegment, deepEqual, describeConflictSide,
  describeFields, diffPlans, editState, isWholeVideoItem, keepMoreLess, mergePlans, moveBoundary, normalizePlan, normalizeReport,
  ensureReframe, findCue, resolveConflict, setAction, setCaptionMode, setLocked, setOutputSize, sizeForPreset, sizePresetOf, spotCheckOrder, uncertaintyKind,
} from "./model.js";

function plan(extra = {}) {
  return normalizePlan({
    recipe: "cut",
    sources: { main: { path: "talk.mp4", duration: 20, width: 640, height: 360 } },
    options: { pace: "natural" },
    outputs: [{
      id: "main",
      segments: [
        { id: "s1", source: "main", start: 0, end: 3, action: "keep", reason: "speech", text: "一" },
        { id: "s2", source: "main", start: 3, end: 5, action: "cut", reason: "silence", confidence: 0.95, detail: "停顿 2s" },
        { id: "s3", source: "main", start: 5, end: 8, action: "keep", reason: "speech", text: "三" },
        { id: "s4", source: "main", start: 8, end: 9, action: "cut", reason: "filler", confidence: 0.6, text: "嗯" },
        { id: "s5", source: "main", start: 9, end: 12, action: "keep", reason: "speech", text: "五" },
        { id: "s6", source: "main", start: 12, end: 14, action: "cut", reason: "retake", confidence: 0.9, text: "重说" },
        { id: "s7", source: "main", start: 14, end: 20, action: "keep", reason: "speech", text: "七" },
      ],
    }],
    ...extra,
  });
}
const seg = (p, id) => p.outputs[0].segments.find((s) => s.id === id);

// ---------- 差异与撤销 ----------

test("diffPlans：字段改动、新增、删除、顺序；applyChanges 往返", () => {
  const a = plan();
  const b = clone(a);
  seg(b, "s2").action = "keep";
  seg(b, "s2").confidence = 1;
  seg(b, "s3").start = 5.5;
  b.outputs[0].segments.splice(3, 1); // 删 s4
  b.outputs[0].segments.push({ ...clone(seg(a, "s1")), id: "n1", start: 20, end: 21 }); // 新增
  b.outputs[0].selected = false;
  b.options.pace = "relaxed";
  const d = diffPlans(a, b);
  const byId = Object.fromEntries(d.items.filter((x) => x.seg).map((x) => [x.seg, x]));
  assert.deepEqual(Object.keys(byId["s2"].fields).sort(), ["action", "confidence"]);
  assert.deepEqual(byId["s2"].fields.action, ["cut", "keep"]);
  assert.equal(byId["s4"].op, "remove");
  assert.equal(byId["n1"].op, "insert");
  assert.ok(d.items.some((x) => x.output === "main" && x.seg === null && "selected" in x.fields));
  assert.ok(d.items.some((x) => x.output === null && "options" in x.fields));

  const c = clone(a);
  applyChanges(c, d.changes, "after");
  assert.deepEqual(c, b);
  applyChanges(c, d.changes, "before");
  assert.deepEqual(c, a);
});

test("diffPlans：顺序变了也能应用和撤回", () => {
  const a = plan();
  const b = clone(a);
  const [x] = b.outputs[0].segments.splice(0, 1);
  b.outputs[0].segments.push(x);
  const d = diffPlans(a, b);
  assert.ok(d.changes.some((c) => c.op === "order"));
  const c = clone(a);
  applyChanges(c, d.changes, "after");
  assert.deepEqual(c.outputs[0].segments.map((s) => s.id), b.outputs[0].segments.map((s) => s.id));
  applyChanges(c, d.changes, "before");
  assert.deepEqual(c, a);
});

test("diffPlans：删除多个片段，撤回后位置和顺序都对", () => {
  const a = plan();
  const b = clone(a);
  b.outputs[0].segments = b.outputs[0].segments.filter((s) => !["s2", "s4", "s6"].includes(s.id));
  const d = diffPlans(a, b);
  const c = clone(a);
  applyChanges(c, d.changes, "after");
  assert.deepEqual(c.outputs[0].segments.map((s) => s.id), ["s1", "s3", "s5", "s7"]);
  applyChanges(c, d.changes, "before");
  assert.deepEqual(c, a);
});

test("外部改动作为一条可撤销的记录：撤销栈保留之前的历史", () => {
  const p = plan();
  const u = new UndoStack();
  u.push("我先改了 s4", setAction(p.outputs[0], "s4", "keep"));
  const theirs = clone(p);
  seg(theirs, "s2").action = "keep";
  const d = diffPlans(p, theirs);
  applyChanges(p, d.changes, "after");
  u.push("agent 的改动（1 处）", d.changes);
  assert.equal(seg(p, "s2").action, "keep");
  assert.equal(u.past.length, 2); // 之前那条还在
  u.undo(p);
  assert.equal(seg(p, "s2").action, "cut"); // 拒绝 agent 的改动
  assert.equal(seg(p, "s4").action, "keep");
  u.undo(p);
  assert.equal(seg(p, "s4").action, "cut");
});

test("applyChange：方案级（options）和结构操作", () => {
  const p = plan();
  applyChange(p, { output: null, seg: null, before: { options: { pace: "natural" } }, after: { options: { pace: "relaxed" } } }, "after");
  assert.equal(p.options.pace, "relaxed");
  applyChange(p, { output: "main", seg: "s9", op: "insert", index: 1, data: { id: "s9", source: "main", start: 1, end: 2 } }, "after");
  assert.equal(p.outputs[0].segments[1].id, "s9");
  applyChange(p, { output: "main", seg: "s9", op: "insert", index: 1, data: { id: "s9" } }, "after"); // 重复应用不重复插入
  assert.equal(p.outputs[0].segments.filter((s) => s.id === "s9").length, 1);
  applyChange(p, { output: "main", seg: "s9", op: "insert", index: 1, data: {} }, "before");
  assert.ok(!p.outputs[0].segments.some((s) => s.id === "s9"));
});

// ---------- 三方合并 ----------

test("mergePlans：两边改了不同片段 → 自动合并，两边的改动都在", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep"); // 我：把"嗯"改回保留
  const theirs = clone(base);
  seg(theirs, "s2").action = "keep"; // agent：长停顿改回保留
  seg(theirs, "s2").confidence = 1;
  seg(theirs, "s6").action = "keep"; // agent：重说也改了
  const r = mergePlans(base, mine, theirs);
  assert.equal(r.conflicts.length, 0);
  assert.equal(seg(r.plan, "s4").action, "keep"); // 我的还在
  assert.equal(seg(r.plan, "s2").action, "keep"); // agent 的进来了
  assert.equal(seg(r.plan, "s6").action, "keep");
  assert.equal(r.applied, 2);
  assert.equal(seg(mine, "s2").action, "cut", "不改动入参");
});

test("mergePlans：同一片段改了不同字段 → 字段级自动合并", () => {
  const base = plan();
  const mine = clone(base);
  seg(mine, "s3").locked = true;
  const theirs = clone(base);
  seg(theirs, "s3").start = 5.4;
  const r = mergePlans(base, mine, theirs);
  assert.equal(r.conflicts.length, 0);
  assert.equal(seg(r.plan, "s3").locked, true);
  assert.equal(seg(r.plan, "s3").start, 5.4);
});

test("mergePlans：同一片段同一字段改得不一样 → 冲突；合并结果先保留我的值（不丢我的改动）", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep");
  const theirs = clone(base);
  setAction(theirs.outputs[0], "s4", "cut"); // agent 明确确认"剪掉"；我明确改回"保留"：结论相反
  seg(theirs, "s2").action = "keep";
  const r = mergePlans(base, mine, theirs);
  assert.equal(r.conflicts.length, 1);
  const g = r.conflicts[0];
  assert.deepEqual(g.segIds, ["s4"]);
  assert.equal(g.kind, "edit");
  const act = g.items.find((i) => i.path === "action");
  assert.deepEqual([act.base, act.mine, act.theirs], ["cut", "keep", "cut"]);
  assert.equal(seg(r.plan, "s4").action, "keep", "冲突期间保留我的值");
  assert.equal(seg(r.plan, "s2").action, "keep", "不冲突的 agent 改动照样合并");
  assert.match(describeConflictSide(g, "mine"), /剪掉 → 保留/);
  assert.match(describeConflictSide(g, "theirs"), /动作|剪掉/);
});

test("两边把同一处改成一样 → 不算冲突", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep");
  const theirs = clone(base);
  setAction(theirs.outputs[0], "s4", "keep");
  const r = mergePlans(base, mine, theirs);
  assert.equal(r.conflicts.length, 0);
  assert.equal(seg(r.plan, "s4").action, "keep");
});

test("resolveConflict：选 agent 的 → 写入对方的值，可撤销；选我的 → 不动", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep");
  const theirs = clone(base);
  seg(theirs, "s4").start = 8.5; // agent 改了边界，我改了动作：不同字段，不冲突
  seg(theirs, "s6").action = "keep";
  const mine2 = clone(mine);
  seg(mine2, "s6").action = "cut"; // 先让 s6 冲突
  seg(mine2, "s6").start = 12.3;
  seg(theirs, "s6").start = 12.6;
  const r = mergePlans(base, mine2, theirs);
  const g = r.conflicts.find((c) => c.segIds.includes("s6"));
  assert.ok(g);
  assert.deepEqual(resolveConflict(r.plan, g, "mine"), []);
  assert.equal(seg(r.plan, "s6").start, 12.3);
  const ch = resolveConflict(r.plan, g, "theirs");
  assert.equal(seg(r.plan, "s6").start, 12.6);
  applyChanges(r.plan, ch, "before");
  assert.equal(seg(r.plan, "s6").start, 12.3);
});

test("agent 删除了我改过的片段 → 冲突（选 agent 的就删除，可撤销）；我没动的 → 跟着删", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep");
  const theirs = clone(base);
  theirs.outputs[0].segments = theirs.outputs[0].segments.filter((s) => s.id !== "s4" && s.id !== "s6");
  const r = mergePlans(base, mine, theirs);
  assert.ok(!r.plan.outputs[0].segments.some((s) => s.id === "s6"), "我没动过 s6：跟着删");
  assert.equal(r.conflicts.length, 1);
  assert.equal(r.conflicts[0].kind, "removed");
  assert.ok(r.plan.outputs[0].segments.some((s) => s.id === "s4"), "我改过的先留着");
  const ch = resolveConflict(r.plan, r.conflicts[0], "theirs");
  assert.ok(!r.plan.outputs[0].segments.some((s) => s.id === "s4"));
  applyChanges(r.plan, ch, "before");
  assert.equal(r.plan.outputs[0].segments.findIndex((s) => s.id === "s4"), 3);
});

test("agent 新增的片段 → 加进来，顺序以 agent 的为准", () => {
  const base = plan();
  const mine = clone(base);
  seg(mine, "s1").locked = true;
  const theirs = clone(base);
  theirs.outputs[0].segments.splice(2, 0, { ...clone(seg(base, "s1")), id: "n1", start: 5, end: 5.01 });
  const r = mergePlans(base, mine, theirs);
  assert.deepEqual(r.plan.outputs[0].segments.map((s) => s.id).slice(0, 4), ["s1", "s2", "n1", "s3"]);
  assert.equal(seg(r.plan, "s1").locked, true);
});

test("相邻的冲突片段合成一组：一次剪口的两边要一起选", () => {
  const base = plan();
  const mine = clone(base);
  moveBoundary(mine.outputs[0], mine.sources, "s2", "end", 5.4); // s2.end 和 s3.start 一起动
  const theirs = clone(base);
  moveBoundary(theirs.outputs[0], theirs.sources, "s2", "end", 4.6);
  const r = mergePlans(base, mine, theirs);
  assert.equal(r.conflicts.length, 1);
  assert.deepEqual(r.conflicts[0].segIds, ["s2", "s3"]);
  resolveConflict(r.plan, r.conflicts[0], "theirs");
  assert.equal(seg(r.plan, "s2").end, 4.6);
  assert.equal(seg(r.plan, "s3").start, 4.6, "两边一起换，不会留缝或重叠");
});

test("输出级字段（是否选中）冲突也按组处理；方案级 options 以 agent 为准", () => {
  const base = plan();
  const mine = clone(base);
  mine.outputs[0].selected = false;
  const theirs = clone(base);
  theirs.outputs[0].selected = true;
  theirs.outputs[0].title = "新标题";
  theirs.options.pace = "relaxed";
  const r = mergePlans(base, mine, theirs);
  assert.equal(r.conflicts.length, 0, "theirs 把 selected 改成 true == base，没有改，所以是我的赢");
  assert.equal(r.plan.outputs[0].selected, false);
  assert.equal(r.plan.outputs[0].title, "新标题");
  assert.equal(r.plan.options.pace, "relaxed");
  theirs.outputs[0].selected = false;
  const base2 = clone(base); base2.outputs[0].selected = true;
  const mine2 = clone(base2); mine2.outputs[0].selected = false;
  const theirs2 = clone(base2); theirs2.outputs[0].title = "x";
  assert.equal(mergePlans(base2, mine2, theirs2).conflicts.length, 0);
  const mine3 = clone(base2); mine3.outputs[0].reframe = { mode: "center" };
  const theirs3 = clone(base2); theirs3.outputs[0].reframe = { mode: "track", keyframes: [[0, 0.3, 0.5]] };
  const r3 = mergePlans(base2, mine3, theirs3);
  assert.equal(r3.conflicts.length, 1);
  assert.equal(r3.conflicts[0].kind, "output");
  resolveConflict(r3.plan, r3.conflicts[0], "theirs");
  assert.equal(r3.plan.outputs[0].reframe.mode, "track");
});

test("合并是幂等的：agent 没改任何东西时 merge(base, mine, base) == mine", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep");
  setLocked(mine.outputs[0], "s1", true);
  const r = mergePlans(base, mine, clone(base));
  assert.deepEqual(r.plan, mine);
  assert.equal(r.conflicts.length, 0);
  assert.equal(r.applied, 0);
});

test("冲突期间人做的修改不会丢：合并 → 继续改 → 再合并", () => {
  const base = plan();
  const mine = clone(base);
  setAction(mine.outputs[0], "s4", "keep");
  const theirs = clone(base);
  setAction(theirs.outputs[0], "s4", "cut");
  seg(theirs, "s2").action = "keep";
  const r1 = mergePlans(base, mine, theirs);
  const working = r1.plan; // 页面上现在的方案：冲突还没解决
  setAction(working.outputs[0], "s6", "keep"); // 冲突期间继续编辑
  // 用户选 agent 的：只改冲突的那一组，别的不受影响
  resolveConflict(working, r1.conflicts[0], "theirs");
  assert.equal(seg(working, "s4").action, "cut");
  assert.equal(seg(working, "s6").action, "keep", "冲突期间的改动还在");
  assert.equal(seg(working, "s2").action, "keep", "agent 的不冲突改动还在");
});

test("describeFields / describeFieldChange", () => {
  assert.equal(describeFields({ action: ["keep", "cut"], confidence: [0.9, 1], evidence: [{}, { edited: true }] }), "保留 → 剪掉");
  assert.equal(describeFields({ start: [3, 3.5], locked: [false, true] }), "起点 3.00 → 3.50，锁定");
  assert.equal(describeFields({ evidence: [{}, {}] }), "改了");
});

// ---------- 拿不准的类型、确认、抽查 ----------

test("uncertaintyKind：切点类、内容类、动作类", () => {
  const lead = { reason: "lead_in", detail: "铺垫 3.0s；起点硬切（没找到停顿）" };
  assert.deepEqual(uncertaintyKind(lead, { candidate: true }), { kind: "boundary", edges: ["start"] });
  const hl = { reason: "highlight", detail: "弹幕 6.1× 爆发；结尾硬切（没找到停顿）" };
  assert.deepEqual(uncertaintyKind(hl, { candidate: true }), { kind: "boundary", edges: ["end"] });
  const both = { reason: "highlight", detail: "弹幕爆发；这里一直有人说话，找不到不打断句子的位置，只能在电平最低处硬切" };
  assert.deepEqual(uncertaintyKind(both, { candidate: true }), { kind: "boundary", edges: ["start", "end"] });
  assert.equal(uncertaintyKind({ reason: "highlight", detail: "只有弹幕，没有声音佐证" }, { candidate: true }).kind, "content");
  assert.equal(uncertaintyKind({ reason: "highlight", detail: "x" }, { candidate: false }).kind, "content");
  assert.equal(uncertaintyKind({ reason: "silence", detail: "停顿前后在讲代码，可能是故意留白" }).kind, "action");
  assert.equal(uncertaintyKind({ reason: "filler", detail: "" }).kind, "action");
});

test("confirmSegment：只确认不改动，记 confirmed 而不是 edited；可撤销", () => {
  const p = plan();
  const u = new UndoStack();
  u.push("就这样", confirmSegment(p.outputs[0], "s4"));
  const s = seg(p, "s4");
  assert.equal(s.confidence, 1);
  assert.equal(s.action, "cut");
  assert.equal(s.evidence.confirmed, true);
  assert.equal(s.evidence.edited, undefined);
  u.undo(p);
  assert.equal(s.confidence, 0.6);
  assert.equal(s.evidence.confirmed, undefined);
});

test("editState：已改 vs 已确认 vs 没动", () => {
  const base = plan();
  const p = clone(base);
  assert.equal(editState(seg(p, "s4"), seg(base, "s4")), null);
  confirmSegment(p.outputs[0], "s4");
  assert.equal(editState(seg(p, "s4"), seg(base, "s4")), "confirmed");
  setAction(p.outputs[0], "s2", "keep");
  assert.equal(editState(seg(p, "s2"), seg(base, "s2")), "changed");
  setAction(p.outputs[0], "s2", "cut"); // 改回原样：动作回到原来，但标了 edited → 算确认
  assert.equal(editState(seg(p, "s2"), seg(base, "s2")), "confirmed");
  assert.equal(editState(seg(p, "s2"), undefined), null);
});

test("spotCheckOrder：没有拿不准时，按把握度从低到高，同分长的在前；确认过的不再出现", () => {
  const p = plan();
  seg(p, "s4").confidence = 0.9; // 现在没有拿不准的
  seg(p, "s2").confidence = 0.95;
  seg(p, "s6").confidence = 0.9;
  assert.deepEqual(spotCheckOrder(p.outputs[0]), ["s6", "s4", "s2"]);
  confirmSegment(p.outputs[0], "s6");
  assert.deepEqual(spotCheckOrder(p.outputs[0]), ["s4", "s2"]);
  assert.ok(!spotCheckOrder(p.outputs[0]).includes("s1"), "普通说话不是决定");
});

test("keepMoreLess：多留 = 动和保留片段相接的那条边；到头时不报错、不改动", () => {
  const p = plan();
  const o = p.outputs[0];
  assert.equal(canKeepMoreLess(o, "s2"), true);
  assert.equal(canKeepMoreLess(o, "s1"), false);
  const r = keepMoreLess(o, p.sources, "s2", 0.5); // s2 前面是保留的 s1 → 起点 3 → 3.5
  assert.equal(r.edge, "start");
  assert.equal(seg(p, "s1").end, 3.5);
  assert.equal(seg(p, "s2").start, 3.5);
  const r2 = keepMoreLess(o, p.sources, "s2", -0.5); // 少留
  assert.equal(seg(p, "s2").start, 3);
  assert.equal(r2.changes.length, 2);
  const r3 = keepMoreLess(o, p.sources, "s2", 99); // 夹到 hi
  assert.ok(seg(p, "s2").start < 5 && seg(p, "s2").start > 4.9);
  assert.ok(r3.changes.length > 0);
  assert.throws(() => keepMoreLess(o, p.sources, "s1", 0.5), /没法/);
  // 后面是保留的、前面不是：动终点
  const q = plan();
  q.outputs[0].segments[0].action = "cut";
  q.outputs[0].segments[1].action = "keep";
  const r4 = keepMoreLess(q.outputs[0], q.sources, "s1", 0.5);
  assert.equal(r4.edge, "end");
  assert.equal(seg(q, "s1").end, 2.5);
});

// ---------- 字幕、尺寸 ----------

test("字幕模式：off / srt / burn 对应 captions.enabled / burn；可撤销", () => {
  const p = plan();
  const o = p.outputs[0];
  assert.equal(captionMode(o), "off");
  const u = new UndoStack();
  u.push("字幕烧进画面", setCaptionMode(o, "burn"));
  assert.deepEqual([o.captions.enabled, o.captions.burn], [true, true]);
  assert.equal(captionMode(o), "burn");
  setCaptionMode(o, "srt");
  assert.deepEqual([o.captions.enabled, o.captions.burn], [true, false]);
  setCaptionMode(o, "off");
  assert.deepEqual([o.captions.enabled, o.captions.burn], [false, false]);
  assert.equal(o.captions.max_chars, 16, "不丢其它字幕设置");
  u.undo(p);
  assert.equal(captionMode(o), "off");
  assert.throws(() => setCaptionMode(o, "x"), /字幕模式只能是/);
});

test("成片尺寸预设：竖屏/横屏/720p/1080p/跟随素材", () => {
  const p = plan(); // 素材 640×360
  const o = p.outputs[0];
  assert.deepEqual(sizeForPreset(o, p.sources, "vertical"), { width: 1080, height: 1920 });
  assert.deepEqual(sizeForPreset(o, p.sources, "horizontal"), { width: 1920, height: 1080 });
  assert.deepEqual(sizeForPreset(o, p.sources, "720p"), { width: 1280, height: 720 });
  assert.deepEqual(sizeForPreset(o, p.sources, "1080p"), { width: 1920, height: 1080 });
  o.width = 1080; o.height = 1920;
  assert.deepEqual(sizeForPreset(o, p.sources, "720p"), { width: 720, height: 1280 }, "保持竖屏方向");
  assert.equal(sizePresetOf(o), "vertical");
  const u = new UndoStack();
  u.push("尺寸", setOutputSize(o, sizeForPreset(o, p.sources, "source")));
  assert.equal(o.width, null);
  assert.equal(sizePresetOf(o), "source");
  u.undo(p);
  assert.deepEqual([o.width, o.height], [1080, 1920]);
  assert.throws(() => sizeForPreset(o, p.sources, "4k"), /不认识/);
});

// ---------- 体检：全片问题与动作 ----------

test("normalizeReport：全片问题归到 whole，不上时间轴；按严重度排序", () => {
  const rep = normalizeReport({
    duration: 28,
    summary: "0 项风险、3 项提醒。这些是留存风险信号，不是爆款保证。",
    disclaimer: "这些是留存风险信号，不是爆款保证。",
    items: [
      { id: "loudness_low", severity: "warn", at: 0, end: 28, message: "整合响度 -18.7 LUFS", fix: "标准化" },
      { id: "start_midspeech", severity: "warn", at: 0, end: 0.2, message: "第一帧就在说话", fix: "补 0.3s" },
      { id: "dead_air", severity: "risk", at: 10, end: 12, message: "有 2s 没声音", fix: "缩短" },
      { id: "captions_sidecar", severity: "info", at: 0, end: 28, message: "字幕是单独的文件", fix: "烧录" },
    ],
  });
  assert.deepEqual(rep.items.map((x) => x.code), ["dead_air", "start_midspeech", "loudness_low", "captions_sidecar"]);
  assert.deepEqual(rep.whole.map((x) => x.code).sort(), ["captions_sidecar", "loudness_low"]);
  assert.deepEqual(rep.timed.map((x) => x.code), ["dead_air", "start_midspeech"]);
  assert.equal(rep.summary, "0 项风险、3 项提醒。");
  assert.equal(isWholeVideoItem({ t: 0, end: 28 }, 28), true);
  assert.equal(isWholeVideoItem({ t: 0, end: 3 }, 28), false);
});

test("checkActions：每类建议都有页面里能做的事，做不到的明说", () => {
  const ctx = { duration: 28, captionMode: "srt", sourcePath: "talk.mp4" };
  const kinds = (code, extra = {}) => checkActions({ code, t: 0, end: 28, message: "", ...extra }, ctx).map((a) => a.kind);
  assert.deepEqual(kinds("start_midspeech"), ["trim", "jump-edge"]);
  assert.deepEqual(checkActions({ code: "end_midsentence", t: 25, end: 28 }, ctx).map((a) => [a.kind, a.which, a.delta]), [["trim", "end", 1], ["jump-edge", "end", undefined]]);
  assert.deepEqual(kinds("loudness_low"), ["loudnorm"]);
  assert.deepEqual(kinds("true_peak"), ["loudnorm"]);
  assert.deepEqual(kinds("captions_sidecar"), ["captions"]);
  assert.deepEqual(checkActions({ code: "captions_sidecar" }, { ...ctx, captionMode: "burn" }), [], "已经烧录就不再建议");
  assert.deepEqual(kinds("resolution"), ["size", "size"]);
  assert.deepEqual(checkActions({ code: "aspect", message: "画幅 640x360，抖音是竖屏平台（9:16）" }, ctx)[0].preset, "vertical");
  assert.deepEqual(checkActions({ code: "aspect", message: "画幅 1080x1920 是竖屏，B 站以横屏为主" }, ctx)[0].preset, "horizontal");
  assert.deepEqual(kinds("hook_late", { t: 0, end: 12 }), ["jump-edge", "export"], "挪句子顺序做不到：写明在剪辑软件里做");
  assert.deepEqual(kinds("static_long", { t: 5, end: 20 }), ["jump", "export"]);
  assert.deepEqual(checkActions({ code: "no_words" }, ctx)[0], { kind: "copy", label: "复制命令", text: 'autocut analyze "talk.mp4"' });
  assert.deepEqual(kinds("dead_air", { t: 10, end: 12 }), ["jump", "filter-silence"]);
  assert.deepEqual(checkActions({ code: "unknown_code", t: 3, end: 4 }, ctx).map((a) => a.kind), ["jump"]);
});

test("deepEqual", () => {
  assert.ok(deepEqual({ a: [1, { b: 2 }] }, { a: [1, { b: 2 }] }));
  assert.ok(!deepEqual({ a: 1 }, { a: 1, b: 2 }));
  assert.ok(!deepEqual([1], { 0: 1 }));
});

test("findCue：二分查找当前字幕；缝隙里和结尾之后没有字幕", () => {
  const cues = [{ start: 0.2, end: 1.5, text: "a" }, { start: 1.5, end: 3, text: "b" }, { start: 4, end: 5, text: "c" }];
  assert.equal(findCue(cues, 0.1), null);
  assert.equal(findCue(cues, 0.2).text, "a");
  assert.equal(findCue(cues, 1.5).text, "b");
  assert.equal(findCue(cues, 3.5), null);
  assert.equal(findCue(cues, 4.99).text, "c");
  assert.equal(findCue(cues, 5), null);
  assert.equal(findCue([], 1), null);
});

test("ensureReframe：没有裁切框就补居中的，可撤销；已有就不动", () => {
  const p = plan();
  const o = p.outputs[0];
  const u = new UndoStack();
  u.push("补裁切框", ensureReframe(o));
  assert.deepEqual(o.reframe, { mode: "center", keyframes: [] });
  assert.deepEqual(ensureReframe(o), []);
  u.undo(p);
  assert.equal(o.reframe, null);
});
