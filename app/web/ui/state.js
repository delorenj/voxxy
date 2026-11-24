// Shared app state: the config the server gave, the voice list, and engine health.

import { getConfig, getHealth, listVoices, isPending } from "./api.js";

export const state = {
  config: null,
  uiVersion: "", // the web app build this page booted with (from /app-config)
  stale: false, // the server now serves a newer build than this page runs
  voices: null, // null until loaded
  health: { level: "unknown", text: "Checking", detail: "" },
};

const bus = new EventTarget();
export const on = (name, fn) => {
  const h = (e) => fn(e.detail);
  bus.addEventListener(name, h);
  return () => bus.removeEventListener(name, h);
};
const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

export async function loadConfig() {
  state.config = await getConfig();
  noteUiVersion(state.config?.ui_version);
  return state.config;
}

let voicesInflight = null;
export function loadVoices(force = false) {
  if (!force && state.voices) return Promise.resolve(state.voices);
  if (!voicesInflight) {
    voicesInflight = listVoices()
      .then((list) => { state.voices = Array.isArray(list) ? list : []; noteReviews(state.voices); emit("voices", state.voices); return state.voices; })
      .finally(() => { voicesInflight = null; });
  }
  return voicesInflight;
}

// Voices under review that this page has seen, name -> display name. When one is no longer pending the
// page says so ("reviewed"): approved if it is still there, not approved if it is gone.
let underReview = null;
function noteReviews(voices) {
  const now = new Map(voices.filter((v) => isPending(v)).map((v) => [v.name, v.display_name || v.name]));
  if (underReview) {
    for (const [name, display] of underReview) {
      if (now.has(name)) continue;
      const voice = voices.find((v) => v.name === name);
      emit("reviewed", { name, display, approved: Boolean(voice) });
    }
  }
  underReview = now;
}

// While any voice is under review, re-check the list (every 30 s while the tab is visible, and as soon
// as it is shown again), so a voice approved by the operator turns usable without a reload.
export function startReviewWatch(ms = 30000) {
  const check = () => { if (!document.hidden && underReview?.size) loadVoices(true).catch(() => {}); };
  const id = setInterval(check, ms);
  document.addEventListener("visibilitychange", check);
  return () => { clearInterval(id); document.removeEventListener("visibilitychange", check); };
}

export function invalidateVoices() {
  state.voices = null;
}

// The first build seen is the one this page runs; any later different one means the server was updated.
function noteUiVersion(v) {
  if (!v) return;
  if (!state.uiVersion) { state.uiVersion = v; return; }
  if (v !== state.uiVersion && !state.stale) { state.stale = true; emit("stale", v); }
}

export async function refreshHealth() {
  try {
    const h = await getHealth();
    noteUiVersion(h.ui_version);
    const ready = (h.engines || []).filter((e) => e.ready).map((e) => e.name);
    const down = (h.engines || []).filter((e) => !e.ready).map((e) => e.name);
    if (h.status === "ok" && ready.length) {
      state.health = {
        level: "ok", text: "Engine ready",
        detail: `Ready: ${ready.join(", ")}${down.length ? `. Not ready: ${down.join(", ")}` : ""}`,
      };
    } else {
      state.health = { level: "degraded", text: "Engine not ready", detail: `No engine is ready${down.length ? ` (${down.join(", ")})` : ""}. Speech may fail until one comes back.` };
    }
  } catch {
    state.health = { level: "down", text: "Server unreachable", detail: "The Voxxy server did not answer." };
  }
  emit("health", state.health);
  return state.health;
}

export function startHealthPolling(ms = 30000) {
  refreshHealth();
  const id = setInterval(() => { if (!document.hidden) refreshHealth(); }, ms);
  // A tab coming back (often after a night and a deploy) checks at once, before anyone presses Save.
  const onVisible = () => { if (!document.hidden) refreshHealth(); };
  document.addEventListener("visibilitychange", onVisible);
  return () => { clearInterval(id); document.removeEventListener("visibilitychange", onVisible); };
}
