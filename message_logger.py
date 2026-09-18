import os
import logging
import asyncio
import functools
from dotenv import load_dotenv

try:
    import psycopg2
    from psycopg2.extras import Json
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    psycopg2 = None
    Json = None

load_dotenv()

logger = logging.getLogger("message_logger")

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DB", "bitlink"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
    "connect_timeout": int(os.getenv("PG_CONNECT_TIMEOUT", "3")),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    platform        TEXT NOT NULL,
    direction       TEXT NOT NULL,
    event_type      TEXT NOT NULL DEFAULT 'message',
    chat_id         TEXT,
    sender_name     TEXT,
    client_name     TEXT,
    message_text    TEXT,
    message_id      TEXT,
    slack_channel   TEXT,
    slack_ts        TEXT,
    media_filename  TEXT,
    media_type      TEXT,
    quoted_text     TEXT,
    is_mapped       BOOLEAN NOT NULL DEFAULT FALSE,
    raw_json        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_platform_created ON messages (platform, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (platform, chat_id);
"""


def init_db():
    """Create the messages table if it does not exist. Non-fatal on failure."""
    if not _DB_AVAILABLE:
        logger.warning("message_logger: psycopg2 not installed; logging disabled.")
        return
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
        conn.close()
        logger.info("message_logger: database schema ready")
    except Exception as e:
        logger.warning(f"message_logger: init_db failed (non-fatal): {e}")


def log_message(
    platform,
    direction,
    event_type="message",
    chat_id=None,
    sender_name=None,
    client_name=None,
    message_text=None,
    message_id=None,
    slack_channel=None,
    slack_ts=None,
    media_filename=None,
    media_type=None,
    quoted_text=None,
    is_mapped=False,
    raw=None,
):
    """Insert a single message row. Never raises — always safe to call."""
    if not _DB_AVAILABLE:
        return
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages
                    (platform, direction, event_type, chat_id, sender_name, client_name,
                     message_text, message_id, slack_channel, slack_ts, media_filename,
                     media_type, quoted_text, is_mapped, raw_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    platform, direction, event_type, chat_id, sender_name, client_name,
                    message_text, message_id, slack_channel, slack_ts, media_filename,
                    media_type, quoted_text, is_mapped,
                    Json(raw) if raw is not None else None,
                ),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"message_logger: log_message failed (non-fatal): {e}")


async def log_message_async(**kwargs):
    """Non-blocking wrapper: runs log_message in a thread so it never blocks
    the event loop. Safe to call from async handlers."""
    if not _DB_AVAILABLE:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, functools.partial(log_message, **kwargs))
    except Exception:
        pass
