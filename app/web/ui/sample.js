// The optional longer sample for "record your own voice": a second recording of 10 to 30 seconds (or, on a
// self-hosted server only, an uploaded clip). When there is one, the voice is made from it instead of from
// the short consent recording, which makes the voice sound closer. The server still matches it against the
// consent recording, so it has to be the same person.

import { h, icons, fmtTime, fmtSeconds } from "./dom.js";
import { micSupport, micErrorText, startRecording, toWav } from "./recorder.js";
import { createPlayer } from "./player.js";

export const SAMPLE_MIN = 10;
export const SAMPLE_MAX = 30;

export function sampleStep({ allowUpload }) {
  let phase = "idle"; // idle | recording | processing | ready
  let sample = null; // { blob, name, seconds, url }
  let controller = null;
  let player = null;
  let live = {};
  let disposed = false;
  const support = micSupport();
  const root = h("div", { class: "stack" });
  const note = h("p", { class: "notice notice--error", role: "alert", hidden: true });

  const say = (msg) => { note.textContent = msg || ""; note.hidden = !msg; };

  function drop() {
    controller?.cancel();
    controller = null;
    if (player) { player.destroy(); player = null; }
    if (sample?.url) URL.revokeObjectURL(sample.url);
    sample = null;
    phase = "idle";
  }

  async function begin() {
    say("");
    try {
      phase = "recording";
      render();
      const L = live;
      controller = await startRecording({
        maxSeconds: SAMPLE_MAX,
        onLevel() {},
        onTick(s) {
          L.time.textContent = fmtTime(s);
          L.fill.style.transform = `scaleX(${Math.min(1, s / SAMPLE_MAX).toFixed(3)})`;
          const left = SAMPLE_MIN - s;
          if (left > 0) {
            L.stop.setAttribute("aria-disabled", "true");
            L.stop.textContent = `Stop (${Math.ceil(left)} s)`;
          } else {
            L.stop.removeAttribute("aria-disabled");
            L.stop.textContent = "Stop";
          }
        },
        onLimit: () => finish(),
      });
    } catch (err) {
      phase = "idle";
      render();
      say(micErrorText(err));
    }
  }

  async function finish() {
    if (!controller) return;
    const c = controller;
    controller = null;
    phase = "processing";
    render();
    try {
      const { blob, seconds } = await c.stop();
      if (disposed) return;
      const wav = await toWav(blob);
      if (disposed) return;
      const secs = wav?.seconds ?? seconds;
      if (secs < SAMPLE_MIN - 0.3) {
        phase = "idle";
        render();
        say(`That was ${fmtSeconds(secs)}. The longer sample needs at least ${SAMPLE_MIN} seconds.`);
        return;
      }
      const type = blob.type || "";
      const ext = wav ? "wav" : type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      const file = wav ? wav.blob : blob;
      sample = { blob: file, name: `longer-sample.${ext}`, seconds: secs, url: URL.createObjectURL(file) };
      phase = "ready";
      render();
    } catch {
      if (disposed) return;
      phase = "idle";
      render();
      say("The recording could not be read back. Try again.");
    }
  }

  function chosen(file) {
    say("");
    if (!file) return;
    sample = { blob: file, name: file.name, seconds: null, url: URL.createObjectURL(file) };
    phase = "ready";
    render();
  }

  function render() {
    if (player) { player.destroy(); player = null; }
    live = {};
    let body;
    if (phase === "idle" || phase === "processing") {
      const rec = h("button", { class: "btn", type: "button" }, ...[icons.mic(), h("span", {}, "Record a longer sample")]);
      rec.addEventListener("click", begin);
      if (!support.ok || phase === "processing") rec.setAttribute("aria-disabled", "true");
      const file = allowUpload
        ? h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a,.ogg,.flac", "aria-label": "Or upload a clip" })
        : null;
      file?.addEventListener("change", () => chosen(file.files?.[0]));
      body = h("div", { class: "stack" },
        h("p", { class: "muted" },
          `Optional. Speak naturally for ${SAMPLE_MIN} to ${SAMPLE_MAX} seconds, in the same place and on the same microphone. ` +
          "Read anything you like. The voice is made from this longer recording, which makes it sound closer to you."),
        support.ok ? null : h("p", { class: "notice notice--error", role: "alert" }, support.reason),
        h("div", { class: "row" }, rec, file),
        phase === "processing" ? h("p", { class: "muted", role: "status" }, "Preparing your recording…") : null);
    } else if (phase === "recording") {
      const time = h("span", { class: "recorder__time", "aria-hidden": "true" }, "0:00");
      const fill = h("div", { class: "bar-meter__fill" });
      const minMark = h("div", { class: "bar-meter__min", style: `left:${((SAMPLE_MIN / SAMPLE_MAX) * 100).toFixed(1)}%` });
      const stop = h("button", { class: "btn btn--primary", type: "button", "aria-disabled": "true" }, `Stop (${SAMPLE_MIN} s)`);
      stop.addEventListener("click", () => { if (stop.getAttribute("aria-disabled") !== "true") finish(); });
      live = { time, fill, stop };
      body = h("div", { class: "recorder" },
        h("div", { class: "recorder__readout" },
          h("span", { class: "recorder__rec", "aria-hidden": "true" }, h("span", { class: "recorder__dot" }), "Recording"),
          time,
          h("span", { class: "vh", role: "status" }, "Recording the longer sample."),
          h("span", { class: "muted small" }, `at least ${SAMPLE_MIN} s, at most ${SAMPLE_MAX} s`)),
        h("div", { class: "bar-meter", "aria-hidden": "true" }, fill, minMark),
        h("div", {}, stop));
    } else {
      player = createPlayer({
        src: sample.url, durationHint: sample.seconds ?? undefined, autoplay: false, downloadName: "longer-sample.wav",
        meta: [h("span", {}, "Longer sample"), h("span", {}, sample.seconds ? fmtSeconds(sample.seconds) : sample.name)],
      });
      const remove = h("button", { class: "btn btn--small", type: "button" }, "Remove it");
      remove.addEventListener("click", () => { drop(); say(""); render(); });
      body = h("div", { class: "stack" },
        h("div", { class: "panel" }, player.el),
        h("div", { class: "row" }, remove, h("span", { class: "muted small" }, "The voice will be made from this recording.")));
    }
    root.replaceChildren(body, note);
  }

  render();
  return {
    el: root,
    // { blob, name } to send as `reference`, or null
    get: () => (phase === "ready" && sample ? { blob: sample.blob, name: sample.name } : null),
    isRecording: () => phase === "recording" || phase === "processing",
    clear() { drop(); say(""); render(); },
    destroy() { disposed = true; drop(); },
  };
}
