// Shared app state: the config the server gave, the voice list, and engine health.

import { getConfig, getHealth, listVoices } from "./api.js";

export const state = {
  config: null,
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
  return state.config;
}

let voicesInflight = null;
export function loadVoices(force = false) {
  if (!force && state.voices) return Promise.resolve(state.voices);
  if (!voicesInflight) {
    voicesInflight = listVoices()
      .then((list) => { state.voices = Array.isArray(list) ? list : []; emit("voices", state.voices); return state.voices; })
      .finally(() => { voicesInflight = null; });
  }
  return voicesInflight;
}

export function invalidateVoices() {
  state.voices = null;
}

export async function refreshHealth() {
  try {
    const h = await getHealth();
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
  return () => clearInterval(id);
}
