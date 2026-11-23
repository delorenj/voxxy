// API access: one fetch wrapper that sends the key, plus per-viewer storage that never throws.

const memory = new Map();

export const store = {
  get(key) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? memory.get(key) ?? null : v;
    } catch {
      return memory.get(key) ?? null;
    }
  },
  set(key, value) {
    memory.set(key, value);
    try { localStorage.setItem(key, value); } catch { /* private window or blocked storage */ }
  },
  del(key) {
    memory.delete(key);
    try { localStorage.removeItem(key); } catch { /* same */ }
  },
};

const KEY = "voxxy.apiKey";
const SINK = "voxxy.sinkKey";
const VOICE = "voxxy.voice";

export const getKey = () => store.get(KEY) || "";
export const setKey = (value) => store.set(KEY, value.trim());
export const forgetKey = () => store.del(KEY);
export const getSink = () => store.get(SINK) || "";
export const setSink = (value) => (value ? store.set(SINK, value) : store.del(SINK));
export const getVoice = () => store.get(VOICE) || "";
export const setVoice = (value) => (value ? store.set(VOICE, value) : store.del(VOICE));

export const SINK_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export class ApiError extends Error {
  constructor(status, message, kind) {
    super(message);
    this.status = status;
    this.kind = kind; // "network" | "auth" | "http"
  }
}

export const events = new EventTarget();

function detailText(body) {
  if (!body) return "";
  if (typeof body === "string") return body;
  const d = body.detail ?? body.error?.message ?? body.message;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || JSON.stringify(e)).join("; ");
  return "";
}

// Turn a failure into one plain sentence that says what to do next.
export function explain(err, fallback = "Something went wrong.") {
  if (!(err instanceof ApiError)) return err?.message || fallback;
  if (err.kind === "network") return "Can't reach the Voxxy server. Check your connection and try again.";
  if (err.status === 401) return "The key was not accepted. Paste the key again.";
  if (err.status === 503) return `${err.message || "The voice engine is busy."} Try again in a moment.`;
  if (err.status >= 500) return `The server hit a problem (${err.status}). Try again; if it keeps failing, tell whoever runs this server.`;
  return err.message || fallback;
}

export async function api(path, { method = "GET", json, form, signal, key } = {}) {
  const headers = {};
  const k = key ?? getKey();
  if (k) headers["X-API-Key"] = k;
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body, signal });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError(0, "", "network");
  }
  let data = null;
  const type = res.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (!res.ok) {
    if (res.status === 401 && key === undefined) events.dispatchEvent(new Event("unauthorized"));
    throw new ApiError(res.status, detailText(data), res.status === 401 ? "auth" : "http");
  }
  return data;
}

export const getConfig = () => api("/app-config", { key: "" });
export const getHealth = () => api("/healthz", { key: "" });
export const listVoices = (key) => api("/voices", key === undefined ? {} : { key });
export const deleteVoice = (name) => api(`/voices/${encodeURIComponent(name)}`, { method: "DELETE" });
export const synth = (text, voice) => api("/synthesize-url", { method: "POST", json: { text, ...(voice ? { voice } : {}) } });
export const sinkStatus = (key) => api(`/sink/${encodeURIComponent(key)}`, { key: "" });
export const sinkPlay = (key, payload) => api(`/sink/${encodeURIComponent(key)}/play`, { method: "POST", json: payload });
export const sinkSay = (key, text, voice) => api(`/sink/${encodeURIComponent(key)}/say`, { method: "POST", json: { text, ...(voice ? { voice } : {}) } });
export const consentChallenge = () => api("/voices/consent/challenge", { method: "POST" });
export const saveOwnVoice = (form) => api("/voices/own", { method: "POST", form });
export const previewDesign = (description, sample_text) =>
  api("/voices/design/preview", { method: "POST", json: { description, ...(sample_text ? { sample_text } : {}) } });
export const saveDesign = (body) => api("/voices/design", { method: "POST", json: body });
export const uploadVoice = (form) => api("/voices", { method: "POST", form });

// ---- voice grouping, shared by Studio and the library ----
export function kindOf(voice) {
  const tags = voice.tags || [];
  if (tags.includes("own")) return "own";
  if (tags.includes("designed")) return "designed";
  if (tags.includes("house")) return "house";
  return "other";
}
export const KIND_ORDER = ["own", "designed", "house", "other"];
export const KIND_LABEL = { own: "Own voices", designed: "Designed voices", house: "House voices", other: "Other voices" };

export function slugify(text) {
  return text
    .normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
}
