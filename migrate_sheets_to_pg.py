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


def migrate_client_mappings(sp) -> int:
    records = rows_of(sp.sheet1)
    data = [(
        as_text(r.get("platform")),
        as_text(r.get("client_name")),
        as_text(r.get("external_id")),
        as_text(r.get("slack_channel_id")),
        as_bool(r.get("paused")),
    ) for r in records]
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO client_mappings
                   (platform, client_name, external_id, slack_channel_id, paused)
            VALUES %s
            ON CONFLICT DO NOTHING
        """, data)
    logging.info(f"client_mappings: {len(data)} rows")
    return len(data)


def migrate_sales_leads(sp) -> int:
    records = rows_of(sp.worksheet("Sales Leads"))
    data = [(
        as_text(r.get("client_name")),
        as_text(r.get("platform")) or "whatsapp",
        as_text(r.get("external_id")),
        as_text(r.get("slack_channel_id")),
        as_text(r.get("tag")) or "sales",
        as_text(r.get("owner_slack_id")),
        as_text(r.get("stage")) or "new",
        as_text(r.get("lead_score")) or "warm",
        as_text(r.get("source")) or "wa",
        as_text(r.get("geo")),
        as_text(r.get("language")),
        as_ts(r.get("created_at")),
        as_ts(r.get("last_client_msg_at")),
        as_bool(r.get("paused")),
        as_numeric(r.get("deal_value")),
        as_ts(r.get("closed_at")),
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
    data = [(
        as_text(r.get("slack_user_id")),
        as_text(r.get("name")),
        as_bool(r.get("active")),
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
    records = rows_of(sp.worksheet("Blocklist"))
    data = []
    for r in records:
        ext = as_text(r.get("external_id"))
        if not ext:
            continue
        low = ext.lower()
        numeric = low.split("@")[0]
        digits = re.sub(r"\D", "", numeric)
        data.append((ext, numeric, digits, as_text(r.get("reason")), as_ts(r.get("added_at"))))
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
    """The live Settings sheet contains duplicate keys, and the old reader was
    last-wins. Dedupe here in Python, preserving the final value for each key.

    Note: this must happen before the insert, not via ON CONFLICT. PostgreSQL
    rejects an ON CONFLICT DO UPDATE that would touch the same row twice in a
    single statement, so the duplicates have to be collapsed first.
    """
    records = rows_of(sp.worksheet("Settings"))
    final: Dict[str, tuple] = {}
    for r in records:
        key = as_text(r.get("key"))
        if not key:
            continue
        # Later rows overwrite earlier ones, matching the previous behaviour.
        final[key] = (key, as_text(r.get("value")), as_text(r.get("")))

    data = list(final.values())
    dupes = len(records) - len(data)
    if dupes:
        logging.warning(
            f"Settings had {dupes} duplicate key(s); kept the last value for each "
            f"(matches the old read behaviour)."
        )
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO settings (key, value, note)
            VALUES %s
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, note = EXCLUDED.note, updated_at = NOW()
        """, data)
    logging.info(f"settings: {len(data)} unique keys from {len(records)} rows")
    return len(data)


def migrate_snippets(sp) -> int:
    data = [(
        as_text(r.get("title")),
        as_text(r.get("content")),
    ) for r in rows_of(sp.worksheet("Snippets")) if as_text(r.get("title")) or as_text(r.get("content"))]
    with db._cursor(commit=True) as cur:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, """
            INSERT INTO snippets (title, content) VALUES %s
        """, data)
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
        ("sales_leads", sp.worksheet("Sales Leads")),
        ("sales_pool", sp.worksheet("Sales Pool")),
        ("blocklist", sp.worksheet("Blocklist")),
        ("project_managers", sp.worksheet("Project Managers")),
        ("snippets", sp.worksheet("Snippets")),
    ]
    ok = True
    print("\n%-20s %10s %10s  %s" % ("table", "sheets", "postgres", "status"))
    print("-" * 55)
    for table, ws in checks:
        try:
            sheet_n = len(rows_of(ws))
        except gspread.exceptions.WorksheetNotFound:
            sheet_n = 0
        pg_n = (db.query_one(f"SELECT COUNT(*) AS c FROM {table}") or {}).get("c", 0)
        status = "OK" if pg_n == sheet_n else "MISMATCH"
        if pg_n != sheet_n:
            ok = False
        print("%-20s %10d %10d  %s" % (table, sheet_n, pg_n, status))
    pg_settings = (db.query_one("SELECT COUNT(*) AS c FROM settings") or {}).get("c", 0)
    print("%-20s %10s %10d  (deduped from sheet)" % ("settings", "-", pg_settings))
    pg_users = (db.query_one("SELECT COUNT(*) AS c FROM dashboard_users") or {}).get("c", 0)
    print("%-20s %10s %10d  (hashed)" % ("dashboard_users", "-", pg_users))

    # Count parity alone misses a row that was imported with the wrong value.
    # Compare the columns that the app actually reads.
    value_mismatches = compare_values(sp)
    if value_mismatches:
        ok = False
        for tbl, detail in value_mismatches:
            print("VALUE MISMATCH %s: %s" % (tbl, detail))
    else:
        print("\nvalue-level check: OK (compared client_mappings, sales_leads, settings)")
    return ok


def compare_values(sp):
    """Compare real column values between Sheets and PostgreSQL.

    Row order is not meaningful (the keyless tables have no stable order after
    an import), so rows are matched on their natural key instead of position.
    """
    problems = []

    def norm(v):
        """Normalise a value so a Sheets string and a PG column compare equal."""
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            # NUMERIC/INT columns come back as Decimal/int from PG.
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
        key = (norm(r.get("external_id")), norm(r.get("slack_channel_id")))
        sheet_rows[key] = r
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
        for col in ("platform", "client_name"):
            if norm(s.get(col)) != norm(p.get(col)):
                problems.append(("client_mappings",
                                 "external_id=%s %s: sheets=%r postgres=%r"
                                 % (key[0], col, norm(s.get(col)), norm(p.get(col)))))
        if norm(s.get("paused")) != norm(p.get("paused")):
            problems.append(("client_mappings",
                             "external_id=%s paused: sheets=%r postgres=%r"
                             % (key[0], norm(s.get("paused")), norm(p.get("paused")))))

    # --- settings: keyed by key ---
    sheet_settings = {}
    for r in rows_of(sp.worksheet("Settings")):
        k = norm(r.get("key")) or norm(r.get("name"))
        if k:
            sheet_settings[k] = r  # last wins, matching migrate_settings
    pg_settings = {norm(r["key"]): r for r in db.query_all("SELECT key, value FROM settings")}
    for k in set(sheet_settings) | set(pg_settings):
        s, p = sheet_settings.get(k), pg_settings.get(k)
        if not s or not p:
            problems.append(("settings", "key=%r present in only one store" % k))
            continue
        sv = norm(s.get("value"))
        pv = norm(p.get("value"))
        if sv != pv:
            problems.append(("settings", "key=%r: sheets=%r postgres=%r" % (k, sv, pv)))

    # --- sales_leads: compare aggregate on the columns the dashboard reads ---
    sheet_leads = rows_of(sp.worksheet("Sales Leads"))
    pg_leads = db.query_all("SELECT * FROM sales_leads")
    for col in ("stage", "paused", "lead_score", "tag"):
        s_counts = {}
        for r in sheet_leads:
            s_counts[norm(r.get(col))] = s_counts.get(norm(r.get(col)), 0) + 1
        p_counts = {}
        for r in pg_leads:
            p_counts[norm(r.get(col))] = p_counts.get(norm(r.get(col)), 0) + 1
        if s_counts != p_counts:
            problems.append(("sales_leads", "%s distribution: sheets=%r postgres=%r"
                             % (col, s_counts, p_counts)))
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

    logging.info("Truncating data tables (preserving dashboard_users) for a clean re-sync from Sheets...")
    db.run_script("""
        TRUNCATE client_mappings, sales_leads, sales_pool, blocklist,
                 project_managers, snippets, settings, critical_alerts, alert_details
        RESTART IDENTITY CASCADE;
    """)

    migrate_client_mappings(sp)
    migrate_sales_leads(sp)
    _simple_people(sp.worksheet("Sales Pool"), "sales_pool")
    _simple_people(sp.worksheet("Project Managers"), "project_managers")
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
