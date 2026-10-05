# sales_store.py — Sales Module Data Access Layer.
#
# Now a thin compatibility shim over db_store, which is PostgreSQL-only. The
# public function names and signatures below are unchanged, so existing callers
# need no edits.
#
# The old 20-30s TTL caches existed to protect against Google API rate limits.
# They are gone: local PostgreSQL reads are sub-millisecond, so caching only
# introduced staleness. The force_refresh arguments are accepted and ignored.
import logging
from typing import Any, Dict, List, Optional, Set

import db_store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)


def get_sales_settings(force_refresh: bool = False) -> Dict[str, Any]:
    return db_store.get_sales_settings(force_refresh=force_refresh)


def get_sales_pool(active_only: bool = True, force_refresh: bool = False) -> List[Dict[str, Any]]:
    return db_store.get_sales_pool(active_only=active_only)


def get_active_sales_pool_user_ids() -> List[str]:
    return db_store.get_active_sales_pool_user_ids()


def get_blocklist(force_refresh: bool = False) -> Set[str]:
    return db_store.get_blocklist(force_refresh=force_refresh)


def get_snippets(force_refresh: bool = False) -> List[Dict[str, Any]]:
    return db_store.get_snippets()


def save_snippet(title: str, content: str) -> bool:
    return db_store.save_snippet(title, content) is not None


def update_snippet(row_idx: int, title: str, content: str) -> bool:
    return db_store.update_snippet(row_idx, title, content)


def delete_snippet(row_idx: int) -> bool:
    return db_store.delete_snippet(row_idx)


def get_leaderboard_metrics() -> List[Dict[str, Any]]:
    return db_store.get_leaderboard_metrics()


def is_external_id_blocked(external_id: str, force_refresh: bool = False) -> bool:
    return db_store.is_external_id_blocked(external_id, force_refresh=force_refresh)


def find_existing_open_lead(external_id: str) -> Optional[Dict[str, Any]]:
    return db_store.find_existing_open_lead(external_id)


def save_new_sales_lead(lead: Dict[str, Any]) -> bool:
    return db_store.save_new_sales_lead(lead) is not None


def update_round_robin_cursor(new_index: int) -> bool:
    return db_store.update_round_robin_cursor(new_index)


def update_lead_owner_by_channel(channel_id: str, new_owner_slack_id: str,
                                 external_id: Optional[str] = None) -> bool:
    return db_store.update_lead_owner_by_channel(channel_id, new_owner_slack_id, external_id)


def find_delivery_mapping_by_external_id(external_id: str) -> Optional[Dict[str, Any]]:
    return db_store.find_delivery_mapping_by_external_id(external_id)

