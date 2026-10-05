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
    closed_at           TIMESTAMPTZ,
    delivery_handed_off BOOLEAN     NOT NULL DEFAULT FALSE
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

-- 'admin' may read alert_details (technical detail); 'sales' may not. Existing
-- rows land on 'sales' so no account is granted privilege by the migration.
ALTER TABLE dashboard_users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'sales';

-- Project managers are auto-invited into new client channels. The developers
-- table below is deliberately separate: it controls membership of the private
-- error channel only, so the two roles can diverge.
CREATE TABLE IF NOT EXISTS developers (
    id           BIGSERIAL PRIMARY KEY,
    slack_user_id TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sales-facing alerts. Technical detail is never stored in this relation, so
-- the sales API can SELECT * from it without any risk of leaking a traceback.
CREATE TABLE IF NOT EXISTS critical_alerts (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity        TEXT NOT NULL,
    component       TEXT NOT NULL,
    code            TEXT NOT NULL,
    summary_plain   TEXT NOT NULL,
    context         JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint     TEXT NOT NULL UNIQUE,
    occurrences     INT NOT NULL DEFAULT 1,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL DEFAULT 'open',
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ,
    slack_permalink TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_status_created ON critical_alerts (status, created_at DESC);

-- UNIQUE is what makes the dedup upsert in db_store.record_alert work. The DROP
-- clears the plain index an earlier revision of this file created under this
-- name; without it CREATE UNIQUE INDEX ... IF NOT EXISTS would see the name
-- already taken and silently leave the non-unique index in place.
DROP INDEX IF EXISTS idx_alerts_fingerprint;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_fingerprint ON critical_alerts (fingerprint);

-- Kept apart from critical_alerts on purpose: only the admin-gated endpoint
-- reads this table. It is the single place technical detail is persisted.
CREATE TABLE IF NOT EXISTS alert_details (
    alert_id         BIGINT PRIMARY KEY REFERENCES critical_alerts (id) ON DELETE CASCADE,
    technical_detail TEXT NOT NULL,
    traceback        TEXT
);

-- Operational / error alert channels. Critical failures broadcast to all active
-- channels in this relation.
CREATE TABLE IF NOT EXISTS alert_channels (
    id           BIGSERIAL PRIMARY KEY,
    channel_id   TEXT NOT NULL UNIQUE,
    channel_name TEXT NOT NULL DEFAULT '',
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Maps developers to the specific alert channels they are assigned to.
CREATE TABLE IF NOT EXISTS developer_channels (
    slack_user_id TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (slack_user_id, channel_id)
);

