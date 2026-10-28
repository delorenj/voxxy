"""``voxxy persona`` — inspect and dry-run the voice-persona plugin's characters.

Personas are markdown files bundled with the Hermes ``voice-persona`` plugin.
This group never reimplements persona logic; it drives the plugin module (see
:mod:`voxxy.persona`). Associating a persona with a voice lives under
``voxxy voice persona``.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from voxxy import clone as cl
from voxxy.client import VoxClient, VoxError, VoxNotFound, VoxUnreachable
from voxxy.config import ProjectNotFound, load_config
from voxxy.llm import CloneLLM
from voxxy.persona import (
    PersonaError,
    PersonaLLM,
    effective_persona,
    load_plugin,
    persona_by_id,
    persona_id,
    rewrite,
)

console = Console()


def register(persona_app: typer.Typer) -> None:
    persona_app.command("list")(list_personas)
    persona_app.command("show")(show)
    persona_app.command("rewrite")(rewrite_cmd)
    persona_app.command("path")(path_cmd)
    persona_app.command("generate")(generate)


def _plugin_or_exit() -> Any:
    try:
        return load_plugin()
    except ProjectNotFound as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except PersonaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _persona_or_exit(plugin: Any, slug: str) -> Any:
    persona = persona_by_id(plugin, slug)
    if persona is None:
        available = ", ".join(persona_id(p) for p in plugin.list_personas()) or "(none)"
        typer.secho(
            f"no bundled persona '{slug}'. available: {available}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=4)
    return persona


def list_personas(
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump raw JSON."),
) -> None:
    """List bundled personas and which voices map to each."""
    plugin = _plugin_or_exit()
    personas = plugin.list_personas()

    # Voxxy records win over frontmatter, so fetch them to label overrides. A
    # down or old server just means frontmatter-only output.
    records: dict[str, Optional[str]] = {}
    try:
        records = {v.name: v.persona for v in VoxClient(load_config().default_url).list_voices()}
    except VoxError as exc:
        typer.secho(f"(voxxy voice records unavailable: {exc})", fg=typer.colors.YELLOW, err=True)

    rows = []
    for p in personas:
        pid = persona_id(p)
        voices = [{"voice": name, "source": "voxxy"} for name, rec in sorted(records.items()) if rec == pid]
        for name in getattr(p, "voices", ()):
            rec = records.get(name)
            entry = {"voice": name, "source": "frontmatter"}
            if rec is not None and rec != pid:
                # A voxxy record (another persona, or 'none') shadows this default.
                entry["overridden_by"] = rec
            voices.append(entry)
        rows.append({
            "id": pid,
            "name": getattr(p, "name", pid),
            "description": getattr(p, "description", ""),
            "temperature": getattr(p, "temperature", None),
            "voices": voices,
        })

    if as_json:
        typer.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        console.print("[dim]no personas bundled[/dim]")
        return

    table = Table(title=f"Personas ({len(rows)})")
    table.add_column("id", style="cyan")
    table.add_column("name")
    table.add_column("temp", justify="right")
    table.add_column("voices")
    table.add_column("description")
    for r in rows:
        labels = []
        for v in r["voices"]:
            label = f"{v['voice']} ({v['source']}"
            if "overridden_by" in v:
                label += f", overridden by {v['overridden_by']}"
            labels.append(label + ")")
        table.add_row(
            r["id"],
            r["name"],
            "-" if r["temperature"] is None else f"{r['temperature']:g}",
            "\n".join(labels) or "-",
            r["description"] or "-",
        )
    console.print(table)


def show(
    persona: str = typer.Argument(..., metavar="ID", help="Persona id (file stem, see 'persona list')."),
    raw: bool = typer.Option(False, "--raw", help="Print the persona file verbatim, frontmatter included."),
) -> None:
    """Render a persona's markdown."""
    plugin = _plugin_or_exit()
    p = _persona_or_exit(plugin, persona)
    path = getattr(p, "path", None)
    if raw:
        if path is None:
            typer.secho("persona has no backing file", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        sys.stdout.write(Path(path).read_text(encoding="utf-8"))
        return
    meta = [f"[bold]id[/bold]:          {persona_id(p)}"]
    meta.append(f"[bold]name[/bold]:        {p.name}")
    meta.append(f"[bold]voices[/bold]:      {', '.join(p.voices) if p.voices else '-'} [dim](frontmatter)[/dim]")
    meta.append(f"[bold]temperature[/bold]: {'-' if p.temperature is None else f'{p.temperature:g}'}")
    if p.description:
        meta.append(f"[bold]description[/bold]: {p.description}")
    console.print(Panel("\n".join(meta), title=f"persona: {persona_id(p)}", border_style="cyan"))
    console.print(Markdown(p.body))


def rewrite_cmd(
    persona: Optional[str] = typer.Argument(
        None, metavar="[ID]", help="Persona id. Omit when using --voice."
    ),
    text: Optional[list[str]] = typer.Argument(
        None, help="Text to rewrite. If omitted and stdin is a pipe, reads from stdin."
    ),
    voice: Optional[str] = typer.Option(
        None, "-v", "--voice", help="Use this voice's effective persona instead of an explicit ID."
    ),
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump the full result as JSON."),
) -> None:
    """Dry-run an in-character rewrite (no audio).

    Prints the rewrite. Exits non-zero with the reason when the plugin's
    validation (length, dropped numbers, ...) or the LLM call rejects it.

    Examples:

      voxxy persona rewrite peter-griffin "The deploy finished in 42 seconds."
      echo "disk is 91% full" | voxxy persona rewrite george-carlin
      voxxy persona rewrite --voice peter "Build 17 passed."
    """
    # With --voice there is no ID, so Typer's first positional is really text.
    words = list(text or [])
    if voice and persona is not None:
        words.insert(0, persona)
        persona = None
    if not voice and persona is None:
        typer.secho("give a persona ID or --voice (see 'voxxy persona list')", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if voice and persona:
        typer.secho("pass either an ID or --voice, not both", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if words:
        source_text = " ".join(words).strip()
    elif not sys.stdin.isatty():
        source_text = sys.stdin.read().strip()
    else:
        typer.secho("no text (pass as args or pipe via stdin)", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if not source_text:
        typer.secho("empty text", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    plugin = _plugin_or_exit()
    if voice:
        chosen = _persona_for_voice_or_exit(plugin, voice)
    else:
        chosen = _persona_or_exit(plugin, persona)

    result = rewrite(plugin, chosen, source_text, PersonaLLM())

    if as_json:
        typer.echo(_json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    elif result.accepted:
        typer.echo(result.rewrite)
    else:
        typer.secho(f"rewrite rejected: {result.reason}", fg=typer.colors.RED, err=True)
    if not result.accepted:
        raise typer.Exit(code=1)


def _persona_for_voice_or_exit(plugin: Any, voice: str) -> Any:
    record: Optional[str] = None
    try:
        record = VoxClient(load_config().default_url).get_voice(voice).persona
    except VoxNotFound:
        typer.secho(f"voice '{voice}' not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except VoxUnreachable as exc:
        # Frontmatter mapping works offline; a voxxy record just can't be seen.
        typer.secho(f"(voxxy unreachable, using frontmatter only: {exc})", fg=typer.colors.YELLOW, err=True)
    eff = effective_persona(voice, record, plugin)
    if eff.persona is None:
        why = eff.problem or ("explicitly set to none" if eff.explicit_none else "no persona is associated")
        typer.secho(f"voice '{voice}' has no usable persona ({why})", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    return eff.persona


def path_cmd() -> None:
    """Print the directory holding the bundled persona files."""
    plugin = _plugin_or_exit()
    typer.echo(str(plugin.PERSONAS_DIR))



def generate(
    persona: str = typer.Argument(..., metavar="PERSONA-ID", help="New persona id: lowercase a-z0-9 and dashes (brian-griffin)."),
    character: str = typer.Option(..., "-c", "--character", help='Who it is, e.g. "Brian Griffin from Family Guy".'),
    voice: Optional[str] = typer.Option(
        None, "-v", "--voice",
        help="Voice slug for the frontmatter `voices:` default; also associates it if the voice exists on the server.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing persona file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the generated persona instead of writing it."),
) -> None:
    """Author a persona file with the LLM (the same generator `voice clone` uses).

    Web-searches the character for facts, then writes plugins/voice-persona/personas/<id>.md
    following the bundled personas' structure: frontmatter, character bible,
    "Rules for rewriting" that forbid dropping facts, before/after examples. The file is
    validated through the plugin's own loader before it is written, and an existing
    persona is never overwritten without --force.

    Examples:

      voxxy persona generate brian-griffin --character "Brian Griffin from Family Guy" --voice brian
      voxxy persona generate stewie-griffin -c "Stewie Griffin, Family Guy" --dry-run
    """
    if not cl.PERSONA_ID_RE.match(persona):
        typer.secho(f"invalid persona id '{persona}': use lowercase a-z0-9 and dashes", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5)
    if voice is not None and not cl.SLUG_RE.match(voice):
        typer.secho(f"invalid voice slug '{voice}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5)
    plugin = _plugin_or_exit()
    target = plugin.PERSONAS_DIR / f"{persona}.md"
    if target.exists() and not force and not dry_run:
        typer.secho(f"{target} already exists (pass --force to overwrite)", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5)

    llm = CloneLLM(load_config())
    with console.status("[cyan]researching the character[/cyan]"):
        facts = cl.web_search(character)
    try:
        with console.status("[cyan]writing the persona[/cyan]"):
            markdown = cl.generate_persona(
                llm, plugin, character=character, voice=voice, facts=facts,
                display_name=character.split(",")[0].split(" from ")[0].strip() or character,
            )
    except cl.CloneError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if dry_run:
        typer.echo(markdown)
        return
    try:
        path = cl.write_persona(plugin, persona, markdown, force=force)
    except cl.CloneError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5)
    console.print(f"[green]wrote[/green] {path}")

    if voice:
        try:
            VoxClient(load_config().default_url).set_voice_persona(voice, persona)
            console.print(f"[green]associated[/green] voice '{voice}' -> {persona}")
        except VoxNotFound:
            console.print(f"[yellow]voice '{voice}' does not exist yet; the frontmatter default will apply once it does[/yellow]")
        except VoxError as exc:
            console.print(f"[yellow]could not associate with the voxxy server: {exc}[/yellow]")
