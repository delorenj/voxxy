"""Voice profile CRUD over asyncpg.

The `voices` table stores metadata and a relative filename under
``VOX_VOICES_DIR``. Audio bytes live on disk, not in postgres.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import asyncpg

VOICES_DIR = Path(os.environ.get("VOX_VOICES_DIR", "/data/voices"))


@dataclass(slots=True)
class Voice:
    name: str
    display_name: str
    wav_path: str  # relative to VOICES_DIR
    duration_s: float
    prompt_text: Optional[str]
    tags: list[str]
    # Optional ElevenLabs voice id for fallback synthesis. When None, the
    # ElevenLabs engine uses its configured default voice.
    elevenlabs_voice_id: Optional[str] = None

    @property
    def abs_path(self) -> Path:
        return VOICES_DIR / self.wav_path


class VoiceRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "VoiceRepo":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def get(self, name: str) -> Optional[Voice]:
        row = await self._pool.fetchrow(
            """SELECT name, display_name, wav_path, duration_s, prompt_text, tags,
                      elevenlabs_voice_id
               FROM voices WHERE name = $1""",
            name,
        )
        return _row_to_voice(row) if row else None

    async def list(self) -> list[Voice]:
        rows = await self._pool.fetch(
            """SELECT name, display_name, wav_path, duration_s, prompt_text, tags,
                      elevenlabs_voice_id
               FROM voices ORDER BY name"""
        )
        return [_row_to_voice(r) for r in rows]

    async def upsert(
        self,
        *,
        name: str,
        display_name: str,
        wav_path: str,
        duration_s: float,
        source_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Voice:
        row = await self._pool.fetchrow(
            """INSERT INTO voices (name, display_name, wav_path, source_path,
                                   duration_s, prompt_text, tags)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (name) DO UPDATE SET
                   display_name = EXCLUDED.display_name,
                   wav_path     = EXCLUDED.wav_path,
                   source_path  = EXCLUDED.source_path,
                   duration_s   = EXCLUDED.duration_s,
                   prompt_text  = EXCLUDED.prompt_text,
                   tags         = EXCLUDED.tags,
                   updated_at   = now()
               RETURNING name, display_name, wav_path, duration_s, prompt_text, tags""",
            name, display_name, wav_path, source_path,
            duration_s, prompt_text, tags or [],
        )
        return _row_to_voice(row)

    async def delete(self, name: str) -> bool:
        res = await self._pool.execute("DELETE FROM voices WHERE name = $1", name)
        return res.endswith(" 1")


def _row_to_voice(row) -> Voice:
    return Voice(
        name=row["name"],
        display_name=row["display_name"],
        wav_path=row["wav_path"],
        duration_s=float(row["duration_s"]),
        prompt_text=row["prompt_text"],
        tags=list(row["tags"] or []),
        elevenlabs_voice_id=row["elevenlabs_voice_id"],
    )
