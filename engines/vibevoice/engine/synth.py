"""VibeVoice-1.5B synthesis wrapper with memory-containment patches.

The VibeVoice model is loaded once at startup and reused for every request.
Memory-containment workarounds (gc.collect + torch.cuda.empty_cache after
every generate, reference-audio trim) mirror the voxcpm engine pattern.

Source of truth for the generate() call:
    https://raw.githubusercontent.com/vibevoice-community/VibeVoice/main/demo/inference_from_file.py

Confirmed processor __call__ signature (vibevoice_processor.py):
    processor(
        text=[full_script],          # List[str] — one entry per batch item
        voice_samples=[voice_paths], # List[List[str|np.ndarray]] — nested batch
        padding=True,
        return_tensors="pt",
        return_attention_mask=True,
    )

    Key kwarg: `voice_samples` (NOT reference_audio, NOT speaker_audio).
    Accepts file paths (strings) or np.ndarray per sample.

Confirmed model.generate() call:
    outputs = model.generate(
        **inputs,
        max_new_tokens=None,
        cfg_scale=cfg_scale,
        tokenizer=processor.tokenizer,
        generation_config={"do_sample": False},
        verbose=False,
        is_prefill=True,   # True = use reference audio for speaker conditioning
    )

Output: outputs.speech_outputs[0] is a float32 tensor (audio-domain, 24 kHz).
Conversion: tensor.float().detach().cpu().numpy() → float32 ndarray in [-1, 1].
We then convert to int16 (PCM_16) to match the contract.
"""

from __future__ import annotations

import gc
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch

logger = logging.getLogger(__name__)

# VibeVoice is more sensitive to long reference clips than VoxCPM. Community
# testing shows 2-10 s is optimal for timbre extraction; the model does not
# benefit from longer clips and may produce artefacts. We keep the same 30 s
# hard cap as voxcpm for safety, but log a warning when the clip exceeds 10 s
# so operators can tune VOX_REF_AUDIO_MAX_SECONDS if needed.
REF_AUDIO_MAX_SECONDS = float(os.environ.get("VOX_REF_AUDIO_MAX_SECONDS", "30"))
REF_AUDIO_WARN_SECONDS = 10.0

# VibeVoice expects 24 kHz mono reference audio. librosa.load resamples on
# the fly, so incoming clips at any sample rate are handled transparently.
VIBEVOICE_SAMPLE_RATE = 24000

# Model identifier. Override via VOX_VIBEVOICE_MODEL_ID env if you're hosting
# the weights locally or using a fine-tuned checkpoint.
DEFAULT_MODEL_ID = os.environ.get("VOX_VIBEVOICE_MODEL_ID", "microsoft/VibeVoice-1.5B")


_SPEAKER_PREFIX_RE = re.compile(r"^\s*Speaker\s*\d+\s*:", re.IGNORECASE | re.MULTILINE)


def _ensure_speaker_labeled(text: str) -> str:
    """Ensure the text carries a ``Speaker N:`` prefix on every non-empty line.

    VibeVoice is multi-speaker by design and its processor validates that each
    script line starts with a speaker label; plain text rejects with
    ``INVALID_INPUT: No valid speaker lines found in script``. Callers of our
    public API pass plain text (single voice is the common case), so we
    normalize here rather than leaking that detail to every consumer.

    If the text already has at least one ``Speaker N:`` prefix (anywhere), we
    assume the caller knows what they're doing and pass it through untouched.
    Otherwise we prepend ``Speaker 1:`` to each non-empty line.
    """
    if _SPEAKER_PREFIX_RE.search(text):
        return text
    lines = text.splitlines() or [text]
    labeled = [
        f"Speaker 1: {ln.strip()}" if ln.strip() else ln
        for ln in lines
    ]
    return "\n".join(labeled)


class VibeVoiceSynth:
    """Singleton wrapper around microsoft/VibeVoice-1.5B.

    Use :meth:`load` once at startup, then call :meth:`generate` per request.
    All heavy imports are deferred to :meth:`load` so import-time cost is cheap.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID) -> None:
        self._model_id = model_id
        self._model = None   # lazy
        self._processor = None  # lazy

    def load(self) -> None:
        """Load the VibeVoice model + processor into GPU/CPU memory. Idempotent."""
        if self._model is not None:
            return

        logger.info("Loading VibeVoice model %s ...", self._model_id)

        # Direct imports from the community package. AutoProcessor /
        # AutoModelForCausalLM don't work here because the HF repo's
        # config.json has no auto_map entry and processor_config.json is
        # absent; the factory can't discover VibeVoiceProcessor. The community
        # package exposes both classes with a .from_pretrained method that
        # reads the same HF-format checkpoint.
        from vibevoice.modular.modeling_vibevoice_inference import (  # noqa: PLC0415
            VibeVoiceForConditionalGenerationInference,
        )
        from vibevoice.processor.vibevoice_processor import (  # noqa: PLC0415
            VibeVoiceProcessor,
        )

        self._processor = VibeVoiceProcessor.from_pretrained(self._model_id)

        # bfloat16 on CUDA is required for numerical stability; float32 on CPU
        # for MPS / CPU-only inference during development.
        if torch.cuda.is_available():
            load_dtype = torch.bfloat16
            device_map: str | dict = "auto"
            # flash_attention_2 is faster on Ampere/Ada. If the kernel is
            # missing the load will raise; set VOX_VIBEVOICE_ATTN=sdpa to
            # fall back without rebuilding the image.
            attn_impl = os.environ.get("VOX_VIBEVOICE_ATTN", "flash_attention_2")
        else:
            load_dtype = torch.float32
            device_map = "cpu"
            attn_impl = "sdpa"

        self._model = VibeVoiceForConditionalGenerationInference.from_pretrained(
            self._model_id,
            torch_dtype=load_dtype,
            device_map=device_map,
            attn_implementation=attn_impl,
        )
        self._model.eval()

        logger.info(
            "VibeVoice loaded on %s (dtype=%s)",
            "cuda" if torch.cuda.is_available() else "cpu",
            load_dtype,
        )

    def _maybe_trim_reference(
        self, ref_path: str, warn_threshold: float = REF_AUDIO_WARN_SECONDS
    ) -> str:
        """Trim reference audio > REF_AUDIO_MAX_SECONDS and downmix to mono.

        Returns either the original path (unchanged) or a temp-file path with
        the trimmed/resampled audio. Caller is responsible for cleanup of any
        returned temp path.
        """
        try:
            info = sf.info(ref_path)
        except Exception as exc:
            logger.warning("Cannot probe reference audio %s: %s", ref_path, exc)
            return ref_path

        if info.duration > warn_threshold:
            logger.warning(
                "Reference clip is %.1fs (>%.0fs). VibeVoice performs best with "
                "2-10s clips; long references may degrade quality. "
                "Set VOX_REF_AUDIO_MAX_SECONDS to reduce the cap.",
                info.duration,
                warn_threshold,
            )

        needs_trim = info.duration > REF_AUDIO_MAX_SECONDS
        needs_downmix = info.channels > 1
        needs_resample = info.samplerate != VIBEVOICE_SAMPLE_RATE

        if not (needs_trim or needs_downmix or needs_resample):
            return ref_path

        logger.info(
            "Preprocessing reference %s (duration=%.1fs, channels=%d, sr=%d) "
            "→ %.0fs mono @ %dHz",
            ref_path,
            info.duration,
            info.channels,
            info.samplerate,
            REF_AUDIO_MAX_SECONDS,
            VIBEVOICE_SAMPLE_RATE,
        )

        # librosa handles resampling, mono downmix, and trimming in one pass.
        import librosa  # noqa: PLC0415  (deferred: only needed when processing audio)

        audio, _ = librosa.load(
            ref_path,
            sr=VIBEVOICE_SAMPLE_RATE,
            mono=True,
            duration=REF_AUDIO_MAX_SECONDS,
        )

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        tmp.close()
        sf.write(tmp.name, audio, VIBEVOICE_SAMPLE_RATE, subtype="PCM_16")
        return tmp.name

    def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> tuple[np.ndarray, int]:
        """Synthesize ``text`` with optional voice cloning from ``reference_wav_path``.

        Args:
            text: Text to synthesize. Must be non-empty.
            reference_wav_path: Path to a WAV file used for speaker conditioning.
                When None, the model uses its default voice.
            cfg: Classifier-free guidance scale (1.0–5.0). Higher = more faithful
                to the reference voice; lower = more natural. Default 2.0.
            steps: Inference steps (1–50). More steps = higher quality but slower.
                Default 10 (good quality/speed balance for 1.5B model).

        Returns:
            Tuple of (wav_int16_array, sample_rate) where sample_rate=24000.
        """
        assert self._model is not None and self._processor is not None, (
            "call load() first"
        )
        if not text or not text.strip():
            raise ValueError("text must be a non-empty string")

        # VibeVoice's processor parses a dialog script with "Speaker N:" line
        # prefixes and rejects unlabeled text as "No valid speaker lines found
        # in script". Our public API accepts plain text (single-voice), so we
        # auto-promote to single-speaker format here. If the caller already
        # supplied a labeled script (e.g. for a dialog), we pass it through.
        text = _ensure_speaker_labeled(text)

        trimmed_ref: str | None = None
        created_temp: str | None = None  # track temp files we created

        try:
            # Prepare reference audio path (trim/resample if needed).
            if reference_wav_path:
                trimmed_ref = self._maybe_trim_reference(reference_wav_path)
                if trimmed_ref != reference_wav_path:
                    created_temp = trimmed_ref

            # Build processor inputs.
            # voice_samples is List[List[str|np.ndarray]] — outer list is batch,
            # inner list is the reference clips for that batch item.
            # Source: vibevoice_processor.py __call__ signature.
            processor_kwargs: dict = dict(
                text=[text],
                padding=True,
                return_tensors="pt",
                return_attention_mask=True,
            )
            if trimmed_ref:
                processor_kwargs["voice_samples"] = [[trimmed_ref]]

            inputs = self._processor(**processor_kwargs)

            # Move all tensor inputs to the model's device and dtype.
            device = next(self._model.parameters()).device
            model_dtype = next(self._model.parameters()).dtype
            inputs = {
                k: v.to(device=device, dtype=model_dtype)
                if isinstance(v, torch.Tensor) and v.is_floating_point()
                else v.to(device=device)
                if isinstance(v, torch.Tensor)
                else v
                for k, v in inputs.items()
            }

            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()

            # Generate audio.
            # is_prefill=True activates speaker conditioning from voice_samples.
            # generation_config do_sample=False for deterministic output.
            # Source: demo/inference_from_file.py
            with torch.inference_mode():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=None,
                    cfg_scale=float(cfg),
                    tokenizer=self._processor.tokenizer,
                    generation_config={"do_sample": False},
                    verbose=False,
                    is_prefill=bool(trimmed_ref),
                )

        finally:
            # Always release transient GPU memory regardless of outcome.
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            # Clean up temp files we created.
            if created_temp:
                try:
                    Path(created_temp).unlink(missing_ok=True)
                except Exception:
                    pass

        if torch.cuda.is_available():
            peak_gb = torch.cuda.max_memory_allocated() / 1e9
            logger.info(
                "synthesis peak VRAM %.2f GB (text_len=%d)", peak_gb, len(text)
            )

        # outputs.speech_outputs[0] is a float32 tensor at 24 kHz.
        # Convert to int16 PCM so the contract response encodes clean WAV bytes.
        # Source: vibevoice_tokenizer_processor.py save_audio uses
        #         tensor.float().detach().cpu().numpy() → sf.write(float32).
        # We do the same conversion then cast to int16 for PCM_16 compat.
        speech_tensor: torch.Tensor = outputs.speech_outputs[0]
        wav_float: np.ndarray = speech_tensor.float().detach().cpu().numpy()

        # Flatten to 1-D (model may return shape [1, T] or [T]).
        if wav_float.ndim > 1:
            wav_float = wav_float.squeeze()

        # Normalize to [-1, 1] if the signal has headroom, then scale to int16.
        max_val = np.abs(wav_float).max()
        if max_val > 0:
            wav_float = wav_float / max(max_val, 1.0)  # don't amplify quiet signal

        wav_int16 = (wav_float * 32767).astype(np.int16)

        return wav_int16, VIBEVOICE_SAMPLE_RATE
