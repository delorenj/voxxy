-- 003_voice_persona.sql
--
-- Voxxy-level voice -> persona association, used by the Hermes voice-persona plugin and
-- `voxxy speak --persona auto` to rewrite spoken text in a character's voice.
--   NULL    = no association set; consumers fall back to persona frontmatter `voices:`.
--   'none'  = explicitly no persona for this voice (suppresses the frontmatter fallback).
--   <slug>  = persona id, the basename of plugins/voice-persona/personas/<slug>.md.
-- The server validates the slug shape only; it never reads persona files.
--
-- Apply against the host postgres `vox` database:
--   psql -h localhost -U "$DEFAULT_USERNAME" -d vox -f migrations/003_voice_persona.sql
--
-- Rollback:
--   ALTER TABLE voices DROP COLUMN IF EXISTS persona;

ALTER TABLE voices
    ADD COLUMN IF NOT EXISTS persona text;
