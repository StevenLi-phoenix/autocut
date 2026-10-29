// 对比度检查：从 style.css 里读出浅色/深色两套设计令牌，逐对计算 WCAG 对比度，正文类 ≥ 4.5:1。
// 运行：cd src/autocut/review/static && node --test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("./style.css", import.meta.url), "utf8");

function tokens(block) {
  const t = {};
  for (const m of block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) t[m[1]] = m[2];
  return t;
}

const light = tokens(css.slice(css.indexOf(":root {"), css.indexOf("@media (prefers-color-scheme: dark)")));
const darkStart = css.indexOf("@media (prefers-color-scheme: dark)");
const dark = { ...light, ...tokens(css.slice(darkStart, css.indexOf("* { box-sizing", darkStart))) };

function lum(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function ratio(a, b) {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

// [前景, 背景]：界面里真实出现的文字/背景组合
const PAIRS = [
  ["ink", "bg"], ["ink", "panel"], ["ink", "panel-2"], ["ink-2", "panel"], ["ink-2", "panel-2"], ["muted", "panel"], ["muted", "bg"], ["muted", "panel-2"],
  ["accent", "panel"], ["accent", "accent-soft"], ["accent-ink", "accent"], ["ink", "accent-soft"],
  ["cut", "panel"], ["cut", "cut-soft"], ["ink", "cut-soft"],
  ["warn-ink", "panel"], ["warn-ink", "warn-soft"], ["ink", "warn-soft"],
  ["ok", "ok-soft"], ["ink", "ok-soft"],
  ["tl-text", "tl-bg"], ["bg", "ink"], ["on-cut", "cut"], ["on-ok", "ok"],
];

for (const [name, t] of [["浅色", light], ["深色", dark]]) {
  test(`${name}主题：文字对比度 ≥ 4.5:1`, () => {
    const bad = [];
    for (const [fg, bg] of PAIRS) {
      assert.ok(t[fg] && t[bg], `缺少令牌 ${fg} 或 ${bg}`);
      const r = ratio(t[fg], t[bg]);
      if (r < 4.5) bad.push(`${fg} ${t[fg]} on ${bg} ${t[bg]} = ${r.toFixed(2)}`);
    }
    assert.deepEqual(bad, []);
  });

  test(`${name}主题：非文字元素（时间线图形、焦点环）对比度 ≥ 3:1`, () => {
    const pairs = [["tl-keep", "tl-bg"], ["tl-cut", "tl-bg"], ["tl-warn", "tl-bg"], ["tl-wave", "tl-bg"], ["tl-grid", "tl-bg"], ["tl-sel", "tl-bg"], ["tl-playhead", "tl-bg"], ["focus", "panel"], ["focus", "bg"], ["line-strong", "panel"]];
    const bad = [];
    for (const [fg, bg] of pairs) {
      const r = ratio(t[fg], t[bg]);
      // 分隔线（grid、line-strong）本身是装饰，放宽到 1.5；其余图形要 3:1
      const need = fg === "tl-grid" || fg === "line-strong" ? 1.5 : 3;
      if (r < need) bad.push(`${fg} on ${bg} = ${r.toFixed(2)}（需要 ${need}）`);
    }
    assert.deepEqual(bad, []);
  });
}
