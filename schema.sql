-- BitLink schema — migrated from the "BitLink Client Mappings" Google Spreadsheet.
-- Every Google Sheets row_idx is replaced by a real surrogate primary key.
-- Google Sheets is untyped; these types are explicit and deliberate.

CREATE TABLE IF NOT EXISTS client_mappings (
    id                BIGSERIAL PRIMARY KEY,
    platform          TEXT        NOT NULL,
    client_name       TEXT        NOT NULL DEFAULT '',
    external_id       TEXT        NOT NULL DEFAULT '',
    slack_channel_id  TEXT        NOT NULL DEFAULT '',
    paused            BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lookups are always "match this platform" and "resolve this slack channel".
CREATE INDEX IF NOT EXISTS idx_client_mappings_platform ON client_mappings (LOWER(platform));
CREATE INDEX IF NOT EXISTS idx_client_mappings_channel  ON client_mappings (slack_channel_id);
CREATE INDEX IF NOT EXISTS idx_client_mappings_extid    ON client_mappings (external_id);

CREATE TABLE IF NOT EXISTS sales_leads (
    id                  BIGSERIAL PRIMARY KEY,
    client_name         TEXT        NOT NULL DEFAULT '',
    platform            TEXT        NOT NULL DEFAULT 'whatsapp',
    external_id         TEXT        NOT NULL DEFAULT '',
    slack_channel_id    TEXT        NOT NULL DEFAULT '',
    tag                 TEXT        NOT NULL DEFAULT 'sales',
    owner_slack_id      TEXT        NOT NULL DEFAULT '',
    stage               TEXT        NOT NULL DEFAULT 'new',
    lead_score          TEXT        NOT NULL DEFAULT 'warm',
    source              TEXT        NOT NULL DEFAULT 'wa',
    geo                 TEXT        NOT NULL DEFAULT '',
    language            TEXT        NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ,
    last_client_msg_at  TIMESTAMPTZ,
    paused              BOOLEAN     NOT NULL DEFAULT FALSE,
    deal_value          NUMERIC(14, 2) NOT NULL DEFAULT 0,
    closed_at           TIMESTAMPTZ
);

-- find_existing_open_lead() and the control panel filter on these every request.
CREATE INDEX IF NOT EXISTS idx_sales_leads_extid   ON sales_leads (external_id);
CREATE INDEX IF NOT EXISTS idx_sales_leads_channel ON sales_leads (slack_channel_id);
CREATE INDEX IF NOT EXISTS idx_sales_leads_owner   ON sales_leads (owner_slack_id);
CREATE INDEX IF NOT EXISTS idx_sales_leads_stage   ON sales_leads (stage);

CREATE TABLE IF NOT EXISTS sales_pool (
    id            BIGSERIAL PRIMARY KEY,
    slack_user_id TEXT    NOT NULL,
    name          TEXT    NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- is_external_id_blocked() also does prefix/endswith matching on digits, so the
-- normalized form is precomputed and indexed here rather than in Python.
CREATE TABLE IF NOT EXISTS blocklist (
    id                BIGSERIAL PRIMARY KEY,
    external_id       TEXT        NOT NULL,
    numeric_part      TEXT        NOT NULL DEFAULT '',
    digits_only       TEXT        NOT NULL DEFAULT '',
    reason            TEXT        NOT NULL DEFAULT '',
    added_at          TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_blocklist_extid   ON blocklist (external_id);
CREATE INDEX IF NOT EXISTS idx_blocklist_numeric       ON blocklist (numeric_part);
CREATE INDEX IF NOT EXISTS idx_blocklist_digits        ON blocklist (digits_only);

CREATE TABLE IF NOT EXISTS project_managers (
    id            BIGSERIAL PRIMARY KEY,
    slack_user_id TEXT    NOT NULL,
    name          TEXT    NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS snippets (
    id         BIGSERIAL PRIMARY KEY,
    title      TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The live Settings sheet contains duplicate keys; the old code was last-wins.
-- That behaviour is preserved explicitly by the ON CONFLICT clause in the
-- migration script rather than being left to chance.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Passwords are bcrypt hashes. Plaintext from the old sheet is never stored.
CREATE TABLE IF NOT EXISTS dashboard_users (
    id            TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
