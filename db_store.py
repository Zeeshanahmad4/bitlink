# db_store.py — data access for BitLink. PostgreSQL is the only source of truth.
#
# Google Sheets was the previous store and has been retired. There is
# deliberately no Sheets fallback on any read or write. Falling back to it would
# split the two stores: a row written to the spreadsheet would never reach
# PostgreSQL, and a row read from it would carry a sheet row number rather than a
# primary key, so the process would quietly be reading and writing a different
# database than the operator believes. When PostgreSQL is unreachable every
# operation here fails loudly and the caller decides what to do.
#
# Every read returns dicts shaped exactly like the old gspread get_all_records()
# output: strings for text, real bools for flags, and '%Y-%m-%d %H:%M:%S' strings
# for timestamps. That keeps every existing caller working without modification.
#
# 'row_idx' is retained in these dicts for frontend compatibility, but it is a
# real PostgreSQL primary key rather than a spreadsheet row number. Deleting a
# row no longer renumbers the ones below it.

import logging
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

import db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)

TS_OUT = "%Y-%m-%d %H:%M:%S"


def _ts_out(val: Any) -> str:
    """Render a PG timestamp the way the Sheets API used to return it."""
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime(TS_OUT)
    return str(val)


def _num_out(val: Any) -> str:
    """Render a numeric column the way the Sheets API used to.

    Postgres NUMERIC comes back as Decimal('0.00'); the dashboard renders
    '$' + value, so a whole number must print as '0' and not '0.00'.
    """
    if val is None or val == "":
        return "0"
    d = Decimal(str(val))
    if d == d.to_integral_value():
        return str(int(d))
    return str(d.normalize())


# ==============================================================================
# Client mappings  (legacy name: the "Sheet1" worksheet, now a Postgres table)
# ==============================================================================

def list_all_client_mappings() -> List[Dict[str, Any]]:
    """Every client mapping regardless of platform, for the control panel."""
    try:
        rows = db.query_all("SELECT * FROM client_mappings ORDER BY id")
        return [{"id": r["id"], "row_idx": r["id"], "platform": r["platform"],
                 "client_name": r["client_name"], "external_id": str(r["external_id"]),
                 "slack_channel_id": r["slack_channel_id"], "paused": bool(r["paused"])}
                for r in rows]
    except Exception as e:
        logging.error(f"PG read failed for client_mappings: {e}")
        raise


def get_client_mappings(platform: str, match_startswith: bool = False) -> List[Dict[str, Any]]:
    """Rows for a platform. Matches the legacy signature and dict shape."""
    try:
        if match_startswith:
            rows = db.query_all(
                "SELECT * FROM client_mappings WHERE LOWER(platform) LIKE LOWER(%s) || '%'",
                (platform,)
            )
        else:
            rows = db.query_all(
                "SELECT * FROM client_mappings WHERE LOWER(platform) = LOWER(%s)",
                (platform,)
            )
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "row_idx": r["id"],
                "platform": r["platform"],
                "client_name": r["client_name"],
                "external_id": str(r["external_id"]),
                "slack_channel_id": r["slack_channel_id"],
                "paused": bool(r["paused"]),
            })
        return out
    except Exception as e:
        logging.error(f"PG read failed for client_mappings ({platform}): {e}")
        raise


def get_client_mapping_by_id(mapping_id: int) -> Optional[Dict[str, Any]]:
    try:
        r = db.query_one("SELECT * FROM client_mappings WHERE id = %s", (mapping_id,))
        if not r:
            return None
        return {"id": r["id"], "row_idx": r["id"], "platform": r["platform"],
                "client_name": r["client_name"], "external_id": str(r["external_id"]),
                "slack_channel_id": r["slack_channel_id"], "paused": bool(r["paused"])}
    except Exception as e:
        logging.error(f"PG read failed for client mapping {mapping_id}: {e}")
        raise


def update_client_mapping(mapping_id: int, fields: Dict[str, Any]) -> bool:
    allowed = [k for k in ("platform", "client_name", "external_id",
                           "slack_channel_id", "paused") if k in fields]
    if not allowed:
        return False
    sets = ", ".join(f"{k} = %s" for k in allowed)
    vals = []
    for k in allowed:
        v = fields[k]
        if k == "paused":
            v = bool(v) if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")
        else:
            v = str(v)
        vals.append(v)
    vals.append(mapping_id)
    try:
        n = db.execute(
            f"UPDATE client_mappings SET {sets}, updated_at = NOW() WHERE id = %s", vals)
        return n > 0
    except Exception as e:
        logging.error(
            f"PG update of client mapping {mapping_id} FAILED and was not applied: {e} "
            f"fields={fields}"
        )
        return False


def add_client_mapping(platform: str, client_name: str, external_id: str,
                       slack_channel_id: str, paused: bool = False) -> int:
    """Insert a client mapping. Returns the new primary key."""
    try:
        new_id = db.execute_returning_id(
            """INSERT INTO client_mappings
                   (platform, client_name, external_id, slack_channel_id, paused)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (platform, client_name, external_id, slack_channel_id, paused)
        )
        return new_id
    except Exception as e:
        logging.error(
            f"PG write of client_mapping FAILED and was not applied: {e}. "
            f"platform={platform} external_id={external_id}"
        )
        # Deliberately no Google Sheets fallback here. Writing to the sheet
        # while PostgreSQL is the source of truth splits the two stores: the row
        # would only exist in the spreadsheet, would get a sheet row number
        # instead of a primary key, and would be silently lost on the next
        # re-sync. Surface the failure instead.
        raise


def set_client_paused(slack_channel_id: str, paused: bool) -> bool:
    """Toggle the paused flag for a mapping, addressed by Slack channel ID."""
    try:
        n = db.execute(
            "UPDATE client_mappings SET paused = %s, updated_at = NOW() WHERE slack_channel_id = %s",
            (paused, slack_channel_id)
        )
        if n == 0:
            logging.warning(f"No client mapping for channel {slack_channel_id}.")
        return n > 0
    except Exception as e:
        logging.error(
            f"PG pause of channel {slack_channel_id} FAILED and was not applied: {e}"
        )
        return False


def delete_client_mapping(mapping_id: int) -> bool:
    try:
        n = db.execute("DELETE FROM client_mappings WHERE id = %s", (mapping_id,))
        return n > 0
    except Exception as e:
        logging.error(f"PG delete of mapping {mapping_id} FAILED and was not applied: {e}")
        return False


# ==============================================================================
# Settings  (was: 'Settings' worksheet)
# ==============================================================================

SETTINGS_BOOL_KEYS = ("kill_switch_autocreate", "triage_mode_enabled",
                      "sunday_autoreply_enabled", "digest_enabled")
SETTINGS_INT_KEYS = ("round_robin_cursor", "archive_after_days", "sla_minutes")

SETTINGS_DEFAULTS = {
    "kill_switch_autocreate": False,
    "kill_switch_alert_channel_id": "",
    "triage_mode_enabled": False,
    "triage_channel_id": "",
    "round_robin_cursor": 0,
    "archive_after_days": 30,
    "sla_minutes": 15,
    "sunday_autoreply_enabled": False,
    "sunday_autoreply_text": "Thank you for reaching out! Our team is currently away for Sunday. We have received your message and will reach out to you first thing Monday morning.",
    "digest_enabled": True,
    "digest_channel_id": "",
    "digest_time": "09:00",
}


def _coerce_settings(key: str, val: str) -> Any:
    if key in SETTINGS_BOOL_KEYS:
        return val.strip().lower() in ("true", "1", "yes")
    if key in SETTINGS_INT_KEYS:
        try:
            return int(val)
        except ValueError:
            return SETTINGS_DEFAULTS.get(key, 0)
    return val


def get_sales_settings(force_refresh: bool = False) -> Dict[str, Any]:
    """Settings with the same defaults and type coercion the sheet path applied."""
    try:
        rows = db.query_all("SELECT key, value FROM settings")
        settings = dict(SETTINGS_DEFAULTS)
        for r in rows:
            settings[r["key"]] = _coerce_settings(r["key"], r["value"])
        return settings
    except Exception as e:
        logging.error(f"PG read failed for settings: {e}")
        raise


def set_setting(key: str, value: str, note: str = "") -> bool:
    try:
        db.execute(
            """INSERT INTO settings (key, value, note) VALUES (%s, %s, %s)
               ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, note = EXCLUDED.note, updated_at = NOW()""",
            (key, value, note)
        )
        return True
    except Exception as e:
        logging.error(f"PG write FAILED for set_setting({key}); not applied: {e}")
        return False


def update_round_robin_cursor(new_index: int) -> bool:
    return set_setting("round_robin_cursor", str(new_index), "Updated by round-robin")


# ==============================================================================
# Sales pool & project managers
# ==============================================================================

def _people(table: str, active_only: bool) -> List[Dict[str, Any]]:
    try:
        sql = f"SELECT * FROM {table}"
        if active_only:
            sql += " WHERE active = TRUE"
        rows = db.query_all(sql)
        return [{"id": r["id"], "row_idx": r["id"], "slack_user_id": r["slack_user_id"],
                 "name": r["name"], "active": bool(r["active"])} for r in rows]
    except Exception as e:
        logging.error("PG read failed for {table}: {e}")
        raise


def get_sales_pool(active_only: bool = True) -> List[Dict[str, Any]]:
    return _people("sales_pool", active_only)


def get_project_managers() -> List[Dict[str, Any]]:
    return _people("project_managers", True)


def get_active_sales_pool_user_ids() -> List[str]:
    return [r["slack_user_id"] for r in get_sales_pool(active_only=True)]


def add_person(table: str, slack_user_id: str, name: str, active: bool) -> int:
    try:
        new_id = db.execute_returning_id(
            f"INSERT INTO {table} (slack_user_id, name, active) VALUES (%s, %s, %s) RETURNING id",
            (slack_user_id, name, active)
        )
        return new_id
    except Exception as e:
        logging.error(
            f"PG insert into {table} FAILED and was not applied: {e} "
            f"(slack_user_id={slack_user_id})"
        )
        raise


def update_person(table: str, person_id: int, slack_user_id: str, name: str, active: bool) -> bool:
    try:
        n = db.execute(
            f"UPDATE {table} SET slack_user_id = %s, name = %s, active = %s WHERE id = %s",
            (slack_user_id, name, active, person_id)
        )
        return n > 0
    except Exception as e:
        logging.error(f"PG update of {table} {person_id} FAILED and was not applied: {e}")
        raise


def delete_person(table: str, person_id: int) -> bool:
    try:
        n = db.execute(f"DELETE FROM {table} WHERE id = %s", (person_id,))
        return n > 0
    except Exception as e:
        logging.error(f"PG delete from {table} {person_id} FAILED and was not applied: {e}")
        raise


# ==============================================================================
# Blocklist
# ==============================================================================

def get_blocklist(force_refresh: bool = False) -> Set[str]:
    try:
        rows = db.query_all("SELECT external_id FROM blocklist")
        return {r["external_id"] for r in rows if r["external_id"]}
    except Exception as e:
        logging.error("PG read failed for blocklist: {e}")
        raise


def list_blocklist() -> List[Dict[str, Any]]:
    """Blocklist rows with their metadata, for the control panel."""
    try:
        rows = db.query_all(
            "SELECT id, external_id, reason, added_at FROM blocklist ORDER BY id"
        )
        return [{"row_idx": r["id"], "id": r["id"], "external_id": r["external_id"],
                 "reason": r["reason"] or "", "added_at": _ts_out(r["added_at"])} for r in rows]
    except Exception as e:
        logging.error("PG read failed for blocklist listing: {e}")
        raise


def add_to_blocklist(external_id: str, reason: str, added_at: str = "") -> bool:
    low = external_id.strip().lower()
    numeric = low.split("@")[0]
    digits = re.sub(r"\D", "", numeric)
    try:
        db.execute(
            """INSERT INTO blocklist (external_id, numeric_part, digits_only, reason, added_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (external_id) DO NOTHING""",
            (external_id, numeric, digits, reason, added_at or None)
        )
        return True
    except Exception as e:
        logging.error(f"PG blocklist insert FAILED and was not applied: {e}")
        raise


def delete_from_blocklist(entry_id: int) -> bool:
    try:
        n = db.execute("DELETE FROM blocklist WHERE id = %s", (entry_id,))
        return n > 0
    except Exception as e:
        logging.error(f"PG blocklist delete of {entry_id} FAILED and was not applied: {e}")
        raise


def is_external_id_blocked(external_id: str, force_refresh: bool = False) -> bool:
    """Exact, numeric, and digits-suffix matching, same semantics as before."""
    if not external_id:
        return False
    clean = str(external_id).strip().lower()
    numeric = clean.split("@")[0]
    digits = re.sub(r"\D", "", numeric)

    try:
        row = db.query_one(
            """SELECT 1 AS hit FROM blocklist
               WHERE external_id = %s
                  OR numeric_part = %s
                  OR (digits_only <> '' AND (
                         digits_only = %s
                      OR %s LIKE '%%' || digits_only
                      OR digits_only LIKE '%%' || %s))
               LIMIT 1""",
            (clean, numeric, digits, digits, digits)
        )
        return row is not None
    except Exception as e:
        logging.error("PG read failed for blocklist check: {e}")
        raise


# ==============================================================================
# Sales leads
# ==============================================================================

LEAD_COLUMNS = ("client_name", "platform", "external_id", "slack_channel_id", "tag",
                "owner_slack_id", "stage", "lead_score", "source", "geo", "language",
                "created_at", "last_client_msg_at", "paused", "deal_value", "closed_at")


def _shape_lead(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "row_idx": r["id"],
        "client_name": r.get("client_name") or "",
        "platform": r.get("platform") or "",
        "external_id": str(r.get("external_id") or ""),
        "slack_channel_id": r.get("slack_channel_id") or "",
        "tag": r.get("tag") or "",
        "owner_slack_id": r.get("owner_slack_id") or "",
        "stage": r.get("stage") or "",
        "lead_score": r.get("lead_score") or "",
        "source": r.get("source") or "",
        "geo": r.get("geo") or "",
        "language": r.get("language") or "",
        "created_at": _ts_out(r.get("created_at")),
        "last_client_msg_at": _ts_out(r.get("last_client_msg_at")),
        "paused": bool(r.get("paused")),
        "deal_value": _num_out(r.get("deal_value")),
        "closed_at": _ts_out(r.get("closed_at")),
    }


def get_sales_leads() -> List[Dict[str, Any]]:
    """All sales leads, in the exact dict shape callers already expect."""
    try:
        rows = db.query_all("SELECT * FROM sales_leads ORDER BY id")
        return [_shape_lead(r) for r in rows]
    except Exception as e:
        logging.error("PG read failed for sales_leads: {e}")
        raise


def find_sales_lead_by_external_id(external_id: str) -> Optional[Dict[str, Any]]:
    """Newest lead matching an external_id, regardless of stage."""
    clean = str(external_id).strip()
    try:
        r = db.query_one(
            "SELECT * FROM sales_leads WHERE external_id = %s ORDER BY id DESC LIMIT 1",
            (clean,)
        )
        return _shape_lead(r) if r else None
    except Exception as e:
        logging.error("PG read failed for lead lookup by external_id: {e}")
        raise


def update_sales_lead_by_external_id(external_id: str, fields: Dict[str, Any]) -> bool:
    """Patch every open lead matching an external_id (used for auto-pause)."""
    clean = str(external_id).strip()
    try:
        n = db.execute(
            f"UPDATE sales_leads SET {', '.join(f'{k} = %s' for k in fields)}, "
            f"WHERE external_id = %s AND LOWER(stage) NOT IN ('won', 'lost')",
            [_coerce_lead_value(k, v) for k, v in fields.items()] + [clean]
        )
        return n > 0
    except Exception as e:
        logging.error(f"PG lead update for {clean} FAILED and was not applied: {e}")
        raise


def find_existing_open_lead(external_id: str) -> Optional[Dict[str, Any]]:
    if not external_id:
        return None
    clean = str(external_id).strip()
    try:
        r = db.query_one(
            """SELECT * FROM sales_leads
               WHERE external_id = %s AND LOWER(stage) NOT IN ('won', 'lost')
               ORDER BY id DESC LIMIT 1""",
            (clean,)
        )
        return _shape_lead(r) if r else None
    except Exception as e:
        logging.error("PG read failed for open lead lookup: {e}")
        raise


def save_new_sales_lead(lead: Dict[str, Any]) -> Optional[int]:
    now_ts = time.strftime(TS_OUT)
    try:
        new_id = db.execute_returning_id(
            """INSERT INTO sales_leads
                   (client_name, platform, external_id, slack_channel_id, tag,
                    owner_slack_id, stage, lead_score, source, geo, language,
                    created_at, last_client_msg_at, paused, deal_value, closed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                str(lead.get("client_name", "")),
                str(lead.get("platform", "whatsapp")),
                str(lead.get("external_id", "")),
                str(lead.get("slack_channel_id", "")),
                str(lead.get("tag", "sales")),
                str(lead.get("owner_slack_id", "")),
                str(lead.get("stage", "new")),
                str(lead.get("lead_score", "warm")),
                str(lead.get("source", "wa")),
                str(lead.get("geo", "")),
                str(lead.get("language", "")),
                lead.get("created_at") or now_ts,
                lead.get("last_client_msg_at") or now_ts,
                bool(lead.get("paused")),
                float(str(lead.get("deal_value", "0") or 0).replace(",", "") or 0),
                lead.get("closed_at") or None,
            )
        )
        logging.info(f"Saved sales lead {lead.get('client_name')} "
                     f"({lead.get('external_id')}) id={new_id}")
        return new_id
    except Exception as e:
        logging.error(
            f"PG insert of sales lead FAILED and was not applied: {e} "
            f"(external_id={lead.get('external_id')})"
        )
        raise


def get_sales_lead_by_id(lead_id: int) -> Optional[Dict[str, Any]]:
    """Fetch one lead by its stable primary key."""
    try:
        r = db.query_one("SELECT * FROM sales_leads WHERE id = %s", (lead_id,))
        return _shape_lead(r) if r else None
    except Exception as e:
        logging.error(f"PG read failed for sales lead {lead_id}: {e}")
        raise


def update_sales_lead(lead_id: int, fields: Dict[str, Any]) -> bool:
    """Patch a lead by primary key. Unknown keys are ignored, matching the
    old 'dynamically append new columns' behaviour but without schema churn."""
    allowed = [k for k in fields.keys() if k in LEAD_COLUMNS and k != "id"]
    if not allowed:
        return False
    sets = ", ".join(f"{k} = %s" for k in allowed)
    vals = [_coerce_lead_value(k, fields[k]) for k in allowed]
    vals.append(lead_id)
    try:
        n = db.execute(f"UPDATE sales_leads SET {sets} WHERE id = %s", vals)
        return n > 0
    except Exception as e:
        # A write that fails is not interchangeable with a write that succeeded.
        # Falling back to Sheets here would leave PostgreSQL and the spreadsheet
        # disagreeing, with no indication to the operator. Fail loudly instead.
        logging.error(
            f"PG update of sales_leads {lead_id} FAILED and was not applied: {e}. "
            f"fields={ {k: fields[k] for k in allowed} }"
        )
        raise


def _coerce_lead_value(key: str, val: Any) -> Any:
    if key == "paused":
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("true", "1", "yes")
    if key == "deal_value":
        try:
            return float(str(val).replace(",", "") or 0)
        except ValueError:
            return 0.0
    if key in ("created_at", "last_client_msg_at", "closed_at"):
        return _parse_ts(val)
    return str(val)


def _parse_ts(val: Any) -> Optional[datetime]:
    """Parse a timestamp into a real datetime for PostgreSQL.

    Note: time.strptime() returns a struct_time tuple, not a datetime, and
    passing that to a TIMESTAMPTZ column raises a type error. That error was
    being swallowed into the Sheets fallback, which made lead updates look
    successful while silently discarding the write.
    """
    if val is None or str(val).strip() == "":
        return None
    if isinstance(val, datetime):
        return val
    if hasattr(val, "strftime"):
        return val
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    logging.warning(f"Unparseable timestamp {s!r}; storing NULL.")
    return None


def update_lead_owner_by_channel(channel_id: str, new_owner_slack_id: str,
                                 external_id: Optional[str] = None) -> bool:
    """Find a lead by channel (or external_id) and reassign its owner."""
    try:
        n = 0
        if channel_id:
            n = db.execute(
                "UPDATE sales_leads SET owner_slack_id = %s WHERE slack_channel_id = %s",
                (str(new_owner_slack_id).strip(), channel_id)
            )
        if n == 0 and external_id:
            n = db.execute(
                "UPDATE sales_leads SET owner_slack_id = %s WHERE external_id = %s",
                (str(new_owner_slack_id).strip(), str(external_id).strip())
            )
        return n > 0
    except Exception as e:
        logging.error(f"PG lead owner update FAILED and was not applied: {e}")
        return False


def get_leaderboard_metrics() -> List[Dict[str, Any]]:
    try:
        rows = db.query_all("""
            SELECT owner_slack_id,
                   COUNT(*) FILTER (WHERE LOWER(stage) = 'won')  AS wins,
                   COUNT(*) FILTER (WHERE LOWER(stage) = 'lost') AS lost,
                   COUNT(*) FILTER (WHERE LOWER(stage) NOT IN ('won', 'lost')) AS active
            FROM sales_leads
            WHERE owner_slack_id <> ''
            GROUP BY owner_slack_id
            ORDER BY wins DESC, active DESC
        """)
        return [{"owner_slack_id": r["owner_slack_id"], "wins": int(r["wins"]),
                 "active": int(r["active"]), "lost": int(r["lost"])} for r in rows]
    except Exception as e:
        logging.error("PG read failed for leaderboard: {e}")
        raise


# ==============================================================================
# Snippets
# ==============================================================================

def get_snippets() -> List[Dict[str, Any]]:
    try:
        rows = db.query_all("SELECT id, title, content FROM snippets ORDER BY id")
        return [{"id": r["id"], "row_idx": r["id"], "title": r["title"] or "",
                 "content": r["content"] or ""} for r in rows]
    except Exception as e:
        logging.error("PG read failed for snippets: {e}")
        raise


def save_snippet(title: str, content: str) -> Optional[int]:
    try:
        new_id = db.execute_returning_id(
            "INSERT INTO snippets (title, content) VALUES (%s, %s) RETURNING id",
            (title, content)
        )
        return new_id
    except Exception as e:
        logging.error(f"PG snippet insert FAILED and was not applied: {e}")
        raise


def update_snippet(snippet_id: int, title: str, content: str) -> bool:
    try:
        n = db.execute("UPDATE snippets SET title = %s, content = %s WHERE id = %s",
                       (title, content, snippet_id))
        return n > 0
    except Exception as e:
        logging.error(f"PG snippet update of {snippet_id} FAILED and was not applied: {e}")
        return False


def delete_snippet(snippet_id: int) -> bool:
    try:
        n = db.execute("DELETE FROM snippets WHERE id = %s", (snippet_id,))
        return n > 0
    except Exception as e:
        logging.error(f"PG snippet delete of {snippet_id} FAILED and was not applied: {e}")
        return False


# ==============================================================================
# Dashboard auth
# ==============================================================================

def verify_login(user_id: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify dashboard credentials against the bcrypt hash in PostgreSQL.

    There is deliberately no Google Sheets fallback here. The legacy sheet still
    holds plaintext passwords, and accepting a plaintext match would mean the
    bcrypt migration could be bypassed any time PostgreSQL is unreachable or an
    account is missing. An outage of the auth store is an outage of login.
    """
    try:
        row = db.query_one(
            "SELECT id, password_hash FROM dashboard_users WHERE id = %s", (user_id,)
        )
    except Exception as e:
        logging.error(f"Auth store unavailable; cannot verify login for '{user_id}': {e}")
        return None

    if not row:
        logging.warning(f"Rejected login for unknown dashboard user '{user_id}'.")
        return None

    import bcrypt
    try:
        if bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return {"id": row["id"]}
    except (ValueError, TypeError):
        logging.error(f"Malformed password hash for user '{user_id}'.")
    return None
