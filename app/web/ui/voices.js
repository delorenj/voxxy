// Voice library: grouped cards with preview, use-in-Say and a confirmed delete.

import { h, toast, fmtSeconds } from "./dom.js";
import { synth, deleteVoice, explain, kindOf, KIND_ORDER, KIND_LABEL, getVoice, setVoice } from "./api.js";
import { state, loadVoices, invalidateVoices } from "./state.js";
import { tagEls } from "./picker.js";
import { playUrl } from "./player.js";

const PREVIEW_LINE = "Hello, this is a short preview of how this voice sounds.";
const GROUP_NOTE = {
  own: "Recorded by a person reading a sentence aloud.",
  designed: "Made from a description. No recording of anyone.",
  house: "Voices that ship with Voxxy.",
  other: "Everything else on this server.",
};

export function mountVoices(root, ctx) {
  const hosted = (ctx?.config?.edition || "hosted") === "hosted";
  let disposed = false;
  let stopPreview = null;

  const search = h("input", { id: "voice-search", type: "search", placeholder: "Search by name or tag", autocomplete: "off" });
  const count = h("p", { class: "muted small", "aria-live": "polite" });
  const groupsHost = h("div", { class: "groups" },
    h("div", { class: "voice-grid" }, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }), h("div", { class: "skeleton" })));

  function card(v) {
    const actions = h("div", { class: "voice__actions" });
    const previewBtn = h("button", { class: "btn btn--small", type: "button" }, "Preview");
    const useBtn = h("button", { class: "btn btn--small", type: "button" }, "Say with it");
    const delBtn = h("button", { class: "btn btn--small btn--danger", type: "button" }, "Delete");
    const status = h("p", { class: "small muted", "aria-live": "polite" });

    const setPreview = (label, isBusy) => {
      previewBtn.textContent = label;
      previewBtn.disabled = false;
      if (isBusy) previewBtn.setAttribute("aria-busy", "true"); else previewBtn.removeAttribute("aria-busy");
    };
    let previewing = false;

    previewBtn.addEventListener("click", async () => {
      if (previewing) { stopPreview?.stop(); return; }
      previewing = true;
      setPreview("Preparing…", true);
      status.textContent = "";
      try {
        const r = await synth(PREVIEW_LINE, v.name);
        if (disposed) return;
        stopPreview?.stop();
        const handle = playUrl(r.audio_url, (s) => {
          if (s === "playing") setPreview("Stop", false);
          else if (s === "loading") setPreview("Preparing…", true);
          else {
            previewing = false;
            setPreview("Preview", false);
            if (s === "error") status.textContent = "The audio did not play. Try Preview again.";
          }
        });
        stopPreview = handle;
      } catch (err) {
        previewing = false;
        setPreview("Preview", false);
        status.textContent = explain(err);
      }
    });

    useBtn.addEventListener("click", () => {
      setVoice(v.name);
      location.hash = "#/say";
    });

    delBtn.addEventListener("click", () => {
      const shared = kindOf(v) !== "own" && kindOf(v) !== "designed";
      const confirm = h("div", { class: "voice__confirm", role: "group", "aria-label": `Delete ${v.display_name}` },
        h("p", { class: "small" }, h("strong", {}, `Delete “${v.display_name}”?`), " ",
          shared ? "Everyone who uses this server loses this voice. " : "", "This cannot be undone."),
        h("div", { class: "row" },
          h("button", { class: "btn btn--small btn--danger-solid", type: "button", id: `del-ok-${v.name}` }, "Delete voice"),
          h("button", { class: "btn btn--small", type: "button", id: `del-cancel-${v.name}` }, "Keep it")));
      actions.replaceWith(confirm);
      const ok = confirm.querySelector(`#del-ok-${CSS.escape(v.name)}`);
      const cancel = confirm.querySelector(`#del-cancel-${CSS.escape(v.name)}`);
      cancel.focus();
      cancel.addEventListener("click", () => { confirm.replaceWith(actions); delBtn.focus(); });
      ok.addEventListener("click", async () => {
        ok.disabled = true; ok.textContent = "Deleting…";
        try {
          await deleteVoice(v.name);
          if (getVoice() === v.name) setVoice("");
          invalidateVoices();
          toast(`Deleted “${v.display_name}”.`);
          await refresh();
        } catch (err) {
          ok.disabled = false; ok.textContent = "Delete voice";
          status.textContent = explain(err);
        }
      });
    });

    // House voices are shared stock: the hosted app does not offer to delete them.
    actions.append(previewBtn, useBtn);
    if (!(hosted && kindOf(v) === "house")) actions.append(delBtn);
    return h("li", { class: "voice" },
      h("div", { class: "voice__top" },
        h("h3", { class: "voice__name" }, v.display_name),
        h("span", { class: "voice__slug" }, v.name),
        tagEls(v, { kind: false }),
        h("p", { class: "small muted" }, [v.duration_s ? `${fmtSeconds(v.duration_s)} sample` : "", v.persona && v.persona !== "none" ? `persona ${v.persona}` : ""].filter(Boolean).join(" · "))),
      status,
      actions);
  }

  function paint() {
    const voices = state.voices || [];
    const q = search.value.trim().toLowerCase();
    const shown = voices.filter((v) => !q || [v.name, v.display_name, ...(v.tags || [])].some((s) => String(s).toLowerCase().includes(q)));
    count.textContent = q ? `${shown.length} of ${voices.length} voices` : `${voices.length} voice${voices.length === 1 ? "" : "s"}`;

    if (!voices.length) {
      groupsHost.replaceChildren(h("div", { class: "empty" },
        h("h2", {}, "No voices yet"),
        h("p", { class: "muted" }, "Record your own voice, or describe one and Voxxy will make it."),
        h("a", { class: "btn btn--primary", href: "#/make" }, "Make a voice")));
      return;
    }
    if (!shown.length) {
      groupsHost.replaceChildren(h("div", { class: "empty" },
        h("h2", {}, "Nothing matches"),
        h("p", { class: "muted" }, `No voice matches “${search.value.trim()}”. Try a name or a tag such as house or designed.`)));
      return;
    }
    const sections = [];
    for (const kind of KIND_ORDER) {
      const group = shown.filter((v) => kindOf(v) === kind);
      if (!group.length) continue;
      sections.push(h("section", { "aria-labelledby": `grp-${kind}` },
        h("div", { class: "group__head" },
          h("h2", { id: `grp-${kind}` }, KIND_LABEL[kind]),
          h("span", { class: "group__count" }, String(group.length)),
          h("span", { class: "muted small" }, GROUP_NOTE[kind])),
        h("ul", { class: "voice-grid" }, group.map(card))));
    }
    groupsHost.replaceChildren(...sections);
  }

  async function refresh() {
    try {
      await loadVoices(true);
      if (!disposed) paint();
    } catch (err) {
      if (!disposed) groupsHost.replaceChildren(h("p", { class: "notice notice--error", role: "alert" }, `${explain(err)} Reload the page to try again.`));
    }
  }

  let t = null;
  search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(paint, 120); });

  root.append(h("div", { class: "page" },
    h("div", { class: "page__head" },
      h("h1", { tabindex: "-1" }, "Voice library"),
      h("p", { class: "lede" }, "Every voice on this server. Preview one, say something with it, or add your own.")),
    h("div", { class: "toolbar" },
      h("div", { class: "field" }, h("label", { for: "voice-search" }, "Search the library"), search),
      h("a", { class: "btn btn--primary", href: "#/make" }, "Make a voice")),
    count,
    groupsHost));

  if (state.voices) paint();
  refresh();

  return () => { disposed = true; clearTimeout(t); stopPreview?.stop(); };
}
