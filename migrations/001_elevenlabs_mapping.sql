-- 001_elevenlabs_mapping.sql
--
-- Adds a per-voice ElevenLabs voice id so the fallback engine can stay on
-- character when VoxCPM2 is unavailable. NULL = use the global default
-- (ELEVENLABS_DEFAULT_VOICE env var).
--
-- Apply against the host postgres `vox` database:
--   psql -h localhost -U "$DEFAULT_USERNAME" -d vox -f migrations/001_elevenlabs_mapping.sql

ALTER TABLE voices
    ADD COLUMN IF NOT EXISTS elevenlabs_voice_id text;
