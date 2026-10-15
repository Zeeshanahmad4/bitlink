# migrate_sheets_to_pg.py — one-time import from Google Sheets into PostgreSQL.
#
# HISTORICAL TOOL. PostgreSQL is now the live database and the app has no Google
# Sheets code path at all: nothing in the runtime reads or writes the
# spreadsheet. This script is the only thing in the repo that still talks to
# Sheets, and it exists solely to seed an empty database from the old sheet.
#
# It reads from Google Sheets and writes only to PostgreSQL. It never writes to
# the spreadsheet. Because the app no longer keeps the sheet in sync, the sheet
# is now stale, so a re-run would overwrite live data with old data. The
# destructive truncate-and-rebuild path therefore refuses to run unless
# --allow-truncate is passed explicitly, and it is only ever correct against an
# empty PostgreSQL.
#
#   python migrate_sheets_to_pg.py --verify           # compare row counts, write nothing
#   python migrate_sheets_to_pg.py --allow-truncate   # DESTROYS target tables, re-imports

import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import bcrypt
import gspread

import db

CREDENTIALS_FILE = os.getenv("SHEETS_CREDENTIALS_FILE", "credentials/service_account.json")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "BitLink Client Mappings")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(migrate_sheets_to_pg)s] - %(message)s'
)

TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def as_bool(val: Any) -> bool:
    """Google Sheets has no booleans — everything arrives as a string."""
    return str(val).strip().lower() in ("true", "1", "yes", "y", "t")


def as_text(val: Any) -> str:
    """Telegram IDs come back as ints from the Sheets API; force to text."""
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip()


def as_ts(val: Any) -> Optional[datetime]:
    s = as_text(val)
    if not s:
        return None
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    logging.warning(f"Unparseable timestamp {s!r}; storing NULL.")
    return None


def as_numeric(val: Any) -> float:
    s = as_text(val).replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def open_sheet() -> gspread.Spreadsheet:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    return gc.open(SPREADSHEET_NAME)


def rows_of(ws) -> List[Dict[str, Any]]:
    return [r for r in ws.get_all_records() if any(as_text(v) for v in r.values())]


def apply_schema() -> None:
    with open("schema.sql", "r", encoding="utf-8") as f:
        db.run_script(f.read())
    logging.info("Schema applied.")


def get_col(row: Dict[str, Any], *candidate_names: str) -> Any:
    """Finds a column in a row dict matching candidate names, case-insensitive and space/underscore-insensitive."""
    import re
    norm_map = {re.sub(r"[\s_]+", "", str(k).lower()): v for k, v in row.items()}
    for name in candidate_names:
        clean_name = re.sub(r"[\s_]+", "", name.lower())
        if clean_name in norm_map:
            return norm_map[clean_name]
    return None


def get_worksheet_safely(sp, sheet_name: str):
    try:
        return sp.worksheet(sheet_name)
    except Exception:
        return None


def migrate_client_mappings(sp) -> int:
    records = rows_of(sp.sheet1)
    data = []
    leads_data = []
    now_ts = datetime.now()

    for r in records:
        platform = as_text(get_col(r, "platform", "platform_name")) or "WhatsApp"
        client_name = as_text(get_col(r, "client_name", "client name", "name"))
        external_id = as_text(get_col(r, "external_id", "external id", "id", "phone", "chat_id"))
        slack_channel_id = as_text(get_col(r, "slack_channel_id", "slack channel id", "channel_id", "channel id"))
        paused = as_bool(get_col(r, "paused"))

        if not external_id and not slack_channel_id:
            continue

        data.append((platform, client_name, external_id, slack_channel_id, paused))

        # Also populate sales_leads so the contact appears in the Sales Contacts Directory and CRM
        leads_data.append((
            client_name,
            platform.lower(),
            external_id,
            slack_channel_id,
            "delivery",
            "",
            "won",
            "warm",
            "wa" if "whatsapp" in platform.lower() else "tg",
            "Unknown",
            "Unknown",
            now_ts,
            now_ts,
            paused,
            0.0,
            now_ts,
            True
        ))

    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO client_mappings
                   (platform, client_name, external_id, slack_channel_id, paused)
            VALUES %s
            ON CONFLICT DO NOTHING
        """, data)
    logging.info(f"client_mappings: {len(data)} rows imported into Delivery Client Mappings")

    # If the sheet is a single sheet and has no separate 'Sales Leads' tab, seed sales_leads from this sheet
    ws_leads = get_worksheet_safely(sp, "Sales Leads")
    if not ws_leads and leads_data:
        with db._cursor(commit=True) as cur:
            import psycopg2.extras
            psycopg2.extras.execute_values(cur, """
                INSERT INTO sales_leads
                       (client_name, platform, external_id, slack_channel_id, tag,
                        owner_slack_id, stage, lead_score, source, geo, language,
                        created_at, last_client_msg_at, paused, deal_value, closed_at,
                        delivery_handed_off)
                VALUES %s
                ON CONFLICT DO NOTHING
            """, leads_data)
        logging.info(f"sales_leads: {len(leads_data)} rows seeded from delivery sheet (for Sales Directory & CRM visibility)")

    return len(data)


def migrate_sales_leads(sp) -> int:
    ws = get_worksheet_safely(sp, "Sales Leads")
    if not ws:
        logging.info("Worksheet 'Sales Leads' not found; using seeded leads from client_mappings.")
        return 0
    records = rows_of(ws)
    data = [(
        as_text(get_col(r, "client_name", "client name")),
        as_text(get_col(r, "platform")) or "whatsapp",
        as_text(get_col(r, "external_id", "external id")),
        as_text(get_col(r, "slack_channel_id", "slack channel id")),
        as_text(get_col(r, "tag")) or "sales",
        as_text(get_col(r, "owner_slack_id", "owner")),
        as_text(get_col(r, "stage")) or "new",
        as_text(get_col(r, "lead_score", "score")) or "warm",
        as_text(get_col(r, "source")) or "wa",
        as_text(get_col(r, "geo")),
        as_text(get_col(r, "language")),
        as_ts(get_col(r, "created_at")),
        as_ts(get_col(r, "last_client_msg_at")),
        as_bool(get_col(r, "paused")),
        as_numeric(get_col(r, "deal_value")),
        as_ts(get_col(r, "closed_at")),
    ) for r in records]
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO sales_leads
                   (client_name, platform, external_id, slack_channel_id, tag,
                    owner_slack_id, stage, lead_score, source, geo, language,
                    created_at, last_client_msg_at, paused, deal_value, closed_at)
            VALUES %s
        """, data)
    logging.info(f"sales_leads: {len(data)} rows")
    return len(data)


def _simple_people(ws, table: str) -> int:
    if not ws:
        logging.info(f"Optional worksheet for {table} not found; skipping.")
        return 0
    data = [(
        as_text(get_col(r, "slack_user_id", "user_id")),
        as_text(get_col(r, "name")),
        as_bool(get_col(r, "active")),
    ) for r in rows_of(ws)]
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, f"""
            INSERT INTO {table} (slack_user_id, name, active)
            VALUES %s
        """, data)
    logging.info(f"{table}: {len(data)} rows")
    return len(data)


def migrate_blocklist(sp) -> int:
    import re
    ws = get_worksheet_safely(sp, "Blocklist")
    if not ws:
        logging.info("Worksheet 'Blocklist' not present; skipping.")
        return 0
    records = rows_of(ws)
    data = []
    for r in records:
        ext = as_text(get_col(r, "external_id", "external id"))
        if not ext:
            continue
        low = ext.lower()
        numeric = low.split("@")[0]
        digits = re.sub(r"\D", "", numeric)
        data.append((ext, numeric, digits, as_text(get_col(r, "reason")), as_ts(get_col(r, "added_at"))))
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO blocklist (external_id, numeric_part, digits_only, reason, added_at)
            VALUES %s
            ON CONFLICT (external_id) DO NOTHING
        """, data)
    logging.info(f"blocklist: {len(data)} rows")
    return len(data)


def migrate_settings(sp) -> int:
    ws = get_worksheet_safely(sp, "Settings")
    if not ws:
        logging.info("Worksheet 'Settings' not present; keeping existing PostgreSQL settings.")
        return 0
    records = rows_of(ws)
    final: Dict[str, tuple] = {}
    for r in records:
        key = as_text(get_col(r, "key"))
        if not key:
            continue
        final[key] = (key, as_text(get_col(r, "value")), as_text(get_col(r, "note", "")))

    data = list(final.values())
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO settings (key, value, note)
            VALUES %s
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, note = EXCLUDED.note, updated_at = NOW()
        """, data)
    logging.info(f"settings: {len(data)} unique keys from sheet")
    return len(data)


def migrate_snippets(sp) -> int:
    ws = get_worksheet_safely(sp, "Snippets")
    if not ws:
        logging.info("Worksheet 'Snippets' not present; skipping.")
        return 0
    data = [(
        as_text(get_col(r, "title")),
        as_text(get_col(r, "content")),
    ) for r in rows_of(ws) if as_text(get_col(r, "title")) or as_text(get_col(r, "content"))]
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO snippets (title, content) VALUES %s
        """, data)
    logging.info(f"snippets: {len(data)} rows")
    return len(data)
    logging.info(f"snippets: {len(data)} rows")
    return len(data)


def migrate_auth(sp) -> int:
    """Hash plaintext passwords from the legacy sheet. The plaintext is never
    written to PostgreSQL and is not echoed to the log."""
    records = rows_of(sp.worksheet("auth"))
    data = []
    for r in records:
        uid = as_text(r.get("id"))
        pw = as_text(r.get("password"))
        if not uid or not pw:
            continue
        hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        data.append((uid, hashed))
        logging.info(f"Hashed credentials for user '{uid}' (plaintext discarded).")
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO dashboard_users (id, password_hash) VALUES %s
            ON CONFLICT (id) DO NOTHING
        """, data)
    logging.info(f"dashboard_users: {len(data)} rows processed (existing users preserved)")
    return len(data)


def verify(sp) -> bool:
    checks = [
        ("client_mappings", sp.sheet1),
        ("sales_leads", get_worksheet_safely(sp, "Sales Leads")),
        ("sales_pool", get_worksheet_safely(sp, "Sales Pool")),
        ("blocklist", get_worksheet_safely(sp, "Blocklist")),
        ("project_managers", get_worksheet_safely(sp, "Project Managers")),
        ("snippets", get_worksheet_safely(sp, "Snippets")),
    ]
    ok = True
    print("\n%-20s %10s %10s  %s" % ("table", "sheets", "postgres", "status"))
    print("-" * 55)
    for table, ws in checks:
        if ws is None:
            sheet_n = 0
        else:
            try:
                sheet_n = len(rows_of(ws))
            except Exception:
                sheet_n = 0
        pg_n = (db.query_one(f"SELECT COUNT(*) AS c FROM {table}") or {}).get("c", 0)
        # In single-sheet mode, sales_leads is seeded from client_mappings if no Sales Leads tab exists
        if table == "sales_leads" and ws is None:
            sheet_n = len(rows_of(sp.sheet1))
        status = "OK" if pg_n == sheet_n else "MISMATCH"
        if pg_n != sheet_n:
            ok = False
        print("%-20s %10d %10d  %s" % (table, sheet_n, pg_n, status))
    pg_settings = (db.query_one("SELECT COUNT(*) AS c FROM settings") or {}).get("c", 0)
    print("%-20s %10s %10d  (preserved)" % ("settings", "-", pg_settings))
    pg_users = (db.query_one("SELECT COUNT(*) AS c FROM dashboard_users") or {}).get("c", 0)
    print("%-20s %10s %10d  (preserved)" % ("dashboard_users", "-", pg_users))

    # Compare real column values
    value_mismatches = compare_values(sp)
    if value_mismatches:
        ok = False
        for tbl, detail in value_mismatches:
            print("VALUE MISMATCH %s: %s" % (tbl, detail))
    else:
        print("\nvalue-level check: OK (compared client_mappings)")
    return ok


def compare_values(sp):
    """Compare real column values between Sheets and PostgreSQL."""
    problems = []

    def norm(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            try:
                f = float(v)
                return str(int(f)) if f == int(f) else str(f)
            except (TypeError, ValueError):
                return str(v)
        s = str(v).strip()
        if s.lower() in ("true", "false"):
            return s.lower()
        return s

    # --- client_mappings: keyed by external_id + slack_channel_id ---
    sheet_rows = {}
    for r in rows_of(sp.sheet1):
        ext = norm(get_col(r, "external_id", "external id", "id", "phone"))
        ch = norm(get_col(r, "slack_channel_id", "slack channel id", "channel_id"))
        if ext or ch:
            sheet_rows[(ext, ch)] = r
    pg_rows = {}
    for r in db.query_all("SELECT * FROM client_mappings"):
        key = (norm(r.get("external_id")), norm(r.get("slack_channel_id")))
        pg_rows[key] = r
    for key in set(sheet_rows) | set(pg_rows):
        s, p = sheet_rows.get(key), pg_rows.get(key)
        if not s or not p:
            problems.append(("client_mappings", "external_id=%s slack_channel_id=%s present in only one store"
                             % (key[0], key[1])))
            continue
        c_name = norm(get_col(s, "client_name", "client name"))
        if c_name != norm(p.get("client_name")):
            problems.append(("client_mappings",
                             "external_id=%s client_name: sheets=%r postgres=%r"
                             % (key[0], c_name, norm(p.get("client_name")))))
        p_val = norm(get_col(s, "paused"))
        if p_val != norm(p.get("paused")):
            problems.append(("client_mappings",
                             "external_id=%s paused: sheets=%r postgres=%r"
                             % (key[0], p_val, norm(p.get("paused")))))

    return problems


def main() -> int:
    if not db.is_available():
        logging.error(f"Cannot reach PostgreSQL at {db.DATABASE_URL}")
        return 1

    sp = open_sheet()
    apply_schema()

    if "--verify" in sys.argv:
        return 0 if verify(sp) else 1

    # Sheets is only authoritative for the *initial* import. Once the app is
    # serving writes from PostgreSQL, a re-run would silently discard every
    # change made since cut-over, so the destructive path has to be requested
    # explicitly.
    if "--allow-truncate" not in sys.argv:
        logging.error(
            "Refusing to truncate. PostgreSQL is now the live database and is no "
            "longer kept in sync with the spreadsheet, so re-importing from Sheets "
            "would destroy data written since cut-over.\n"
            "  Initial import from an empty database : venv/bin/python migrate_sheets_to_pg.py --allow-truncate\n"
            "  Verify only (never writes)            : venv/bin/python migrate_sheets_to_pg.py --verify"
        )
        return 2

    logging.info("Truncating data tables (preserving dashboard_users AND settings) for a clean re-sync from Sheets...")
    db.run_script("""
        TRUNCATE client_mappings, sales_leads, sales_pool, blocklist,
                 project_managers, snippets, critical_alerts, alert_details
        RESTART IDENTITY CASCADE;
    """)

    migrate_client_mappings(sp)
    migrate_sales_leads(sp)
    _simple_people(get_worksheet_safely(sp, "Sales Pool"), "sales_pool")
    _simple_people(get_worksheet_safely(sp, "Project Managers"), "project_managers")
    migrate_blocklist(sp)
    migrate_settings(sp)
    migrate_snippets(sp)
    migrate_auth(sp)

    try:
        from control_server import dispatch_bridge_refresh
        dispatch_bridge_refresh()
        logging.info("Triggered real-time bridge refresh for all imported mappings.")
    except Exception:
        pass

    logging.info("Migration complete. Google Sheets left untouched as rollback target.")
    return 0 if verify(sp) else 1


if __name__ == "__main__":
    sys.exit(main())
