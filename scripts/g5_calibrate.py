#!/usr/bin/env python3
"""G5 calibration spike: speaker-match thresholds, ASR choice and the phrase-match rule.

Nothing here is a voxxy runtime dependency. Run each step in an isolated env:

  E="uv run --no-project --isolated --with sherpa-onnx --with faster-whisper --with soundfile --with numpy python -I"
  $E scripts/g5_calibrate.py build                 # cut consent/reference windows (wax.db, ~/d/Voices)
  $E scripts/g5_calibrate.py embed --model large   # TitaNet-large, sherpa-onnx, CPU
  $E scripts/g5_calibrate.py embed --model small   # TitaNet-small
  $E scripts/g5_calibrate.py score                 # distributions, thresholds, EER per model
  $E scripts/g5_calibrate.py latency               # per-embedding CPU latency, 1 and 4 threads
  W="uv run --no-project --isolated --with faster-whisper --with soundfile --with numpy python -I"
  $W scripts/g5_calibrate.py asr-bench             # tiny.en / base.en / small.en: latency, RAM
  $W scripts/g5_calibrate.py phrase                # the phrase rule over synthesized readings
  $E scripts/g5_calibrate.py selftest              # unit checks of the phrase rule

Ground truth comes in three tiers, kept apart in the report:

  call  A channel-separated phone call. Channel A is the owner by construction; channel B is the
        contact the caller ID names. Independent of any embedding: the honest tier.
  room  A diarized desk or phone-mic recording whose speaker Wax labelled (owner or matched).
        Wax labelled it WITH TitaNet-large at 0.60, so its same-person scores lean high.
  clip  ~/d/Voices: one short clip per registered voice; names and merges set by a person. A merged
        clip was first registered as a NEW voice (Wax scored it low), so same-person leans low.

Consent windows are also pushed through an Opus 32 kb/s round trip, because a browser consent
recording arrives via MediaRecorder (webm/opus).

Everything is written under --out (default ~/.local/state/voxxy/g5). The windows are cuts of
private recordings: delete <out>/windows when the spike is done.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

SR = 16000
HOME = Path.home()
OUT = Path(os.environ.get("G5_OUT", HOME / ".local/state/voxxy/g5"))
WAX_DB = HOME / "HeyMa/var/wax.db"
VOICES_DIR = HOME / "d/Voices"
OWNER = "voice-ac3355"  # the Wax owner voice; channel A of every call
MODELS = {
    "large": HOME / ".cache/phonograph/nemo_en_titanet_large.onnx",
    "small": HOME / "audio/models/nemo_en_titanet_small.onnx",
}
MODEL_URLS = {
    "large": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx",
    "small": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx",
}

TRIM_S = 0.15        # turn edges carry the previous speaker's tail (as Wax)
MIN_SEG_S = 1.0      # shorter turns are mostly backchannel
CONSENT_S = (6.5, 12.0)     # voiced seconds: an 8-15 s read of the sentence is ~80% speech
REFERENCE_S = (9.0, 28.0)   # voiced seconds of a 10-30 s reference clip

# ----------------------------------------------------------------------------- audio


def decode(path: str | Path, channels: int) -> np.ndarray:
    """(channels, samples) float32 at 16 kHz, via ffmpeg."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ac", str(channels), "-ar", str(SR),
         "-f", "f32le", "-"], check=True, capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    return x.reshape(-1, channels).T.copy()


def opus_roundtrip(x: np.ndarray, bitrate: str = "32k") -> np.ndarray:
    enc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-c:a", "libopus", "-b:a", bitrate, "-f", "ogg", "-"],
        input=x.astype(np.float32).tobytes(), check=True, capture_output=True).stdout
    dec = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", "-", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        input=enc, check=True, capture_output=True).stdout
    return np.frombuffer(dec, dtype=np.float32).copy()


def write_wav(path: Path, x: np.ndarray) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(x, -1, 1), SR, subtype="PCM_16")


def read_wav(path: Path) -> np.ndarray:
    import soundfile as sf

    x, sr = sf.read(str(path), dtype="float32")
    assert sr == SR, (path, sr)
    return x if x.ndim == 1 else x.mean(axis=1)


def voiced(x: np.ndarray) -> np.ndarray:
    """Only the speech in x (silero VAD as bundled with faster-whisper, so no extra model file).

    Whisper's segment spans and diarized turns carry long pauses and line hiss; embedding those
    halves same-person scores (median 0.5 between two halves of one window before this step).
    The gate must do the same: VAD first, then embed only speech, then require enough of it."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    if len(x) < SR // 2:
        return np.zeros(0, np.float32)
    ts = get_speech_timestamps(x.astype(np.float32), VadOptions(
        min_silence_duration_ms=300, speech_pad_ms=100, min_speech_duration_ms=250))
    if not ts:
        return np.zeros(0, np.float32)
    return np.concatenate([x[t["start"]:t["end"]] for t in ts]).astype(np.float32)


def cut(track: np.ndarray, spans: list[tuple[float, float]], target: float) -> tuple[np.ndarray, float, list]:
    """Concatenate the voiced speech of spans (in order) until `target` seconds; (audio, seconds, spans used)."""
    pieces, used, total = [], [], 0.0
    for a, b in spans:
        if total >= target:
            break
        clip = voiced(track[int(a * SR):int(b * SR)])[: int((target - total) * SR)]
        if len(clip) < int(0.5 * SR):
            continue
        pieces.append(clip)
        used.append((a, b))
        total += len(clip) / SR
    return (np.concatenate(pieces) if pieces else np.zeros(0, np.float32)), total, used


# ----------------------------------------------------------------------------- build


def resolve(voices: dict[str, dict], vid: str) -> str:
    seen = set()
    while vid in voices and voices[vid].get("merged_into") and vid not in seen:
        seen.add(vid)
        vid = voices[vid]["merged_into"].strip("[]\"")
    return vid


def speaker_spans(segments: list[dict], *, channel: int | None, label: str | None) -> list[tuple[float, float]]:
    spans = []
    for s in segments:
        if channel is not None and s.get("speaker") != channel:
            continue
        if label is not None and s.get("speaker_label") != label:
            continue
        a, b = float(s["start"]) + TRIM_S, float(s["end"]) - TRIM_S
        if b - a >= MIN_SEG_S:
            spans.append((a, b))
    return sorted(spans)


TURN_RE = re.compile(r"^\*\*(?P<label>[^*\n()]+?) \((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\):\*\*", re.M)


def md_turn_spans(md_path: str, labels: set[str], duration: float) -> list[tuple[float, float]]:
    """Diarized room transcripts keep speakers only in the rendered turns (as Wax reads them):
    a turn runs from its timestamp to the next turn's. Second resolution, so trim harder (0.5 s)
    and keep turns of 3 s or more, to stay clear of the neighbouring speaker."""
    try:
        body = Path(md_path).read_text(errors="replace")
    except OSError:
        return []
    turns = []
    for m in TURN_RE.finditer(body):
        secs = 0
        for part in m["ts"].split(":"):
            secs = secs * 60 + int(part)
        turns.append((m["label"].strip(), float(secs)))
    spans = []
    for k, (label, start) in enumerate(turns):
        end = turns[k + 1][1] if k + 1 < len(turns) else duration
        if label in labels and end - start >= 3.0:
            spans.append((start + 0.5, min(end - 0.5, start + 30.0)))
    return spans


def cmd_build(args) -> None:
    rng = random.Random(args.seed)
    out = Path(args.out)
    (out / "windows").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{args.wax_db}?mode=ro", uri=True)
    voices = {r[0]: {"name": r[1], "status": r[2], "merged_into": r[3]}
              for r in db.execute("select voice_id, name, status, merged_into from voices")}
    rows = db.execute(
        "select s.item_id, s.label, s.rendered, s.voice_id, s.verdict, s.domain, t.asr_path, t.md_path, "
        "i.path, coalesce(i.duration_s, t.audio_duration, 0) "
        "from voice_sightings s join items i using(item_id) left join transcripts t using(item_id) "
        "where s.verdict in ('owner','matched','caller_id','caller_id_new')").fetchall()

    # what each (tier, person) may contribute, and the per-person cap
    plans: dict[tuple[str, str], list] = defaultdict(list)
    for item, label, rendered, vid, verdict, domain, asr, md, path, duration in rows:
        if not os.path.exists(path) or not vid:
            continue
        person = resolve(voices, vid)
        if voices.get(person, {}).get("status") not in ("named",):
            continue  # unnamed voices could be anyone (even the owner): no ground truth
        if domain == "call":
            if not asr or not os.path.exists(asr):
                continue
            if verdict == "owner" and label == "Me":
                plans[("call", person)].append((item, path, asr, 0, None))
            elif verdict in ("caller_id", "caller_id_new"):
                plans[("call", person)].append((item, path, asr, 1, None))
        elif domain == "room" and verdict in ("owner", "matched"):
            if label == "(whole recording)":
                if asr and os.path.exists(asr):
                    plans[("room", person)].append((item, path, asr, None, label))
            elif md:
                names = {label, rendered, voices.get(vid, {}).get("name"), voices.get(person, {}).get("name")}
                plans[("room", person)].append((item, path, (md, duration), None, {n for n in names if n}))
    cap = {"call": args.per_person_call, "room": args.per_person_room}

    manifest, asr_cache = [], {}
    for (tier, person), items in sorted(plans.items()):
        rng.shuffle(items)
        taken, seen_items = 0, set()
        for item, path, asr, channel, label in items:
            if taken >= cap[tier]:
                break
            if item in seen_items:  # a person split over several diarized labels: one pair per recording
                continue
            if isinstance(asr, tuple):
                spans = md_turn_spans(asr[0], label, float(asr[1] or 0))
            else:
                if asr not in asr_cache:
                    asr_cache.clear()
                    asr_cache[asr] = json.load(open(asr))["result"]
                result = asr_cache[asr]
                segs = result.get("segments") or []
                if tier == "call":
                    if not result.get("channel_separated"):
                        continue
                    spans = speaker_spans(segs, channel=channel, label=None)
                else:
                    spans = speaker_spans(segs, channel=None, label=None)
            speech = sum(b - a for a, b in spans)
            if speech < CONSENT_S[0] + REFERENCE_S[0] + 1:
                continue
            try:
                audio = decode(path, 2 if tier == "call" else 1)
            except subprocess.CalledProcessError:
                continue
            track = audio[channel] if tier == "call" else audio[0]
            consent_target = rng.uniform(*CONSENT_S)
            ref_target = min(rng.uniform(*REFERENCE_S), speech - consent_target - 0.5)
            # consent from the first turns, reference from the last ones: different moments
            c_audio, c_s, c_used = cut(track, spans, consent_target)
            rest = [s for s in spans if s[0] >= (c_used[-1][1] if c_used else 0)]
            r_audio, r_s, _ = cut(track, list(reversed(rest)), ref_target)
            if c_s < 6.0 or r_s < 8.5:
                continue
            base = f"{tier}-{person}-{item}"
            for role, x, secs in (("consent", c_audio, c_s), ("reference", r_audio, r_s)):
                f = out / "windows" / f"{base}-{role}.wav"
                write_wav(f, x)
                entry = {"id": f"{base}-{role}", "tier": tier, "domain": tier, "person": person,
                         "item": item, "role": role, "seconds": round(secs, 2), "file": str(f)}
                if role == "consent":
                    fo = out / "windows" / f"{base}-consent.opus.wav"
                    write_wav(fo, opus_roundtrip(x))
                    entry["opus_file"] = str(fo)
                manifest.append(entry)
            taken += 1
            seen_items.add(item)
        print(f"{tier:5s} {person}: {taken} recordings", file=sys.stderr)

    # tier clip: ~/d/Voices, human-curated names and merges; one clip per registered voice
    for md in sorted(Path(args.voices_dir).glob("voice-*.md")):
        text = md.read_text(errors="replace")
        status = re.search(r"^status:\s*(\S+)", text, re.M)
        merged = re.search(r"^merged-into:\s*\"?\[\[(voice-[0-9a-f]+)\]\]", text, re.M)
        status = status.group(1) if status else ""
        if status not in ("named", "merged"):
            continue
        vid = md.stem
        person = merged.group(1) if merged else vid
        person = resolve(voices, person)
        if voices.get(person, {}).get("status") != "named" and person != vid:
            continue
        ogg = md.with_suffix(".ogg")
        if not ogg.exists():
            continue
        x = voiced(decode(ogg, 1)[0])
        secs = len(x) / SR
        if secs < 4.0:
            continue
        f = out / "windows" / f"clip-{vid}.wav"
        write_wav(f, x)
        fo = out / "windows" / f"clip-{vid}.opus.wav"
        write_wav(fo, opus_roundtrip(x))
        manifest.append({"id": f"clip-{vid}", "tier": "clip", "domain": "clip", "person": person,
                         "item": vid, "role": "clip", "seconds": round(secs, 2), "file": str(f),
                         "opus_file": str(fo)})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    n = defaultdict(int)
    for m in manifest:
        n[(m["tier"], m["role"])] += 1
    print(json.dumps({f"{k[0]}/{k[1]}": v for k, v in sorted(n.items())}, indent=1))


# ----------------------------------------------------------------------------- embed


def extractor(model: str, threads: int):
    import sherpa_onnx

    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODELS[model]), num_threads=threads,
                                                      provider="cpu")
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


def embed_one(ex, x: np.ndarray) -> np.ndarray:
    s = ex.create_stream()
    s.accept_waveform(SR, x.astype(np.float32))
    s.input_finished()
    e = np.asarray(ex.compute(s), dtype=np.float32)
    return e / (np.linalg.norm(e) or 1.0)


CHUNK_S = 3.0
CHUNK_KEEP_COS = 0.45  # a 3 s chunk this far from its clip's centroid is another voice


def purify(ex, x: np.ndarray) -> tuple[np.ndarray, float, float]:
    """One voice only: embed 3 s chunks, drop chunks far from the centroid, re-embed the rest.

    Returns (embedding of the kept speech, consistency = median chunk-to-centroid cosine,
    fraction of chunks kept). The gate can use consistency to refuse a consent clip with two
    voices in it ("we heard more than one person")."""
    n = int(CHUNK_S * SR)
    chunks = [x[i:i + n] for i in range(0, len(x), n)]
    if len(chunks) > 1 and len(chunks[-1]) < n // 2:
        chunks[-2] = np.concatenate([chunks[-2], chunks[-1]])
        chunks = chunks[:-1]
    if len(chunks) < 2:
        v = embed_one(ex, x)
        return v, 1.0, 1.0
    es = np.stack([embed_one(ex, c) for c in chunks])
    c = es.mean(axis=0)
    c /= np.linalg.norm(c)
    sims = es @ c
    keep = sims >= CHUNK_KEEP_COS
    if keep.sum() == 0:
        keep = sims >= np.median(sims)
    v = embed_one(ex, np.concatenate([ch for ch, k in zip(chunks, keep) if k]))
    return v, float(np.median(sims)), float(keep.mean())


def cmd_embed(args) -> None:
    out = Path(args.out)
    manifest = json.loads((out / "manifest.json").read_text())
    extra = sorted((out / "extra").glob("*.wav")) if (out / "extra").exists() else []
    ex = extractor(args.model, args.threads)
    ids, vecs, times, secs, pure, consistency, kept_frac = [], [], [], [], [], [], []
    jobs = [(m["id"], m["file"]) for m in manifest]
    jobs += [(m["id"] + ":opus", m["opus_file"]) for m in manifest if m.get("opus_file")]
    jobs += [(f"extra:{p.stem}", str(p)) for p in extra]
    for k, (wid, f) in enumerate(jobs):
        x = read_wav(Path(f))
        if wid.startswith("extra:"):
            x = voiced(x)  # windows and clips were VAD-trimmed at build time
        t = time.perf_counter()
        v = embed_one(ex, x)
        times.append(time.perf_counter() - t)
        secs.append(len(x) / SR)
        ids.append(wid)
        vecs.append(v)
        pv, cons, kept = purify(ex, x)
        pure.append(pv)
        consistency.append(cons)
        kept_frac.append(kept)
        if k % 100 == 0:
            print(f"{args.model}: {k}/{len(jobs)}", file=sys.stderr)
    np.savez(out / f"emb-{args.model}.npz", ids=np.array(ids), vecs=np.stack(vecs),
             times=np.array(times), secs=np.array(secs), pure=np.stack(pure),
             consistency=np.array(consistency), kept=np.array(kept_frac))
    t, s = np.array(times), np.array(secs)
    print(json.dumps({"model": args.model, "n": len(ids), "threads": args.threads,
                      "median_ms": round(1000 * float(np.median(t)), 1),
                      "ms_per_audio_s": round(1000 * float(np.median(t / s)), 1)}))


def cmd_latency(args) -> None:
    """Per-embedding wall time on 10 s and 30 s of real speech, 1 and 4 threads, model load, size."""
    out = Path(args.out)
    manifest = json.loads((out / "manifest.json").read_text())
    refs = [m for m in manifest if m["role"] == "reference" and m["seconds"] >= 28][:1]
    x30 = read_wav(Path(refs[0]["file"]))[: 30 * SR]
    x10 = x30[: 10 * SR]
    res = {}
    for model in ("large", "small"):
        size = MODELS[model].stat().st_size
        for threads in (1, 4):
            t0 = time.perf_counter()
            ex = extractor(model, threads)
            load = time.perf_counter() - t0
            embed_one(ex, x10)  # warm
            row = {"size_mb": round(size / 1e6, 1), "load_s": round(load, 2)}
            for name, x in (("10s", x10), ("30s", x30)):
                ts = []
                for _ in range(args.repeat):
                    t = time.perf_counter()
                    embed_one(ex, x)
                    ts.append(time.perf_counter() - t)
                row[f"{name}_ms_median"] = round(1000 * float(np.median(ts)), 1)
                row[f"{name}_ms_p90"] = round(1000 * float(np.percentile(ts, 90)), 1)
            res[f"{model}/t{threads}"] = row
    print(json.dumps(res, indent=1))


# ----------------------------------------------------------------------------- score


def q(a, p):
    return round(float(np.percentile(a, p)), 3) if len(a) else None


def describe(a) -> dict:
    a = np.asarray(a)
    if not len(a):
        return {"n": 0}
    return {"n": int(len(a)), "min": q(a, 0), "p1": q(a, 1), "p5": q(a, 5), "p10": q(a, 10),
            "median": q(a, 50), "p90": q(a, 90), "p95": q(a, 95), "p99": q(a, 99), "max": q(a, 100)}


def eer(pos, neg) -> dict | None:
    """Equal error rate and the threshold it sits at."""
    pos, neg = np.asarray(pos), np.asarray(neg)
    if not len(pos) or not len(neg):
        return None
    ts = np.linspace(-0.2, 1.0, 1201)
    far = np.array([(neg >= t).mean() for t in ts])
    frr = np.array([(pos < t).mean() for t in ts])
    k = int(np.argmin(np.abs(far - frr)))
    return {"eer": round(float(far[k] + frr[k]) / 2, 4), "at": round(float(ts[k]), 3)}


def rates(pos, neg, t) -> dict:
    pos, neg = np.asarray(pos), np.asarray(neg)
    return {"threshold": t, "FAR": round(float((neg >= t).mean()), 4) if len(neg) else None,
            "FRR": round(float((pos < t).mean()), 4) if len(pos) else None}


def cmd_score(args) -> None:
    out = Path(args.out)
    manifest_all = {m["id"]: m for m in json.loads((out / "manifest.json").read_text())}
    report = {}
    for model in ("large", "small"):
        f = out / f"emb-{model}.npz"
        if not f.exists():
            continue
        z = np.load(f)
        E = dict(zip(z["ids"].tolist(), z["pure"] if args.pure else z["vecs"]))
        cons = dict(zip(z["ids"].tolist(), z["consistency"]))
        if args.min_consistency > 0:  # the gate refuses a clip whose chunks disagree (two voices)
            dropped = {k.split(":")[0] for k, c in cons.items() if c < args.min_consistency and not k.startswith("extra:")}
            manifest = {k: m for k, m in manifest_all.items() if k not in dropped}
        else:
            manifest = manifest_all
        if args.center:  # Wax-style: take the cohort's average voice out, back onto the unit sphere
            mu = np.mean([v for k, v in E.items() if not k.startswith("extra:")], axis=0)
            E = {k: (v - mu) / np.linalg.norm(v - mu) for k, v in E.items()}
        consent = [m for m in manifest.values() if m["role"] == "consent"]
        reference = [m for m in manifest.values() if m["role"] == "reference"]
        clips = [m for m in manifest.values() if m["role"] == "clip"]
        groups: dict[str, list] = defaultdict(list)
        persons_pos: dict[str, list] = defaultdict(list)

        def cvec(m):  # the consent side arrives through the browser: opus round trip
            return E[m["id"] + (":opus" if args.consent == "opus" else "")]

        for c in consent:
            for r in reference:
                s = float(cvec(c) @ E[r["id"]])
                dom = f"{c['domain']}>{r['domain']}"
                if c["person"] == r["person"]:
                    if c["item"] == r["item"]:
                        groups[f"same/session/{dom}"].append(s)
                    else:
                        groups[f"same/cross-rec/{dom}"].append(s)
                        persons_pos[c["person"]].append(s)
                elif c["item"] == r["item"]:  # two people in one recording: diarization slips land here
                    groups[f"diff-same-rec/{dom}"].append(s)
                else:
                    groups[f"diff/{dom}"].append(s)
        for i, a in enumerate(clips):
            for b in clips[i + 1:]:
                s = float(E[a["id"] + ":opus"] @ E[b["id"]])
                groups[("same" if a["person"] == b["person"] else "diff") + "/clip>clip"].append(s)
        # clip (human-curated) against call/room references of the same named person
        for a in clips:
            for r in reference:
                s = float(E[a["id"] + ":opus"] @ E[r["id"]])
                groups[("same" if a["person"] == r["person"] else "diff") + f"/clip>{r['domain']}"].append(s)

        # step 5: account voiceprint = centroid of the person's earlier references (other recordings)
        by_person: dict[str, list] = defaultdict(list)
        for r in reference:
            by_person[r["person"]].append(r)
        for c in consent:
            for p, rs in by_person.items():
                others = [r for r in rs if r["item"] != c["item"]][:5]
                if len(others) < 2:
                    continue
                v = np.mean([E[r["id"]] for r in others], axis=0)
                v = v / np.linalg.norm(v)
                s = float(cvec(c) @ v)
                groups[("same" if p == c["person"] else "diff") + "/voiceprint"].append(s)

        # raw-vs-opus delta on the consent side
        delta = [float(E[c["id"] + ":opus"] @ E[r["id"]] - E[c["id"]] @ E[r["id"]])
                 for c in consent for r in reference if c["person"] == r["person"] and c["item"] != r["item"]]

        res = {"groups": {k: describe(v) for k, v in sorted(groups.items())},
               "opus_delta_same_person": describe(delta),
               "per_person_cross_rec_median": {p: {"n": len(v), "median": q(v, 50), "p10": q(v, 10)}
                                               for p, v in sorted(persons_pos.items())}}
        # thresholds on the pools that matter for the product
        pos_all = groups["same/cross-rec/room>room"] + groups["same/cross-rec/call>call"] + \
            groups["same/cross-rec/room>call"] + groups["same/cross-rec/call>room"] + groups["same/clip>clip"]
        neg_all = groups["diff/room>room"] + groups["diff/call>call"] + groups["diff/room>call"] + \
            groups["diff/call>room"] + groups["diff/clip>clip"]
        res["eer"] = {
            "all": eer(pos_all, neg_all),
            "call>call (independent truth)": eer(groups["same/cross-rec/call>call"], groups["diff/call>call"]),
            "room>room": eer(groups["same/cross-rec/room>room"], groups["diff/room>room"]),
            "cross-domain": eer(groups["same/cross-rec/room>call"] + groups["same/cross-rec/call>room"],
                                groups["diff/room>call"] + groups["diff/call>room"]),
            "clip>clip (human truth)": eer(groups["same/clip>clip"], groups["diff/clip>clip"]),
            "voiceprint": eer(groups["same/voiceprint"], groups["diff/voiceprint"]),
        }
        sweep = {}
        for t in np.round(np.arange(0.0, 0.91, 0.05), 2):
            t = float(t)
            sweep[t] = {
                "all": rates(pos_all, neg_all, t),
                "same-session FRR": rates(groups["same/session/room>room"] + groups["same/session/call>call"], [], t)["FRR"],
                "room FAR/FRR": rates(groups["same/cross-rec/room>room"], groups["diff/room>room"], t),
                "call FAR/FRR": rates(groups["same/cross-rec/call>call"], groups["diff/call>call"], t),
                "cross FAR/FRR": rates(groups["same/cross-rec/room>call"] + groups["same/cross-rec/call>room"],
                                       groups["diff/room>call"] + groups["diff/call>room"], t),
                "clip FAR/FRR": rates(groups["same/clip>clip"], groups["diff/clip>clip"], t),
                "voiceprint FAR/FRR": rates(groups["same/voiceprint"], groups["diff/voiceprint"], t),
            }
        res["sweep"] = sweep
        # extra clips (synthetic clones) against the real person's references
        extras = [k for k in E if k.startswith("extra:")]
        if extras:
            ext = {}
            for k in extras:
                target = k.split(":", 1)[1].split("__")[0]  # extra/<person>__<what>.wav
                sims = [float(E[k] @ E[r["id"]]) for r in reference + clips if r["person"] == target]
                ext[k] = describe(sims) if sims else None
            res["extra_vs_real_person"] = ext
        res["consistency_by_tier"] = {
            t: describe([cons[m["id"]] for m in manifest_all.values() if m["tier"] == t])
            for t in ("call", "room", "clip")}
        res["consistency_by_tier"]["tts"] = describe([c for k, c in cons.items() if k.startswith("extra:")])
        res["windows_used"] = len(manifest)
        report[model] = res
    tag = args.consent + ("-centered" if args.center else "") + ("-pure" if args.pure else "") + \
        (f"-cons{args.min_consistency}" if args.min_consistency else "")
    (out / f"score-{tag}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


# ----------------------------------------------------------------------------- phrase rule

UNITS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90}
# ASR's usual homophones for digits, honoured only next to another digit
DIGIT_HOMOPHONES = {"for": 4, "fore": 4, "to": 2, "too": 2, "ate": 8, "won": 1, "tree": 3, "sex": 6}
SPELLING = {"harbour": "harbor", "colour": "color", "neighbour": "neighbor", "grey": "gray", "ok": "okay"}


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = re.sub(r"(\d),(\d)", r"\1\2", text)          # 482,917 -> 482917
    text = re.sub(r"[-‐–—/]", " ", text)
    text = re.sub(r"'", "", text)
    text = re.sub(r"(\d)([a-z])|([a-z])(\d)", lambda m: " ".join(g for g in m.groups() if g), text)
    return [SPELLING.get(t, t) for t in re.findall(r"[a-z]+|\d+", text)]


def _is_num_word(t: str) -> bool:
    return t in UNITS or t in TEENS or t in TENS or t in ("hundred", "double", "triple")


def normalise(text: str) -> list[str]:
    """Lowercase word tokens with spoken numbers folded to digit strings.

    "four eighty-two, nine seventeen" -> ["482", "917"] is NOT done here; numbers become digit
    tokens ("4", "82", "9", "17") and the code check reads them as one digit stream.
    """
    toks = _tokens(text)
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev_digit = bool(out) and out[-1].isdigit()
        next_digit = nxt.isdigit() or (nxt in UNITS and nxt not in ("o", "oh")) or nxt in TEENS or nxt in TENS
        if t in ("double", "triple") and nxt in UNITS:
            out.append(str(UNITS[nxt]) * (2 if t == "double" else 3))
            i += 2
            continue
        if t in ("o", "oh") and not (prev_digit or next_digit):
            out.append(t)
            i += 1
            continue
        if t in DIGIT_HOMOPHONES and (prev_digit and next_digit):
            out.append(str(DIGIT_HOMOPHONES[t]))
            i += 1
            continue
        if t in UNITS and nxt == "hundred":
            value = UNITS[t] * 100
            i += 2
            if i < len(toks) and toks[i] == "and":
                i += 1
            if i < len(toks) and toks[i] in TEENS:
                value += TEENS[toks[i]]
                i += 1
            elif i < len(toks) and toks[i] in TENS:
                value += TENS[toks[i]]
                i += 1
                if i < len(toks) and toks[i] in UNITS and toks[i] not in ("o", "oh", "zero"):
                    value += UNITS[toks[i]]
                    i += 1
            elif i < len(toks) and toks[i] in UNITS and toks[i] not in ("o", "oh"):
                value += UNITS[toks[i]]
                i += 1
            out.append(f"{value:03d}")
            continue
        if t in TENS:
            if nxt in UNITS and nxt not in ("o", "oh", "zero"):
                out.append(str(TENS[t] + UNITS[nxt]))
                i += 2
            else:
                out.append(str(TENS[t]))
                i += 1
            continue
        if t in TEENS:
            out.append(str(TEENS[t]))
        elif t in UNITS:
            out.append(str(UNITS[t]))
        else:
            out.append(t)
        i += 1
    return out


def _tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    return len(a) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.8


def sentence_score(sentence: str, transcript: str) -> float:
    """1 - WER of the sentence against its best-aligned span of the transcript.

    Semi-global word alignment: the transcript may carry extra words (the code, "um", a restart)
    before and after at no cost; inside the span every substitution, insertion and deletion costs
    one. A near-identical spelling (character ratio >= 0.8, 4+ letters) counts as a match.
    """
    s, t = normalise(sentence), normalise(transcript)
    n, m = len(s), len(t)
    if not n:
        return 0.0
    prev = [0] * (m + 1)  # free start anywhere in the transcript
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if _tok_eq(s[i - 1], t[j - 1]) else 1))
        prev = cur
    return max(0.0, 1.0 - min(prev) / n)  # free end anywhere


def digit_stream(transcript: str) -> str:
    """Every digit the transcript speaks, in order, as one string ("for" counts only between digits)."""
    return "".join(t for t in normalise(transcript) if t.isdigit())


def code_distance(code: str, transcript: str) -> int:
    """Edit distance from the code to its best-matching substring of the transcript's digit stream."""
    d = digit_stream(transcript)
    n, m = len(code), len(d)
    prev = [0] * (m + 1)
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (code[i - 1] != d[j - 1]))
        prev = cur
    return min(prev)


SENTENCE_PASS = 0.80
CODE_MAX_EDITS = 1


def phrase_check(sentence: str, code: str, transcript: str) -> dict:
    ss = sentence_score(sentence, transcript)
    cd = code_distance(code, transcript)
    return {"sentence_score": round(ss, 3), "code_edits": cd,
            "ok": ss >= SENTENCE_PASS and cd <= CODE_MAX_EDITS}


def cmd_selftest(args) -> None:
    S = "Please call me back when the weather clears, and bring the blue folder from the kitchen table."
    cases = [
        (S + " My code is 4 8 2 9 1 7.", "482917", True),
        (S + " 482 917", "482917", True),
        (S + " Four eighty-two, nine seventeen.", "482917", True),
        (S + " four hundred eighty two nine hundred seventeen", "482917", True),
        (S + " four eight two nine one seven", "482917", True),
        (S + " for eight to nine one seven", "482917", True),
        (S + " 4 8 2 9 1 7", "482917", True),
        ("Please call me back when the weather clears and bring the blue folder from the kitchen table, code oh four double five six one", "045561", True),
        (S.replace("blue", "glue") + " 482917", "482917", True),       # one ASR slip
        (S + " 482 918", "482917", True),                                 # one digit wrong
        (S + " 481 918", "482917", False),                                # two digits wrong
        (S, "482917", False),                                             # no code
        ("Four eight two nine one seven.", "482917", False),              # code only
        ("The quick brown fox jumped over the lazy dog while the morning fog lifted off the harbor. 482917",
         "482917", False),                                                # another sentence
        ("Please call me back when the weather clears. 482917", "482917", False),  # half the sentence
    ]
    bad = 0
    for text, code, want in cases:
        got = phrase_check(S, code, text)
        flag = "ok " if got["ok"] == want else "BAD"
        bad += got["ok"] != want
        print(flag, got, "|", text[-60:])
    print("selftest", "FAILED" if bad else "passed", f"({bad} wrong)")
    sys.exit(1 if bad else 0)


# ----------------------------------------------------------------------------- ASR


def _rss_mb() -> float:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def cmd_asr_one(args) -> None:
    """One model in its own process, so peak RSS is that model's alone."""
    from faster_whisper import WhisperModel

    base = _rss_mb()
    t0 = time.perf_counter()
    model = WhisperModel(args.size, device="cpu", compute_type="int8", cpu_threads=args.threads)
    load = time.perf_counter() - t0
    clips = [Path(p) for p in args.clips]
    rows = []
    for k, p in enumerate(clips):
        x = read_wav(p)
        runs = []
        text = ""
        for r in range(args.repeat if k == 0 else 1):
            t = time.perf_counter()
            segs, _ = model.transcribe(x, **{**decode_options(args.mode), "beam_size": args.beam})
            text = " ".join(s.text.strip() for s in segs)
            runs.append(time.perf_counter() - t)
        rows.append({"clip": p.name, "seconds": round(len(x) / SR, 2), "text": text,
                     "latency_s": [round(v, 3) for v in runs]})
    print(json.dumps({"size": args.size, "threads": args.threads, "beam": args.beam, "load_s": round(load, 2),
                      "rss_mb_before_model": round(base, 1), "peak_rss_mb": round(_rss_mb(), 1), "rows": rows}))


def cmd_asr_bench(args) -> None:
    out = Path(args.out)
    manifest = json.loads((out / "manifest.json").read_text())
    real = [m for m in manifest if m["role"] == "consent" and m["tier"] == "room" and 10 <= m["seconds"] <= 15][:1]
    synth = sorted((out / "tts").glob("*.wav"))[:2]
    clips = [real[0]["opus_file"]] + [str(p) for p in synth]
    results = []
    for size in args.sizes:
        for threads in args.threads_list:
            cmd = [sys.executable, "-I", __file__, "asr-one", "--size", size, "--threads", str(threads), "--mode", args.mode,
                   "--beam", str(args.beam), "--repeat", str(args.repeat), "--clips", *clips]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                print(r.stderr[-2000:], file=sys.stderr)
                continue
            row = json.loads(r.stdout.strip().splitlines()[-1])
            results.append(row)
            lat = row["rows"][0]["latency_s"]
            print(f"{size:9s} t{threads} beam{args.beam} load {row['load_s']}s peak {row['peak_rss_mb']} MB "
                  f"real {row['rows'][0]['seconds']}s clip: first {lat[0]}s median {np.median(lat[1:] or lat):.3f}s",
                  file=sys.stderr)
    (out / f"asr-bench-{args.mode}-beam{args.beam}.json").write_text(json.dumps(results, indent=1))
    print(json.dumps(results, indent=1))


def decode_options(mode: str) -> dict:
    """faster-whisper decode settings under test. NEVER pass the expected sentence or code as
    initial_prompt/hotwords: that biases the transcript toward what the check wants to hear."""
    base = {"language": "en", "beam_size": 5, "condition_on_previous_text": False}
    if mode == "notimestamps":  # one 30 s window, one segment: drops a short trailing utterance
        return {**base, "without_timestamps": True}
    if mode == "timestamps":    # timestamp tokens let the decoder seek past the sentence's full stop
        return {**base, "without_timestamps": False}
    if mode == "vad":           # silero VAD chunks, timestamps on
        return {**base, "without_timestamps": False, "vad_filter": True}
    if mode == "digits-prompt":  # a generic hint, never the challenge itself
        return {**base, "without_timestamps": False, "initial_prompt": "Then a code, read as digits: 1 2 3, 4 5 6."}
    raise ValueError(mode)


def cmd_phrase(args) -> None:
    """Transcribe every synthesized reading with each model and apply the rule, plus negatives."""
    from faster_whisper import WhisperModel

    out = Path(args.out)
    cases = json.loads((out / "tts" / "cases.json").read_text())
    noise_rng = np.random.default_rng(7)
    results = {}
    for size in args.sizes:
        model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=args.threads)
        rows = []
        for c in cases:
            x = read_wav(out / "tts" / c["file"])
            variants = {"clean": x, "opus": opus_roundtrip(x)}
            # room noise at 10 dB SNR (brown-ish), on top of the opus round trip
            n = np.cumsum(noise_rng.standard_normal(len(x))).astype(np.float32)
            n -= np.convolve(n, np.ones(400) / 400, mode="same")
            n *= np.sqrt(np.mean(x ** 2) / (np.mean(n ** 2) * 10 ** (10 / 10)))
            variants["opus+noise10dB"] = opus_roundtrip(x + n)
            for vname, xv in variants.items():
                segs, _ = model.transcribe(xv, **decode_options(args.mode))
                text = " ".join(s.text.strip() for s in segs)
                pos = phrase_check(c["sentence"], c["code"], text)
                # negatives on the same audio: another sentence's check, another code
                other_s = next(o["sentence"] for o in cases if o["sentence"] != c["sentence"])
                other_c = f"{(int(c['code']) + 371_529) % 10**6:06d}"
                rows.append({"file": c["file"], "voice": c["voice"], "style": c["style"], "variant": vname,
                             "text": text, "pos": pos,
                             "neg_sentence": phrase_check(other_s, c["code"], text),
                             "neg_code": phrase_check(c["sentence"], other_c, text)})
        results[size] = rows
        ok = sum(r["pos"]["ok"] for r in rows)
        fa = sum(r["neg_sentence"]["ok"] for r in rows) + sum(r["neg_code"]["ok"] for r in rows)
        print(f"{args.mode} {size}: true-accept {ok}/{len(rows)}, false-accept {fa}/{2 * len(rows)}", file=sys.stderr)
    (out / f"phrase-{args.mode}.json").write_text(json.dumps(results, indent=1))


# ----------------------------------------------------------------------------- synth

SENTENCES = (  # app/consent.py SENTENCES, copied so this script never imports the app
    "The quick brown fox jumped over the lazy dog while the morning fog lifted off the harbor.",
    "I am recording this voice myself, and I am happy for it to be used to speak my own words.",
    "Please call me back when the weather clears, and bring the blue folder from the kitchen table.",
    "Every evening the lighthouse keeper climbs the stairs, lights the lamp and watches the water.",
    "My voice is my own, and I am choosing to make a copy of it today, in my own words.",
    "A small green bicycle leaned against the old stone wall beside the market on Saturday.",
    "We planted twelve tomato seeds in the garden, and by July the vines were taller than the fence.",
    "Today I am reading these words aloud so that this voice can be made from my recording.",
)
DIGIT_WORDS = "zero one two three four five six seven eight nine".split()
TENS_WORDS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
TEEN_WORDS = "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()


def _say_pair(d2: str) -> str:
    a, b = int(d2[0]), int(d2[1])
    if a == 0:
        return f"oh {DIGIT_WORDS[b]}"
    if a == 1:
        return TEEN_WORDS[b]
    return TENS_WORDS[a] + ("" if b == 0 else f"-{DIGIT_WORDS[b]}")


def spoken_code(code: str, style: str) -> str:
    """How a person might read 482 917 aloud."""
    if style == "digits":
        return f"{' '.join(DIGIT_WORDS[int(c)] for c in code[:3])}, {' '.join(DIGIT_WORDS[int(c)] for c in code[3:])}"
    if style == "grouped":  # "four eighty-two, nine seventeen"
        return f"{DIGIT_WORDS[int(code[0])]} {_say_pair(code[1:3])}, {DIGIT_WORDS[int(code[3])]} {_say_pair(code[4:])}"
    return f"{code[:3]} {code[3:]}"  # raw: let the reader decide


def _synth(text: str, voice: str, key: str) -> np.ndarray:
    import urllib.request

    req = urllib.request.Request(
        os.environ.get("VOX_URL", "https://vox.delo.sh") + "/synthesize",
        data=json.dumps({"text": text, "voice": voice}).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        wav = resp.read()
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", "-", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         input=wav, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def cmd_synth(args) -> None:
    """Readings of the challenge for the ASR test (house voices), and clone-vs-person clips.

    Synthesized audio is not a person: it only tests the transcript rule, and (extra/) how a
    voice clone of a real person scores against that person's real recordings."""
    key = os.environ.get("VOX_API_KEY", "")
    if not key:
        sys.exit("set VOX_API_KEY")
    rng = random.Random(11)
    out = Path(args.out)
    (out / "tts").mkdir(parents=True, exist_ok=True)
    (out / "extra").mkdir(parents=True, exist_ok=True)
    cases = []
    styles = ("digits", "grouped", "raw")
    k = 0
    for voice in args.voices:
        for sentence in rng.sample(SENTENCES, args.per_voice):
            code = f"{rng.randrange(10**6):06d}"
            style = styles[k % 3]
            lead = rng.choice(("My code is ", "The code is ", ""))
            text = f"{sentence} {lead}{spoken_code(code, style)}."
            f = f"{voice}-{k:02d}-{style}.wav"
            write_wav(out / "tts" / f, _synth(text, voice, key))
            cases.append({"file": f, "voice": voice, "style": style, "sentence": sentence, "code": code,
                          "spoken": text})
            print(f, file=sys.stderr)
            k += 1
    (out / "tts" / "cases.json").write_text(json.dumps(cases, indent=1))
    # voice clones on the live server against the real person's recordings (Wax voice id)
    for pair in args.clones:
        voice, person = pair.split("=")
        for j, sentence in enumerate(rng.sample(SENTENCES, 2)):
            write_wav(out / "extra" / f"{person}__{voice}-{j}.wav", _synth(sentence, voice, key))
            print(f"extra {person} <- {voice} {j}", file=sys.stderr)


# ----------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--wax-db", default=str(WAX_DB))
    b.add_argument("--voices-dir", default=str(VOICES_DIR))
    b.add_argument("--per-person-call", type=int, default=25)
    b.add_argument("--per-person-room", type=int, default=15)
    b.add_argument("--seed", type=int, default=5)
    e = sub.add_parser("embed")
    e.add_argument("--model", choices=sorted(MODELS), required=True)
    e.add_argument("--threads", type=int, default=1)
    s = sub.add_parser("score")
    s.add_argument("--consent", choices=("opus", "raw"), default="opus")
    s.add_argument("--center", action="store_true", help="subtract the cohort mean embedding first")
    s.add_argument("--pure", action="store_true", help="use the one-voice (outlier-chunk-dropped) embeddings")
    s.add_argument("--min-consistency", type=float, default=0.0, help="drop windows below this self-consistency")
    lt = sub.add_parser("latency")
    lt.add_argument("--repeat", type=int, default=7)
    a1 = sub.add_parser("asr-one")
    a1.add_argument("--size", required=True)
    a1.add_argument("--threads", type=int, default=4)
    a1.add_argument("--beam", type=int, default=5)
    a1.add_argument("--repeat", type=int, default=4)
    a1.add_argument("--clips", nargs="+", required=True)
    a1.add_argument("--mode", default="timestamps")
    ab = sub.add_parser("asr-bench")
    ab.add_argument("--sizes", nargs="+", default=["tiny.en", "base.en", "small.en"])
    ab.add_argument("--threads-list", type=int, nargs="+", default=[1, 4])
    ab.add_argument("--beam", type=int, default=5)
    ab.add_argument("--mode", default="timestamps")
    ab.add_argument("--repeat", type=int, default=4)
    ph = sub.add_parser("phrase")
    ph.add_argument("--sizes", nargs="+", default=["tiny.en", "base.en", "small.en"])
    ph.add_argument("--threads", type=int, default=4)
    ph.add_argument("--mode", choices=("notimestamps", "timestamps", "vad", "digits-prompt"), default="timestamps")
    sub.add_parser("selftest")
    sy = sub.add_parser("synth")
    sy.add_argument("--voices", nargs="+", default=["house-f1", "house-f2", "house-m1", "house-m2", "kurzgesagt"])
    sy.add_argument("--per-voice", type=int, default=5)
    sy.add_argument("--clones", nargs="*", default=[], help="live voice=Wax voice id, e.g. damian=voice-849669")
    args = ap.parse_args()
    {"build": cmd_build, "embed": cmd_embed, "score": cmd_score, "latency": cmd_latency,
     "asr-one": cmd_asr_one, "asr-bench": cmd_asr_bench, "phrase": cmd_phrase,
     "selftest": cmd_selftest, "synth": cmd_synth}[args.cmd](args)


if __name__ == "__main__":
    main()
