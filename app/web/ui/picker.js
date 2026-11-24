// Searchable voice picker: a button showing the current voice, and a panel with a filter and radio rows.

import { h, icons } from "./dom.js";
import { kindOf, isPending, KIND_ORDER, KIND_LABEL } from "./api.js";

const DEFAULT_ENGINE_VOICE = { name: "", display_name: "Engine default", tags: [], duration_s: null };

// Internal tags get plain words; tags with no entry here (fleet bookkeeping like clinical_ok) are not shown.
const TAG_TEXT = {
  "consent-verified": ["Verified", "ok"],
  "consent-pending": ["Under review", "flag"],
  "consent-unchecked-self": ["Consent not checked", "flag"],
  "unverified-consent": ["Under review", "flag"],
  uploaded: ["Uploaded", "plain"],
};

// `kind: false` leaves out the kind tag where a heading already says it (the library's groups).
export function tagEls(voice, { kind: showKind = true } = {}) {
  const tags = voice.tags || [];
  const kind = kindOf(voice);
  const out = [];
  if (showKind && kind !== "other") out.push(h("span", { class: "tag", dataset: { kind } }, kind));
  for (const t of tags) {
    if (!TAG_TEXT[t]) continue;
    out.push(h("span", { class: "tag", dataset: { kind: TAG_TEXT[t][1] } }, TAG_TEXT[t][0]));
  }
  return h("span", { class: "tags" }, out);
}

export function voicePicker({ voices, value, onChange }) {
  const all = [DEFAULT_ENGINE_VOICE, ...voices];
  let current = all.find((v) => v.name === value) || DEFAULT_ENGINE_VOICE;
  let open = false;

  const currentBtn = h("button", { class: "picker__current", type: "button", "aria-expanded": "false", "aria-controls": "picker-panel" });
  const search = h("input", { type: "search", placeholder: "Search by name or tag", "aria-label": "Search voices", autocomplete: "off" });
  const list = h("div", { class: "picker__list", role: "presentation" });
  const empty = h("p", { class: "muted small", hidden: true }, "No voice matches. Try another word.");
  const panel = h("div", { class: "picker__panel", id: "picker-panel", hidden: true }, search, list, empty);
  const root = h("div", { class: "picker" }, currentBtn, panel);

  const paintCurrent = () => {
    currentBtn.replaceChildren(
      h("span", { class: "picker__name" }, current.display_name),
      current.name ? tagEls(current) : h("span", { class: "muted small" }, "The server's stock voice"),
      h("span", { class: "picker__chev" }, icons.chevron()),
    );
  };

  const matches = (v, q) => !q || [v.name, v.display_name, ...(v.tags || [])].some((s) => String(s).toLowerCase().includes(q));

  const paintList = () => {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    let shown = 0;
    const row = (v) => {
      shown += 1;
      const id = `voice-opt-${v.name || "default"}`;
      const input = h("input", { type: "radio", name: "voice", id, value: v.name });
      input.checked = v.name === current.name;
      if (isPending(v)) input.disabled = true; // cannot speak until reviewed
      input.addEventListener("change", () => {
        current = v;
        paintCurrent();
        setOpen(false);
        currentBtn.focus();
        onChange(v.name);
      });
      return h("div", { class: "option" }, input,
        h("label", { class: "option__row", for: id },
          h("span", { class: "option__name" }, v.display_name),
          v.name ? tagEls(v) : null,
          v.duration_s ? h("span", { class: "option__meta" }, `${Math.round(v.duration_s)} s`) : null));
    };
    if (matches(DEFAULT_ENGINE_VOICE, q)) list.append(row(DEFAULT_ENGINE_VOICE));
    for (const kind of KIND_ORDER) {
      const group = voices.filter((v) => kindOf(v) === kind && matches(v, q));
      if (!group.length) continue;
      list.append(h("div", { class: "picker__group", role: "presentation" }, KIND_LABEL[kind]));
      for (const v of group) list.append(row(v));
    }
    empty.hidden = shown > 0;
  };

  function setOpen(next) {
    open = next;
    currentBtn.setAttribute("aria-expanded", String(open));
    panel.hidden = !open;
    if (open) {
      paintList();
      search.focus();
    }
  }

  currentBtn.addEventListener("click", () => setOpen(!open));
  search.addEventListener("input", paintList);
  root.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && open) { e.stopPropagation(); setOpen(false); currentBtn.focus(); }
  });

  paintCurrent();
  return {
    el: root,
    set(name) {
      current = all.find((v) => v.name === name) || DEFAULT_ENGINE_VOICE;
      paintCurrent();
    },
    get: () => current.name,
  };
}
