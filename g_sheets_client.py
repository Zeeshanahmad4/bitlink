# g_sheets_client.py
#
# Historical module name kept so existing imports do not break. There is no
# Google Sheets code here and none is reachable from it: every read goes to
# PostgreSQL through db_store, and propagates the error if PostgreSQL is down.
#
# The stale backup modules (backup_mainwhatsapp, tele_backup, main_upwork,
# discum_ai_http) still import this. They need no changes.
import logging

import db_store

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s')


def get_client_mappings(platform: str, match_startswith: bool = False) -> list:
    """
    Retrieves client mappings from PostgreSQL.

    Args:
        platform (str): The platform to filter by (e.g., "Discord", "WhatsApp").
        match_startswith (bool): If True, matches platforms that start with the
                                 given string. Defaults to False (exact match).
    """
    return db_store.get_client_mappings(platform, match_startswith)
