-- vox: voice profiles for VoxCPM2 TTS service
-- Schema is intentionally flat; audio bytes live on disk under ./voices/,
-- only metadata + pointer live here.

CREATE TABLE IF NOT EXISTS voices (
    -- short slug used as the API key (e.g. "rick", "morty")
    name         text        PRIMARY KEY,
    -- human-readable display name
    display_name text        NOT NULL,
    -- relative path under VOX_VOICES_DIR (e.g. "rick.wav")
    wav_path     text        NOT NULL,
    -- original source path for provenance (optional)
    source_path  text,
    -- trimmed duration in seconds
    duration_s   real        NOT NULL,
    -- optional: default transcript for Ultimate Cloning mode
    prompt_text  text,
    -- freeform tags for future filtering (e.g. {"rick", "cartoon", "male"})
    tags         text[]      NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS voices_tags_gin ON voices USING GIN (tags);
