"""Voice CRUD commands.

`list`, `info`, `add`, `delete`, plus `persona` to associate a Hermes
voice-persona character with a voice at the voxxy level. `candidates` and `clone`
(the clean-reference-clip workflow) live in :mod:`voxxy.commands.voice_clone`.
"""

from __future__ import annotations

import json as _json
import re
import sys
import tempfile
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from voxxy.audio import (
    DEFAULT_TRIM_SECONDS,
    AudioProbeError,
    FfmpegMissing,
    probe,
    preprocess,
)
from voxxy import refclip
from voxxy.client import VoxClient, VoxNotFound, VoxUnreachable, VoxValidationError
from voxxy.commands import voice_clone
from voxxy.config import load_config
from voxxy.persona import (
    NONE_SENTINEL,
    PERSONA_ID_RE,
    Effective,
    effective_persona,
    persona_id,
    try_load_plugin,
)

console = Console()

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def register(voice_app: typer.Typer) -> None:
    voice_app.command("list")(list_voices)
    voice_app.command("info")(voice_info)
    voice_app.command("add")(add)
    voice_app.command("delete")(delete)
    voice_app.command("persona")(persona)
    voice_clone.register(voice_app)


def list_voices(
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump raw JSON."),
) -> None:
    """List saved voice profiles."""
    cfg = load_config()
    try:
        voices = VoxClient(cfg.default_url).list_voices()
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    if as_json:
        typer.echo(_json.dumps([v.model_dump() for v in voices], indent=2))
        return

    if not voices:
        console.print("[dim]no voices registered[/dim]")
        return

    table = Table(title=f"Voices ({len(voices)})")
    table.add_column("name", style="cyan")
    table.add_column("display_name")
    table.add_column("duration_s", justify="right")
    table.add_column("tags")
    table.add_column("engines")
    table.add_column("persona")
    # Frontmatter defaults only matter for voices with no voxxy record; the
    # plugin is optional here (no project root => just show the records).
    plugin, _ = try_load_plugin()
    for v in voices:
        engines = []
        if v.vibevoice_ref_path:
            engines.append("vibevoice")
        if v.elevenlabs_voice_id is not None:
            engines.append("elevenlabs")
        # voxcpm uses wav_path implicitly; always available.
        engines.insert(0, "voxcpm")
        table.add_row(
            v.name,
            v.display_name,
            f"{v.duration_s:.1f}",
            ",".join(v.tags) if v.tags else "-",
            ",".join(engines),
            _persona_cell(v.name, v.persona, plugin),
        )
    console.print(table)


def voice_info(
    name: str = typer.Argument(..., help="Voice slug."),
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump raw JSON."),
) -> None:
    """Show all metadata for a single voice."""
    cfg = load_config()
    try:
        v = VoxClient(cfg.default_url).get_voice(name)
    except VoxNotFound:
        typer.secho(f"voice '{name}' not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    plugin, _ = try_load_plugin()
    eff = effective_persona(v.name, v.persona, plugin)

    if as_json:
        typer.echo(_json.dumps({**v.model_dump(), "effective_persona": eff.as_dict()}, indent=2))
        return

    body = "\n".join(
        [
            f"[bold]display_name[/bold]: {v.display_name}",
            f"[bold]duration_s[/bold]:   {v.duration_s:.2f}",
            f"[bold]tags[/bold]:         {', '.join(v.tags) if v.tags else '-'}",
            f"[bold]prompt_text[/bold]:  {v.prompt_text or '-'}",
            f"[bold]elevenlabs[/bold]:   {v.elevenlabs_voice_id or '-'}",
            f"[bold]vibevoice_ref[/bold]: {v.vibevoice_ref_path or '(falls back to wav_path)'}",
            f"[bold]vibevoice_tag[/bold]: {v.vibevoice_speaker_tag or '-'}",
            f"[bold]persona[/bold]:      {v.persona or '-'}",
            f"[bold]effective[/bold]:    {eff.label()}",
        ]
    )
    console.print(Panel(body, title=f"voice: {v.name}", border_style="cyan"))


def _persona_cell(voice: str, record: str | None, plugin) -> str:
    """Persona column: the voxxy record, else a dim frontmatter default."""
    if record:
        return record
    eff = effective_persona(voice, None, plugin)
    if eff.persona_id:
        return f"[dim]{eff.persona_id} (frontmatter)[/dim]"
    return "-"


def _show_persona(name: str, record: str | None, eff: Effective, as_json: bool) -> None:
    if as_json:
        typer.echo(_json.dumps({"voice": name, "persona": record, "effective": eff.as_dict()}, indent=2))
        return
    console.print(f"[bold]voice[/bold]:     {name}")
    console.print(f"[bold]persona[/bold]:   {record or '-'} [dim](voxxy record)[/dim]")
    console.print(f"[bold]effective[/bold]: {eff.label()}")
    if eff.problem:
        console.print(f"[yellow]warning: {eff.problem}[/yellow]")
    if eff.source == "unknown":
        console.print("[dim]frontmatter default not checked (voxxy project root not found)[/dim]")


def persona(
    name: str = typer.Argument(..., metavar="VOICE", help="Voice slug."),
    persona_arg: str | None = typer.Argument(
        None, metavar="[PERSONA]",
        help="Persona id to associate, or 'none' for explicitly no persona. Omit to show.",
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the voxxy-level association (back to frontmatter defaults)."),
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump JSON."),
) -> None:
    """Show or set which persona a voice speaks as.

    The association is stored on the voxxy server, so it follows the voice to
    every consumer (Hermes TTS enrichment, `voxxy speak --persona auto`).
    With no voxxy-level record, consumers fall back to the persona file's own
    `voices:` frontmatter; 'none' suppresses that fallback.

    Examples:

      voxxy voice persona peter                  # show current + effective
      voxxy voice persona peter peter-griffin    # associate
      voxxy voice persona peter none             # explicitly no persona
      voxxy voice persona peter --clear          # back to frontmatter default
    """
    if clear and persona_arg is not None:
        typer.secho("pass either PERSONA or --clear, not both", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    cfg = load_config()
    client = VoxClient(cfg.default_url)
    plugin, plugin_err = try_load_plugin()

    if persona_arg is None and not clear:
        try:
            v = client.get_voice(name)
        except VoxNotFound:
            typer.secho(f"voice '{name}' not found", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=4)
        except VoxUnreachable as exc:
            typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=3)
        _show_persona(name, v.persona, effective_persona(name, v.persona, plugin), as_json)
        return

    new_value: str | None = None
    if persona_arg is not None:
        new_value = persona_arg.strip()
        if new_value.lower() == NONE_SENTINEL:
            new_value = NONE_SENTINEL
        elif not PERSONA_ID_RE.match(new_value):
            typer.secho(
                f"invalid persona id '{persona_arg}': must match {PERSONA_ID_RE.pattern}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=5)
        elif plugin is None:
            typer.secho(
                f"warning: cannot check '{new_value}' against bundled personas ({plugin_err}); setting anyway",
                fg=typer.colors.YELLOW, err=True,
            )
        else:
            known = [persona_id(p) for p in plugin.list_personas()]
            if new_value not in known:
                typer.secho(
                    f"warning: no bundled persona '{new_value}' (available: {', '.join(known) or '-'}); setting anyway",
                    fg=typer.colors.YELLOW, err=True,
                )

    try:
        v = client.set_voice_persona(name, new_value)
    except VoxNotFound:
        typer.secho(
            f"voice '{name}' not found (or the server predates persona support)",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=4)
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)
    except VoxValidationError as exc:
        typer.secho(f"server rejected persona: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5)

    _show_persona(name, v.persona, effective_persona(name, v.persona, plugin), as_json)


def add(
    path: Path = typer.Argument(..., help="Source audio file path."),
    name: str | None = typer.Option(None, "-n", "--name", help="Slug (a-z0-9-); auto-prompted if missing."),
    display_name: str | None = typer.Option(None, "-d", "--display-name"),
    tags: str | None = typer.Option(None, "-t", "--tags", help="Comma-separated."),
    engines: str = typer.Option("voxcpm,vibevoice", "-e", "--engine", help="Comma-separated list of engines to populate refs for."),
    trim_seconds: float = typer.Option(
        DEFAULT_TRIM_SECONDS, "-T", "--trim-seconds",
        help="Seconds of reference audio to keep; matches the server cap.",
    ),
    sample_rate: int = typer.Option(24000, "-R", "--sample-rate"),
    no_prompt: bool = typer.Option(False, "-N", "--no-prompt", help="Skip interactive prompts; --name required."),
    prompt_text: str | None = typer.Option(
        None, "--prompt-text",
        help="Exact transcript of the uploaded audio. Switches voxcpm to continuation mode "
             "(the biggest cloning-quality lever). Must match the clip word for word.",
    ),
    auto: bool = typer.Option(
        False, "--auto",
        help="Transcribe, pick the cleanest 10-15 s window, export it cleanly and set prompt_text "
             "automatically (needs the clone extra).",
    ),
    start: float | None = typer.Option(None, "--start", help="Clip start in seconds (clean export; transcript is automatic)."),
    end: float | None = typer.Option(None, "--end", help="Clip end in seconds (default: start + --max-seconds)."),
    min_seconds: float = typer.Option(refclip.DEFAULT_MIN_SECONDS, "--min-seconds", help="With --auto: shortest clip."),
    max_seconds: float = typer.Option(refclip.DEFAULT_MAX_SECONDS, "--max-seconds", help="With --auto/--start: longest clip."),
) -> None:
    """Upload an audio file as a new voice profile.

    Plain `voice add FILE` keeps its original behaviour (first --trim-seconds of
    audio, no transcript). The new flags are the high-quality path:

    \b
      --prompt-text TEXT   you already have the exact transcript
      --auto               pick the cleanest window and transcribe it for you
      --start S [--end E]  you pick the window, the transcript is automatic

    Examples:

      voxxy voice add talk.mp3 --auto --name brian
      voxxy voice add talk.mp3 --start 12.6 --end 26.1 --name brian
      voxxy voice add clip.wav --name brian --prompt-text "exact words spoken in the clip"
    """
    # 1. Verify source path exists and is readable.
    if not path.exists():
        typer.secho(f"not found: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    try:
        path.open("rb").close()
    except OSError as exc:
        typer.secho(f"cannot read {path}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # 2. Probe source audio.
    try:
        src_info = probe(path)
    except FfmpegMissing as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except AudioProbeError as exc:
        typer.secho(f"probe failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # 3. Print source info.
    console.print(
        f"probing: [cyan]{src_info.sample_rate} Hz[/cyan], "
        f"[cyan]{src_info.channels}ch[/cyan], "
        f"[cyan]{src_info.duration:.1f}s[/cyan]"
    )

    # 4. Preprocess to temp WAV.
    tmp_path: str | None = None
    clean_export = auto or start is not None or end is not None
    if clean_export and (min_seconds <= 0 or max_seconds < min_seconds):
        typer.secho("need 0 < --min-seconds <= --max-seconds", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_fh:
            tmp_path = tmp_fh.name

        if clean_export:
            # High-quality path: a sentence-aligned, cleaned window plus its exact
            # transcript. Replaces the plain trim below.
            prompt_text = _clean_reference(
                path, Path(tmp_path), src_info.duration, auto=auto, start=start, end=end,
                min_seconds=min_seconds, max_seconds=max_seconds, prompt_text=prompt_text,
            )
        else:
            try:
                preprocess(
                    src=path,
                    dst=Path(tmp_path),
                    sample_rate=sample_rate,
                    channels=1,
                    trim_seconds=trim_seconds,
                )
            except FfmpegMissing as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            except RuntimeError as exc:
                typer.secho(f"preprocessing failed: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            if prompt_text:
                console.print(
                    "[dim]prompt_text is stored as given; it must be the exact words in the uploaded "
                    f"first {trim_seconds:g}s or continuation mode will drift[/dim]"
                )

        # 5. Print preprocessed info.
        try:
            dst_info = probe(Path(tmp_path))
            console.print(
                f"preprocessed [dim]→[/dim] [green]{dst_info.sample_rate} Hz[/green], "
                f"[green]{dst_info.channels}ch[/green], "
                f"[green]{dst_info.duration:.1f}s[/green]"
            )
        except (FfmpegMissing, AudioProbeError):
            console.print(f"preprocessed [dim]→[/dim] [green]{sample_rate} Hz, 1ch, {trim_seconds:.1f}s (approx)[/green]")

        # 6. Auto-detect interactive.
        is_interactive = sys.stdin.isatty() and not no_prompt

        # 7. Gather metadata.
        # --- name ---
        if name is None:
            if is_interactive:
                name = questionary.text(
                    "Voice name (slug)",
                    validate=lambda v: bool(_SLUG_RE.match(v)) or "Must match a-z0-9-",
                ).ask()
                if name is None:
                    # User cancelled (Ctrl-C)
                    raise typer.Exit(code=1)
            else:
                typer.secho("missing --name (required in --no-prompt mode)", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=5)

        if not _SLUG_RE.match(name):
            typer.secho(f"invalid slug '{name}': must match ^[a-z0-9-]+$", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=5)

        # --- display_name ---
        default_display = name.title()
        if display_name is None:
            if is_interactive:
                display_name = questionary.text(
                    "Display name",
                    default=default_display,
                ).ask()
                if display_name is None:
                    raise typer.Exit(code=1)
            else:
                display_name = default_display

        # --- tags ---
        tag_list: list[str] = []
        if tags is None:
            if is_interactive:
                tags_input = questionary.text(
                    "Tags (comma-separated, optional)",
                    default="",
                ).ask()
                if tags_input is None:
                    raise typer.Exit(code=1)
                tag_list = [t.strip() for t in tags_input.split(",") if t.strip()]
            # else: leave tag_list empty
        else:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]

        # --- engines hint ---
        console.print(
            f"engines: [dim]{engines}[/dim] "
            f"[dim](vibevoice_ref_path auto-populated by server)[/dim]"
        )

        # 8. POST to server.
        cfg = load_config()
        try:
            voice = VoxClient(cfg.default_url).create_voice(
                name=name,
                display_name=display_name,
                audio_path=Path(tmp_path),
                tags=tag_list if tag_list else None,
                prompt_text=prompt_text.strip() if prompt_text and prompt_text.strip() else None,
            )
        except VoxUnreachable as exc:
            typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=3)
        except VoxValidationError as exc:
            typer.secho(f"server rejected upload: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        # 9. Render result panel.
        body = "\n".join(
            [
                f"[bold]display_name[/bold]:  {voice.display_name}",
                f"[bold]duration_s[/bold]:    {voice.duration_s:.2f}",
                f"[bold]tags[/bold]:          {', '.join(voice.tags) if voice.tags else '-'}",
                f"[bold]vibevoice_ref[/bold]: {voice.vibevoice_ref_path or '(auto-populated from wav_path)'}",
                f"[bold]engines[/bold]:       {engines}",
                f"[bold]prompt_text[/bold]:   {voice.prompt_text or '-'}",
            ]
        )
        console.print(Panel(body, title=f"[green]created: {voice.name}[/green]", border_style="green"))

    finally:
        # 10. Always clean up the temp file.
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


def _clean_reference(
    src: Path, dst: Path, source_duration: float, *, auto: bool, start: float | None, end: float | None,
    min_seconds: float, max_seconds: float, prompt_text: str | None,
) -> str:
    """Write the cleaned window to ``dst`` and return the prompt_text to upload.

    --auto analyses the whole file and takes the best window; --start/--end export an
    explicit span and only transcribe that span (cheap). An explicit --prompt-text
    always wins over the automatic transcript.
    """
    if auto:
        analysis = voice_clone.analyze_or_exit(src, min_seconds=min_seconds, max_seconds=max_seconds)
        best = analysis.top(1)
        if not best:
            typer.secho(
                f"no clean {min_seconds:g}-{max_seconds:g}s window found. See `voxxy voice candidates {src}` "
                "for why, or choose a span with --start/--end.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=1)
        marker = best[0]
        console.print(
            f"auto: [cyan]{marker.start:.2f}-{marker.end:.2f}s[/cyan] "
            f"(score {marker.score:.2f}) [dim]{marker.text[:90]}[/dim]"
        )
        lo, hi, fallback = marker.start, marker.end, marker.text
    else:
        lo = start if start is not None else 0.0
        hi = end if end is not None else lo + max_seconds
        if source_duration > 0:
            hi = min(hi, source_duration)
        if lo < 0 or hi <= lo:
            typer.secho(f"invalid window {lo:g}-{hi:g}s", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        if hi - lo > 30.0:
            typer.secho(
                f"warning: {hi - lo:.0f}s window; the server keeps ~30s and long clips dilute the transcript benefit",
                fg=typer.colors.YELLOW, err=True,
            )
        fallback = ""
        console.print(f"window: [cyan]{lo:.2f}-{hi:.2f}s[/cyan]")
    with console.status("[cyan]exporting and transcribing the clip[/cyan]"):
        prepared = voice_clone.prepare_clip(src, dst, lo, hi, fallback_text=fallback, prompt_text=prompt_text)
    console.print(f"prompt_text: [dim]{prepared.prompt_text}[/dim]")
    return prepared.prompt_text


def delete(
    name: str = typer.Argument(..., help="Voice slug."),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation."),
) -> None:
    """Delete a voice profile by slug."""
    cfg = load_config()
    client = VoxClient(cfg.default_url)

    # 1. Verify voice exists.
    try:
        voice = client.get_voice(name)
    except VoxNotFound:
        typer.secho(f"voice '{name}' not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    is_interactive = sys.stdin.isatty()

    # 2. Confirmation.
    if not yes:
        if is_interactive:
            tags_str = ", ".join(voice.tags) if voice.tags else "-"
            confirmed = questionary.confirm(
                f"Delete voice '{name}' (duration={voice.duration_s:.1f}s, tags={tags_str})?",
                default=False,
            ).ask()
            if not confirmed:
                console.print("[dim]aborted[/dim]")
                raise typer.Exit(code=0)
        else:
            typer.secho(
                "refusing to delete without --yes in non-interactive mode",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=5)

    # 3. Delete.
    try:
        client.delete_voice(name)
    except VoxNotFound:
        typer.secho(f"voice '{name}' not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    console.print(f"[green]deleted: {name}[/green]")
