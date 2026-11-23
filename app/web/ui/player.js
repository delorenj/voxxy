// Audio playback: one shared element so only one thing speaks at a time, and the result player.

import { h, icons, arcs, fmtTime } from "./dom.js";

let active = null; // { audio, stop() } for whatever is playing now

function claim(entry) {
  if (active && active !== entry) active.stop();
  active = entry;
}

// Play a URL on a throwaway element. `onState('loading'|'playing'|'idle')`. Returns { stop }.
export function playUrl(url, onState = () => {}) {
  const audio = new Audio();
  audio.preload = "auto";
  const entry = {
    audio,
    stop() {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      onState("idle");
      if (active === entry) active = null;
    },
  };
  claim(entry);
  onState("loading");
  audio.addEventListener("playing", () => onState("playing"));
  audio.addEventListener("ended", () => entry.stop());
  audio.addEventListener("error", () => onState("error"));
  audio.src = url;
  audio.play().catch(() => onState("idle"));
  return entry;
}

// The result player: play/pause, scrubber, time, meta line, and actions (download and friends).
export function createPlayer({ src, durationHint, meta = [], actions = [], autoplay = true, downloadName = "voxxy.ogg" }) {
  const audio = new Audio();
  audio.preload = "auto";
  audio.src = src;

  const entry = { audio, stop: () => audio.pause() };
  const playBtn = h("button", { class: "player__play", type: "button", "aria-label": "Play" }, icons.play());
  const scrub = h("input", { type: "range", min: "0", max: "1000", step: "1", value: "0", "aria-label": "Position" });
  const cur = h("span", {}, "0:00");
  const total = h("span", {}, fmtTime(durationHint));
  const indicator = arcs({ size: 18 });
  indicator.style.color = "var(--accent)";

  const downloadLink = h("a", { class: "btn btn--small", href: src, download: downloadName }, "Download");

  const root = h("div", { class: "player" },
    playBtn,
    h("div", { class: "player__scrub" }, scrub, h("div", { class: "player__times" }, cur, total)),
    h("div", { class: "player__meta" }, indicator, ...meta),
    h("div", { class: "player__actions" }, downloadLink, ...actions),
  );

  const duration = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : durationHint || 0);
  const paint = () => {
    const d = duration();
    const frac = d ? Math.min(1, audio.currentTime / d) : 0;
    scrub.value = String(Math.round(frac * 1000));
    scrub.style.setProperty("--fill", `${frac * 100}%`);
    cur.textContent = fmtTime(audio.currentTime);
    total.textContent = fmtTime(d);
  };
  const setPlaying = (on) => {
    playBtn.replaceChildren(on ? icons.pause() : icons.play());
    playBtn.setAttribute("aria-label", on ? "Pause" : "Play");
    indicator.dataset.active = String(on);
  };

  audio.addEventListener("timeupdate", paint);
  audio.addEventListener("loadedmetadata", paint);
  audio.addEventListener("durationchange", paint);
  audio.addEventListener("play", () => { claim(entry); setPlaying(true); });
  audio.addEventListener("pause", () => setPlaying(false));
  audio.addEventListener("ended", () => { audio.currentTime = 0; setPlaying(false); paint(); });
  playBtn.addEventListener("click", () => { if (audio.paused) audio.play().catch(() => {}); else audio.pause(); });
  scrub.addEventListener("input", () => {
    const d = duration();
    if (d) audio.currentTime = (Number(scrub.value) / 1000) * d;
    paint();
  });

  if (autoplay) audio.play().catch(() => { /* the browser wants a tap first; the Play button is right there */ });
  paint();

  return {
    el: root,
    audio,
    play: () => audio.play().catch(() => {}),
    destroy() { audio.pause(); audio.removeAttribute("src"); audio.load(); if (active === entry) active = null; },
  };
}
