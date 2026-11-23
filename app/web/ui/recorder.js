// Live microphone recording for "record your own voice": MediaRecorder, a level meter, and a
// browser-side conversion to mono 16-bit WAV (the server also converts, as a fallback).

export function micSupport() {
  if (!window.isSecureContext) return { ok: false, reason: "Recording needs a secure page (https). Open Voxxy over https and try again." };
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    return { ok: false, reason: "This browser cannot record audio. Use a current Chrome, Edge, Firefox or Safari." };
  }
  return { ok: true };
}

function pickMime() {
  const wanted = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return wanted.find((m) => MediaRecorder.isTypeSupported?.(m)) || "";
}

export function micErrorText(err) {
  switch (err?.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "Voxxy can't use the microphone. Allow microphone access for this site in the address bar, then press Start recording again.";
    case "NotFoundError":
    case "OverconstrainedError":
      return "No microphone found. Plug one in or pick one in your system sound settings, then try again.";
    case "NotReadableError":
    case "AbortError":
      return "The microphone is busy in another app. Close that app and try again.";
    default:
      return err?.message || "The microphone did not start. Try again.";
  }
}

// Start recording. Resolves once the microphone is open.
//   onLevel(0..1)   about 30 times a second
//   onTick(seconds) about 10 times a second, measured from the recorder's own start event
//   onLimit()       called when maxSeconds is reached (the recording is stopped for you)
export async function startRecording({ onLevel, onTick, onLimit, maxSeconds }) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  const mimeType = pickMime();
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks = [];
  recorder.addEventListener("dataavailable", (e) => { if (e.data?.size) chunks.push(e.data); });

  const Ctx = window.AudioContext || window.webkitAudioContext;
  let ctx = null;
  let raf = 0;
  if (Ctx) {
    ctx = new Ctx();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    let lastPaint = 0;
    const loop = (now) => {
      raf = requestAnimationFrame(loop);
      if (now - lastPaint < 33) return;
      lastPaint = now;
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      onLevel?.(Math.min(1, Math.sqrt(sum / buf.length) * 4));
    };
    raf = requestAnimationFrame(loop);
  }

  let startedAt = 0;
  let seconds = 0;
  let ticker = 0;
  let stopped = false;

  const done = new Promise((resolve) => {
    recorder.addEventListener("stop", () => {
      resolve({ blob: new Blob(chunks, { type: recorder.mimeType || mimeType || "audio/webm" }), seconds });
    });
  });

  const release = () => {
    clearInterval(ticker);
    cancelAnimationFrame(raf);
    stream.getTracks().forEach((t) => t.stop());
    ctx?.close().catch(() => {});
    onLevel?.(0);
  };

  const stop = () => {
    if (stopped) return done;
    stopped = true;
    seconds = (performance.now() - startedAt) / 1000;
    if (recorder.state !== "inactive") recorder.stop();
    release();
    return done;
  };

  recorder.addEventListener("start", () => {
    startedAt = performance.now();
    ticker = setInterval(() => {
      const s = (performance.now() - startedAt) / 1000;
      onTick?.(s);
      if (maxSeconds && s >= maxSeconds && !stopped) { onLimit?.(); }
    }, 100);
  });
  recorder.start(250);

  return {
    stop,
    cancel() {
      if (stopped) return;
      stopped = true;
      if (recorder.state !== "inactive") recorder.stop();
      release();
    },
  };
}

// Decode whatever the browser recorded and re-encode it as mono 16-bit PCM WAV.
// Returns { blob, seconds } or null when this browser cannot decode its own recording.
export async function toWav(blob) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  const ctx = new Ctx();
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const n = decoded.length;
    const mono = new Float32Array(n);
    for (let c = 0; c < decoded.numberOfChannels; c++) {
      const ch = decoded.getChannelData(c);
      for (let i = 0; i < n; i++) mono[i] += ch[i] / decoded.numberOfChannels;
    }
    return { blob: encodeWav(mono, decoded.sampleRate), seconds: decoded.duration };
  } catch {
    return null;
  } finally {
    ctx.close().catch(() => {});
  }
}

function encodeWav(samples, sampleRate) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  str(8, "WAVE");
  str(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  str(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let o = 44;
  for (let i = 0; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([bytes], { type: "audio/wav" });
}
