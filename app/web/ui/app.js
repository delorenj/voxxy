// Voxxy web app entry: config, key gate, router, header.

import { h, clear, toast } from "./dom.js";
import { getKey, setKey, forgetKey, listVoices, events, explain, ApiError } from "./api.js";
import { state, on, loadConfig, invalidateVoices, startHealthPolling } from "./state.js";
import { mountStudio } from "./studio.js";
import { mountVoices } from "./voices.js";
import { mountMake } from "./make.js";
import { mountSpeakers } from "./speakers.js";

const main = document.getElementById("main");
const ROUTES = {
  say: { title: "Say something", mount: mountStudio },
  voices: { title: "Voice library", mount: mountVoices },
  make: { title: "Make a voice", mount: mountMake },
  speakers: { title: "Speakers", mount: mountSpeakers },
};

let cleanup = null;
let started = false;
let firstRender = true;

const setBodyState = (s) => { document.body.dataset.state = s; };

// ---------------------------------------------------------------- header

const health = document.getElementById("health");
function paintHealth(hh) {
  health.dataset.level = hh.level;
  health.querySelector(".health__text").textContent = hh.text;
  health.title = hh.detail || "";
}
on("health", paintHealth);

const keyPop = document.getElementById("key-pop");
const keyButton = document.getElementById("key-button");
const keyForm = document.getElementById("key-form");
const keyInput = document.getElementById("key-input");
const keyStatus = document.getElementById("key-status");
const keyError = document.getElementById("key-error");
const keyForget = document.getElementById("key-forget");
const keyEdition = document.getElementById("key-edition");

// Browsers without the popover attribute: toggle by hand.
if (!("popover" in HTMLElement.prototype)) {
  keyPop.hidden = true;
  keyButton.addEventListener("click", () => { keyPop.hidden = !keyPop.hidden; });
}
const closeKeyPop = () => {
  if ("popover" in HTMLElement.prototype) { try { keyPop.hidePopover(); } catch { /* already closed */ } } else keyPop.hidden = true;
};

function paintKeyStatus() {
  const has = Boolean(getKey());
  keyStatus.textContent = has
    ? "A key is saved in this browser and sent with every request."
    : state.config?.auth_required ? "No key is saved. This server needs one." : "No key is saved. This server does not ask for one.";
  keyForget.disabled = !has;
  if (state.config) keyEdition.textContent = state.config.edition === "self" ? "Self-hosted edition." : "Hosted edition.";
}

keyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = keyInput.value.trim();
  keyError.hidden = true;
  if (!value) { keyError.textContent = "Paste a key first."; keyError.hidden = false; keyInput.focus(); return; }
  try {
    await listVoices(value);
  } catch (err) {
    keyError.textContent = err instanceof ApiError && err.status === 401
      ? "That key was not accepted. Check it and paste it again."
      : explain(err);
    keyError.hidden = false;
    return;
  }
  setKey(value);
  keyInput.value = "";
  paintKeyStatus();
  closeKeyPop();
  toast("Key saved in this browser.");
  invalidateVoices();
  if (document.body.dataset.state === "gate") enterApp(); else route();
});

keyForget.addEventListener("click", () => {
  forgetKey();
  invalidateVoices();
  paintKeyStatus();
  closeKeyPop();
  toast("Key forgotten. It is no longer stored in this browser.");
  if (state.config?.auth_required) showGate("Enter a key to keep using Voxxy.");
});

// A deploy replaced the web app while this page was open: old code can disagree with the new server
// (it may not know a step the server now asks for), so say so and offer a reload. Not automatic: the
// person may be halfway through a recording.
on("stale", () => {
  if (document.getElementById("update-banner")) return;
  const reload = h("button", { class: "btn btn--primary", type: "button" }, "Reload");
  reload.addEventListener("click", () => location.reload());
  const banner = h("div", { class: "update-banner", id: "update-banner", role: "status" },
    h("p", {}, "Voxxy has been updated since you opened this page. Reload to use the new version. Anything you have not saved yet will be lost."),
    reload);
  main.before(banner);
});

events.addEventListener("unauthorized", () => {
  if (!state.config?.auth_required || document.body.dataset.state === "gate") return;
  forgetKey();
  paintKeyStatus();
  showGate("The server did not accept your key. Paste a working key to continue.");
});

// ---------------------------------------------------------------- gate

function showGate(message) {
  cleanup?.(); cleanup = null;
  setBodyState("gate");
  closeKeyPop();
  const input = h("input", { id: "gate-key", type: "password", autocomplete: "off", autocapitalize: "off", spellcheck: "false", "aria-describedby": "gate-help" });
  const error = h("p", { class: "notice notice--error", role: "alert", hidden: !message }, message || "");
  const submit = h("button", { class: "btn btn--primary", type: "submit" }, "Continue");
  const form = h("form", { class: "stack", autocomplete: "off" },
    h("div", { class: "field" },
      h("label", { for: "gate-key" }, "API key"),
      input,
      h("p", { class: "hint", id: "gate-help" }, "Ask whoever runs this Voxxy for a key. It stays in this browser and is sent with each request.")),
    error,
    h("div", {}, submit));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = input.value.trim();
    error.hidden = true;
    if (!value) { error.textContent = "Paste your key to continue."; error.hidden = false; input.focus(); return; }
    submit.disabled = true; submit.textContent = "Checking…";
    try {
      await listVoices(value);
      setKey(value);
      paintKeyStatus();
      enterApp();
    } catch (err) {
      error.textContent = err instanceof ApiError && err.status === 401
        ? "That key was not accepted. Check it and paste it again."
        : explain(err);
      error.hidden = false;
      submit.disabled = false; submit.textContent = "Continue";
      input.focus();
    }
  });
  main.replaceChildren(h("div", { class: "gate" },
    h("div", { class: "stack stack--sm" },
      h("h1", { tabindex: "-1" }, "HeyMa hears you. Voxxy answers in a voice.")),
    form));
  main.querySelector("h1")?.focus();
  input.focus();
}

function showOffline(err) {
  setBodyState("gate");
  const retry = h("button", { class: "btn btn--primary", type: "button" }, "Try again");
  retry.addEventListener("click", boot);
  main.replaceChildren(h("div", { class: "gate" },
    h("h1", { tabindex: "-1" }, "Can't reach Voxxy"),
    h("p", { class: "notice notice--error", role: "alert" }, `${explain(err)} If this keeps happening, tell whoever runs this server.`),
    h("div", {}, retry)));
}

// ---------------------------------------------------------------- router

function currentRoute() {
  const name = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return ROUTES[name] ? name : "say";
}

function route() {
  if (document.body.dataset.state !== "app") return;
  const name = currentRoute();
  const def = ROUTES[name];
  cleanup?.(); cleanup = null;
  clear(main);
  document.title = `${def.title} · Voxxy`;
  for (const a of document.querySelectorAll("#tabs a")) {
    if (a.dataset.route === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  cleanup = def.mount(main, state) || null;
  if (!firstRender) {
    window.scrollTo({ top: 0 });
    main.querySelector("h1")?.focus({ preventScroll: true });
  }
  firstRender = false;
}
window.addEventListener("hashchange", route);

function enterApp() {
  setBodyState("app");
  if (!started) { started = true; startHealthPolling(); }
  paintKeyStatus();
  route();
}

// ---------------------------------------------------------------- boot

async function boot() {
  setBodyState("booting");
  try {
    await loadConfig();
  } catch (err) {
    showOffline(err);
    return;
  }
  paintKeyStatus();
  document.documentElement.dataset.edition = state.config.edition;
  if (!state.config.auth_required) { enterApp(); return; }
  const key = getKey();
  if (!key) { showGate(""); return; }
  try {
    await listVoices(key);
    enterApp();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      forgetKey();
      paintKeyStatus();
      showGate("The saved key was not accepted any more. Paste a working key to continue.");
    } else {
      showOffline(err);
    }
  }
}

boot();
