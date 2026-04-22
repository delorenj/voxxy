-- 002_engine_mapping.sql
--
-- Per-engine reference audio and speaker tag for the decoupled engine topology.
-- Each local engine (voxcpm, vibevoice, ...) gets its own optional reference
-- clip path so operators can tune a voice per engine without a global swap.
-- NULL means "fall back to wav_path" for engines that accept the default clip.
--
-- See docs/specs/engine-decoupling.md §4 AC4.
--
-- Apply against the host postgres `vox` database:
--   psql -h localhost -U "$DEFAULT_USERNAME" -d vox -f migrations/002_engine_mapping.sql
--
-- Rollback:
--   ALTER TABLE voices
--       DROP COLUMN IF EXISTS vibevoice_ref_path,
--       DROP COLUMN IF EXISTS vibevoice_speaker_tag;

ALTER TABLE voices
    ADD COLUMN IF NOT EXISTS vibevoice_ref_path    text,
    ADD COLUMN IF NOT EXISTS vibevoice_speaker_tag text;
