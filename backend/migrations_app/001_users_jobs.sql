-- Users and job history in the app database. {app} is APP_DB_SCHEMA.
-- gen_random_uuid() is built into Postgres 13 and later.

CREATE SCHEMA IF NOT EXISTS {app};

-- Keeps modified_at honest: a column DEFAULT only fires on INSERT.
CREATE OR REPLACE FUNCTION {app}.touch_modified_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.modified_at := now();
    RETURN NEW;
END
$$;

-- People who can sign in. Never hard-deleted: is_active = false instead, so job
-- history keeps pointing at whoever ran each job.
CREATE TABLE IF NOT EXISTS {app}.users (
    user_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_name            varchar(100) NOT NULL,
    user_email_id        varchar(255) NOT NULL,           -- stored lower-cased
    password_hash        varchar(255) NOT NULL,           -- pbkdf2_sha256$<iterations>$<salt>$<hash>
    is_admin             boolean NOT NULL DEFAULT false,
    user_phone_number    varchar(20),
    is_active            boolean NOT NULL DEFAULT true,
    must_change_password boolean NOT NULL DEFAULT false,  -- set by an admin-issued password
    last_login_at        timestamptz,
    expiry_date          timestamptz,                     -- no sign-in from this moment on
    created_at           timestamptz NOT NULL DEFAULT now(),
    modified_at          timestamptz NOT NULL DEFAULT now(),
    created_by           uuid REFERENCES {app}.users (user_id) ON DELETE SET NULL,
    modified_by          uuid REFERENCES {app}.users (user_id) ON DELETE SET NULL
);

-- One account per address, whatever its capitalisation.
CREATE UNIQUE INDEX IF NOT EXISTS users_user_email_id_key ON {app}.users (lower(user_email_id));

-- A sign-in only stamps last_login_at; that is not a modification of the account.
DROP TRIGGER IF EXISTS users_touch_modified_at ON {app}.users;
CREATE TRIGGER users_touch_modified_at BEFORE UPDATE ON {app}.users FOR EACH ROW
    WHEN (OLD.last_login_at IS NOT DISTINCT FROM NEW.last_login_at)
    EXECUTE FUNCTION {app}.touch_modified_at();

-- Job history: one row per output of a run, every row of a run sharing its job_id --
-- the id in each output file name. A row with no output_name stands for the run while
-- it is queued or running, and stays when it ends without outputs (failed, cancelled).
CREATE TABLE IF NOT EXISTS {app}.jobs (
    job_row_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         uuid NOT NULL,
    job_type       varchar(20) NOT NULL
                   CHECK (job_type IN ('clean', 'bronze_ingest', 'silver_load')),
    status         varchar(20) NOT NULL
                   CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped', 'interrupted')),
    batch_id       uuid,           -- files cleaned together
    source_job_id  uuid,           -- lineage: the clean run that bronze / silver data came from
    source_file    varchar(255),
    source_sha256  char(64),       -- SHA-256 of the uploaded file
    source_sheets  text[],
    output_name    varchar(255),   -- cleaned table, bronze table, or the bronze table a silver load read
    output_file    varchar(255),
    row_count      integer,
    rows_removed   integer,
    log            text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    ended_at       timestamptz,
    modified_at    timestamptz NOT NULL DEFAULT now(),
    created_by     uuid REFERENCES {app}.users (user_id) ON DELETE SET NULL,
    modified_by    uuid REFERENCES {app}.users (user_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS jobs_job_id ON {app}.jobs (job_id);
CREATE INDEX IF NOT EXISTS jobs_created_at ON {app}.jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_created_by ON {app}.jobs (created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_batch_id ON {app}.jobs (batch_id);
CREATE INDEX IF NOT EXISTS jobs_source_sha256 ON {app}.jobs (source_sha256);
CREATE INDEX IF NOT EXISTS jobs_active ON {app}.jobs (status) WHERE status IN ('queued', 'running');

DROP TRIGGER IF EXISTS jobs_touch_modified_at ON {app}.jobs;
CREATE TRIGGER jobs_touch_modified_at BEFORE UPDATE ON {app}.jobs FOR EACH ROW
    EXECUTE FUNCTION {app}.touch_modified_at();
