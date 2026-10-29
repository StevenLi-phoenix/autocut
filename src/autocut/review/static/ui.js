// 很小的 DOM 工具：建元素、提示、菜单、对话框。没有框架，也不需要。

/** h("button", {class: "x", onclick: fn, "aria-label": "…"}, "文字", 子节点…) */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k in el && k !== "list" && typeof v !== "object") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** 清空 el 再放入 children；和 el.replaceChildren(...) 的区别是 false/null 会被跳过（原生的会把它们变成 "false" 文字）。 */
export function fill(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ✂ 和 ⚠ 在 macOS 上默认会变成彩色 emoji；加 U+FE0E 让它们跟随文字颜色（颜色是我们的设计，不是系统的）
export const GLYPH = { cut: "✂︎", warn: "⚠︎", play: "▶︎", pause: "❚❚", lock: "🔒︎", check: "✓", pause_mark: "┄" };

/** 给读屏软件的播报，不在视觉上出现。 */
export function announce(msg) {
  const live = document.getElementById("sr-live");
  if (!live) return;
  live.textContent = "";
  requestAnimationFrame(() => { live.textContent = msg; });
}

/** 右下角的短暂提示。action: {label, onclick}。 */
export function toast(msg, { kind = "info", timeout = 4500, action = null } = {}) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, h("span", {}, msg));
  if (action) el.append(h("button", { class: "btn btn-quiet", onclick: () => { action.onclick(); el.remove(); } }, action.label));
  const close = h("button", { class: "icon-btn", "aria-label": "关闭提示", onclick: () => el.remove() }, "×");
  el.append(close);
  host.append(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
  return el;
}

/** 通用弹出菜单：button 触发，items = [{label, hint, onselect, disabled}]。键盘：↑↓ 移动，Enter 选择，Esc 关闭并还焦点。 */
export function menu(button, getItems) {
  let pop = null;
  const close = (refocus = true) => {
    if (!pop) return;
    pop.remove();
    pop = null;
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", outside, true);
    if (refocus) button.focus();
  };
  const outside = (e) => { if (pop && !pop.contains(e.target) && e.target !== button && !button.contains(e.target)) close(false); };
  const open = () => {
    if (pop) return close();
    pop = h("div", { class: "menu", role: "menu", "aria-label": button.getAttribute("aria-label") || button.textContent });
    for (const it of getItems()) {
      const b = h("button", {
        class: "menu-item", role: "menuitem", disabled: !!it.disabled, tabindex: -1,
        onclick: () => { close(false); it.onselect(); },
      }, h("span", { class: "menu-label" }, it.label), it.hint && h("span", { class: "menu-hint" }, it.hint));
      pop.append(b);
    }
    pop.addEventListener("keydown", (e) => {
      const items = $$(".menu-item:not(:disabled)", pop);
      const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
      else if (e.key === "Tab") close(false);
    });
    button.parentElement.append(pop);
    button.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", outside, true);
    $$(".menu-item:not(:disabled)", pop)[0]?.focus();
  };
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", open);
  button.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" && !pop) { e.preventDefault(); open(); } });
  return { close };
}

export function basename(p) {
  return String(p ?? "").split(/[\\/]/).pop();
}

export function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  d.flush = (...a) => { clearTimeout(t); fn(...a); };
  return d;
}

export function copyText(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const ta = h("textarea", { value: text, style: { position: "fixed", opacity: 0 } });
  document.body.append(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
  return Promise.resolve();
}

export function safeStorage() {
  try {
    const k = "__t";
    localStorage.setItem(k, "1");
    localStorage.removeItem(k);
    return localStorage;
  } catch {
    const mem = new Map();
    return { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
  }
}
