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

import json
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


def find_delivery_mapping_by_external_id(external_id: str) -> Optional[Dict[str, Any]]:
    """Lookup active delivery mapping by external_id or numeric digits."""
    if not external_id:
        return None
    clean = str(external_id).strip()
    try:
        r = db.query_one("SELECT * FROM client_mappings WHERE external_id = %s ORDER BY paused ASC, id DESC LIMIT 1", (clean,))
        if r:
            return {"id": r["id"], "row_idx": r["id"], "platform": r["platform"],
                    "client_name": r["client_name"], "external_id": str(r["external_id"]),
                    "slack_channel_id": r["slack_channel_id"], "paused": bool(r["paused"])}
        import re
        digits = re.sub(r"\D", "", clean)
        if len(digits) >= 7:
            r = db.query_one("SELECT * FROM client_mappings WHERE external_id LIKE %s ORDER BY paused ASC, id DESC LIMIT 1", (f"%{digits}%",))
            if r:
                return {"id": r["id"], "row_idx": r["id"], "platform": r["platform"],
                        "client_name": r["client_name"], "external_id": str(r["external_id"]),
                        "slack_channel_id": r["slack_channel_id"], "paused": bool(r["paused"])}
        return None
    except Exception as e:
        logging.error(f"PG read failed for delivery mapping lookup ({clean}): {e}")
        return None



def is_client_in_delivery(external_id: str) -> bool:
    """Returns True if the client has been handed off to delivery."""
    return find_delivery_mapping_by_external_id(external_id) is not None


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
    "critical_alerts_channel_id": "",
    "triage_mode_enabled": False,
    "triage_channel_id": "",
    "round_robin_cursor": 0,
    "archive_after_days": 30,
    "sla_minutes": 15,
    "sunday_autoreply_enabled": False,
    "sunday_autoreply_text": "Thank you for reaching out! Our team is currently away on Sundays. We have received your message and we will reach out to you first thing Monday morning.",
    "digest_enabled": True,
    "digest_channel_id": "",
    "digest_time": "09:00",
}

# Bookkeeping written by background workers. These live in the settings table
# because that is where durable key/value state already belongs, but they are
# deliberately withheld from get_sales_settings(): the dashboard saves the whole
# settings object along with an _expected snapshot per key, so exposing a key
# that a worker rewrites on its own schedule would make an unrelated manual
# save fail with a spurious "page is out of date" conflict.
INTERNAL_SETTING_KEYS = ("digest_last_posted_day", "auto_archive_last_run_day")


def get_setting(key: str, default: str = "") -> str:
    """Read one raw settings row, including internal worker bookkeeping.

    Unlike get_sales_settings this does not apply defaults or type coercion,
    because callers are workers checking their own persisted state rather than
    the dashboard rendering configuration.
    """
    try:
        row = db.query_one("SELECT value FROM settings WHERE key = %s", (key,))
    except Exception as e:
        logging.error(f"PG read failed for setting {key}: {e}")
        return default
    if row is None:
        return default
    return "" if row["value"] is None else str(row["value"])


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
            if r["key"] in INTERNAL_SETTING_KEYS:
                continue
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


def list_project_managers() -> List[Dict[str, Any]]:
    """All project managers, including deactivated ones, for the roster editor."""
    return _people("project_managers", False)


def list_developers() -> List[Dict[str, Any]]:
    """All error-channel developers, including deactivated ones, with their channel_ids."""
    devs = _people("developers", False)
    try:
        rows = db.query_all("SELECT slack_user_id, channel_id FROM developer_channels")
        mapping: Dict[str, List[str]] = {}
        for r in rows:
            mapping.setdefault(r["slack_user_id"], []).append(r["channel_id"])
        for d in devs:
            uid = d.get("slack_user_id", "")
            d["channel_ids"] = mapping.get(uid, [])
    except Exception as e:
        logging.warning(f"Could not load developer channel assignments: {e}")
        for d in devs:
            d["channel_ids"] = []
    return devs


def get_active_developer_user_ids() -> List[str]:
    """Slack IDs of active developers, used for error channel membership."""
    return [str(r["slack_user_id"]).strip()
            for r in _people("developers", True) if r.get("slack_user_id")]


def get_active_sales_pool_user_ids() -> List[str]:
    return [r["slack_user_id"] for r in get_sales_pool(active_only=True)]


# ---------------------------------------------------------------------------
# Alert Channels
# ---------------------------------------------------------------------------

def list_alert_channels(active_only: bool = False) -> List[Dict[str, Any]]:
    try:
        sql = "SELECT id, channel_id, channel_name, active, created_at FROM alert_channels"
        if active_only:
            sql += " WHERE active = TRUE"
        sql += " ORDER BY id ASC"
        rows = db.query_all(sql)
        return [{
            "id": r["id"],
            "channel_id": r["channel_id"],
            "channel_name": r["channel_name"],
            "active": bool(r["active"]),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None
        } for r in rows]
    except Exception as e:
        logging.error(f"Error querying alert_channels: {e}")
        return []


def add_alert_channel(channel_id: str, channel_name: str, active: bool = True) -> int:
    try:
        row_id = db.execute_returning_id(
            """INSERT INTO alert_channels (channel_id, channel_name, active)
               VALUES (%s, %s, %s)
               ON CONFLICT (channel_id) DO UPDATE SET channel_name = EXCLUDED.channel_name, active = EXCLUDED.active
               RETURNING id""",
            (channel_id, channel_name, active)
        )
        return row_id
    except Exception as e:
        logging.error(f"Error adding alert_channel {channel_id}: {e}")
        raise


def delete_alert_channel(channel_id: str) -> bool:
    try:
        db.execute("DELETE FROM developer_channels WHERE channel_id = %s", (channel_id,))
        n = db.execute("DELETE FROM alert_channels WHERE channel_id = %s", (channel_id,))
        return n > 0
    except Exception as e:
        logging.error(f"Error deleting alert_channel {channel_id}: {e}")
        raise


def toggle_alert_channel(channel_id: str, active: bool) -> bool:
    try:
        n = db.execute("UPDATE alert_channels SET active = %s WHERE channel_id = %s", (active, channel_id))
        return n > 0
    except Exception as e:
        logging.error(f"Error toggling alert_channel {channel_id}: {e}")
        raise


def get_developer_channel_ids(slack_user_id: str) -> List[str]:
    try:
        rows = db.query_all("SELECT channel_id FROM developer_channels WHERE slack_user_id = %s", (slack_user_id,))
        return [str(r["channel_id"]).strip() for r in rows if r.get("channel_id")]
    except Exception as e:
        logging.error(f"Error getting channels for {slack_user_id}: {e}")
        return []


def set_developer_channels(slack_user_id: str, channel_ids: List[str]) -> None:
    try:
        db.execute("DELETE FROM developer_channels WHERE slack_user_id = %s", (slack_user_id,))
        for cid in channel_ids:
            cid = (cid or "").strip()
            if cid:
                db.execute(
                    "INSERT INTO developer_channels (slack_user_id, channel_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (slack_user_id, cid)
                )
    except Exception as e:
        logging.error(f"Error setting channels for {slack_user_id}: {e}")
        raise


def get_developers_for_channel(channel_id: str) -> List[str]:
    """Return active developer slack user IDs assigned to a specific channel."""
    try:
        rows = db.query_all(
            """SELECT d.slack_user_id FROM developers d
               JOIN developer_channels dc ON d.slack_user_id = dc.slack_user_id
               WHERE dc.channel_id = %s AND d.active = TRUE""",
            (channel_id,)
        )
        if rows:
            return [str(r["slack_user_id"]).strip() for r in rows if r.get("slack_user_id")]
        # Fallback: if no explicit bindings exist in developer_channels, return all active developers
        return get_active_developer_user_ids()
    except Exception as e:
        logging.error(f"Error getting developers for channel {channel_id}: {e}")
        return get_active_developer_user_ids()


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
                "created_at", "last_client_msg_at", "paused", "deal_value", "closed_at",
                "delivery_handed_off")


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
        "delivery_handed_off": bool(r.get("delivery_handed_off")),
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
    """Patch leads matching an external_id (used for auto-pause and handoff)."""
    clean = str(external_id).strip()
    allowed = [k for k in fields.keys() if k in LEAD_COLUMNS and k != "id"]
    if not allowed:
        return False
    sets = ", ".join(f"{k} = %s" for k in allowed)
    vals = [_coerce_lead_value(k, fields[k]) for k in allowed]
    vals.append(clean)
    try:
        n = db.execute(
            f"UPDATE sales_leads SET {sets} WHERE external_id = %s",
            vals
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
    if key in ("paused", "delivery_handed_off"):
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


def get_archivable_channels(after_days: int = 30) -> List[Dict[str, Any]]:
    """Return Slack channels eligible for auto-archive.

    A channel qualifies when it has been closed (won/lost) OR handed over to
    delivery for at least ``after_days`` days and its Slack channel has not yet
    been archived.

    Returns a list of dicts with keys:
      slack_channel_id, client_name, external_id, platform, reason, closed_at
    """
    if after_days <= 0:
        logging.warning(f"[AUTO-ARCHIVE] archive_after_days={after_days} is invalid; defaulting to 1.")
        after_days = 1
    try:
        rows = db.query_all(
            """
            SELECT DISTINCT ON (slack_channel_id)
                slack_channel_id,
                client_name,
                external_id,
                platform,
                CASE
                    WHEN delivery_handed_off THEN 'delivery_handoff'
                    ELSE 'deal_closed'
                END AS reason,
                COALESCE(closed_at, created_at) AS closed_at
            FROM sales_leads
            WHERE
                slack_channel_id <> ''
                AND (
                    LOWER(stage) IN ('won', 'lost')
                    OR delivery_handed_off = TRUE
                )
                AND COALESCE(closed_at, created_at) <= NOW() - INTERVAL '1 day' * %s
            ORDER BY slack_channel_id, id DESC
            """,
            (after_days,)
        )
        return [
            {
                "slack_channel_id": r["slack_channel_id"],
                "client_name": r.get("client_name") or "",
                "external_id": str(r.get("external_id") or ""),
                "platform": r.get("platform") or "",
                "reason": r.get("reason") or "deal_closed",
                "closed_at": _ts_out(r.get("closed_at")),
            }
            for r in rows
            if r.get("slack_channel_id")
        ]
    except Exception as e:
        logging.error(f"PG read failed for get_archivable_channels: {e}")
        raise


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
# Critical alerts
# ==============================================================================

_SALES_ALERT_SQL = (
    "SELECT id, created_at, severity, component, code, summary_plain, "
    "occurrences, last_seen_at, status, acknowledged_by, acknowledged_at "
    "FROM critical_alerts"
)
_SALES_ALERT_COLUMNS = ("id", "created_at", "severity", "component", "code",
                        "summary_plain", "occurrences", "last_seen_at", "status",
                        "acknowledged_by", "acknowledged_at")

_ALERT_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _alert_to_public(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "last_seen_at", "acknowledged_at"):
        val = out.get(key)
        if hasattr(val, "strftime"):
            out[key] = val.strftime(_ALERT_TS_FMT)
    return out


def record_alert(fingerprint: str, code: str, severity: str, component: str,
                  summary_plain: str, context: Dict[str, Any],
                  technical_detail: str, traceback_text: Optional[str] = None) -> Dict[str, Any]:
    """Insert an alert, or fold a repeat of the same fingerprint into a count.

    A repeat reopens the row and clears any acknowledgement: an alert that was
    acknowledged once and then happens again is a live problem again, and must
    not stay hidden behind a stale acknowledgement. Returns {'alert_id',
    'occurrences', 'is_new'}. Raises on failure so the caller can fall back to
    the buffer file rather than losing the alert.
    """
    is_new = db.query_one(
        "SELECT id FROM critical_alerts WHERE fingerprint = %s", (fingerprint,)) is None
    row = db.execute_returning_one(
        """INSERT INTO critical_alerts
             (severity, component, code, summary_plain, context, fingerprint)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s)
           ON CONFLICT (fingerprint) DO UPDATE SET
             occurrences = critical_alerts.occurrences + 1,
             last_seen_at = NOW(),
             severity = EXCLUDED.severity,
             summary_plain = EXCLUDED.summary_plain,
             status = 'open',
             acknowledged_by = NULL,
             acknowledged_at = NULL
           RETURNING id, occurrences""",
        (severity, component, code, summary_plain, json.dumps(context or {}), fingerprint))
    if row is None:
        raise RuntimeError("alert upsert returned no row")
    alert_id = int(row["id"])
    db.execute(
        """INSERT INTO alert_details (alert_id, technical_detail, traceback)
           VALUES (%s, %s, %s)
           ON CONFLICT (alert_id) DO UPDATE SET
             technical_detail = COALESCE(NULLIF(EXCLUDED.technical_detail, ''),
                                         alert_details.technical_detail),
             traceback = COALESCE(EXCLUDED.traceback, alert_details.traceback)""",
        (alert_id, technical_detail, traceback_text))
    return {"alert_id": alert_id, "occurrences": int(row["occurrences"]), "is_new": is_new}


def categorize_alert(code: str, component: str, context: Any = None) -> str:
    """Classify an alert into client_message, server, or scripts."""
    c = (code or "").upper()
    comp = (component or "").lower()
    ctx = context if isinstance(context, dict) else {}
    if (c in ("SLACK_FORWARD_FAILED", "SLACK_POST_EXHAUSTED", "EMERGENCY_INBOUND_DROPPED",
              "MEDIA_DOWNLOAD_FAILED", "MEDIA_UPLOAD_FAILED", "MEDIA_HANDLER_ERROR",
              "FORWARD_HANDLER_ERROR") or "chat_id" in ctx or "client_key" in ctx
            or "event_id" in ctx or "forward" in c.lower() or "slack" in c.lower()):
        return "client_message"
    if (comp in ("dashboard", "control_server", "service_watcher", "database", "system")
            or "service" in comp or "server" in comp or "service" in c.lower() or "server" in c.lower()
            or "database" in c.lower() or "auth" in c.lower()):
        return "server"
    return "scripts"


def get_sales_alerts(severity: Optional[str] = None, include_ackd: bool = True,
                     limit: int = 100) -> List[Dict[str, Any]]:
    """Sales-facing alerts.

    This reads a hardcoded column list that excludes `context`, `fingerprint`
    and `slack_permalink`, and it never touches alert_details. A traceback
    therefore cannot reach the sales API even if the caller adds fields later.
    """
    sql = f"{_SALES_ALERT_SQL} WHERE 1=1"
    params: List[Any] = []
    if severity:
        sql += " AND severity = %s"
        params.append(severity)
    if not include_ackd:
        sql += " AND status = 'open'"
    sql += " ORDER BY last_seen_at DESC LIMIT %s"
    params.append(max(1, min(int(limit), 500)))
    rows = db.query_all(sql, tuple(params))
    out = []
    for r in rows:
        item = _alert_to_public(r)
        item["category"] = categorize_alert(item.get("code", ""), item.get("component", ""))
        out.append(item)
    return out


def get_alerts_with_detail(severity: Optional[str] = None, component: Optional[str] = None,
                           status: Optional[str] = None, category: Optional[str] = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
    """Full technical alerts for admins only; joins the isolated detail table."""
    sql = ("SELECT a.id, a.created_at, a.severity, a.component, a.code, "
           "a.summary_plain, a.context, a.fingerprint, a.occurrences, "
           "a.last_seen_at, a.status, a.acknowledged_by, a.acknowledged_at, "
           "a.slack_permalink, d.technical_detail, d.traceback "
           "FROM critical_alerts a LEFT JOIN alert_details d ON d.alert_id = a.id WHERE 1=1")
    params: List[Any] = []
    for clause, val in (("severity = %s", severity), ("component = %s", component),
                        ("status = %s", status)):
        if val:
            sql += " AND a." + clause
            params.append(val)
    sql += " ORDER BY a.last_seen_at DESC LIMIT %s"
    params.append(max(1, min(int(limit), 500)))
    rows = db.query_all(sql, tuple(params))
    out = []
    for r in rows:
        item = _alert_to_public(r)
        ctx = item.get("context")
        if isinstance(ctx, str):
            try:
                item["context"] = json.loads(ctx)
            except (TypeError, ValueError):
                item["context"] = {"raw": ctx}
        item["category"] = categorize_alert(item.get("code", ""), item.get("component", ""), item.get("context"))
        if category and category != "all" and item["category"] != category:
            continue
        out.append(item)
    return out


def get_alert_detail(alert_id: int) -> Optional[Dict[str, Any]]:
    """Full technical detail for one alert. Admin-gated at the endpoint."""
    row = db.query_one(
        """SELECT a.*, d.technical_detail, d.traceback
           FROM critical_alerts a LEFT JOIN alert_details d ON d.alert_id = a.id
           WHERE a.id = %s""", (int(alert_id),))
    if not row:
        return None
    out = _alert_to_public(row)
    ctx = out.get("context")
    if isinstance(ctx, str):
        try:
            out["context"] = json.loads(ctx)
        except (TypeError, ValueError):
            out["context"] = {"raw": ctx}
    out["category"] = categorize_alert(out.get("code", ""), out.get("component", ""), out.get("context"))
    return out


def ack_alert(alert_id: int, user_id: str) -> bool:
    n = db.execute(
        """UPDATE critical_alerts
           SET status = 'acknowledged', acknowledged_by = %s, acknowledged_at = NOW()
           WHERE id = %s AND status <> 'resolved'""", (user_id, int(alert_id)))
    return n > 0


def resolve_alert(alert_id: int) -> bool:
    n = db.execute("UPDATE critical_alerts SET status = 'resolved' WHERE id = %s",
                   (int(alert_id),))
    return n > 0


def delete_alert(alert_id: int) -> bool:
    """Permanently delete an alert and its detail row (cascaded).

    The DB constraint enforces the same guard: only acknowledged or resolved
    alerts can be deleted.  Returns False when the row is missing or the
    status guard blocks it — the caller surfaces a clear error to the user.
    """
    try:
        n = db.execute(
            "DELETE FROM critical_alerts WHERE id = %s AND status IN ('acknowledged', 'resolved')",
            (int(alert_id),)
        )
        return n > 0
    except Exception as e:
        logging.error(f"PG delete of alert {alert_id} FAILED: {e}")
        return False


def open_alert_count(severity: Optional[str] = None) -> int:
    if severity:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM critical_alerts WHERE severity = %s AND status = 'open'",
            (severity,))
    else:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM critical_alerts WHERE status = 'open'")
    return int(row["n"]) if row else 0


def set_alert_permalink(alert_id: int, permalink: str) -> None:
    try:
        db.execute("UPDATE critical_alerts SET slack_permalink = %s WHERE id = %s",
                   (permalink, int(alert_id)))
    except Exception as e:
        logging.error(f"Failed to store alert permalink for {alert_id}: {e}")


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
            "SELECT id, password_hash, role FROM dashboard_users WHERE id = %s", (user_id,)
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
            return {"id": row["id"], "role": row.get("role") or "sales"}
    except (ValueError, TypeError):
        logging.error(f"Malformed password hash for user '{user_id}'.")
    return None


# ==============================================================================
# Admin user helpers
# ==============================================================================

def get_admin_user_ids() -> List[str]:
    """Return Slack user IDs of dashboard users whose role is 'admin'.

    Falls back to an empty list when the auth store is unavailable.
    """
    try:
        rows = db.query_all(
            "SELECT id FROM dashboard_users WHERE role = 'admin'"
        )
        return [str(r["id"]).strip() for r in rows if r.get("id")]
    except Exception as e:
        logging.error(f"Could not read admin users from PostgreSQL: {e}")
        return []


def list_dashboard_users() -> List[Dict[str, Any]]:
    """List all dashboard users with their roles (excluding password hashes)."""
    try:
        rows = db.query_all(
            "SELECT id, role, updated_at FROM dashboard_users ORDER BY id ASC"
        )
        return [
            {
                "id": str(r["id"]),
                "role": str(r.get("role") or "sales"),
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else ""
            }
            for r in rows
        ]
    except Exception as e:
        logging.error(f"Failed to list dashboard users: {e}")
        return []


def create_dashboard_user(user_id: str, password: str, role: str = "sales") -> Dict[str, Any]:
    """Create a new dashboard user with bcrypt hashed password and specified role."""
    user_id = str(user_id or "").strip()
    password = str(password or "").strip()
    role = str(role or "sales").strip().lower()
    if not user_id:
        return {"ok": False, "error": "Username cannot be empty."}
    if not password:
        return {"ok": False, "error": "Password cannot be empty."}
    if role not in ("admin", "sales"):
        return {"ok": False, "error": "Role must be 'admin' or 'sales'."}
    import bcrypt
    p_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    try:
        existing = db.query_one("SELECT id FROM dashboard_users WHERE id = %s", (user_id,))
        if existing:
            return {"ok": False, "error": f"User '{user_id}' already exists."}
        db.execute(
            "INSERT INTO dashboard_users (id, password_hash, role) VALUES (%s, %s, %s)",
            (user_id, p_hash, role)
        )
        return {"ok": True, "id": user_id, "role": role}
    except Exception as e:
        logging.error(f"Failed to create dashboard user '{user_id}': {e}")
        return {"ok": False, "error": str(e)}


def update_dashboard_user_role(user_id: str, role: str) -> Dict[str, Any]:
    """Update role for a dashboard user ('admin' or 'sales')."""
    user_id = str(user_id or "").strip()
    role = str(role or "").strip().lower()
    if role not in ("admin", "sales"):
        return {"ok": False, "error": "Role must be 'admin' or 'sales'."}
    try:
        n = db.execute(
            "UPDATE dashboard_users SET role = %s, updated_at = NOW() WHERE id = %s",
            (role, user_id)
        )
        if n == 0:
            return {"ok": False, "error": f"User '{user_id}' not found."}
        return {"ok": True, "id": user_id, "role": role}
    except Exception as e:
        logging.error(f"Failed to update role for dashboard user '{user_id}': {e}")
        return {"ok": False, "error": str(e)}


def update_dashboard_user_password(user_id: str, password: str) -> Dict[str, Any]:
    """Update password for a dashboard user with new bcrypt hash."""
    user_id = str(user_id or "").strip()
    password = str(password or "").strip()
    if not password:
        return {"ok": False, "error": "Password cannot be empty."}
    import bcrypt
    p_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    try:
        n = db.execute(
            "UPDATE dashboard_users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
            (p_hash, user_id)
        )
        if n == 0:
            return {"ok": False, "error": f"User '{user_id}' not found."}
        return {"ok": True, "id": user_id}
    except Exception as e:
        logging.error(f"Failed to update password for dashboard user '{user_id}': {e}")
        return {"ok": False, "error": str(e)}


def delete_dashboard_user(user_id: str) -> Dict[str, Any]:
    """Delete a dashboard user by ID."""
    user_id = str(user_id or "").strip()
    try:
        n = db.execute("DELETE FROM dashboard_users WHERE id = %s", (user_id,))
        if n == 0:
            return {"ok": False, "error": f"User '{user_id}' not found."}
        return {"ok": True, "id": user_id}
    except Exception as e:
        logging.error(f"Failed to delete dashboard user '{user_id}': {e}")
        return {"ok": False, "error": str(e)}
