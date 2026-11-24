"""``voxxy voice candidates`` and ``voxxy voice isolate``: the clean-reference-clip tools.

``candidates`` ranks clean 10-15 s windows in a file you own and uploads nothing;
``isolate`` strips music and noise with Demucs. ``voice add --auto`` uses the same
helpers. Both sit on :mod:`voxxy.refclip` (audio analysis) and :mod:`voxxy.vocals`.

This module is part of every edition. ``voxxy voice clone`` (find a named person's
recordings online) lives in :mod:`voxxy.commands.voice_clone`, which the self-hosted
bundle leaves out; it reuses these helpers.

Exit codes follow ``voxxy.errors``: 1 generic failure, 2 bad invocation, 5 validation.
"""

from __future__ import annotations

import json as _json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from voxxy import refclip
from voxxy import vocals as vc
from voxxy.audio import FfmpegMissing, probe
from voxxy.playback import _play_wav

console = Console()

PICK_POOL = 10  # candidates shown, pickable with --pick, and written to markers.json: one number everywhere


class ClipAnalysisError(RuntimeError):
    """A per-file analysis failure raised by ``analyze_or_exit(soft=True)`` instead of exiting."""


def register(voice_app: typer.Typer) -> None:
    voice_app.command("candidates")(candidates)
    voice_app.command("isolate")(isolate)

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


def isolate_or_exit(
    path: Path, dst: Optional[Path] = None, *, model: str = vc.DEFAULT_MODEL,
    device: str = "auto", use_cache: bool = True,
) -> Path:
    """Run Demucs vocal isolation with a spinner, exiting cleanly on failure."""
    try:
        with console.status(f"[cyan]isolating vocals (Demucs {model})[/cyan]") as status:
            return vc.isolate_vocals(
                path, dst, model=model, device=device, use_cache=use_cache,
                progress=lambda m: status.update(f"[cyan]{m}[/cyan]"),
            )
    except vc.VocalIsolationUnavailable as exc:
        raise _fail(f"vocal isolation failed: {exc}")


def analyze_or_exit(
    path: Path, *, min_seconds: float, max_seconds: float, label: str = "analysing", use_cache: bool = True,
    soft: bool = False, isolate: Optional[bool] = None,
) -> refclip.Analysis:
    """Run the refclip analysis with a spinner, turning every failure into a clean exit.

    A missing dependency is always fatal. With ``soft`` a per-file failure (undecodable
    download, whisper error) raises :class:`ClipAnalysisError` instead of exiting,
    so a multi-video caller can skip that video and try the next.
    """
    try:
        with console.status(f"[cyan]{label}[/cyan]") as status:
            return refclip.analyze(
                path, min_seconds=min_seconds, max_seconds=max_seconds, use_cache=use_cache,
                isolate_vocals=isolate,
                progress=lambda m: status.update(f"[cyan]{label}: {m}[/cyan]"),
            )
    except refclip.CloneDepsMissing as exc:
        raise _fail(str(exc))
    except FfmpegMissing as exc:
        raise _fail(str(exc))
    except (RuntimeError, OSError) as exc:
        if soft:
            raise ClipAnalysisError(f"analysis failed: {exc}") from exc
        raise _fail(f"analysis failed: {exc}")


def candidates_table(markers: list[refclip.Marker], title: str, *, show_reasons: bool = False) -> Table:
    table = Table(title=title)
    table.add_column("#", justify="right", style="bold")
    table.add_column("window", style="cyan", no_wrap=True)
    table.add_column("dur", justify="right")
    table.add_column("score", justify="right", style="green")
    table.add_column("snr", justify="right")
    table.add_column("clar", justify="right")
    table.add_column("sent", justify="right")
    table.add_column("bg", justify="right")
    table.add_column("spk", justify="right")
    table.add_column("1-voice", justify="right")
    table.add_column("transcript" if not show_reasons else "transcript / why rejected")
    for i, m in enumerate(markers, 1):
        b = m.breakdown
        text = escape(m.text if len(m.text) <= 110 else m.text[:107] + "...")
        if show_reasons and m.reasons:
            text += f"\n[red]{escape('; '.join(m.reasons))}[/red]"
        table.add_row(
            str(i), f"{m.start:.2f}-{m.end:.2f}s", f"{m.duration:.1f}s", f"{m.score:.2f}",
            f"{m.metrics.get('snr_db', 0):.0f}dB", f"{b.get('clarity', 0):.2f}", f"{b.get('sentence', 0):.2f}",
            f"{b.get('background', 0):.2f}", f"{b.get('speaker', 0):.2f}",
            f"{m.metrics['coherence']:.2f}" if "coherence" in m.metrics else "-", text,
        )
    return table


def write_markers(path: Path, analysis: refclip.Analysis, markers: list[refclip.Marker]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(refclip.markers_payload(analysis, markers), indent=2, ensure_ascii=False))


@dataclass
class PreparedClip:
    clip: Path
    start: float
    end: float
    prompt_text: str
    marker: Optional[refclip.Marker] = None


def prepare_clip(src: Path, dst: Path, start: float, end: float, *, fallback_text: str = "",
                 prompt_text: Optional[str] = None) -> PreparedClip:
    """Export the window with the clean recipe and settle prompt_text.

    An explicit ``prompt_text`` wins; otherwise the exported clip is re-transcribed so
    the words match exactly what is audible (falling back to the window's own words).
    """
    try:
        refclip.export_clip(src, dst, start, end)
        text = prompt_text.strip() if prompt_text and prompt_text.strip() else refclip.clip_prompt_text(dst, fallback_text)
    except refclip.CloneDepsMissing as exc:
        raise _fail(str(exc))
    except FfmpegMissing as exc:
        raise _fail(str(exc))
    except (RuntimeError, ValueError) as exc:
        raise _fail(f"clip export failed: {exc}")
    return PreparedClip(clip=dst, start=start, end=end, prompt_text=text)


def audition(src: Path, marker: refclip.Marker, player: str) -> None:
    """Export a candidate to a temp wav and play it locally.

    Local playback only: an audition is a file on this disk, and the sink relay carries
    server-cached URLs, so there is nothing to push without uploading first.
    """
    with tempfile.TemporaryDirectory(prefix="voxxy-audition-") as tmp:
        wav = Path(tmp) / "audition.wav"
        try:
            refclip.export_clip(src, wav, marker.start, marker.end)
            _play_wav(wav.read_bytes(), player)
        except typer.Exit:
            typer.secho("(could not play here; judge by the transcript or use --pick)", fg=typer.colors.YELLOW, err=True)
        except (RuntimeError, FfmpegMissing) as exc:
            typer.secho(f"audition failed: {exc}", fg=typer.colors.YELLOW, err=True)


def pick_marker(
    src: Path, markers: list[refclip.Marker], *, yes: bool, pick: Optional[int], player: str,
    can_play: bool = True,
) -> refclip.Marker:
    """--pick N, else the best (--yes / non-interactive), else audition and choose."""
    if pick is not None:
        if not 1 <= pick <= len(markers):
            raise _fail(f"--pick {pick} out of range (1-{len(markers)})", 2)
        return markers[pick - 1]
    if yes or not sys.stdin.isatty():
        return markers[0]
    while True:
        choices = [
            questionary.Choice(
                title=f"#{i} {m.start:.1f}-{m.end:.1f}s score {m.score:.2f}  {m.text[:70]}", value=i - 1,
            )
            for i, m in enumerate(markers, 1)
        ]
        choices.append(questionary.Choice(title="abort", value=None))
        idx = questionary.select("Choose a reference clip", choices=choices).ask()
        if idx is None:
            raise typer.Exit(code=1)
        action = questionary.select(
            f"#{idx + 1}", choices=(["audition"] if can_play else []) + ["use this one", "back"],
        ).ask()
        if action == "audition":
            console.print(f"[dim]playing {markers[idx].start:.1f}-{markers[idx].end:.1f}s ...[/dim]")
            audition(src, markers[idx], player)
            if questionary.confirm("Use this one?", default=True).ask():
                return markers[idx]
        elif action == "use this one":
            return markers[idx]
        elif action is None:
            raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# voxxy voice isolate
# ---------------------------------------------------------------------------


def isolate(
    path: Path = typer.Argument(..., help="Source audio/video file to extract vocals from."),
    output: Optional[Path] = typer.Option(None, "-o", "--output", help="Output WAV path (default: <stem>_vocals.wav next to source)."),
    model: str = typer.Option(vc.DEFAULT_MODEL, "-m", "--model", help="Demucs model name (htdemucs or htdemucs_ft)."),
    device: str = typer.Option("auto", "--device", help="Device for Demucs: auto, cuda, or cpu."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Re-run Demucs instead of reusing the cached vocal stem."),
    as_json: bool = typer.Option(False, "-j", "--json", help="Print result as JSON."),
) -> None:
    """Isolate the vocal track from background music using Demucs.

    Writes a clean vocal WAV (`<stem>_vocals.wav` by default) and caches the separated
    stem under `~/.cache/voxxy/refclip/` so subsequent `voxxy voice candidates` or
    `voxxy voice add --auto` on the original file reuse it automatically.

    Examples:

      voxxy voice isolate talk.mp3
      voxxy voice isolate talk.mp3 -o talk_vocals.wav
    """
    if not path.is_file():
        raise _fail(f"not found: {path}", 4)
    dst = output if output is not None else path.with_name(f"{path.stem}_vocals.wav")
    out_path = isolate_or_exit(path, dst, model=model, device=device, use_cache=not no_cache)
    duration = None
    try:
        duration = round(probe(out_path).duration, 2)
    except Exception:  # noqa: BLE001
        pass
    if as_json:
        typer.echo(_json.dumps({
            "source": str(path),
            "vocals": str(out_path),
            "model": model,
            "duration": duration,
        }, indent=2))
    else:
        dur_str = f" ({duration:.1f}s)" if duration is not None else ""
        console.print(f"[green]isolated vocals[/green] → [cyan]{out_path}[/cyan]{dur_str}")


# ---------------------------------------------------------------------------
# voxxy voice candidates
# ---------------------------------------------------------------------------


def candidates(
    path: Path = typer.Argument(..., help="Source audio/video file to scan."),
    top: int = typer.Option(5, "--top", "-n", min=1, help="How many non-overlapping candidates to list."),
    min_seconds: float = typer.Option(refclip.DEFAULT_MIN_SECONDS, "--min-seconds", help="Shortest clip."),
    max_seconds: float = typer.Option(refclip.DEFAULT_MAX_SECONDS, "--max-seconds", help="Longest clip."),
    isolate: Optional[bool] = typer.Option(
        None, "--isolate/--no-isolate",
        help="Isolate vocals with Demucs before scoring (default: auto when background music blocks all windows).",
    ),
    as_json: bool = typer.Option(False, "-j", "--json", help="Print the markers as JSON instead of a table."),
    markers_file: Optional[Path] = typer.Option(None, "--markers", help="Also write the markers JSON to FILE."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Re-transcribe instead of using the cached transcript."),
) -> None:
    """Rank the cleanest reference-clip windows in a recording (uploads nothing).

    Transcribes with faster-whisper, finds energy pauses, and scores every span that
    starts after a pause and ends before one: whole sentences, no fillers or cut-off
    fragments at the edges, one speaker, no music, no clipping. When background music
    causes every window to fail, Demucs vocal isolation runs automatically and re-scores
    the clean vocal stem. This is the same analysis `voice add --auto` uses.

    Examples:

      voxxy voice candidates interview.wav --top 5
      voxxy voice candidates talk.mp3 --isolate --top 5
      voxxy voice candidates talk.mp3 --json --markers /tmp/talk.markers.json
    """
    if not path.is_file():
        raise _fail(f"not found: {path}", 4)
    if min_seconds <= 0 or max_seconds < min_seconds:
        raise _fail("need 0 < --min-seconds <= --max-seconds", 2)

    analysis = analyze_or_exit(
        path, min_seconds=min_seconds, max_seconds=max_seconds, use_cache=not no_cache, isolate=isolate,
    )
    chosen = analysis.top(top)
    shown = chosen or analysis.top(top, include_rejected=True)

    if markers_file is not None:
        write_markers(markers_file, analysis, shown)
    if as_json:
        typer.echo(_json.dumps(refclip.markers_payload(analysis, shown), indent=2, ensure_ascii=False))
    else:
        spk = analysis.speakers
        iso_tag = ", vocals: isolated (Demucs)" if analysis.vocals_isolated else (
            f", vocals: raw ({analysis.isolation_note})" if analysis.isolation_note else ""
        )
        console.print(
            f"[dim]{analysis.duration:.1f}s, {len(analysis.words)} words, {len(analysis.pauses)} pauses "
            f"(pause = {analysis.pause_below_db:.0f} dB under speech), "
            f"{'speakers: ' + str(spk.n_clusters) if spk else 'speaker gate: not enough speech'}"
            f"{iso_tag}, {len(analysis.markers)} windows scored[/dim]"
        )
        if chosen:
            console.print(candidates_table(chosen, f"Top {len(chosen)} clean clips ({path.name})"))
            if markers_file is not None:
                console.print(f"[dim]markers written to {markers_file}[/dim]")
        elif shown:
            console.print(candidates_table(shown, "No window passed the gates; best rejected", show_reasons=True))
        else:
            console.print(
                f"[red]no admissible window of {min_seconds:g}-{max_seconds:g}s[/red] "
                "(no pauses at sentence boundaries?). Try --min-seconds lower, or cut by hand with "
                "`voxxy voice add --start S --end E`."
            )
    if not chosen:
        raise typer.Exit(code=1)
