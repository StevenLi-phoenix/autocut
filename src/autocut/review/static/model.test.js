// 运行：cd src/autocut/review/static && node --test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  UndoStack, applyChange, boundaryRange, bucketPeaks, buildPlaylist, cropRect, diffCount, fmtTime, fromOutputTime,
  isCandidateMode, isDecision, isUncertain, locatePlaylist, mapSourceTime, moveBoundary, moveBoundaryClamped, normalizePlan,
  outputDuration, paceId, planStats, reframeCenter, setAction, setLocked, setSelected, skipList, timeline, toOutputTime,
  trimOutput, wordIndexAt, wordsText,
} from "./model.js";

// 和 tests/test_plan.py 的 make_plan 同一份：0–3 留，3–5 剪（长停顿），5–6 剪（口头禅 0.6），6–9.5 留
function makePlan() {
  return normalizePlan({
    recipe: "cut",
    sources: { main: { path: "talk.mp4", duration: 10 } },
    outputs: [{
      id: "main",
      segments: [
        { id: "d001", source: "main", start: 0, end: 3, action: "keep", reason: "speech", text: "大家好" },
        { id: "d002", source: "main", start: 3, end: 5, action: "cut", reason: "silence", confidence: 0.96, detail: "停顿 2.0s" },
        { id: "d003", source: "main", start: 5, end: 6, action: "cut", reason: "filler", confidence: 0.6, text: "嗯" },
        { id: "d004", source: "main", start: 6, end: 9.5, action: "keep", reason: "speech", text: "今天聊剪辑" },
      ],
    }],
  });
}

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} !≈ ${b}`);

// ---------- 时间映射 ----------

test("timeline / outputDuration 对应 Output.timeline()", () => {
  const o = makePlan().outputs[0];
  assert.deepEqual(timeline(o).map((x) => [x.seg.id, x.o0, x.o1]), [["d001", 0, 3], ["d004", 3, 6.5]]);
  near(outputDuration(o), 6.5);
});

test("素材时间 → 成片时间：保留段精确，剪掉的返回 null / 恢复处", () => {
  const o = makePlan().outputs[0];
  near(toOutputTime(o, "main", 1.5), 1.5);
  near(toOutputTime(o, "main", 7), 4);
  assert.equal(toOutputTime(o, "main", 4), null);
  assert.deepEqual(mapSourceTime(o, "main", 4), { out: 3, kept: false }); // 播放会在成片 3s 处恢复
  assert.deepEqual(mapSourceTime(o, "main", 9.8), { out: 6.5, kept: false }); // 超过最后一个保留段 → 成片末尾
});

test("成片时间 → 素材时间，往返一致", () => {
  const o = makePlan().outputs[0];
  const r = fromOutputTime(o, 4);
  assert.equal(r.seg.id, "d004");
  near(r.t, 7);
  assert.equal(fromOutputTime(o, -1), null);
  assert.equal(fromOutputTime(o, 7), null);
  near(fromOutputTime(o, 6.5).t, 9.5); // 恰好等于总时长：最后一段的末尾
  for (const t of [0, 1, 2.9, 6, 7, 9.4]) near(fromOutputTime(o, toOutputTime(o, "main", t)).t, t);
});

test("变速片段：成片时间按 speed 折算", () => {
  const o = makePlan().outputs[0];
  o.segments[3].speed = 2; // 3.5s 的素材 → 1.75s
  near(outputDuration(o), 3 + 1.75);
  near(toOutputTime(o, "main", 7.0), 3.5);
  near(fromOutputTime(o, 3.5).t, 7.0);
});

// ---------- 播放列表 / 跳过列表 ----------

test("跳过列表：只在需要 seek 的地方出现，连续的剪掉区间合并成一次跳转", () => {
  const o = makePlan().outputs[0];
  assert.deepEqual(skipList(o, {}), [{ source: "main", at: 3, to: 6, toSource: "main", outTime: 3 }]);
});

test("相邻的保留段合并；把剪掉的改回保留后不再有跳转", () => {
  const p = makePlan();
  const o = p.outputs[0];
  setAction(o, "d002", "keep");
  setAction(o, "d003", "keep");
  assert.equal(skipList(o, {}).length, 0);
  const pl = buildPlaylist(o, {});
  assert.equal(pl.length, 1);
  assert.deepEqual([pl[0].start, pl[0].end, pl[0].o1], [0, 9.5, 9.5]);
  assert.deepEqual(pl[0].segIds, ["d001", "d002", "d003", "d004"]);
});

test("速度不同的相邻保留段不合并；图片素材不进播放列表", () => {
  const o = makePlan().outputs[0];
  setAction(o, "d002", "keep");
  setAction(o, "d003", "keep");
  o.segments[1].speed = 1.5;
  assert.equal(buildPlaylist(o, {}).length, 3);
  const withImg = buildPlaylist(o, { main: { kind: "image" } });
  assert.equal(withImg.length, 0);
});

test("素材中靠后的片段排在前面（组装类输出）：播放列表按输出顺序，跳转可以往回跳", () => {
  const o = normalizePlan({
    sources: { a: { path: "a.mp4", duration: 20 }, b: { path: "b.mp4", duration: 10 } },
    outputs: [{ id: "m", segments: [
      { id: "s1", source: "a", start: 10, end: 12, reason: "shot" },
      { id: "s2", source: "a", start: 2, end: 4, reason: "shot" },
      { id: "s3", source: "b", start: 0, end: 1, reason: "shot" },
    ] }],
  }).outputs[0];
  const jumps = skipList(o, {});
  assert.deepEqual(jumps.map((j) => [j.source, j.at, j.to, j.toSource]), [["a", 12, 2, "a"], ["a", 4, 0, "b"]]);
});

test("locatePlaylist：落在保留段、落在剪掉区间、在末尾之后", () => {
  const pl = buildPlaylist(makePlan().outputs[0], {});
  assert.deepEqual(locatePlaylist(pl, "main", 1), { index: 0, inside: true });
  assert.deepEqual(locatePlaylist(pl, "main", 4), { index: 1, inside: false }); // 剪掉区间 → 下一项
  assert.deepEqual(locatePlaylist(pl, "main", 6), { index: 1, inside: true });
  assert.deepEqual(locatePlaylist(pl, "main", 9.7), { index: -1, inside: false });
  assert.deepEqual(locatePlaylist(pl, "other", 1), { index: -1, inside: false });
});

test("locatePlaylist：同一段素材被用了两次时，优先取当前位置之后的那一项", () => {
  const pl = buildPlaylist(normalizePlan({
    sources: { a: { path: "a.mp4", duration: 20 } },
    outputs: [{ id: "m", segments: [
      { id: "s1", source: "a", start: 0, end: 2 }, { id: "s2", source: "a", start: 5, end: 6 }, { id: "s3", source: "a", start: 0, end: 2 },
    ] }],
  }).outputs[0], {});
  assert.equal(locatePlaylist(pl, "a", 1, 0).index, 0);
  assert.equal(locatePlaylist(pl, "a", 1, 2).index, 2);
});

// ---------- 修改 ----------

test("setAction 和 plan.set_action 一致：改动作、把握度置 1、打 edited 标记；reason 保留", () => {
  const o = makePlan().outputs[0];
  setAction(o, "d003", "keep");
  const s = o.segments[2];
  assert.equal(s.action, "keep");
  assert.equal(s.confidence, 1);
  assert.equal(s.evidence.edited, true);
  assert.equal(s.reason, "filler");
  assert.throws(() => setAction(o, "d003", "drop"), /action 只能是/);
  assert.throws(() => setAction(o, "zzz", "cut"), /没有 id 为 'zzz' 的片段/);
});

test("isUncertain / isDecision", () => {
  const o = makePlan().outputs[0];
  assert.deepEqual(o.segments.map(isUncertain), [false, false, true, false]);
  assert.deepEqual(o.segments.map(isDecision), [false, true, true, false]);
  o.segments[0].reason = "pause";
  assert.equal(isDecision(o.segments[0]), false); // 保留的自然停顿不算决定
  o.segments[0].reason = "highlight";
  assert.equal(isDecision(o.segments[0]), true);
});

test("moveBoundary：相接的相邻片段一起动，范围夹在邻居之内", () => {
  const p = makePlan();
  const o = p.outputs[0];
  const ch = moveBoundary(o, p.sources, "d002", "start", 3.5);
  assert.equal(o.segments[0].end, 3.5); // d001.end 跟着动
  assert.equal(o.segments[1].start, 3.5);
  assert.equal(ch.length, 2);
  assert.throws(() => moveBoundary(o, p.sources, "d002", "start", 0.0), /d002\.start=0\.000 超出允许范围 \[0\.010, 4\.990\]/);
  assert.throws(() => moveBoundary(o, p.sources, "d002", "end", 6.5), /超出允许范围/);
  assert.throws(() => moveBoundary(o, p.sources, "d002", "mid", 1), /edge 只能是/);
});

test("moveBoundary：不相接的边界只动自己；最后一段的终点不能超过素材时长", () => {
  const p = makePlan();
  const o = p.outputs[0];
  o.segments[1].start = 3.2; // 和 d001.end=3 之间留了缝
  moveBoundary(o, p.sources, "d002", "start", 3.4);
  assert.equal(o.segments[0].end, 3);
  assert.equal(o.segments[1].start, 3.4);
  const r = boundaryRange(o, p.sources, "d004", "end");
  assert.equal(r.hi, 10);
  assert.throws(() => moveBoundary(o, p.sources, "d004", "end", 10.5), /超出允许范围/);
});

test("moveBoundaryClamped：拖过头时夹在边界上", () => {
  const p = makePlan();
  const o = p.outputs[0];
  moveBoundaryClamped(o, p.sources, "d002", "end", 99); // 和 d003 相接，hi = d003.end - 0.01
  assert.equal(o.segments[1].end, 5.99);
  assert.equal(o.segments[2].start, 5.99);
});

test("与 Python plan.move_boundary 逐例一致（golden 由 tests/test_review_server.py 校验是最新的）", () => {
  const g = JSON.parse(readFileSync(new URL("./testdata/move_boundary.golden.json", import.meta.url), "utf8"));
  assert.ok(g.cases.length >= 10);
  g.cases.forEach((c, i) => {
    const p = normalizePlan(g.plan);
    const o = p.outputs[0];
    const exp = g.expected[i];
    if (exp.error) {
      assert.throws(() => moveBoundary(o, p.sources, c.seg, c.edge, c.t), (e) => e.message === exp.error, `case ${i}`);
    } else {
      moveBoundary(o, p.sources, c.seg, c.edge, c.t);
      assert.deepEqual(o.segments.map((s) => [s.start, s.end]), exp.segments, `case ${i}`);
      assert.equal(o.segments.find((s) => s.id === c.seg).evidence.edited, true);
    }
  });
});

test("trimOutput：候选的开头结尾 ±1s，夹在素材范围内", () => {
  const p = makePlan();
  const o = p.outputs[0];
  trimOutput(o, p.sources, "start", -1); // 已经在 0，夹住
  assert.equal(o.segments[0].start, 0);
  trimOutput(o, p.sources, "end", 1);
  assert.equal(o.segments[3].end, 10); // 9.5 + 1 夹到素材时长 10
  trimOutput(o, p.sources, "start", 1);
  assert.equal(o.segments[0].start, 1);
  for (const s of o.segments) s.action = "cut";
  assert.throws(() => trimOutput(o, p.sources, "end", 1), /没有保留的片段/);
});

// ---------- 撤销栈 ----------

test("撤销 / 重做：切换动作", () => {
  const p = makePlan();
  const o = p.outputs[0];
  const u = new UndoStack();
  u.push("改回保留", setAction(o, "d003", "keep"));
  assert.equal(o.segments[2].action, "keep");
  assert.ok(u.canUndo && !u.canRedo);
  u.undo(p);
  assert.equal(o.segments[2].action, "cut");
  assert.equal(o.segments[2].confidence, 0.6); // 把握度也恢复
  assert.equal(o.segments[2].evidence.edited, undefined);
  u.redo(p);
  assert.equal(o.segments[2].action, "keep");
  assert.equal(o.segments[2].confidence, 1);
});

test("撤销：一次边界移动会还原两个相邻片段；push 新改动会清空重做栈", () => {
  const p = makePlan();
  const o = p.outputs[0];
  const u = new UndoStack();
  u.push("拖动边界", moveBoundary(o, p.sources, "d002", "start", 3.5));
  u.undo(p);
  assert.deepEqual(o.segments.map((s) => [s.start, s.end]), [[0, 3], [3, 5], [5, 6], [6, 9.5]]);
  assert.equal(o.segments[1].evidence.edited, undefined);
  assert.ok(u.canRedo);
  u.push("锁定", setLocked(o, "d001", true));
  assert.ok(!u.canRedo);
});

test("连续微调按 key 合并成一步，撤销一次回到最初", () => {
  const p = makePlan();
  const o = p.outputs[0];
  let now = 1000;
  const u = new UndoStack({ now: () => now });
  for (let i = 1; i <= 4; i++) {
    now += 100;
    u.push("微调", moveBoundary(o, p.sources, "d002", "start", 3 + 0.05 * i), { key: "nudge:d002:start" });
  }
  assert.equal(u.past.length, 1);
  near(o.segments[1].start, 3.2);
  u.undo(p);
  assert.equal(o.segments[1].start, 3);
  assert.equal(o.segments[0].end, 3);
  assert.equal(o.segments[1].evidence.edited, undefined);
  u.redo(p);
  near(o.segments[1].start, 3.2);
  near(o.segments[0].end, 3.2);
});

test("超过合并窗口、key 不同都不合并；撤销栈有上限", () => {
  const p = makePlan();
  const o = p.outputs[0];
  let now = 0;
  const u = new UndoStack({ now: () => now, limit: 3 });
  u.push("a", moveBoundary(o, p.sources, "d002", "start", 3.1), { key: "k" });
  now += 5000;
  u.push("b", moveBoundary(o, p.sources, "d002", "start", 3.2), { key: "k" });
  u.push("c", moveBoundary(o, p.sources, "d002", "start", 3.3), { key: "other" });
  u.push("d", setLocked(o, "d001", true));
  assert.equal(u.past.length, 3);
  assert.deepEqual(u.past.map((e) => e.label), ["b", "c", "d"]);
});

test("输出级改动（是否选中）也可撤销", () => {
  const p = makePlan();
  const u = new UndoStack();
  u.push("取消选中", setSelected(p.outputs[0], false));
  assert.equal(p.outputs[0].selected, false);
  u.undo(p);
  assert.equal(p.outputs[0].selected, true);
});

test("applyChange 对不存在的片段是空操作（外部载入新版本后遗留的撤销项不会抛错）", () => {
  const p = makePlan();
  applyChange(p, { output: "main", seg: "nope", before: { action: "cut" }, after: { action: "keep" } }, "after");
  applyChange(p, { output: "nope", seg: null, before: {}, after: {} }, "after");
});

// ---------- 统计与比较 ----------

test("diffCount：改回原样算 0 处", () => {
  const base = makePlan();
  const cur = normalizePlan(base);
  assert.equal(diffCount(base, cur), 0);
  setAction(cur.outputs[0], "d003", "keep");
  assert.equal(diffCount(base, cur), 1);
  moveBoundary(cur.outputs[0], cur.sources, "d002", "start", 3.5);
  assert.equal(diffCount(base, cur), 3); // d001.end、d002.start 也变了
  setAction(cur.outputs[0], "d003", "cut");
  cur.outputs[0].segments[2].confidence = 0.6;
  assert.equal(diffCount(base, cur), 2);
  setSelected(cur.outputs[0], false);
  assert.equal(diffCount(base, cur), 3);
});

test("planStats：剪掉多少、占比、拿不准几处、按理由统计", () => {
  const p = makePlan();
  const s = planStats(p, p.outputs[0]);
  near(s.span, 10); // 单素材：用素材时长
  near(s.removed, 3);
  near(s.removedPct, 0.3);
  near(s.duration, 6.5);
  assert.equal(s.uncertain, 1);
  assert.deepEqual(s.cutByReason, { silence: 1, filler: 1 });
});

test("isCandidateMode：多个输出或带 score", () => {
  const p = makePlan();
  assert.equal(isCandidateMode(p), false);
  p.outputs[0].score = 90;
  assert.equal(isCandidateMode(p), true);
  p.outputs[0].score = null;
  p.outputs.push({ ...p.outputs[0], id: "second" });
  assert.equal(isCandidateMode(p), true);
});

// ---------- 竖屏裁切 ----------

test("cropRect：16:9 → 9:16 横向裁切，框跟随主体并夹在画面内", () => {
  const r = cropRect(1920, 1080, 9 / 16, 0.5, 0.5);
  near(r.w, (9 / 16) / (16 / 9), 1e-9);
  near(r.x + r.w / 2, 0.5, 1e-9);
  assert.equal(r.h, 1);
  const left = cropRect(1920, 1080, 9 / 16, 0.0, 0.5);
  assert.equal(left.x, 0);
  const right = cropRect(1920, 1080, 9 / 16, 1.0, 0.5);
  near(right.x + right.w, 1);
  const tall = cropRect(1080, 1920, 16 / 9, 0.5, 0.2); // 竖屏素材 → 横屏输出：纵向裁
  assert.equal(tall.w, 1);
  assert.ok(tall.y >= 0 && tall.y + tall.h <= 1 + 1e-12);
});

test("reframeCenter：center 取正中，track 在关键帧之间线性插值，两端保持", () => {
  assert.deepEqual(reframeCenter(null, 3), { cx: 0.5, cy: 0.5 });
  assert.deepEqual(reframeCenter({ mode: "center", keyframes: [] }, 3), { cx: 0.5, cy: 0.5 });
  const rf = { mode: "track", keyframes: [[0, 0.2, 0.5], [10, 0.6, 0.3]] };
  assert.deepEqual(reframeCenter(rf, -1), { cx: 0.2, cy: 0.5 });
  const mid = reframeCenter(rf, 5);
  near(mid.cx, 0.4);
  near(mid.cy, 0.4);
  assert.deepEqual(reframeCenter(rf, 99), { cx: 0.6, cy: 0.3 });
});

// ---------- 杂项 ----------

test("fmtTime", () => {
  assert.equal(fmtTime(0), "00:00");
  assert.equal(fmtTime(65.4), "01:05");
  assert.equal(fmtTime(3725), "1:02:05");
  assert.equal(fmtTime(10.46, { ms: 1 }), "00:10.5");
  assert.equal(fmtTime(59.96, { ms: 1 }), "01:00.0");
  assert.equal(fmtTime(1.234, { ms: 2 }), "00:01.23");
  assert.equal(fmtTime(NaN), "--:--");
});

test("wordIndexAt / wordsText", () => {
  const words = [
    { text: "大家好", start: 0.1, end: 0.8 }, { text: "，", start: 0.8, end: 0.8 }, { text: "欢迎", start: 0.9, end: 1.3 },
  ];
  assert.equal(wordIndexAt(words, 0.5), 0);
  assert.equal(wordIndexAt(words, 0.85), 2); // 标点 end == 0.8 不大于 0.85
  assert.equal(wordIndexAt(words, 5), 3);
  assert.equal(wordsText(words), "大家好，欢迎");
  assert.equal(wordsText([{ text: "hello" }, { text: "world" }, { text: "," }]), "hello world,");
});

test("paceId：认 cut 配方的取值，兼容别名，秒数不对应任何一档", () => {
  assert.equal(paceId("tight"), "tight");
  assert.equal(paceId("compact"), "tight");
  assert.equal(paceId("自然"), "natural");
  assert.equal(paceId("relaxed"), "relaxed");
  assert.equal(paceId(0.3), null);
  assert.equal(paceId(undefined), null);
});

test("bucketPeaks：每桶取最大值", () => {
  const peaks = [0, 1, 0, 0, 0.5, 0, 0, 0];
  const b = bucketPeaks(peaks, 4, 0, 2, 4); // 8 个值 → 4 个桶
  assert.deepEqual([...b], [1, 0, 0.5, 0]);
  assert.equal(bucketPeaks(peaks, 4, 1, 1, 4).length, 4);
});
