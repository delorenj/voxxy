// Small DOM helpers. No framework: views are functions that build nodes.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "html") el.innerHTML = value; // only ever called with strings written in this app
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(markup, attrs = {}) {
  const wrap = document.createElementNS(SVG_NS, "svg");
  wrap.innerHTML = markup;
  for (const [k, v] of Object.entries(attrs)) wrap.setAttribute(k, v);
  wrap.setAttribute("aria-hidden", "true");
  wrap.setAttribute("focusable", "false");
  return wrap;
}

// The mark's three arcs on their own, for state: speaking, listening, working.
export function arcs({ size = 24, className = "" } = {}) {
  const el = svg(
    '<path class="arc" d="M28.38 36.48 A14 14 0 0 1 35.62 36.48"/>' +
      '<path class="arc" d="M25.79 26.82 A24 24 0 0 1 38.21 26.82"/>' +
      '<path class="arc" d="M23.20 17.16 A34 34 0 0 1 40.80 17.16"/>',
    { viewBox: "20 10 24 32", width: size, height: size * 1.1 },
  );
  el.setAttribute("class", `arcs ${className}`.trim());
  el.dataset.active = "false";
  return el;
}

export const icons = {
  play: () => svg('<path d="M8 5.5v13a1 1 0 0 0 1.5.86l11-6.5a1 1 0 0 0 0-1.72l-11-6.5A1 1 0 0 0 8 5.5z"/>', { viewBox: "0 0 28 24", width: 24, height: 24 }),
  pause: () => svg('<rect x="7" y="5" width="4.5" height="14" rx="1"/><rect x="15" y="5" width="4.5" height="14" rx="1"/>', { viewBox: "0 0 26 24", width: 24, height: 24 }),
  chevron: () => svg('<path d="M6 9l6 6 6-6"/>', { viewBox: "0 0 24 24", width: 18, height: 18 }),
  download: () => svg('<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>', { viewBox: "0 0 24 24", width: 18, height: 18 }),
  mic: () => svg('<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>', { viewBox: "0 0 24 24", width: 18, height: 18 }),
  desk: () => svg('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>', { viewBox: "0 0 24 24", width: 18, height: 18 }),
};

export function iconBtnContent(icon, label) {
  const svgEl = icon();
  svgEl.setAttribute("class", "btn__icon");
  return [svgEl, h("span", {}, label)];
}

export function toast(message, kind = "info", ms = 5000) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = h("p", { class: "toast", dataset: { kind } }, message);
  host.append(el);
  setTimeout(() => el.remove(), ms);
}

export function fmtTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtSeconds(seconds) {
  if (!Number.isFinite(seconds)) return "";
  return seconds < 10 ? `${seconds.toFixed(1)} s` : `${Math.round(seconds)} s`;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied.");
  } catch {
    toast("Could not copy. Select the text and copy it yourself.", "error");
  }
}

// Run `fn` while `button` shows a busy state. Re-entry is ignored.
export async function busy(button, label, fn) {
  if (button.getAttribute("aria-busy") === "true") return undefined;
  const original = button.textContent;
  const children = [...button.childNodes];
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  button.replaceChildren(label);
  try {
    return await fn();
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
    button.replaceChildren(...children);
    if (!children.length) button.textContent = original;
  }
}
