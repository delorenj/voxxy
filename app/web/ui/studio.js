// Say something (the home page). Text, voice, speak, play the result, send it to a desk.

import { h, busy, icons, iconBtnContent, arcs, fmtSeconds } from "./dom.js";
import { synth, sinkStatus, sinkPlay, sinkSay, explain, getSink, setSink, getVoice, setVoice, SINK_RE } from "./api.js";
import { state, on, loadVoices } from "./state.js";
import { voicePicker } from "./picker.js";
import { createPlayer } from "./player.js";

const SOURCE_TEXT = { reference: "your clip", registered: "saved voice", stock: "stock voice" };

export function mountStudio(root, ctx) {
  const hosted = (ctx?.config?.edition || "hosted") === "hosted";
  let disposed = false;
  let player = null;
  let last = null; // { text, voice, result }
  const cleanups = [];

  // ---- text
  const textarea = h("textarea", { id: "say-text", rows: "6", placeholder: "Type or paste what you want to hear.", "aria-describedby": "say-count" });
  const counter = h("span", { class: "counter", id: "say-count" }, "0 characters");
  textarea.addEventListener("input", () => {
    counter.textContent = `${textarea.value.length.toLocaleString()} character${textarea.value.length === 1 ? "" : "s"}`;
    textarea.removeAttribute("aria-invalid");
  });

  // ---- voice
  const pickerHost = h("div", {}, h("div", { class: "skeleton", style: "height:52px" }));

  // ---- result
  const resultBox = h("div", { class: "result", "aria-live": "polite" },
    h("p", { class: "muted" }, "Your audio shows up here. Press Speak, or Ctrl+Enter inside the text box."));

  const speakBtn = h("button", { class: "btn btn--primary", type: "button" }, "Speak");
  const engineLine = h("p", { class: "muted small", id: "say-engine" });
  // the engine name is operator detail: shown on a self-hosted server only
  const paintEngine = (hh) => { engineLine.textContent = !hosted && hh.detail ? hh.detail : ""; };
  paintEngine(state.health);
  cleanups.push(on("health", paintEngine));

  // ---- desk
  const sinkInput = h("input", { id: "desk-name", type: "text", value: getSink(), placeholder: "for example my-laptop", autocomplete: "off", autocapitalize: "off", spellcheck: "false", "aria-describedby": "desk-hint" });
  const listenerN = h("span", { class: "listeners__n", "aria-hidden": "true", hidden: true });
  const listenerText = h("p", { class: "muted", id: "desk-listeners", "aria-live": "polite" }, "Enter a desk name to see who is listening.");
  const deskNote = h("p", { class: "small", "aria-live": "polite" });
  const deskBtn = h("button", { class: "btn", type: "button" }, ...iconBtnContent(icons.desk, "Play on that machine"));
  const checkBtn = h("button", { class: "btn btn--small", type: "button" }, "Check");

  let deskTimer = null;
  async function checkDesk(quiet = false) {
    const key = sinkInput.value.trim();
    if (!key) { listenerN.hidden = true; listenerText.textContent = "Enter a desk name to see who is listening."; return null; }
    if (!SINK_RE.test(key)) {
      listenerN.hidden = true;
      listenerText.textContent = "Desk names use letters, numbers, dots, dashes and underscores, and start with a letter or number.";
      return null;
    }
    try {
      const s = await sinkStatus(key);
      if (disposed) return null;
      listenerN.textContent = String(s.listeners);
      listenerN.hidden = false;
      listenerText.replaceChildren(s.listeners > 0
        ? `${s.listeners === 1 ? "1 machine is" : `${s.listeners} machines are`} listening on “${key}”.`
        : h("span", {}, `Nobody is listening on “${key}”. On that machine run `, h("span", { class: "mono" }, "voxxy sink on"),
            ", or switch on Speak on this desk in the HeyMa tray. ", h("a", { class: "link", href: "#/speakers" }, "How")));
      return s.listeners;
    } catch (err) {
      if (!quiet) listenerText.textContent = explain(err);
      return null;
    }
  }
  sinkInput.addEventListener("input", () => {
    setSink(sinkInput.value.trim());
    clearTimeout(deskTimer);
    deskTimer = setTimeout(() => checkDesk(true), 450);
  });
  checkBtn.addEventListener("click", () => checkDesk());

  async function playOnDesk() {
    const key = sinkInput.value.trim();
    if (!key || !SINK_RE.test(key)) {
      deskNote.textContent = "Enter a desk name first (letters, numbers, dots, dashes, underscores).";
      deskPanel.open = true;
      sinkInput.setAttribute("aria-invalid", "true");
      sinkInput.focus();
      return;
    }
    sinkInput.removeAttribute("aria-invalid");
    const text = textarea.value.trim();
    const voice = picker?.get() || "";
    if (!last && !text) {
      deskNote.textContent = "Type something to say first.";
      textarea.focus();
      return;
    }
    setSink(key);
    deskNote.textContent = "";
    await busy(deskBtn, "Sending…", async () => {
      try {
        const reuse = last && last.text === text && last.voice === voice;
        const res = reuse
          ? await sinkPlay(key, { audio_url: last.result.audio_url, text, voice: voice || null, engine: last.result.engine, duration_s: last.result.duration_s, format: last.result.format })
          : await sinkSay(key, text || last.text, voice);
        deskNote.textContent = res.delivered > 0
          ? `Sent to ${res.delivered === 1 ? "1 listener" : `${res.delivered} listeners`} on “${key}”.`
          : `Made the audio, but nobody is listening on “${key}”. Turn on Speak on this desk on that machine, then press the button again.`;
        checkDesk(true);
      } catch (err) {
        deskNote.textContent = explain(err);
      }
    });
  }
  deskBtn.addEventListener("click", playOnDesk);

  // ---- speak
  async function speak() {
    const text = textarea.value.trim();
    if (!text) {
      textarea.setAttribute("aria-invalid", "true");
      showError("Type something to say first.");
      textarea.focus();
      return;
    }
    const voice = picker?.get() || "";
    await busy(speakBtn, "Speaking…", async () => {
      resultBox.replaceChildren(h("div", { class: "row" }, workingArcs(), h("p", { class: "muted" }, "Making the audio. A few seconds.")));
      try {
        const result = await synth(text, voice);
        if (disposed) return;
        last = { text, voice, result };
        showResult(result, text, voice);
      } catch (err) {
        if (!disposed) showError(explain(err, "That did not work."));
      }
    });
  }
  speakBtn.addEventListener("click", speak);
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); speak(); }
  });

  function workingArcs() {
    const a = arcs({ size: 28 });
    a.dataset.active = "true";
    a.style.color = "var(--accent)";
    return a;
  }

  function showError(message) {
    if (player) { player.destroy(); player = null; }
    resultBox.replaceChildren(h("p", { class: "notice notice--error", role: "alert" }, message));
  }

  function showResult(result, text, voice) {
    if (player) player.destroy();
    const label = voice ? (state.voices || []).find((v) => v.name === voice)?.display_name || voice : "Engine default";
    const deskAction = h("button", { class: "btn btn--small", type: "button" }, "Play on another machine");
    deskAction.addEventListener("click", () => { playOnDesk(); });
    player = createPlayer({
      src: result.audio_url,
      durationHint: result.duration_s,
      downloadName: "voxxy.ogg",
      meta: [
        h("span", {}, label),
        h("span", {}, result.duration_s ? fmtSeconds(result.duration_s) : ""),
        h("span", {}, !hosted && result.engine ? `via ${result.engine}` : ""),
        result.voice_source ? h("span", {}, SOURCE_TEXT[result.voice_source] || result.voice_source) : null,
      ],
      actions: [deskAction],
    });
    resultBox.replaceChildren(player.el);
  }

  // ---- voices -> picker
  let picker = null;
  const mountPicker = (voices) => {
    const stored = getVoice();
    const value = voices.some((v) => v.name === stored) ? stored : "";
    picker = voicePicker({ voices, value, onChange: (name) => setVoice(name) });
    pickerHost.replaceChildren(picker.el);
  };
  loadVoices()
    .then((voices) => { if (!disposed) mountPicker(voices); })
    .catch((err) => {
      if (disposed) return;
      pickerHost.replaceChildren(h("p", { class: "notice notice--error", role: "alert" }, `${explain(err)} You can still speak with the engine default.`));
      picker = { get: () => "", set() {} };
    });

  if (sinkInput.value) checkDesk(true);
  const poll = setInterval(() => { if (!document.hidden && SINK_RE.test(sinkInput.value.trim())) checkDesk(true); }, 20000);

  const deskPanel = h("details", { class: "panel desk", open: Boolean(sinkInput.value) },
    h("summary", {}, h("h2", { id: "desk-h" }, "Play on another machine")),
    h("div", { class: "stack desk__body" },
      h("p", { class: "muted small" }, "Send speech to the speakers of a machine you have set up as a desk. It plays there, not in this tab."),
      h("div", { class: "field" },
        h("label", { for: "desk-name" }, "Desk name"),
        sinkInput,
        h("p", { class: "hint", id: "desk-hint" }, "The name that machine listens on. By default that is its host name.")),
      h("div", { class: "listeners" }, listenerN, listenerText),
      h("div", { class: "row" }, deskBtn, checkBtn),
      deskNote));

  root.append(
    h("div", { class: "page" },
      h("div", { class: "page__head" },
        h("h1", { tabindex: "-1" }, "Say something"),
        h("p", { class: "lede" }, "Type a line, pick a voice, hear it.")),
      h("div", { class: "stack stack--lg" },
        h("div", { class: "stack stack--lg" },
          h("div", { class: "stack" },
            h("div", { class: "field" },
              h("label", { for: "say-text" }, "What should it say?"),
              textarea,
              h("div", { class: "row row--between" }, counter)),
            h("div", { class: "field" }, h("span", { class: "label", id: "voice-label" }, "Voice"), pickerHost),
            h("div", { class: "row" }, speakBtn, engineLine)),
          h("section", { class: "panel", "aria-labelledby": "result-h" },
            h("h2", { id: "result-h", class: "vh" }, "Result"),
            resultBox)),
        deskPanel)),
  );

  return () => {
    disposed = true;
    clearInterval(poll);
    clearTimeout(deskTimer);
    cleanups.forEach((fn) => fn());
    if (player) player.destroy();
  };
}
