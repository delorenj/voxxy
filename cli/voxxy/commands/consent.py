"""``voxxy consent`` — the operator's side of the G5 consent gate.

Own-voice recordings that the consent check could not decide (a voice-match score between the reject
and accept bars, or an ASR doubt) wait in a review queue. These commands list the queue, let you listen
to what was recorded, and approve or reject each one. They need the operator key (``VOX_OPERATOR_KEY``,
or ``VOX_OPERATOR_KEY_REF`` pointing at an ``op://`` reference), which is not the ordinary API key.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Callable, Optional, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from voxxy.client import (
    ConsentReview,
    VoxClient,
    VoxError,
    VoxNotFound,
    VoxServerError,
    VoxUnreachable,
    VoxValidationError,
    env_operator_key,
)
from voxxy.config import load_config

console = Console()
T = TypeVar("T")

NO_KEY_HELP = (
    "no operator key: set VOX_OPERATOR_KEY, or VOX_OPERATOR_KEY_REF to an op:// reference "
    "(the review routes do not take the ordinary API key)."
)


def register(consent_app: typer.Typer) -> None:
    consent_app.command("reviews")(reviews)
    consent_app.command("approve")(approve)
    consent_app.command("reject")(reject)
    consent_app.command("audio")(audio)


def _client() -> VoxClient:
    key = env_operator_key()
    if not key:
        typer.secho(NO_KEY_HELP, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return VoxClient(load_config().default_url, api_key=key)


def _call(fn: Callable[[], T]) -> T:
    """Run a server call and turn its failures into one plain line and an exit code."""
    try:
        return fn()
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)
    except VoxNotFound as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except VoxServerError as exc:
        text = str(exc)
        if "no operator key configured" in text:
            typer.secho("the server has no operator key configured (VOX_OPERATOR_KEY is unset there), "
                        "so the review routes are switched off.", fg=typer.colors.RED, err=True)
        else:
            typer.secho(text, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except VoxValidationError as exc:
        text = str(exc)
        if "operator key" in text or "→ 403" in text:
            typer.secho("the server did not accept that as its operator key.", fg=typer.colors.RED, err=True)
        else:
            typer.secho(text, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5)
    except VoxError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _full_id(client: VoxClient, given: str) -> str:
    """A record id as typed: the full id, or any unique prefix of one."""
    if len(given) >= 32:
        return given
    matches = [r.id for r in _call(lambda: client.list_consent_reviews("all")) if r.id.startswith(given)]
    if len(matches) != 1:
        typer.secho(f"{given!r} matches {len(matches)} consent records; give more of the id.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4 if not matches else 5)
    return matches[0]


def _score(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def reviews(
    show_all: bool = typer.Option(False, "--all", help="Every consent record, not just those waiting for review."),
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump raw JSON."),
) -> None:
    """List own-voice recordings waiting for a person to approve or reject them."""
    items = _call(lambda: _client().list_consent_reviews("all" if show_all else "review"))
    if as_json:
        typer.echo(_json.dumps([i.model_dump() for i in items], indent=2))
        return
    if not items:
        console.print("[dim]nothing is waiting for review[/dim]" if not show_all else "[dim]no consent records[/dim]")
        return
    table = Table(title=f"Consent {'records' if show_all else 'reviews'} ({len(items)})")
    for col in ("id", "voice", "decision", "phrase", "code", "ref", "acct", "why"):
        table.add_column(col, style="cyan" if col == "id" else None)
    for i in items:
        table.add_row(
            i.id[:12], i.voice, i.decision, _score(i.phrase_score),
            "-" if i.code_ok is None else ("ok" if i.code_ok else "off"),
            _score(i.speaker_score_reference), _score(i.speaker_score_account), (i.reason or "")[:60])
    console.print(table)
    console.print("[dim]ref = match against the longer sample, acct = match against the account's voiceprint. "
                  "Listen with `voxxy consent audio <id>`, decide with `voxxy consent approve|reject <id>`.[/dim]")


def _resolve(record_id: str, decision: str, note: Optional[str]) -> ConsentReview:
    client = _client()
    return _call(lambda: client.resolve_consent_review(_full_id(client, record_id), decision, note))


def approve(
    record_id: str = typer.Argument(..., help="Consent record id (or a unique prefix)."),
    note: Optional[str] = typer.Option(None, "-n", "--note", help="Why, for the record."),
) -> None:
    """Approve a queued recording: the voice becomes verified and can speak."""
    done = _resolve(record_id, "approve", note)
    console.print(f"[green]approved[/green] {done.voice} ({done.id[:12]}): the voice can speak now.")


def reject(
    record_id: str = typer.Argument(..., help="Consent record id (or a unique prefix)."),
    note: Optional[str] = typer.Option(None, "-n", "--note", help="Why, for the record."),
) -> None:
    """Reject a queued recording: the voice is deleted; the record and its audio are kept."""
    done = _resolve(record_id, "reject", note)
    console.print(f"[yellow]rejected[/yellow] {done.voice} ({done.id[:12]}): the voice was deleted, the record is kept.")


def audio(
    record_id: str = typer.Argument(..., help="Consent record id (or a unique prefix)."),
    reference: bool = typer.Option(False, "--reference", help="The longer sample instead of the consent recording."),
    out: Optional[Path] = typer.Option(None, "-o", "--out", help="Where to write the WAV (default: ./<id>.wav)."),
) -> None:
    """Download the recording behind a consent record so you can listen to it."""
    client = _client()
    full = _full_id(client, record_id)
    data = _call(lambda: client.consent_review_audio(full, "reference" if reference else "consent"))
    dest = out or Path(f"{full[:12]}{'.reference' if reference else ''}.wav")
    dest.write_bytes(data)
    console.print(f"wrote {dest} ({len(data) // 1024} KiB)")
