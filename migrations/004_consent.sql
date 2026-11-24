-- 004_consent.sql
--
-- Story G5: the consent gate for likeness-making (own-voice recording today, any clone path later).
--   consent_challenges   one random sentence + 6-digit code per request, bound to the caller (principal),
--                        valid 10 minutes, redeemed once (replaces the in-memory store of web app v1).
--   consent_records      one row per attempt, whatever the decision: transcript, phrase/code result,
--                        speaker scores, the decision and its reason, terms version, where the consent
--                        audio is kept. `decision` is accepted | review | rejected, plus `unchecked`
--                        (self edition, check could not run). `review` rows are the operator's queue
--                        (GET /consent/reviews); resolving one sets accepted or rejected, reviewer, note.
--   account_voiceprints  the principal's own-voice fingerprint, fixed by the first accepted own voice;
--                        every later one must match it. principal = first 16 hex of sha256(API key),
--                        or 'open' when auth is off (app/auth.py).
--
-- `embedding` columns hold TitaNet-small's 192 floats (the model id is stored beside them: a voiceprint
-- made by another model is never compared, it sends the next voice to review).
--
-- Apply against the host postgres `vox` database (the app does NOT apply migrations itself):
--   mise run migrate        # applies every migrations/*.sql; all of them are idempotent
--
-- Voices tagged `unverified-consent` by web app v1 are retagged by the app at startup (consent-pending in
-- the hosted edition, consent-unchecked-self in the self edition): the migration cannot see VOX_EDITION.
--
-- Rollback:
--   DROP TABLE IF EXISTS account_voiceprints; DROP TABLE IF EXISTS consent_records;
--   DROP TABLE IF EXISTS consent_challenges;

CREATE TABLE IF NOT EXISTS consent_challenges (
    id          text PRIMARY KEY,
    principal   text        NOT NULL,
    sentence    text        NOT NULL,
    code        text        NOT NULL,
    issued_at   timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    redeemed_at timestamptz
);
CREATE INDEX IF NOT EXISTS consent_challenges_open_idx
    ON consent_challenges (expires_at) WHERE redeemed_at IS NULL;

CREATE TABLE IF NOT EXISTS consent_records (
    id                       text PRIMARY KEY,
    voice                    text        NOT NULL,
    principal                text        NOT NULL,
    challenge_id             text        NOT NULL,
    sentence                 text        NOT NULL,
    code                     text        NOT NULL,
    transcript               text,
    phrase_score             real,
    code_ok                  boolean,
    speaker_score_reference  real,
    speaker_score_account    real,
    decision                 text        NOT NULL
        CHECK (decision IN ('accepted', 'review', 'rejected', 'unchecked')),
    reason                   text,
    terms_version            text,
    consent_audio_path       text,
    reference_audio_path     text,
    embedding_model          text,
    embedding                double precision[],
    edition                  text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    reviewed_at              timestamptz,
    reviewer                 text,
    review_note              text
);
CREATE INDEX IF NOT EXISTS consent_records_review_idx
    ON consent_records (created_at) WHERE decision = 'review';
CREATE INDEX IF NOT EXISTS consent_records_principal_idx ON consent_records (principal);
CREATE INDEX IF NOT EXISTS consent_records_voice_idx ON consent_records (voice);

CREATE TABLE IF NOT EXISTS account_voiceprints (
    principal        text PRIMARY KEY,
    embedding        double precision[] NOT NULL,
    embedding_model  text        NOT NULL,
    source_record    text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
