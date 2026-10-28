"""``voxxy voice clone`` and ``voxxy voice candidates``: the clean-reference-clip workflow.

``candidates`` is the analysis half (rank clean 10-15 s windows in a file, upload
nothing). ``clone`` is the whole story: describe a person, get a voice and a
persona. Both sit on :mod:`voxxy.refclip` (audio analysis) and :mod:`voxxy.clone`
(search/LLM/download/persona); this module only owns prompts, tables and exit
codes, in the same style as ``commands/voice.py``.

Exit codes follow ``voxxy.errors``: 1 generic failure, 2 degraded (voice created but
persona or verification fell short) or bad invocation, 3 server unreachable, 4 not
found, 5 validation / refusal to overwrite.
"""

from __future__ import annotations

import importlib.util
import json as _json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from voxxy import clone as cl
from voxxy import refclip
from voxxy.audio import FfmpegMissing
from voxxy.client import VoxClient, VoxError, VoxNotFound, VoxUnreachable
from voxxy.config import ProjectNotFound, load_config
from voxxy.llm import CloneLLM
from voxxy.persona import PersonaError, load_plugin, persona_by_id
from voxxy.playback import PlaybackError, play_encoded, resolve_player, _play_wav

console = Console()

EXPECTED_ENGINE = "voxcpm"
PICK_POOL = 10  # candidates shown, pickable with --pick, and written to markers.json: one number everywhere


def register(voice_app: typer.Typer) -> None:
    voice_app.command("candidates")(candidates)
    voice_app.command("clone")(clone)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


def analyze_or_exit(
    path: Path, *, min_seconds: float, max_seconds: float, label: str = "analysing", use_cache: bool = True,
    soft: bool = False,
) -> refclip.Analysis:
    """Run the refclip analysis with a spinner, turning every failure into a clean exit.

    A missing dependency is always fatal. With ``soft`` a per-file failure (undecodable
    download, whisper error) raises :class:`voxxy.clone.CloneError` instead of exiting,
    so a multi-video caller can skip that video and try the next.
    """
    try:
        with console.status(f"[cyan]{label}[/cyan]") as status:
            return refclip.analyze(
                path, min_seconds=min_seconds, max_seconds=max_seconds, use_cache=use_cache,
                progress=lambda m: status.update(f"[cyan]{label}: {m}[/cyan]"),
            )
    except refclip.CloneDepsMissing as exc:
        raise _fail(str(exc))
    except FfmpegMissing as exc:
        raise _fail(str(exc))
    except (RuntimeError, OSError) as exc:
        if soft:
            raise cl.CloneError(f"analysis failed: {exc}") from exc
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
# voxxy voice candidates
# ---------------------------------------------------------------------------


def candidates(
    path: Path = typer.Argument(..., help="Source audio/video file to scan."),
    top: int = typer.Option(5, "--top", "-n", min=1, help="How many non-overlapping candidates to list."),
    min_seconds: float = typer.Option(refclip.DEFAULT_MIN_SECONDS, "--min-seconds", help="Shortest clip."),
    max_seconds: float = typer.Option(refclip.DEFAULT_MAX_SECONDS, "--max-seconds", help="Longest clip."),
    as_json: bool = typer.Option(False, "-j", "--json", help="Print the markers as JSON instead of a table."),
    markers_file: Optional[Path] = typer.Option(None, "--markers", help="Also write the markers JSON to FILE."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Re-transcribe instead of using the cached transcript."),
) -> None:
    """Rank the cleanest reference-clip windows in a recording (uploads nothing).

    Transcribes with faster-whisper, finds energy pauses, and scores every span that
    starts after a pause and ends before one: whole sentences, no fillers or cut-off
    fragments at the edges, one speaker, no music, no clipping. This is the same
    analysis `voice add --auto` and `voice clone` use. Run big files through `heavy`.

    Examples:

      voxxy voice candidates interview.wav --top 5
      voxxy voice candidates talk.mp3 --json --markers /tmp/talk.markers.json
    """
    if not path.is_file():
        raise _fail(f"not found: {path}", 4)
    if min_seconds <= 0 or max_seconds < min_seconds:
        raise _fail("need 0 < --min-seconds <= --max-seconds", 2)

    analysis = analyze_or_exit(path, min_seconds=min_seconds, max_seconds=max_seconds, use_cache=not no_cache)
    chosen = analysis.top(top)
    shown = chosen or analysis.top(top, include_rejected=True)

    if markers_file is not None:
        write_markers(markers_file, analysis, shown)
    if as_json:
        typer.echo(_json.dumps(refclip.markers_payload(analysis, shown), indent=2, ensure_ascii=False))
    else:
        spk = analysis.speakers
        console.print(
            f"[dim]{analysis.duration:.1f}s, {len(analysis.words)} words, {len(analysis.pauses)} pauses "
            f"(pause = {analysis.pause_below_db:.0f} dB under speech), "
            f"{'speakers: ' + str(spk.n_clusters) if spk else 'speaker gate: not enough speech'}, "
            f"{len(analysis.markers)} windows scored[/dim]"
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


# ---------------------------------------------------------------------------
# voxxy voice clone
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    video: cl.Video
    dir: Path
    wav: Path
    analysis: refclip.Analysis
    top: list[refclip.Marker]

    @property
    def best_score(self) -> float:
        return self.top[0].score if self.top else -1.0


def _confirm_identity(
    query: str, llm: CloneLLM, results: list[dict[str, str]], *, yes: bool, name: Optional[str] = None,
    force: bool = False,
) -> tuple[cl.Identity, list[dict[str, str]]]:
    """Resolve, show 'Brian Griffin, from Family Guy', and let a human confirm or edit.

    Returns the identity AND the web results it was resolved from: after an edit the
    results are a fresh search, and persona authoring must use those, not the first.
    Non-interactively, an identity nobody confirmed is not accepted blindly: a fallback
    (LLM down, so the raw words) needs an explicit ``--name``, and a low-confidence
    guess needs ``--force``.
    """
    interactive = sys.stdin.isatty() and not yes
    while True:
        with console.status("[cyan]resolving who you mean[/cyan]"):
            identity = cl.resolve_identity(query, llm, results)
        if identity.resolved_by == "fallback":
            why = llm.last_error or "model reply unusable"
            console.print(f"[yellow]could not resolve with the LLM ({escape(why)}); using your words as-is[/yellow]")
        conf = f"confidence {identity.confidence:.0%}" if identity.resolved_by == "llm" else "unverified"
        actor = f", voiced by {identity.voice_actor}" if identity.voice_actor else ""
        console.print(f"[bold]Resolved:[/bold] [green]{escape(identity.label)}[/green]{escape(actor)} [dim]({conf})[/dim]")
        if not interactive:
            if identity.resolved_by == "fallback" and name is None:
                raise _fail(
                    "the LLM could not resolve who you mean, so the voice would be named from your raw words "
                    f"('{identity.voice_name}'). Pass --name <slug> (or run without --yes to confirm interactively).", 5,
                )
            if identity.resolved_by == "llm" and identity.confidence < 0.5 and not force:
                raise _fail(
                    f"low-confidence identity ({identity.confidence:.0%}) for '{identity.label}'; refusing to "
                    "clone it unattended. Describe them more specifically, drop --yes to confirm, or pass --force.", 5,
                )
            return identity, results
        action = questionary.select(
            f"Clone {identity.label}?", choices=["yes", "edit the description", "abort"],
        ).ask()
        if action == "yes":
            return identity, results
        if action == "edit the description":
            edited = questionary.text("Describe who you mean", default=query).ask()
            if not edited:
                raise typer.Exit(code=1)
            query = edited
            results = cl.web_search(query)
            continue
        raise typer.Exit(code=1)


def _collect_attempts(
    identity: cl.Identity, videos: list[cl.Video], workdir: Path, *, max_videos: int, max_minutes: float,
    min_seconds: float, max_seconds: float,
) -> list[Attempt]:
    attempts: list[Attempt] = []
    for k, video in enumerate(videos[:max_videos], 1):
        console.print(f"[bold]video {k}/{min(max_videos, len(videos))}[/bold] {escape(video.title)} "
                      f"[dim]({video.channel}, {int(video.duration)}s) {video.url}[/dim]")
        adir = workdir / "candidates" / f"{k}-{cl.slugify(video.id)}"
        try:
            with console.status("[cyan]downloading audio (yt-dlp)[/cyan]"):
                wav = cl.download_audio(video, adir, max_minutes=max_minutes, identity=identity)
        except cl.CloneError as exc:
            console.print(f"[yellow]{escape(str(exc))}; trying the next video[/yellow]")
            continue
        try:
            analysis = analyze_or_exit(wav, min_seconds=min_seconds, max_seconds=max_seconds, label=f"video {k}", soft=True)
        except cl.CloneError as exc:
            console.print(f"  [yellow]{escape(str(exc))}; trying the next video[/yellow]")
            wav.unlink(missing_ok=True)
            continue
        top = analysis.top(PICK_POOL)
        write_markers(adir / "markers.json", analysis, refclip.select_top(analysis.markers, PICK_POOL))
        best = f"best {top[0].score:.2f} @ {top[0].start:.1f}-{top[0].end:.1f}s" if top else "no clean window"
        console.print(f"  [dim]{analysis.duration:.0f}s audio, {len(analysis.markers)} windows, {best}[/dim]")
        wanted = min(video.duration, max_minutes * 60)
        if wanted > 0 and analysis.duration < 0.6 * wanted:
            console.print(f"  [yellow]only {analysis.duration:.0f}s of the requested {wanted:.0f}s downloaded[/yellow]")
        attempts.append(Attempt(video, adir, wav, analysis, top))
        if top and top[0].score >= cl.WEAK_SCORE:
            break
        if k < min(max_videos, len(videos)):
            console.print("  [yellow]weak result; trying the next video[/yellow]")
    return attempts


def _finalize_workdir(workdir: Path, chosen: Attempt, attempts: list[Attempt]) -> Path:
    """Promote the chosen attempt's metadata to the workdir root; drop the other wavs.

    The chosen wav stays where it was downloaded (``candidates/<k>-<id>/source.wav``) so a
    re-run finds it (download reuse) and the whisper cache key (file path + mtime) still
    matches; ``<workdir>/source.wav`` is a symlink to it for convenience.
    """
    root_wav = workdir / "source.wav"
    root_wav.unlink(missing_ok=True)
    try:
        root_wav.symlink_to(chosen.wav.resolve())
    except OSError:  # no symlinks here: a copy is fine, it is only a convenience
        shutil.copy2(chosen.wav, root_wav)
    if (chosen.dir / "source.json").is_file():
        shutil.copy2(chosen.dir / "source.json", workdir / "source.json")
    markers = chosen.dir / "markers.json"
    if markers.is_file():
        (workdir / "markers.json").write_text(markers.read_text())
    for a in attempts:
        if a is not chosen:
            a.wav.unlink(missing_ok=True)
    return chosen.wav


def _record_selection(workdir: Path, **fields: object) -> None:
    """Write/refresh ``<workdir>/clip.json``: the chosen window, transcript and where it went."""
    path = workdir / "clip.json"
    try:
        data = _json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    data.update(fields)
    path.write_text(_json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _merge_facts(a: list[dict[str, str]], b: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for r in [*a, *b]:
        key = r.get("url") or r.get("title", "")
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:8]


def _play_url(client: VoxClient, url: str, sink_key: Optional[str], player: str, voice: str) -> None:
    """Play the verification line: on the sink if someone listens, else here."""
    if sink_key:
        try:
            if client.sink_status(sink_key).listeners > 0:
                result = client.sink_play(sink_key, url, voice=voice)
                if result.delivered > 0:
                    console.print(f"[green]→[/green] played on sink {sink_key}")
                    return
        except VoxError:
            pass
    try:
        play_encoded(client.fetch_audio(url), player)
    except (PlaybackError, VoxError) as exc:
        typer.secho(f"could not play: {exc}", fg=typer.colors.YELLOW, err=True)


def clone(
    query: str = typer.Argument(..., help='Who to clone, in your own words, e.g. "the dog from Family Guy".'),
    name: Optional[str] = typer.Option(None, "-n", "--name", help="Voice slug. Default: first meaningful token of the resolved slug (brian). Required with --yes when the LLM could not resolve the identity."),
    yes: bool = typer.Option(False, "-y", "--yes", help="No prompts: accept the identity, pick the best clip."),
    pick: Optional[int] = typer.Option(None, "--pick", min=1, help="Take the Nth ranked candidate (1 = best, up to 10). Required with --yes for fictional characters or multi-speaker sources."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing voice of the same name; also accepts a low-confidence identity under --yes."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do everything except upload (and write no persona into the repo)."),
    no_persona: bool = typer.Option(False, "--no-persona", help="Skip persona generation and association."),
    force_persona: bool = typer.Option(False, "--force-persona", help="Overwrite an existing persona file."),
    max_videos: int = typer.Option(3, "--max-videos", min=1, help="Try up to N ranked videos before giving up."),
    max_minutes: float = typer.Option(cl.DEFAULT_MAX_MINUTES, "--max-minutes", help="Download only the first N minutes of each video."),
    min_seconds: float = typer.Option(refclip.DEFAULT_MIN_SECONDS, "--min-seconds", help="Shortest reference clip."),
    max_seconds: float = typer.Option(refclip.DEFAULT_MAX_SECONDS, "--max-seconds", help="Longest reference clip."),
    text: str = typer.Option(cl.DEFAULT_VERIFY_TEXT, "--text", help="Line synthesized to verify the new voice."),
    sink: Optional[str] = typer.Option(None, "-s", "--sink", help="Play the verification on this sink (default $VOX_SINK / config)."),
    no_play: bool = typer.Option(False, "--no-play", help="Never play audio (auditions, verification)."),
    player: Optional[str] = typer.Option(None, "-P", "--player", help="Audio player binary (default $VOX_PLAYER / platform)."),
) -> None:
    """Describe a person or character; get a cloned voice and persona.

    \b
    1. web search + LLM resolve who you mean, you confirm ("Brian Griffin, from Family Guy")
    2. YouTube search, ranked for the target's own clean voice
    3. yt-dlp downloads the audio head into ~/.cache/voxxy/clone/<slug>/
    4. faster-whisper + pause analysis rank clean 10-15 s windows (markers.json)
    5. a weak video falls through to the next ranked one (--max-videos)
    6. you audition and pick (or --yes / --pick N); the clip is exported cleanly
    7. voice uploaded WITH prompt_text (the exact transcript), persona authored and
       associated, one line synthesized to prove the engine is voxcpm

    Needs the clone extra: uv tool install --editable "$HOME/code/voxxy/cli\\[clone]" --force
    Run through `heavy` (whisper is CPU-hungry). A re-run reuses the downloaded audio
    and cached transcript, so `--pick N` after a first pass is cheap.

    With --yes (or no TTY) nobody listens to the clip, and a fictional character's
    source is mixed speakers by nature, so --pick N is then required: look at the
    candidates (or `voxxy voice candidates`) first.

    Examples:

      voxxy voice clone "the dog from Family Guy"
      voxxy voice clone "Arnold Schwarzenegger" --yes --dry-run --no-persona
      voxxy voice clone "Morgan Freeman" --pick 2 --name morgan2
    """
    if min_seconds <= 0 or max_seconds < min_seconds:
        raise _fail("need 0 < --min-seconds <= --max-seconds", 2)
    if name is not None and not cl.SLUG_RE.match(name):
        raise _fail(f"invalid --name '{name}': must match ^[a-z0-9-]+$", 5)
    if not yes and not sys.stdin.isatty():
        raise _fail("refusing to run without a TTY unless --yes is given (it confirms the identity for you)", 5)
    interactive = sys.stdin.isatty() and not yes

    # Fail before any search/LLM/download work if the audio stack is not installed.
    for module, dist in (("numpy", "numpy"), ("scipy", "scipy"), ("faster_whisper", "faster-whisper")):
        if importlib.util.find_spec(module) is None:
            raise _fail(f"missing optional dependency '{dist}'.\nFix:  {cl.INSTALL_HINT}")

    cfg = load_config()
    llm = CloneLLM(cfg)
    client = VoxClient(cfg.default_url)
    player_bin = resolve_player(player)
    sink_key = sink or os.environ.get("VOX_SINK") or cfg.default_sink or None

    # 1. who?
    with console.status("[cyan]web search[/cyan]"):
        results = cl.web_search(query)
    if not results:
        console.print("[dim]no web results (web-search missing or failed); relying on the model's knowledge[/dim]")
    identity, results = _confirm_identity(query, llm, results, yes=yes, name=name, force=force)
    voice_name = name or identity.voice_name
    if name is None and identity.resolved_by == "fallback" and interactive:
        # The default is a guess from the raw words; let the human fix it before it is uploaded.
        typed = questionary.text("Voice name (a-z, 0-9, dashes)", default=voice_name,
                                 validate=lambda v: bool(cl.SLUG_RE.match(v)) or "must match ^[a-z0-9-]+$").ask()
        if not typed:
            raise typer.Exit(code=1)
        voice_name = typed
    # A fallback identity's slug is the user's raw sentence ("the-dog-from-family-guy"): name the persona after the voice.
    persona_id = voice_name if identity.resolved_by == "fallback" else identity.slug
    workdir = cl.workdir_for(identity.slug)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "identity.json").write_text(_json.dumps(identity.as_dict(), indent=2, ensure_ascii=False))

    # Refuse early, before minutes of download + whisper, rather than after.
    existing = None
    try:
        existing = client.get_voice(voice_name)
    except VoxNotFound:
        pass
    except VoxUnreachable as exc:
        if not dry_run:
            raise _fail(f"unreachable: {exc}", 3)
    except VoxError as exc:  # e.g. a 500 from the server: a message, not a traceback
        if not dry_run:
            raise _fail(f"could not check for an existing voice '{voice_name}': {exc}")
    if existing is not None and not force:
        if not dry_run:
            raise _fail(f"voice '{voice_name}' already exists; pass --force to overwrite or --name to pick another", 5)
        console.print(f"[yellow]note: voice '{voice_name}' already exists; a real run would need --force or another --name[/yellow]")

    plugin = None
    reuse_persona = False
    if not no_persona:
        try:
            plugin = load_plugin()
            reuse_persona = persona_by_id(plugin, persona_id) is not None and not force_persona
        except (ProjectNotFound, PersonaError) as exc:
            console.print(f"[yellow]persona skipped: {escape(str(exc).splitlines()[0])}[/yellow]")
        if reuse_persona:
            console.print(f"[dim]persona '{persona_id}' already exists; will reuse it (--force-persona regenerates)[/dim]")

    # 2. which videos?
    with console.status("[cyan]searching YouTube[/cyan]"):
        videos = cl.find_videos(identity, llm)
    if not videos:
        raise _fail("no suitable YouTube videos found (45 s - 40 min, own voice); try a more specific description")
    console.print(f"[dim]{len(videos)} candidate videos; trying up to {max_videos}[/dim]")

    # 3-5. download, analyse, fall through on weak results
    attempts = _collect_attempts(
        identity, videos, workdir, max_videos=max_videos, max_minutes=max_minutes,
        min_seconds=min_seconds, max_seconds=max_seconds,
    )
    usable = [a for a in attempts if a.top]
    if not usable:
        raise _fail(
            f"no clean {min_seconds:g}-{max_seconds:g}s window in any of {len(attempts)} downloaded video(s). "
            f"Markers are in {workdir}/candidates/. Try --max-videos higher, --max-minutes higher, or a more specific description."
        )
    chosen_attempt = max(usable, key=lambda a: a.best_score)
    source_wav = _finalize_workdir(workdir, chosen_attempt, attempts)

    # 6. choose + export
    console.print(candidates_table(chosen_attempt.top, f"Candidates ({chosen_attempt.video.title[:50]})"))
    risk = _speaker_risk(identity, chosen_attempt.analysis, chosen_attempt.video, chosen_attempt.top[0])
    if risk is None and identity.franchise and pick is None and not interactive:
        console.print(
            f"[yellow]unattended pick for {escape(identity.display_name)}: ECAPA says one dominant voice and the title "
            "names the character, but nobody listened. Audition with `voxxy voice candidates` or re-run with --pick N.[/yellow]"
        )
    if risk and pick is None and not interactive:
        raise _fail(
            f"won't pick a clip unattended: {risk}, so the best-scoring window may be the wrong voice. "
            f"Listen to the candidates (markers: {workdir}/markers.json; audio: {source_wav}) and re-run with "
            "--pick N, or drop --yes to audition. The download and transcript are reused on a re-run.", 5,
        )
    marker = pick_marker(
        source_wav, chosen_attempt.top, yes=yes, pick=pick, player=player_bin, can_play=not no_play,
    )
    clip_path = workdir / f"{identity.slug}.wav"
    prepared = prepare_clip(source_wav, clip_path, marker.start, marker.end, fallback_text=marker.text)
    prepared.marker = marker
    if not prepared.prompt_text:
        raise _fail("could not determine the transcript of the exported clip; refusing to upload without prompt_text")
    _record_selection(
        workdir, selected=marker.to_dict(), start=prepared.start, end=prepared.end, prompt_text=prepared.prompt_text,
        clip=str(clip_path), source=str(source_wav), video=chosen_attempt.video.as_dict(), voice=voice_name,
        persona=None, engine=None, audio_url=None, uploaded=False,
    )
    console.print(f"[green]exported[/green] {clip_path} [dim]({marker.duration:.1f}s)[/dim]")
    console.print(f"[bold]prompt_text[/bold]: {escape(prepared.prompt_text)}")

    # persona authoring happens before upload so a dry run can preview it too
    persona_md: Optional[str] = None
    persona_status = "skipped (--no-persona)" if no_persona else "skipped"
    persona_degraded = False
    if plugin is not None and not reuse_persona:
        try:
            with console.status("[cyan]writing the persona[/cyan]"):
                persona_md = cl.generate_persona(
                    llm, plugin, character=identity.label, voice=voice_name, brief=identity.persona_brief,
                    facts=_merge_facts(results, facts_for(identity)), display_name=identity.display_name,
                )
        except cl.CloneError as exc:
            persona_status = f"FAILED: {exc}"
            persona_degraded = True
            console.print(f"[yellow]persona generation failed: {escape(str(exc))}[/yellow]")

    if dry_run:
        if persona_md is not None:
            preview = workdir / f"persona-{persona_id}.md"
            preview.write_text(persona_md, encoding="utf-8")
            persona_status = f"preview written to {preview} (dry run)"
        elif reuse_persona:
            persona_status = f"{persona_id} (existing file, would reuse)"
        _summary(identity, chosen_attempt.video, prepared, voice_name, persona_status, workdir, engine=None,
                 audio_url=None, dry_run=True)
        raise typer.Exit(code=2 if persona_degraded else 0)

    # 7. upload
    tags = ["clone"] + ([cl.slugify(identity.franchise)] if identity.franchise else [])
    try:
        voice = client.create_voice(
            name=voice_name, display_name=identity.display_name, audio_path=clip_path,
            tags=tags, prompt_text=prepared.prompt_text,
        )
    except VoxUnreachable as exc:
        raise _fail(f"unreachable: {exc}", 3)
    except VoxError as exc:
        raise _fail(f"server rejected upload: {exc}")
    console.print(f"[green]uploaded voice[/green] {voice.name} ({voice.duration_s:.1f}s, prompt_text set)")
    _record_selection(workdir, uploaded=True)

    # persona: write the file, then associate it with the voice
    # Never associate a persona id whose file does not exist (generation failed).
    if plugin is not None and (persona_md is not None or reuse_persona):
        try:
            if persona_md is not None:
                path = cl.write_persona(plugin, persona_id, persona_md, force=force_persona)
                console.print(f"[green]persona written[/green] {path}")
            client.set_voice_persona(voice_name, persona_id)
            persona_status = f"{persona_id} (associated)"
        except cl.CloneError as exc:
            persona_status, persona_degraded = f"FAILED: {exc}", True
            console.print(f"[yellow]{escape(str(exc))}[/yellow]")
        except VoxError as exc:
            persona_status, persona_degraded = f"written but not associated: {exc}", True
            console.print(f"[yellow]persona association failed: {escape(str(exc))}[/yellow]")

    # 8. verify the engine
    engine = None
    audio_url = None
    degraded = persona_degraded
    try:
        with console.status("[cyan]synthesizing a verification line[/cyan]"):
            started = time.monotonic()
            resp = client.synthesize_url(text=text, voice=voice_name)
        engine = resp.vox_engine_header or resp.engine
        audio_url = resp.audio_url
        console.print(f"[dim]synthesized in {time.monotonic() - started:.1f}s[/dim]")
        if engine != EXPECTED_ENGINE or resp.engine != EXPECTED_ENGINE:
            degraded = True
            console.print(Panel(
                f"served by [bold]{engine}[/bold], not {EXPECTED_ENGINE}.\n"
                "ElevenLabs/other engines ignore the reference audio, so this is NOT a clone. "
                "Check `voxxy engine list` and `voxxy daemon status`.",
                title="[red]NOT A CLONE[/red]", border_style="red",
            ))
        elif not no_play and sys.stdout.isatty() and not yes:
            _play_url(client, resp.audio_url, sink_key, player_bin, voice_name)
    except VoxError as exc:
        degraded = True
        console.print(f"[red]verification synthesis failed: {escape(str(exc))}[/red]")

    _record_selection(workdir, persona=persona_status, engine=engine, audio_url=audio_url, degraded=degraded)
    _summary(identity, chosen_attempt.video, prepared, voice_name, persona_status, workdir, engine=engine,
             audio_url=audio_url, dry_run=False)
    if degraded:
        raise typer.Exit(code=2)


def facts_for(identity: cl.Identity) -> list[dict[str, str]]:
    """Persona facts searched on the RESOLVED identity ("Brian Griffin, from Family Guy"), not the user's loose words."""
    return cl.web_search(f"{identity.label} personality and speaking style")


def _title_names_target(identity: cl.Identity, video: cl.Video) -> bool:
    """True when the video title contains a (>=3 letter) word of the target's name or voice actor.

    "Brian's apology speech" / "Brian Griffin best moments" is a video ABOUT the target,
    where the target is the dominant voice. "Family Guy funniest scenes" is not.
    """
    import re

    title = f" {video.title.lower()} "
    names = f"{identity.display_name} {identity.voice_actor or ''}".lower()
    for tok in re.findall(r"[a-z0-9']+", names):
        tok = tok.removesuffix("'s")
        if len(tok) >= 3 and re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", title):
            return True
    return False


def _speaker_risk(
    identity: cl.Identity, analysis: refclip.Analysis, video: Optional[cl.Video] = None,
    marker: Optional[refclip.Marker] = None,
) -> Optional[str]:
    """Why the top-ranked window cannot be trusted to be the target speaker, or None.

    Two independent signals. The ECAPA gate (:mod:`voxxy.speakergate`) proves the window is
    ONE voice and the dominant voice of the source; it ran on ``marker`` when
    ``marker.metrics`` carries ``voice_gated``. A title that names the target ("Brian's
    apology speech") is the evidence that the dominant voice IS the target. A fictional
    character needs both (a compilation titled "Family Guy scenes" is a dozen characters);
    a real person with one voice in the source needs neither. Without ECAPA the old MFCC
    gate only knows "the most talkative voice", which on scene compilations reported a single
    speaker for every window, so the unattended answer is then to ask for --pick.
    """
    gated = marker is not None and marker.metrics.get("voice_gated", 0.0) > 0.0
    named = video is not None and _title_names_target(identity, video)
    if gated:
        if identity.franchise and not named:
            return (f"{identity.display_name} is a fictional character and the video title does not name them "
                    "(the clip is one voice, but whose?)")
        return None
    if analysis.voice_gate_note:
        note = f" ({analysis.voice_gate_note})"
    else:
        note = ""
    if identity.franchise:
        return f"{identity.display_name} is a fictional character and the ECAPA voice gate did not run{note}"
    spk = analysis.speakers
    if spk is None:
        return "the speaker gate had too little speech to run"
    if spk.n_clusters > 1:
        return f"{spk.n_clusters} speakers were detected in the source"
    return None


def _summary(
    identity: cl.Identity, video: cl.Video, prepared: PreparedClip, voice_name: str, persona_status: str,
    workdir: Path, *, engine: Optional[str], audio_url: Optional[str], dry_run: bool,
) -> None:
    m = prepared.marker
    scores = "-"
    if m is not None and m.breakdown:
        scores = (
            f"{m.score:.2f}  (clarity {m.breakdown.get('clarity', 0):.2f}, sentence {m.breakdown.get('sentence', 0):.2f}, "
            f"snr {m.metrics.get('snr_db', 0):.0f} dB, background {m.breakdown.get('background', 0):.2f}, "
            f"speaker {m.breakdown.get('speaker', 0):.2f})"
        )
    lines = [
        f"[bold]identity[/bold]:   {escape(identity.label)}"
        + (f" [dim](voiced by {escape(identity.voice_actor)})[/dim]" if identity.voice_actor else ""),
        f"[bold]video[/bold]:      {escape(video.title)}\n              {video.url}",
        f"[bold]window[/bold]:     {prepared.start:.2f}-{prepared.end:.2f}s ({prepared.end - prepared.start:.1f}s)",
        f"[bold]transcript[/bold]: {escape(prepared.prompt_text)}",
        f"[bold]scores[/bold]:     {scores}",
        f"[bold]voice[/bold]:      {voice_name}" + ("  [dim](not uploaded: dry run)[/dim]" if dry_run else ""),
        f"[bold]persona[/bold]:    {escape(persona_status)}",
        f"[bold]clip[/bold]:       {prepared.clip}",
        f"[bold]workdir[/bold]:    {workdir}",
    ]
    if engine is not None:
        ok = engine == EXPECTED_ENGINE
        lines.append(f"[bold]engine[/bold]:     [{'green' if ok else 'red'}]{engine}[/]")
    if audio_url:
        lines.append(f"[bold]audio[/bold]:      {audio_url}")
    console.print(Panel("\n".join(lines), title="[green]dry run complete[/green]" if dry_run else "[green]cloned[/green]",
                        border_style="green"))
