// Make a voice: record your own (live, against a challenge), describe one, or (self-hosted) upload a clip.

import { h, busy, toast, icons, arcs, fmtTime, fmtSeconds } from "./dom.js";
import {
  consentChallenge, saveOwnVoice, previewDesign, saveDesign, uploadVoice,
  explain, ApiError, slugify, kindOf, setVoice,
} from "./api.js";
import { state, loadVoices, invalidateVoices } from "./state.js";
import { micSupport, micErrorText, startRecording, toWav } from "./recorder.js";
import { createPlayer } from "./player.js";
import { sampleStep } from "./sample.js";

const EXAMPLES = [
  "A calm woman in her forties, slow and warm, with a light Irish accent",
  "A young man, quick and bright, friendly and a little playful, American",
  "An older man with a deep, gravelly voice, steady pace, serious and kind",
];

// ---------------------------------------------------------------- shared bits

// A display-name field that shows the slug the voice will be saved under, and warns on a clash.
function nameField({ kind, hint }) {
  const input = h("input", { id: "voice-name", type: "text", maxlength: "80", autocomplete: "off", placeholder: "for example Morning narrator", "aria-describedby": "voice-name-hint" });
  const note = h("p", { class: "hint", id: "voice-name-hint", "aria-live": "polite" }, hint || "");
  const slugOf = () => slugify(input.value);
  const taken = () => {
    const slug = slugOf();
    const existing = (state.voices || []).find((v) => v.name === slug);
    return existing && (kind === "any" ? "replace" : kindOf(existing) === kind ? "replace" : "clash");
  };
  const paint = () => {
    const slug = slugOf();
    input.removeAttribute("aria-invalid");
    if (!slug) { note.textContent = hint || ""; return; }
    const t = taken();
    note.textContent = t === "clash"
      ? `A different voice already uses the name “${slug}”. Choose another name.`
      : t === "replace" ? `Saved as “${slug}”. This replaces your existing voice with that name.` : `Saved as “${slug}”.`;
  };
  input.addEventListener("input", paint);
  return {
    el: h("div", { class: "field" }, h("label", { for: "voice-name" }, "Voice name"), input, note),
    input,
    // -> { display, slug } or null after marking the field invalid
    value() {
      const slug = slugOf();
      if (!slug) {
        input.setAttribute("aria-invalid", "true");
        note.textContent = "Give the voice a name with at least one letter or number.";
        input.focus();
        return null;
      }
      if (taken() === "clash") {
        input.setAttribute("aria-invalid", "true");
        note.textContent = `A different voice already uses the name “${slug}”. Choose another name.`;
        input.focus();
        return null;
      }
      return { display: input.value.trim(), slug };
    },
  };
}

function errorBox() {
  const el = h("p", { class: "notice notice--error", role: "alert", hidden: true });
  return {
    el,
    show(msg) { el.textContent = msg; el.hidden = false; el.scrollIntoView({ block: "nearest" }); },
    hide() { el.hidden = true; el.textContent = ""; },
  };
}

function savedPanel({ title, note, onAgain, voiceName, usable = true }) {
  const say = h("button", { class: "btn btn--primary", type: "button" }, "Say something with it");
  say.addEventListener("click", () => { setVoice(voiceName); location.hash = "#/say"; });
  const again = h("button", { class: "btn", type: "button" }, "Make another");
  again.addEventListener("click", onAgain);
  const library = h("a", { class: usable ? "btn" : "btn btn--primary", href: "#/voices" }, usable ? "See all voices" : "See it in the library");
  const box = h("div", { class: "stack", role: "status" },
    h("h2", { tabindex: "-1" }, title),
    note ? h("p", { class: "muted" }, note) : null,
    h("div", { class: "row" }, usable ? say : null, library, again));
  queueMicrotask(() => box.querySelector("h2")?.focus());
  return box;
}

// ---------------------------------------------------------------- record your own

// What the consent check said about a saved voice, in plain words, as a saved-panel.
function ownVoiceSaved(voice, onAgain) {
  const result = voice.consent_result || {};
  const base = { voiceName: voice.name, onAgain };
  if (result.state === "pending") {
    return savedPanel({
      ...base, usable: false,
      title: `“${voice.display_name}” is saved and waiting for a quick check`,
      note: "A person will listen to your recording before the voice can speak. You will see it in your library marked Under review, and it becomes available once it is approved.",
    });
  }
  if (result.state === "unchecked") {
    return savedPanel({
      ...base,
      title: `“${voice.display_name}” is saved`,
      note: result.warning
        ? `The consent check did not confirm this recording (${result.warning}). On this self-hosted server the voice is saved anyway; making sure it is yours is up to you.`
        : "The consent check did not run on this recording. On this self-hosted server the voice is saved anyway.",
    });
  }
  return savedPanel({
    ...base,
    title: `“${voice.display_name}” is saved and verified`,
    note: "Your recording matched the sentence, the code and your voice. It is ready to use.",
  });
}

function recordPath({ done, edition }) {
  const name = nameField({ kind: "own", hint: "What you will call this voice." });
  const sample = sampleStep({ allowUpload: edition !== "hosted" });
  // The hosted edition asks for a biometric release before a voice is recorded and stored.
  const release = edition !== "self" && state.config?.consent_release ? state.config.consent_release : null;
  const flow = h("div", { class: "steps" });
  const errors = errorBox();
  const stepEl = (state_, title, body) =>
    h("section", { class: "step", dataset: { state: state_ } },
      h("h2", { class: "step__title" }, title),
      h("div", { class: "step__body" }, body));
  const nameStep = stepEl("active", "Name your voice", name.el);

  let phase = "start"; // start | ready | recording | processing | review | saving
  let challenge = null;
  let rec = null; // { blob, wav, seconds, url }
  let controller = null;
  let player = null;
  let expiryTimer = null;
  let disposed = false;
  let live = {}; // nodes updated while recording
  let accepted = false; // the person ticked the release; kept across re-renders
  let releaseBox = null; // the "I accept" checkbox, rebuilt with the save step

  const support = micSupport();
  const minS = () => challenge?.record_min_seconds ?? 8;
  const maxS = () => challenge?.record_max_seconds ?? 30;

  async function getChallenge() {
    errors.hide();
    try {
      challenge = await consentChallenge();
      if (disposed) return;
      phase = "ready";
      startExpiry();
      render();
    } catch (err) {
      errors.show(explain(err, "Could not get a sentence."));
    }
  }

  function startExpiry() {
    clearInterval(expiryTimer);
    expiryTimer = setInterval(() => {
      const left = (Date.parse(challenge.expires_at) - Date.now()) / 1000;
      const node = flow.querySelector(".challenge__timer");
      if (left <= 0) {
        clearInterval(expiryTimer);
        resetToStart("That sentence expired. Get a new one when you are ready to record.");
        return;
      }
      if (node) node.textContent = `Good for ${fmtTime(left)}`;
    }, 1000);
  }

  function resetToStart(message) {
    controller?.cancel();
    controller = null;
    clearInterval(expiryTimer);
    if (player) { player.destroy(); player = null; }
    if (rec?.url) URL.revokeObjectURL(rec.url);
    rec = null;
    challenge = null;
    sample.clear();
    phase = "start";
    render();
    if (message) errors.show(message);
  }

  async function begin() {
    if (!support.ok) return;
    errors.hide();
    const barMin = ((minS() / maxS()) * 100).toFixed(1);
    try {
      phase = "recording";
      render();
      // keep the sentence on screen: the recorder sits below it
      flow.querySelector(".challenge")?.scrollIntoView({ block: "start" });
      const L = live; // the nodes of this recording; render() replaces `live` when the phase changes
      const a = L.arcs;
      controller = await startRecording({
        maxSeconds: maxS(),
        onLevel(level) {
          a.dataset.level = level > 0.5 ? "3" : level > 0.22 ? "2" : level > 0.06 ? "1" : "0";
        },
        onTick(s) {
          L.time.textContent = fmtTime(s);
          L.fill.style.transform = `scaleX(${Math.min(1, s / maxS()).toFixed(3)})`;
          const left = minS() - s;
          if (left > 0) {
            L.stop.setAttribute("aria-disabled", "true");
            L.stop.textContent = `Stop recording (${Math.ceil(left)} s)`;
            L.hint.textContent = `Keep reading. About ${Math.ceil(left)} more second${Math.ceil(left) === 1 ? "" : "s"} before you can stop.`;
          } else {
            L.stop.removeAttribute("aria-disabled");
            L.stop.textContent = "Stop recording";
            L.hint.textContent = "Done when you are. It stops by itself at " + fmtTime(maxS()) + ".";
          }
        },
        onLimit: () => finish(),
      });
      L.minMark.style.left = `${barMin}%`;
    } catch (err) {
      phase = "ready";
      render();
      errors.show(micErrorText(err));
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
      rec = { blob, wav, seconds: wav?.seconds ?? seconds, url: URL.createObjectURL(wav?.blob ?? blob) };
      if (rec.seconds < minS() - 0.3) {
        resetKeepChallenge(`That was only ${fmtSeconds(rec.seconds)}. Read the whole sentence and the code; it needs at least ${minS()} seconds.`);
        return;
      }
      phase = "review";
      render();
    } catch {
      if (disposed) return;
      resetKeepChallenge("The recording could not be read back. Try again, and allow the microphone if the browser asks.");
    }
  }

  function resetKeepChallenge(message) {
    if (player) { player.destroy(); player = null; }
    if (rec?.url) URL.revokeObjectURL(rec.url);
    rec = null;
    phase = "ready";
    render();
    if (message) errors.show(message);
  }

  async function save(btn) {
    const v = name.value();
    if (!v) return;
    errors.hide();
    if (release && !accepted) { errors.show("Tick the box to accept the notice before saving your voice."); releaseBox?.focus(); return; }
    if (sample.isRecording()) { errors.show("Finish the longer sample first, or remove it, then press Save voice."); return; }
    await busy(btn, "Saving…", async () => {
      const fd = new FormData();
      fd.append("challenge_id", challenge.challenge_id);
      fd.append("name", v.slug);
      fd.append("display_name", v.display);
      if (release) fd.append("release_accepted", "true");
      const file = rec.wav ? rec.wav.blob : rec.blob;
      const type = rec.blob.type || "";
      const ext = rec.wav ? "wav" : type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      fd.append("audio", file, `recording.${ext}`);
      const longer = sample.get();
      if (longer) fd.append("reference", longer.blob, longer.name);
      try {
        const voice = await saveOwnVoice(fd);
        invalidateVoices();
        clearInterval(expiryTimer);
        done(ownVoiceSaved(voice, () => done(null)));
      } catch (err) {
        if (err instanceof ApiError && err.code === "release_required") {
          errors.show("Please read the notice above and tick the box to accept it, then press Save voice again. Your recording is still here.");
          releaseBox?.focus();
        } else if (err instanceof ApiError && err.code === "consent_rejected") {
          // The check ran and did not accept this recording. The sentence is used up: a new one is needed.
          resetToStart(`Your voice was not added this time. ${err.message}`);
        } else if (err instanceof ApiError && err.code === "consent_unavailable") {
          errors.show("The consent check is not available right now, so your voice was not saved. Your sentence is still good; press Save voice again in a moment.");
        } else if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
          resetToStart(`${err.message} Your recording was not saved; get a new sentence and record it again.`);
        } else if (err instanceof ApiError && err.status === 409) {
          errors.show(`${err.message}. Change the name above, then press Save voice again.`);
          name.input.focus();
        } else {
          errors.show(explain(err, "The voice was not saved."));
        }
      }
    });
  }

  function render() {
    if (player) { player.destroy(); player = null; }
    live = {};
    const ready = phase !== "start";

    // step 2: the sentence
    let s2;
    if (!ready) {
      const get = h("button", { class: "btn btn--primary", type: "button" }, "Get my sentence");
      get.addEventListener("click", () => busy(get, "Getting…", getChallenge));
      s2 = stepEl("active", "Get your sentence", h("div", { class: "stack" },
        h("p", { class: "muted" }, "You will read one sentence and a short code aloud, live. Voxxy checks that you said both and that the voice matches, so only your own voice can be added. The code stops anyone using an old recording."),
        h("div", {}, get)));
    } else {
      const fresh = h("button", { class: "btn btn--small", type: "button" }, "Different sentence");
      fresh.addEventListener("click", () => resetToStart());
      if (phase === "recording" || phase === "processing" || phase === "saving") fresh.disabled = true;
      s2 = stepEl(phase === "recording" ? "active" : "done", "Your sentence", h("div", { class: "challenge" },
        h("p", { class: "challenge__sentence" }, challenge.sentence),
        h("div", { class: "challenge__code" },
          h("span", {}, "Then say this code:"),
          h("span", { class: "challenge__digits", "aria-label": challenge.code.split("").join(" ") }, `${challenge.code.slice(0, 3)} ${challenge.code.slice(3)}`)),
        h("div", { class: "row row--between" },
          h("span", { class: "challenge__timer" }, ""),
          fresh)));
      if (phase === "recording") s2.dataset.sticky = "true";
    }

    // step 3: record
    let s3;
    if (phase === "start") {
      s3 = stepEl("idle", "Record it", h("p", { class: "muted" }, "Press Start recording after you have your sentence."));
    } else if (phase === "ready") {
      const start = h("button", { class: "btn btn--primary", type: "button" }, ...[icons.mic(), h("span", {}, "Start recording")]);
      start.addEventListener("click", begin);
      if (!support.ok) start.setAttribute("aria-disabled", "true");
      s3 = stepEl("active", "Record it", h("div", { class: "stack" },
        h("p", { class: "muted" }, `Find a quiet spot. Read the whole sentence, then the six digits of the code. It needs at least ${minS()} seconds and stops at ${maxS()}.`),
        support.ok ? null : h("p", { class: "notice notice--error", role: "alert" }, support.reason),
        h("div", {}, start)));
    } else if (phase === "recording") {
      const a = arcs({ size: 52 });
      a.classList.add("recorder__arcs");
      a.dataset.level = "0";
      const time = h("span", { class: "recorder__time", "aria-hidden": "true" }, "0:00");
      const hint = h("p", { class: "muted small", "aria-live": "off" }, `Keep reading. About ${minS()} more seconds before you can stop.`);
      const fill = h("div", { class: "bar-meter__fill" });
      const minMark = h("div", { class: "bar-meter__min", style: `left:${((minS() / maxS()) * 100).toFixed(1)}%` });
      const bar = h("div", { class: "bar-meter", "aria-hidden": "true" }, fill, minMark);
      const stop = h("button", { class: "btn btn--primary", type: "button", "aria-disabled": "true" }, `Stop recording (${minS()} s)`);
      stop.addEventListener("click", () => { if (stop.getAttribute("aria-disabled") !== "true") finish(); });
      live = { arcs: a, time, hint, fill, bar, minMark, stop };
      s3 = stepEl("active", "Recording", h("div", { class: "recorder" },
        h("div", { class: "recorder__stage" }, a,
          h("div", { class: "recorder__readout" },
            h("span", { class: "recorder__rec", "aria-hidden": "true" }, h("span", { class: "recorder__dot" }), "Recording"),
            time,
            h("span", { class: "vh", role: "status" }, "Recording. Read the sentence, then the code."),
            h("span", { class: "muted small" }, `at least ${minS()} s, at most ${maxS()} s`))),
        bar, hint, h("div", {}, stop)));
    } else if (phase === "processing") {
      s3 = stepEl("active", "Recording", h("p", { class: "muted", role: "status" }, "Preparing your recording…"));
    } else {
      s3 = stepEl("done", `Recorded (${fmtSeconds(rec.seconds)})`, null);
    }

    // step 4 (optional): a longer sample to make the voice from
    const s4a = phase === "review" || phase === "saving"
      ? stepEl("active", "Add a longer sample (optional)", sample.el)
      : stepEl("idle", "Add a longer sample (optional)", h("p", { class: "muted" }, "After your recording you can add a longer one to make the voice sound closer."));

    // step 5: check and save
    let s4;
    if (phase === "review" || phase === "saving") {
      player = createPlayer({ src: rec.url, durationHint: rec.seconds, autoplay: false, downloadName: "my-voice.wav", meta: [h("span", {}, "Your recording")] });
      const again = h("button", { class: "btn", type: "button" }, "Record again");
      again.addEventListener("click", () => resetKeepChallenge());
      const saveBtn = h("button", { class: "btn btn--primary", type: "button" }, "Save voice");
      saveBtn.addEventListener("click", () => save(saveBtn));
      let releaseEl = null;
      releaseBox = null;
      if (release) {
        const wasChecked = accepted;
        releaseBox = h("input", { type: "checkbox", id: "release-accept", "aria-describedby": "release-text" });
        releaseBox.checked = wasChecked;
        saveBtn.disabled = !wasChecked;
        releaseBox.addEventListener("change", () => { accepted = releaseBox.checked; saveBtn.disabled = !accepted; });
        releaseEl = h("div", { class: "field release" },
          h("p", { class: "release__text", id: "release-text" }, release.text),
          h("label", { class: "release__accept", for: "release-accept" }, releaseBox, h("span", {}, "I have read this and I accept it.")));
      }
      s4 = stepEl("active", "Listen, then save", h("div", { class: "stack" },
        h("div", { class: "panel" }, player.el),
        releaseEl,
        h("div", { class: "row" }, saveBtn, again)));
    } else {
      s4 = stepEl("idle", "Listen, then save", h("p", { class: "muted" }, "You can play your recording back before it is saved."));
    }

    flow.replaceChildren(nameStep, s2, s3, s4a, s4);
    // errors sit inside whichever step the person is working in
    const actives = [...flow.querySelectorAll('.step[data-state="active"]')];
    actives[actives.length - 1].querySelector(".step__body").append(errors.el);
    if (challenge && ready) {
      const left = (Date.parse(challenge.expires_at) - Date.now()) / 1000;
      const node = flow.querySelector(".challenge__timer");
      if (node) node.textContent = `Good for ${fmtTime(Math.max(0, left))}`;
    }
  }

  render();
  return {
    el: h("div", { class: "stack" },
      h("p", { class: "lede" }, "Record your own voice, reading a sentence Voxxy gives you. Only your own voice, made live."),
      flow),
    destroy() {
      disposed = true;
      controller?.cancel();
      clearInterval(expiryTimer);
      sample.destroy();
      if (player) player.destroy();
      if (rec?.url) URL.revokeObjectURL(rec.url);
    },
  };
}

// ---------------------------------------------------------------- describe a voice

function describePath({ done, edition }) {
  const hosted = edition === "hosted";
  const name = nameField({ kind: "designed", hint: "What you will call this voice." });
  const desc = h("textarea", { id: "design-desc", rows: "4", maxlength: "400", placeholder: EXAMPLES[0], "aria-describedby": "design-guide" });
  const sample = h("textarea", { id: "design-sample", rows: "3", maxlength: "400", placeholder: "Hello, this is my voice. I can read you a short story, answer a question, or just say good morning." });
  const errors = errorBox();
  const previewHost = h("div", { class: "result", "aria-live": "polite" });
  let player = null;

  const chips = EXAMPLES.map((text) => {
    const b = h("button", { class: "chip-btn", type: "button" }, text);
    b.addEventListener("click", () => { desc.value = text; desc.focus(); });
    return b;
  });

  const checkInputs = () => {
    const v = name.value();
    if (!v) return null;
    if (desc.value.trim().length < 8) {
      desc.setAttribute("aria-invalid", "true");
      errors.show("Describe the voice in a few words: age, pace, tone, accent.");
      desc.focus();
      return null;
    }
    desc.removeAttribute("aria-invalid");
    return v;
  };
  desc.addEventListener("input", () => { desc.removeAttribute("aria-invalid"); errors.hide(); });

  const refusal = (err) => (err instanceof ApiError && err.status === 422 ? err.message : explain(err));

  const previewBtn = h("button", { class: "btn", type: "button" }, "Preview");
  previewBtn.addEventListener("click", async () => {
    errors.hide();
    if (desc.value.trim().length < 8) { checkInputs(); return; }
    await busy(previewBtn, "Making a preview…", async () => {
      previewHost.replaceChildren(h("p", { class: "muted" }, "Making a preview. This takes a few seconds."));
      try {
        const r = await previewDesign(desc.value.trim(), sample.value.trim() || undefined);
        if (player) player.destroy();
        player = createPlayer({ src: r.audio_url, durationHint: r.duration_s, downloadName: "voice-preview.ogg", meta: [h("span", {}, "Preview"), h("span", {}, r.duration_s ? fmtSeconds(r.duration_s) : "")] });
        previewHost.replaceChildren(h("div", { class: "panel" }, player.el));
      } catch (err) {
        previewHost.replaceChildren();
        errors.show(refusal(err));
      }
    });
  });

  const saveBtn = h("button", { class: "btn btn--primary", type: "button" }, "Save voice");
  saveBtn.addEventListener("click", async () => {
    errors.hide();
    const v = checkInputs();
    if (!v) return;
    await busy(saveBtn, "Making the voice…", async () => {
      try {
        const voice = await saveDesign({ name: v.slug, display_name: v.display, description: desc.value.trim(), ...(sample.value.trim() ? { sample_text: sample.value.trim() } : {}) });
        invalidateVoices();
        done(savedPanel({ title: `“${voice.display_name}” is ready`, note: "It is saved as a designed voice.", voiceName: voice.name, onAgain: () => done(null) }));
      } catch (err) {
        errors.show(err instanceof ApiError && err.status === 409 ? `${err.message}. Change the name, then try again.` : refusal(err));
      }
    });
  });

  return {
    el: h("div", { class: "stack" },
      h("p", { class: "lede" }, "Describe the voice you want and Voxxy makes it. No recording of anyone is used."),
      name.el,
      h("div", { class: "field" },
        h("label", { for: "design-desc" }, "Describe the voice"),
        desc,
        h("dl", { class: "guide", id: "design-guide" },
          h("dt", {}, "Age"), h("dd", {}, "young, in her forties, older"),
          h("dt", {}, "Pace"), h("dd", {}, "slow and unhurried, quick, steady"),
          h("dt", {}, "Tone"), h("dd", {}, "warm, bright, serious, playful"),
          h("dt", {}, "Accent"), h("dd", {}, "light Irish, Southern American, neutral")),
        hosted ? h("p", { class: "hint" }, "Describe the voice itself. Leave out names of real people and who it should sound like.") : null,
        errors.el),
      h("div", { class: "field" }, h("span", { class: "label" }, "Try one of these"), h("div", { class: "examples" }, chips)),
      h("div", { class: "field" },
        h("label", { for: "design-sample" }, "What should the sample say? (optional)"),
        sample),
      h("div", { class: "row" }, previewBtn, saveBtn),
      previewHost),
    destroy() { if (player) player.destroy(); },
  };
}

// ---------------------------------------------------------------- upload (self-hosted only)

function uploadPath({ done }) {
  const name = nameField({ kind: "any", hint: "What you will call this voice." });
  const file = h("input", { id: "up-file", type: "file", accept: "audio/*,.wav,.mp3,.m4a,.ogg,.flac", "aria-describedby": "up-hint" });
  const transcript = h("textarea", { id: "up-text", rows: "3", maxlength: "2000", placeholder: "The exact words in the clip. It helps the voice match." });
  const errors = errorBox();
  const saveBtn = h("button", { class: "btn btn--primary", type: "button" }, "Save voice");

  saveBtn.addEventListener("click", async () => {
    errors.hide();
    const v = name.value();
    if (!v) return;
    const f = file.files?.[0];
    if (!f) { file.setAttribute("aria-invalid", "true"); errors.show("Choose an audio clip first. Ten to thirty seconds of one voice works best."); file.focus(); return; }
    file.removeAttribute("aria-invalid");
    await busy(saveBtn, "Saving…", async () => {
      const fd = new FormData();
      fd.append("name", v.slug);
      fd.append("display_name", v.display);
      fd.append("tags", "uploaded");
      if (transcript.value.trim()) fd.append("prompt_text", transcript.value.trim());
      fd.append("audio", f, f.name);
      try {
        const voice = await uploadVoice(fd);
        invalidateVoices();
        done(savedPanel({ title: `“${voice.display_name}” is saved`, voiceName: voice.name, onAgain: () => done(null) }));
      } catch (err) {
        errors.show(explain(err, "The clip was not saved."));
      }
    });
  });

  return {
    el: h("div", { class: "stack" },
      h("p", { class: "lede" }, "Upload a clip of one voice. Use recordings you have the rights to."),
      name.el,
      h("div", { class: "field" },
        h("label", { for: "up-file" }, "Audio clip"),
        file,
        h("p", { class: "hint", id: "up-hint" }, "WAV, MP3, M4A, OGG or FLAC. Ten to thirty seconds of a single voice; longer clips are trimmed.")),
      h("div", { class: "field" }, h("label", { for: "up-text" }, "What the clip says (optional)"), transcript),
      errors.el,
      h("div", {}, saveBtn)),
    destroy() {},
  };
}

// ---------------------------------------------------------------- the page

export function mountMake(root, ctx) {
  const edition = ctx.config?.edition || "hosted";
  const paths = [
    { id: "record", label: "Record yours", build: recordPath },
    { id: "describe", label: "Describe one", build: describePath },
  ];
  if (ctx.config?.features?.upload_clip) paths.push({ id: "upload", label: "Upload a clip", build: uploadPath });

  let current = paths[0].id;
  let view = null;
  const host = h("div", { class: "panel" });

  function show(id) {
    view?.destroy();
    current = id;
    const def = paths.find((p) => p.id === id);
    view = def.build({ edition, done: (panel) => { if (panel) { view?.destroy(); view = { destroy() {} }; host.replaceChildren(panel); } else show(id); } });
    host.replaceChildren(view.el);
  }

  const seg = h("fieldset", { class: "seg-wrap" },
    h("legend", { class: "vh" }, "How do you want to make a voice?"),
    h("div", { class: "seg", style: `--n:${paths.length}` }, paths.map((p) => {
      const input = h("input", { type: "radio", name: "path", value: p.id });
      input.checked = p.id === current;
      input.addEventListener("change", () => show(p.id));
      return h("label", {}, input, h("span", {}, p.label));
    })));

  root.append(h("div", { class: "page" },
    h("div", { class: "page__head" },
      h("h1", { tabindex: "-1" }, "Make a voice"),
      h("p", { class: "lede" }, edition === "hosted"
        ? "Two ways to a new voice: read a sentence aloud in your own voice, or describe the voice you want."
        : "Record your own voice, describe a voice, or upload a clip.")),
    paths.length > 1 ? seg : null,
    host));

  loadVoices().catch(() => {}).finally(() => {});
  show(current);
  return () => view?.destroy();
}
