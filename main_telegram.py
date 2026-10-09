import uuid
import time
from slack_log_handler import setup_slack_logging
import os
import sys
import logging
import asyncio
import requests
import re
from pathlib import Path
from io import BytesIO
import json
import traceback
from typing import Optional
from slack_format import for_client
from telethon.tl import types
import aiohttp
from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient, events
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from aiohttp import web
import threading
from collections import deque

import sales_provisioner
import sales_store
import db_store
import critical_alerts

# Client mappings come from the shared data layer
from g_sheets_client import get_client_mappings

# --- 1. INITIALIZATION AND CONFIGURATION ---

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
setup_slack_logging()
# Load configuration from environment - TELEGRAM SPECIFIC TOKENS
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
SESSION_NAME = os.getenv('SESSION_NAME', 'bitlink_telegram')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN_TELEGRAM')
SLACK_APP_TOKEN = os.getenv('SLACK_APP_TOKEN_TELEGRAM')
TELEGRAM_REFRESH_PORT = int(os.getenv("TELEGRAM_REFRESH_PORT", 8003))
SLACK_ADMIN_CHANNEL_ID = os.getenv('SLACK_ADMIN_CHANNEL_ID_tele')
SENT_ALERTS_FILE = os.getenv("TELEGRAM_ALERTS_FILE", "sent_telegram_alerts.json")
MESSAGE_MAP_FILE = os.getenv("TELEGRAM_MESSAGE_MAP_FILE", "telegram_message_map.json")

# ==============================================================================
# EMERGENCY STOP (shared with the WhatsApp bridge)
# ==============================================================================
# Same dashboard toggle as the WhatsApp bridge. When ON, no Telegram message is
# sent to or accepted from a customer. Kept as a local flag (not shared memory)
# so each bridge can be stopped independently while both honour the same switch.
_tg_stop_lock = threading.Lock()
_tg_emergency_state = {
    "active": False, "since": None,
    "inbound_dropped": 0, "outbound_blocked": 0, "outbox_held": 0,
}


def tg_is_stopped() -> bool:
    with _tg_stop_lock:
        return _tg_emergency_state["active"]


def tg_note(**deltas):
    with _tg_stop_lock:
        for k, v in deltas.items():
            _tg_emergency_state[k] = _tg_emergency_state.get(k, 0) + v


def tg_snapshot() -> dict:
    with _tg_stop_lock:
        return dict(_tg_emergency_state)


def tg_set_stopped(active: bool):
    with _tg_stop_lock:
        if _tg_emergency_state["active"] == active:
            return
        _tg_emergency_state["active"] = active
        if active:
            _tg_emergency_state["since"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _tg_emergency_state["inbound_dropped"] = 0
            _tg_emergency_state["outbound_blocked"] = 0
            _tg_emergency_state["outbox_held"] = 0
    if active:
        logging.critical("🛑 TELEGRAM EMERGENCY STOP ENGAGED - all Telegram traffic halted.")
        _report("EMERGENCY_STOP_ON",
                "Client intake is paused. Telegram messages are not being delivered.",
                "The kill switch engaged; all Telegram traffic is halted. Inbound "
                "messages are discarded and nothing is sent to customers.",
                {"client_key": "emergency_stop"})
    else:
        s = tg_snapshot()
        logging.critical(
            f"▶️ TELEGRAM EMERGENCY STOP RELEASED. {s.get('inbound_dropped', 0)} inbound "
            f"message(s) discarded, {s.get('outbound_blocked', 0)} outbound send(s) blocked."
        )
        _report("EMERGENCY_STOP_OFF",
                "Client intake has resumed.",
                f"The kill switch was released. {s.get('inbound_dropped', 0)} inbound "
                f"message(s) were discarded and {s.get('outbound_blocked', 0)} "
                f"outbound send(s) were blocked while stopped.",
                {"client_key": "emergency_stop",
                 "inbound_dropped": s.get("inbound_dropped", 0),
                 "outbound_blocked": s.get("outbound_blocked", 0)},
                severity="info")
        if s.get("outbox_held"):
            logging.warning(
                f"⚠️ {s.get('outbox_held')} Telegram message(s) queued before the stop are still "
                f"pending and WILL be delivered on the next outbox flush."
            )


async def telegram_emergency_stop_watcher():
    """Polls the dashboard toggle and halts/resumes the Telegram bridge."""
    logging.info("🚦 Telegram emergency stop watcher initialized.")
    import db_store as _db_store
    while True:
        try:
            active = bool(_db_store.get_sales_settings().get("kill_switch_autocreate", False))
            tg_set_stopped(active)
        except Exception as e:
            logging.error(f"[TG STOP] Could not read kill switch state: {e}")
        await asyncio.sleep(5)

if not all([API_ID, API_HASH, SLACK_BOT_TOKEN]):
    logging.critical("FATAL: Missing one or more required environment variables.")
    sys.exit(1)

# --- 2. GLOBAL VARIABLES AND CLIENTS ---
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")
Path(SESSIONS_DIR).mkdir(parents=True, exist_ok=True)
telethon_client = TelegramClient(os.path.join(SESSIONS_DIR, SESSION_NAME), API_ID, API_HASH)
slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

main_loop, aiohttp_session = None, None
telegram_to_slack_map, slack_to_telegram_map = {}, {}
slack_channel_state = {}
sent_alerts = set()
socket_mode_active = False
slack_bot_user_id = None
slack_bot_id = None
manager_bot_id = None
manager_bot_user_id = None
event_count = 0

DOWNLOAD_DIR = Path("temp_downloads_telegram")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# IMPROVEMENT #2: Better deduplication with client_msg_id
processed_slack_events = deque(maxlen=1000)  # Increased capacity
posted_telegram_bridge_events = deque(maxlen=2000)

# IMPROVEMENT #3: Message ID mapping for edit/delete support
# Format: "channel_id:ts" -> telegram_message_id
slack_to_telegram_message_map = {}

# Reverse map: telegram_msg_id -> {channel, ts, sender_name}
REVERSE_MAP_FILE = os.getenv("TELEGRAM_REVERSE_MAP_FILE", "telegram_reverse_map.json")
telegram_to_slack_message_map = {}

# IMPROVEMENT #1: Background worker queue
event_queue = None  # Will be initialized as asyncio.Queue

# --- Outbox and Slack Reaction Helpers ---
TG_OUTBOX_PATH = Path("telegram_outbox.jsonl")
TG_OUTBOX_LOCK = asyncio.Lock()
TG_OUTBOX_MAX_ATTEMPTS = 5
TG_OUTBOX_FLUSH_INTERVAL = 10

PRIMARY_SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
fallback_slack_client = AsyncWebClient(token=PRIMARY_SLACK_BOT_TOKEN) if PRIMARY_SLACK_BOT_TOKEN else slack_client

async def add_slack_reaction_async(channel: str, ts: str, name: str = "white_check_mark"):
    """Safely adds an emoji reaction to a Slack message with automatic token fallback."""
    if not channel or not ts:
        return
    for client in [slack_client, fallback_slack_client]:
        try:
            await client.reactions_add(channel=channel, timestamp=ts, name=name)
            return
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "already_reacted":
                return
            if err == "missing_scope" and client is not fallback_slack_client:
                continue
            logging.debug(f"Slack reaction {name} could not be added to {ts}: {e}")
            break
        except Exception as e:
            logging.debug(f"Error adding reaction {name}: {e}")
            break

async def remove_slack_reaction_async(channel: str, ts: str, name: str = "x"):
    """Safely removes an emoji reaction from a Slack message."""
    if not channel or not ts:
        return
    for client in [slack_client, fallback_slack_client]:
        try:
            await client.reactions_remove(channel=channel, timestamp=ts, name=name)
            return
        except Exception:
            pass

async def queue_telegram_outbox(entry: dict):
    entry = dict(entry)
    entry.setdefault("id", str(uuid.uuid4()))
    entry.setdefault("attempts", 0)
    entry.setdefault("created_at", time.time())
    line = json.dumps(entry, ensure_ascii=False)
    async with TG_OUTBOX_LOCK:
        try:
            with open(TG_OUTBOX_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            logging.info(f"Queued message {entry['id']} to Telegram outbox.")
        except Exception as e:
            logging.error(f"Failed to write to telegram outbox: {e}")

async def flush_telegram_outbox():
    if not TG_OUTBOX_PATH.exists():
        return
    if tg_is_stopped():
        try:
            pending = sum(1 for ln in TG_OUTBOX_PATH.read_text(encoding="utf-8").splitlines() if ln.strip())
        except Exception:
            pending = -1
        with _tg_stop_lock:
            _tg_emergency_state["outbox_held"] = pending
        if pending:
            logging.warning(
                f"🛑 Telegram emergency stop: holding {pending} queued outbound message(s) "
                f"that were queued before the stop."
            )
        return
    async with TG_OUTBOX_LOCK:
        try:
            with open(TG_OUTBOX_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception as e:
            logging.error(f"Failed to read telegram outbox: {e}")
            return

        remaining = []
        for ln in lines:
            if not ln.strip():
                continue
            try:
                item = json.loads(ln)
            except Exception:
                continue

            attempts = item.get("attempts", 0)
            channel_id = item.get("channel_id")
            slack_ts = item.get("slack_ts")
            telegram_id = item.get("telegram_id")

            if attempts >= TG_OUTBOX_MAX_ATTEMPTS:
                await add_slack_reaction_async(channel_id, slack_ts, "x")
                try:
                    await slack_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=slack_ts,
                        text=f"⚠️ Message queued for Telegram failed permanently after {TG_OUTBOX_MAX_ATTEMPTS} attempts."
                    )
                except Exception:
                    pass
                file_path = item.get("file_path")
                if file_path and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except Exception: pass
                continue

            # Try sending
            try:
                msg = None
                if item.get("is_file"):
                    file_path = item.get("file_path")
                    filename = item.get("filename", "file.bin")
                    caption = item.get("caption", "")
                    if file_path and os.path.exists(file_path):
                        msg = await telethon_client.send_file(telegram_id, file_path, caption=caption, force_document=True)
                        try: os.remove(file_path)
                        except Exception: pass
                    else:
                        logging.error(f"Outbox file {file_path} no longer exists. Skipping.")
                        continue
                else:
                    text = item.get("text", "")
                    msg = await telethon_client.send_message(telegram_id, text)

                if msg:
                    map_key = f"{channel_id}:{slack_ts}"
                    slack_to_telegram_message_map[map_key] = {
                        "telegram_id": telegram_id,
                        "telegram_msg_id": msg.id
                    }
                    save_message_map()

                    await remove_slack_reaction_async(channel_id, slack_ts, "x")
                    await add_slack_reaction_async(channel_id, slack_ts, "white_check_mark")
                    try:
                        await slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=slack_ts,
                            text="✅ Previously queued message delivered to Telegram."
                        )
                    except Exception:
                        pass
                    continue
                else:
                    item["attempts"] = attempts + 1
                    remaining.append(json.dumps(item, ensure_ascii=False))
            except Exception as e:
                logging.error(f"Error flushing telegram outbox item: {e}")
                item["attempts"] = attempts + 1
                remaining.append(json.dumps(item, ensure_ascii=False))

        try:
            tmp = TG_OUTBOX_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(remaining) + ("\n" if remaining else ""))
            tmp.replace(TG_OUTBOX_PATH)
        except Exception as e:
            logging.error(f"Failed to write telegram outbox atomically: {e}")

async def periodic_telegram_outbox_worker():
    logging.info("Telegram Outbox background worker started.")
    while True:
        try:
            await flush_telegram_outbox()
        except Exception as e:
            logging.error(f"Telegram outbox worker error: {e}")
        await asyncio.sleep(TG_OUTBOX_FLUSH_INTERVAL)

async def sync_slack_downtime_telegram():
    """
    On startup, inspects active Telegram channels for any messages sent by agents
    while the BitLink bridge was down (messages that have no ✅ reaction).
    """
    await asyncio.sleep(3)
    if tg_is_stopped():
        logging.warning(
            "🛑 Telegram emergency stop is active - skipping downtime catch-up. "
            "Queued agent messages will not be replayed to customers."
        )
        return
    logging.info("🔄 [CATCH-UP] Scanning Slack for messages sent to Telegram during downtime...")
    channels = list(slack_to_telegram_map.keys())
    recovered_count = 0

    for channel_id in channels:
        try:
            res = await slack_client.conversations_history(
                channel=channel_id,
                oldest=str(time.time() - 86400),
                limit=15
            )
            messages = res.get("messages", [])
            for msg in reversed(messages):
                ts = msg.get("ts")
                subtype = msg.get("subtype")
                if msg.get("bot_id") or msg.get("username") or (subtype and subtype not in ["file_share"]):
                    continue
                if msg.get("user") == slack_bot_user_id:
                    continue

                reactions = [r.get("name") for r in msg.get("reactions", [])]
                if "white_check_mark" in reactions:
                    continue

                text = (msg.get("text") or "").strip()
                if text.startswith("!!"):
                    continue

                map_key = f"{channel_id}:{ts}"
                if map_key in slack_to_telegram_message_map:
                    continue

                logging.info(f"🔄 [CATCH-UP] Recovered unsent message {ts} in channel {channel_id}. Forwarding to Telegram...")
                msg_copy = dict(msg)
                msg_copy["channel"] = channel_id
                await process_slack_event_async(msg_copy, SLACK_BOT_TOKEN)
                recovered_count += 1
                await asyncio.sleep(0.5)
        except Exception as e:
            logging.warning(f"[CATCH-UP] Error checking downtime messages in Telegram channel {channel_id}: {e}")

    if recovered_count > 0:
        logging.info(f"✅ [CATCH-UP] Delivered {recovered_count} messages sent during downtime to Telegram.")
    else:
        logging.info("✅ [CATCH-UP] No unsent downtime messages found for Telegram.")

# --- 3. MESSAGE MAPPING PERSISTENCE ---

def load_message_map():
    """Loads the Slack->Telegram message ID mapping from disk."""
    if not os.path.exists(MESSAGE_MAP_FILE):
        return {}
    try:
        with open(MESSAGE_MAP_FILE, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Could not load message map: {e}")
        return {}

def save_message_map():
    """Saves the message map to disk."""
    try:
        # Only keep last 10,000 mappings to prevent file from growing infinitely
        if len(slack_to_telegram_message_map) > 10000:
            # Keep only the most recent 5000
            items = list(slack_to_telegram_message_map.items())
            new_map = dict(items[-5000:])
            slack_to_telegram_message_map.clear()
            slack_to_telegram_message_map.update(new_map)

        with open(MESSAGE_MAP_FILE, 'w') as f:
            json.dump(slack_to_telegram_message_map, f, indent=2)
    except IOError as e:
        logging.error(f"Could not write message map: {e}")

def load_reverse_map():
    """Loads the Telegram->Slack message ID reverse mapping from disk."""
    if not os.path.exists(REVERSE_MAP_FILE):
        return {}
    try:
        with open(REVERSE_MAP_FILE, 'r') as f:
            data = json.load(f)
            return {str(k): v for k, v in data.items()}
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Could not load reverse map: {e}")
        return {}

def save_reverse_map():
    """Saves the Telegram->Slack reverse map to disk, capped at 5000 entries."""
    try:
        if len(telegram_to_slack_message_map) > 10000:
            items = list(telegram_to_slack_message_map.items())
            new_map = dict(items[-5000:])
            telegram_to_slack_message_map.clear()
            telegram_to_slack_message_map.update(new_map)
        with open(REVERSE_MAP_FILE, 'w') as f:
            json.dump({str(k): v for k, v in telegram_to_slack_message_map.items()}, f, indent=2)
    except IOError as e:
        logging.error(f"Could not write reverse map: {e}")

# --- 4. CONFIGURATION MANAGEMENT ---

async def slack_post_message_with_retry(channel_id: str, text: str, max_retries: int = 5):
    """Posts a message to Slack with handling for 429 and transient errors."""
    delay = 1
    for attempt in range(max_retries):
        try:
            await slack_client.chat_postMessage(channel=channel_id, text=text)
            return True
        except SlackApiError as e:
            try:
                status = getattr(e.response, 'status_code', None)
                err = None
                try:
                    err = e.response.get('error')
                except Exception:
                    err = None
                if status == 429 or err == 'ratelimited':
                    retry_after = 0
                    try:
                        retry_after = int(e.response.headers.get('Retry-After', '0'))
                    except Exception:
                        retry_after = 0
                    wait_seconds = retry_after if retry_after > 0 else delay
                    logging.warning(f"Slack rate limit hit. Waiting {wait_seconds}s before retry.")
                    await asyncio.sleep(wait_seconds)
                    delay = min(delay * 2, 30)
                    continue
            except Exception:
                pass
            logging.warning(f"Slack post failed: {e}. Backing off {delay}s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as e:
            logging.warning(f"Unexpected error posting to Slack: {e}. Backing off {delay}s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    return False

async def reload_config():
    global telegram_to_slack_map, slack_to_telegram_map, slack_channel_state
    loop = asyncio.get_running_loop()
    logging.info("(Telegram Bridge) Refresh signal received! Reloading config...")

    # Base Layer: Load Sales Leads
    sales_mappings = []
    try:
        sales_leads = await loop.run_in_executor(None, db_store.get_sales_leads)
        for lead in sales_leads:
            if str(lead.get("platform", "")).strip().lower() != "telegram":
                continue
            ext_id = str(lead.get("external_id", "")).strip()
            ch_id = str(lead.get("slack_channel_id", "")).strip()
            if ext_id and ch_id:
                try:
                    tg_id = int(ext_id)
                except ValueError:
                    tg_id = ext_id
                sales_mappings.append({
                    "client_name": lead.get("client_name", "Telegram Lead"),
                    "external_id": ext_id,
                    "telegram_id": tg_id,
                    "slack_channel_id": ch_id,
                    "paused": str(lead.get("paused", False)).strip().lower() in ["true", "1", "yes"],
                    "delivery_handed_off": bool(lead.get("delivery_handed_off")),
                    "type": "sales"
                })
    except Exception as e:
        logging.warning(f"Could not load Sales Leads during reload_config: {e}")

    # Priority Layer: Load Delivery Mappings from PostgreSQL
    delivery_mappings = []
    mappings_raw = await loop.run_in_executor(None, get_client_mappings, "Telegram") or []
    for c in mappings_raw:
        ext_id = str(c.get("external_id", "")).strip()
        ch_id = str(c.get("slack_channel_id", "")).strip()
        if ext_id and ch_id:
            try:
                tg_id = int(ext_id)
            except ValueError:
                tg_id = ext_id
            delivery_mappings.append({
                "client_name": c.get("client_name", "Telegram Client"),
                "external_id": ext_id,
                "telegram_id": tg_id,
                "slack_channel_id": ch_id,
                "paused": c.get("paused", False),
                "type": "delivery"
            })

    new_telegram_map = {}
    new_slack_map = {}

    for item in sales_mappings:
        new_telegram_map[str(item["external_id"])] = item
        new_telegram_map[str(item["telegram_id"])] = item
        new_slack_map[item["slack_channel_id"]] = item

    for item in sorted(delivery_mappings, key=lambda m: 1 if not m.get("paused") else 0):
        new_telegram_map[str(item["external_id"])] = item
        new_telegram_map[str(item["telegram_id"])] = item
        new_slack_map[item["slack_channel_id"]] = item

    for new_channel_id in new_slack_map:
        if new_channel_id not in slack_to_telegram_map:
            logging.info(f"New client channel found: {new_channel_id}. Initializing state.")
            try:
                response = await slack_client.conversations_history(channel=new_channel_id, limit=1)
                if response.get("messages"):
                    slack_channel_state[new_channel_id] = response["messages"][0]['ts']
            except Exception as e:
                logging.error(f"Could not initialize state for new channel {new_channel_id}: {e}")

    telegram_to_slack_map = new_telegram_map
    slack_to_telegram_map = new_slack_map
    logging.info(f"(Telegram Bridge) Configuration reloaded. Now tracking {len(telegram_to_slack_map)} clients.")

async def handle_refresh(request):
    """Endpoint handler that triggers the config reload as a background task."""
    asyncio.create_task(reload_config())
    return web.Response(text="Refresh signal received.")

async def handle_test_alert(request):
    """Sends a test alert to the admin Slack channel to verify wiring."""
    try:
        if not SLACK_ADMIN_CHANNEL_ID:
            return web.Response(status=400, text="SLACK_ADMIN_CHANNEL_ID_tele not set")
        await slack_post_message_with_retry(SLACK_ADMIN_CHANNEL_ID, "🔧 Telegram bridge test: admin alert path is working.")
        return web.Response(text="Test alert sent.")
    except Exception as e:
        logging.error(f"Test alert failed: {e}")
        return web.Response(status=500, text=f"Error: {e}")
# Place this new function before run_refresh_server in main_telegram.py
async def handle_send_message(request):
    """Endpoint to receive a send command and forward to Telegram."""
    if tg_is_stopped():
        tg_note(outbound_blocked=1)
        return web.Response(status=503, text="Emergency stop is active - sending is halted")
    try:
        data = await request.json()
        recipient_id = data.get("recipient_id")
        text = data.get("text")

        if not recipient_id or not text:
            return web.Response(status=400, text="Missing recipient_id or text")

        # Global Blocklist Check (Outbound)
        if sales_store.is_external_id_blocked(str(recipient_id)):
            logging.warning(f"🛡️ [BLOCKLIST] Telegram recipient {recipient_id} is in BLOCKLIST. Outbound message dropped.")
            return web.Response(status=403, text="Recipient is blocked in blocklist.")

        # Use asyncio.create_task to send without blocking the server
        asyncio.create_task(telethon_client.send_message(int(recipient_id), text))

        logging.info(f"Received send command. Queued message for Telegram ID: {recipient_id}")
        return web.Response(text="Message queued for sending.")
    except Exception as e:
        logging.error(f"Error in handle_send_message: {e}")
        return web.Response(status=500, text=f"Error: {e}")

async def handle_health(request):
    """Health check endpoint consumed by control_server.py's dashboard monitor."""
    snap = tg_snapshot()
    # Must reflect the emergency stop. Reporting a hardcoded "ready" while
    # traffic is halted makes the bridge report healthy during an outage, which
    # stops the dashboard watcher from ever noticing.
    return web.json_response({
        "status": "not_ready" if snap.get("active") else "ready",
        "ok": not snap.get("active"),
        "bridge": "telegram",
        "clients": len(telegram_to_slack_map),
        "emergency_stop": snap,
    })


async def run_refresh_server():
    """Runs the aiohttp server to listen for the refresh signal."""
    app = web.Application()
    app.add_routes([
        web.get('/health', handle_health),
        web.post('/refresh', handle_refresh),
        web.post('/test-alert', handle_test_alert),
        web.post('/send', handle_send_message)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', TELEGRAM_REFRESH_PORT)
    await site.start()
    logging.info(f"Telegram refresh server listening on port {TELEGRAM_REFRESH_PORT}")
    while True:
        await asyncio.sleep(3600)

# --- 5. CORE LOGIC (Telegram -> Slack) ---

def _resolve_sender_name(sender) -> Optional[str]:
    """Best-effort display name for whoever sent a message, or None if unknown.

    In a group the sender is normally the member who wrote the message, but it
    can also be a Channel (an anonymous admin posting, or a message sent as the
    channel) or be missing altogether, so every attribute is read defensively
    instead of assuming the sender is a User with a ``first_name``.
    """
    if sender is None:
        return None

    # "Posted as" wins: it is the name the group itself displays.
    post_author = getattr(sender, "post_author", None)
    if post_author:
        return post_author

    full_name = " ".join(
        part for part in (getattr(sender, "first_name", None),
                          getattr(sender, "last_name", None)) if part
    )
    if full_name:
        return full_name

    return getattr(sender, "username", None) or getattr(sender, "title", None)


async def get_sender_details(event):
    """Gets sender's name and profile picture URL."""
    sender = await event.get_sender()
    pfp_path = None

    name = _resolve_sender_name(sender)
    photo_source = sender if name else None

    if not name:
        # No usable sender entity. Only fall back to the chat identity for a
        # direct message: in a group the chat title is the *group's* name, and
        # using it here would credit the message to the group, not the member.
        chat = getattr(event, "chat", None)
        if isinstance(chat, types.User):
            name = getattr(chat, "first_name", None) or getattr(chat, "username", None)
            photo_source = chat
        else:
            name = getattr(getattr(event, "message", None), "post_author", None)

    if photo_source is not None:
        try:
            pfp_path = await telethon_client.download_profile_photo(
                photo_source,
                file=DOWNLOAD_DIR / f"{photo_source.id}_pfp.jpg"
            )
        except Exception:
            pfp_path = None

    return name or "Unknown User", pfp_path

def _report(code: str, summary_plain: str, technical_detail: str, context: dict,
            severity: str = "critical", tb_text: Optional[str] = None) -> None:
    """Push a Telegram bridge failure to the developer channel.

    Never raises. The async event handler this is called from must keep running
    even when the alerting path is broken.
    """
    try:
        critical_alerts.report(code=code, severity=severity, component="telegram_bridge",
                               summary_plain=summary_plain, technical_detail=technical_detail,
                               context=context, key=context.get("client_key"), tb_text=tb_text)
    except Exception as e:
        logging.error(f"Alert reporting for {code} failed: {e}")


def _current_traceback() -> Optional[str]:
    """Traceback of the exception being handled, or None when there is not one.

    Passing a literal 'NoneType: None' would overwrite a real traceback when the
    same alert recurs, so this returns None instead of formatting an empty one.
    """
    return traceback.format_exc() if sys.exc_info()[0] is not None else None


@telethon_client.on(events.NewMessage(incoming=True, forwards=False))
async def handle_telegram_message(event):
    """Listens for new inbound Telegram messages and forwards them to Slack."""
    logging.info(f"DEBUG: New message received from Chat ID: {event.chat_id}")
    if event.out: return

    if tg_is_stopped():
        tg_note(inbound_dropped=1)
        logging.warning("🛑 Telegram emergency stop: inbound message discarded.")
        return

    chat_id = str(event.chat_id)

    # 1. Blocklist check
    if sales_store.is_external_id_blocked(chat_id):
        logging.info(f"🛡️ [BLOCKLIST] Sender {chat_id} is in Blocklist. Dropping inbound message.")
        return

    if chat_id not in telegram_to_slack_map:
        # Provision telegram lead
        sender_name = "Unknown"
        group_name = None
        try:
            sender = await event.get_sender()
            sender_name = _resolve_sender_name(sender) or "Unknown"
        except Exception:
            pass

        chat = getattr(event, "chat", None)
        if chat and hasattr(chat, "title") and chat.title:
            group_name = chat.title

        msg_dict = {
            "chatId": chat_id,
            "senderId": chat_id,
            "senderName": sender_name,
            "groupName": group_name,
            "isGroup": event.is_group or event.is_channel or str(chat_id).startswith("-"),
            "body": event.text or ""
        }

        loop = asyncio.get_running_loop()

        # Check if client belongs to Delivery
        delivery_rec = await loop.run_in_executor(None, db_store.find_delivery_mapping_by_external_id, str(chat_id))
        if delivery_rec and delivery_rec.get("slack_channel_id"):
            del_ch = delivery_rec["slack_channel_id"]
            client_info = {
                "client_name": delivery_rec.get("client_name", "Telegram Client"),
                "platform": "telegram",
                "external_id": str(chat_id),
                "telegram_id": chat_id,
                "slack_channel_id": del_ch,
                "paused": delivery_rec.get("paused", False),
                "type": "delivery"
            }
            telegram_to_slack_map[chat_id] = client_info
            slack_to_telegram_map[del_ch] = client_info
            logging.info(f"✅ Routed incoming Telegram message from delivery client {chat_id} directly to Delivery channel {del_ch}")
        else:
            logging.info(f"TG-IN NEW LEAD: chat_id={chat_id!r} (sender={msg_dict['senderName']}). Triggering Sales Provisioner...")
            from slack_sdk import WebClient
            web_client_sync = WebClient(token=SLACK_BOT_TOKEN)

            provision_res = await loop.run_in_executor(None, sales_provisioner.provision_telegram_lead, msg_dict, web_client_sync)

            status = provision_res.get("status")

            if status in ["provisioned", "deduplicated"]:
                slack_channel = provision_res.get("channel_id")
                client_info = provision_res.get("lead") or {
                    "client_name": provision_res.get("channel_name", f"sale-tg-{chat_id}"),
                    "platform": "telegram",
                    "whatsapp_chat_id": chat_id,
                    "external_id": chat_id,
                    "slack_channel_id": slack_channel,
                    "paused": False
                }
                telegram_to_slack_map[chat_id] = client_info
                slack_to_telegram_map[slack_channel] = client_info
                logging.info(f"✅ Real-time mapping established: {chat_id} <-> {slack_channel}")
            else:
                logging.info(f"Lead intake status for {chat_id}: {status}. Skipping forward.")
                return

    client_info = telegram_to_slack_map[chat_id]
    slack_channel_id = client_info["slack_channel_id"]

    # CHECK IF CHANNEL IS PAUSED OR HANDED OFF TO DELIVERY
    if client_info.get("type") == "sales":
        is_handed_off = bool(client_info.get("delivery_handed_off"))
        if not is_handed_off:
            loop = asyncio.get_running_loop()
            is_handed_off = await loop.run_in_executor(None, db_store.is_client_in_delivery, str(chat_id))
        if is_handed_off:
            logging.info(f"Sales channel {slack_channel_id} is handed off to delivery. Skipping Telegram→Slack forwarding.")
            return
        if client_info.get("paused", False):
            logging.info(f"Sales channel {slack_channel_id} is paused. Skipping Telegram→Slack forwarding.")
            return
    elif client_info.get("paused", False):
        logging.info(f"Delivery channel {slack_channel_id} is paused. Skipping Telegram→Slack forwarding.")
        return

    client_name = client_info["client_name"]  # ← CRITICAL: Get client name from mapping

    sender_name, pfp_path = await get_sender_details(event)
    message_text = event.message.text

    try:
        if event.message.media:
            logging.info(f"Media message received from '{sender_name}' ({chat_id}). Downloading...")
            file_path = None
            try:
                file_path = await telethon_client.download_media(event.message, file=DOWNLOAD_DIR)
                if not file_path or not os.path.exists(file_path):
                    logging.error(f"Failed to download media from Telegram message in {chat_id}")
                    _report("MEDIA_DOWNLOAD_FAILED",
                            f"A file from {sender_name} could not be retrieved.",
                            "Telethon download_media returned no usable file path.",
                            {"sender_name": sender_name, "chat_id": chat_id,
                             "message_id": getattr(event.message, "id", None),
                             "slack_channel": slack_channel_id,
                             "client_key": str(chat_id)})
                    return

                file_name = Path(file_path).name
                caption_text = message_text.strip() if message_text else ""
                if caption_text:
                    initial_comment = f"*{sender_name}:*\n{caption_text}"
                else:
                    initial_comment = f"*{sender_name}* sent a file: {file_name}"

                # Upload directly into the Slack channel using Telegram bot
                upload_resp = None
                try:
                    upload_resp = await slack_client.files_upload_v2(
                        channel=slack_channel_id,
                        file=file_path,
                        filename=file_name,
                        title=file_name,
                        initial_comment=initial_comment
                    )
                except SlackApiError as e:
                    logging.error(f"Slack API error uploading file to Slack channel {slack_channel_id}: {e.response.get('error')}")
                    _report("MEDIA_UPLOAD_FAILED",
                            f"A file from {sender_name} could not be processed.",
                            f"SlackApiError: {e.response.get('error')}",
                            {"sender_name": sender_name, "chat_id": chat_id,
                             "message_id": getattr(event.message, "id", None),
                             "slack_channel": slack_channel_id, "file_name": file_name,
                             "client_key": str(chat_id)},
                            tb_text=_current_traceback())
                except Exception as up_err:
                    logging.error(f"Critical error uploading file to Slack channel {slack_channel_id}: {up_err}")
                    _report("MEDIA_UPLOAD_FAILED",
                            f"A file from {sender_name} could not be processed.",
                            f"{type(up_err).__name__}: {up_err}",
                            {"sender_name": sender_name, "chat_id": chat_id,
                             "message_id": getattr(event.message, "id", None),
                             "slack_channel": slack_channel_id, "file_name": file_name,
                             "client_key": str(chat_id)},
                            tb_text=_current_traceback())

                if upload_resp:
                    file_info = upload_resp.get("file") or (upload_resp.get("files") and upload_resp["files"][0]) or {}
                    shares = file_info.get("shares", {})
                    channel_shares = shares.get("public", {}).get(slack_channel_id) or shares.get("private", {}).get(slack_channel_id) or []
                    slack_ts = channel_shares[0].get("ts") if channel_shares else file_info.get("timestamp")

                    if slack_ts:
                        posted_telegram_bridge_events.append((slack_channel_id, slack_ts))
                        if event.message.id:
                            map_key = f"{event.chat_id}_{event.message.id}"
                            telegram_to_slack_message_map[map_key] = {
                                "channel": slack_channel_id,
                                "ts": slack_ts,
                                "sender_name": sender_name
                            }
                            save_reverse_map()

                    logging.info(f"Forwarded file '{file_name}' from '{sender_name}' to Slack channel {slack_channel_id}.")
                else:
                    logging.error(f"Could not upload file '{file_name}' to Slack channel {slack_channel_id}.")
            except Exception as e:
                logging.error(f"Error handling Telegram media message: {e}", exc_info=True)
                _report("MEDIA_HANDLER_ERROR",
                        f"A message from {sender_name} could not be delivered.",
                        f"{type(e).__name__}: {e}",
                        {"sender_name": sender_name, "chat_id": chat_id,
                         "message_id": getattr(event.message, "id", None),
                         "slack_channel": slack_channel_id,
                         "client_key": str(chat_id)},
                        tb_text=_current_traceback())
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        elif message_text:
            # For text messages, we can customize the user profile
            slack_response = await slack_client.chat_postMessage(
                channel=slack_channel_id,
                text=f"{sender_name}: {message_text}",
                username=sender_name,
                icon_emoji=":robot_face:",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
            )
            slack_ts = slack_response.get("ts")
            if slack_ts:
                posted_telegram_bridge_events.append((slack_channel_id, slack_ts))
                if event.message.id:
                    map_key = f"{event.chat_id}_{event.message.id}"
                    telegram_to_slack_message_map[map_key] = {
                        "channel": slack_channel_id,
                        "ts": slack_ts,
                        "sender_name": sender_name
                    }
                    if len(telegram_to_slack_message_map) > 10000:
                        oldest = next(iter(telegram_to_slack_message_map))
                        del telegram_to_slack_message_map[oldest]
                    save_reverse_map()
            logging.info(f"Forwarded text from '{sender_name}' to Slack.")

        if pfp_path and os.path.exists(pfp_path):
            os.remove(pfp_path) # Clean up profile picture

    except SlackApiError as e:
        logging.error(f"Slack API Error forwarding from Telegram: {e.response['error']}")
        _report("SLACK_FORWARD_FAILED",
                "A message from Telegram could not be delivered to the team channel.",
                f"SlackApiError: {e.response.get('error')}",
                {"chat_id": getattr(event.chat_id, "chat_id", event.chat_id),
                 "message_id": getattr(event.message, "id", None),
                 "slack_channel": slack_channel_id,
                 "client_key": str(getattr(event.chat_id, "chat_id", event.chat_id))},
                tb_text=_current_traceback())
    except Exception as e:
        logging.error(f"An unexpected error occurred in handle_telegram_message: {e}", exc_info=True)
        _report("FORWARD_HANDLER_ERROR",
                "A message from Telegram could not be delivered.",
                f"{type(e).__name__}: {e}",
                {"chat_id": getattr(event.chat_id, "chat_id", event.chat_id),
                 "message_id": getattr(event.message, "id", None),
                 "slack_channel": slack_channel_id,
                 "client_key": str(getattr(event.chat_id, "chat_id", event.chat_id))},
                tb_text=_current_traceback())

@telethon_client.on(events.MessageEdited)
async def handle_telegram_edit(event):
    """Forwards Telegram message edits to Slack via chat_update."""
    if tg_is_stopped():
        tg_note(inbound_dropped=1)
        return
    if event.message is None or not event.message.text:
        return
    telegram_msg_id = event.message.id
    map_key = f"{event.chat_id}_{telegram_msg_id}"
    new_text = event.message.text
    mapping = telegram_to_slack_message_map.get(map_key)
    if not mapping:
        return
    try:
        sender_name = mapping.get("sender_name", "Client")
        await slack_client.chat_update(
            channel=mapping["channel"],
            ts=mapping["ts"],
            text=f"{sender_name}: {new_text}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}]
        )
        logging.info(f"[EDIT TG→Slack] msg {telegram_msg_id} updated in Slack")
    except SlackApiError as e:
        logging.error(f"Failed to edit Slack message: {e}")
    except Exception as e:
        logging.error(f"Error in edit handler: {e}")

@telethon_client.on(events.MessageDeleted)
async def handle_telegram_delete(event):
    """Posts a thread reply in Slack when a Telegram message is deleted."""
    if tg_is_stopped():
        tg_note(inbound_dropped=1)
        return
    for deleted_id in event.deleted_ids:
        # Telethon MessageDeleted event has chat_id in supergroups/channels,
        # but chat_id is None in private 1-on-1 chats.
        mapping = None
        if event.chat_id:
            map_key = f"{event.chat_id}_{deleted_id}"
            mapping = telegram_to_slack_message_map.pop(map_key, None)
        
        # Robust fallback: match any key ending with _{deleted_id}
        if not mapping:
            matched_key = next((k for k in telegram_to_slack_message_map if k.endswith(f"_{deleted_id}")), None)
            if matched_key:
                mapping = telegram_to_slack_message_map.pop(matched_key, None)

        if not mapping:
            logging.debug(f"[DELETE TG→Slack] No mapping found for deleted msg {deleted_id}")
            continue

        try:
            await slack_client.chat_postMessage(
                channel=mapping["channel"],
                thread_ts=mapping["ts"],
                text="🗑️ This message was deleted by the client in Telegram.",
                username="Bitlink Bridge Info"
            )
            logging.info(f"[DELETE TG→Slack] posted delete notice for msg {deleted_id}")
            save_reverse_map()
        except SlackApiError as e:
            logging.error(f"Failed to post delete notice: {e}")
        except Exception as e:
            logging.error(f"Error in delete handler: {e}")

# --- NEW CHAT ALERTS ---

def load_sent_alerts():
    if not os.path.exists(SENT_ALERTS_FILE):
        return set()
    try:
        with open(SENT_ALERTS_FILE, 'r') as f:
            return set(json.load(f))
    except (IOError, json.JSONDecodeError):
        return set()

def save_sent_alerts(alerts_set):
    try:
        with open(SENT_ALERTS_FILE, 'w') as f:
            json.dump(list(alerts_set), f, indent=4)
    except IOError:
        logging.error(f"Could not write to {SENT_ALERTS_FILE}")

async def send_new_chat_alert_to_slack(chat, chat_id, action_description):
    chat_type = "Unknown"
    if isinstance(chat, types.User):
        chat_type = "Direct Message (DM)"
    elif isinstance(chat, types.Chat):
        chat_type = "Small Group"
    elif isinstance(chat, types.Channel):
        chat_type = "Supergroup" if chat.megagroup else "Channel"

    try:
        if isinstance(chat, types.User):
            name_display = chat.first_name or chat.username or "User"
        elif hasattr(chat, 'title') and chat.title:
            name_display = chat.title
        else:
            name_display = str(chat_id)
    except Exception:
        name_display = str(chat_id)

    message_text = (
        f"🔔 *New Telegram Chat Alert*\n\n"
        f"{action_description}\n\n"
        f"• *Name:* {name_display}\n"
        f"• *Type:* {chat_type}\n"
        f"• *Chat ID:* `{chat_id}`\n\n"
        f"To map this chat, add the ID via the control dashboard."
    )

    try:
        sent_alerts.add(str(chat_id))
        save_sent_alerts(sent_alerts)

        if not SLACK_ADMIN_CHANNEL_ID:
            logging.warning("SLACK_ADMIN_CHANNEL_ID is not set. Cannot send alert.")
            return

        await slack_post_message_with_retry(SLACK_ADMIN_CHANNEL_ID, message_text)
        logging.info(f"Successfully sent new chat alert for '{name_display}'.")

    except Exception as e:
        logging.error(f"Failed to send new chat alert to Slack: {e}")

@telethon_client.on(events.NewMessage(pattern=None, forwards=False, outgoing=True))
async def handle_bot_created_chat(event):
    if not hasattr(event.message, 'action') or not isinstance(event.message.action, types.MessageActionChatCreate):
        return

    chat = await event.get_chat()
    chat_id = event.chat_id

    if str(chat_id) in telegram_to_slack_map or str(chat_id) in sent_alerts:
        return

    logging.info(f"Bot has created a new group: '{chat.title}' ({chat_id})")
    await send_new_chat_alert_to_slack(
        chat=chat,
        chat_id=chat_id,
        action_description="I have created a new group."
    )

@telethon_client.on(events.ChatAction)
async def handle_chat_actions_universal(event):
    try:
        me = await telethon_client.get_me()
        user_id = getattr(event, 'user_id', None)
        if not user_id or user_id != me.id:
            return
    except Exception:
        return

    chat = await event.get_chat()
    chat_id = event.chat_id

    if str(chat_id) in telegram_to_slack_map or str(chat_id) in sent_alerts:
        return

    logging.info(f"Bot has been added to a new chat: '{getattr(chat, 'title', chat_id)}' ({chat_id})")

    adder = await event.get_added_by()
    adder_name = _resolve_sender_name(adder) or "an unknown user"

    await send_new_chat_alert_to_slack(
        chat=chat,
        chat_id=chat_id,
        action_description=f"I have been added to a new chat by *{adder_name}*."
    )

@telethon_client.on(events.NewMessage)
async def discover_unmapped_on_message(event):
    if tg_is_stopped():
        return
    if event.out:
        return
    chat_id = str(event.chat_id)
    if chat_id in telegram_to_slack_map or chat_id in sent_alerts:
        return
    try:
        chat = await event.get_chat()
        await send_new_chat_alert_to_slack(
            chat=chat,
            chat_id=chat_id,
            action_description="I have discovered a new chat from an incoming message."
        )
    except Exception as e:
        logging.error(f"Failed to send discovery alert: {e}")
async def send_paused_notification(slack_channel_id, thread_ts):
    """Send a thread notification when trying to send from a paused channel."""
    try:
        await slack_client.chat_postMessage(
            channel=slack_channel_id,
            thread_ts=thread_ts,
            text="*Info:* This channel is currently paused. Outgoing Slack messages are not forwarded to Telegram.",
            username="Bitlink Bridge Info"
        )
        logging.info(f"Sent paused notification to Slack channel {slack_channel_id}")
    except Exception as e:
        logging.error(f"Failed to send paused notification to Slack: {e}")
# --- 6. SLACK -> TELEGRAM (Background Worker) ---

def clean_slack_formatting(text: str) -> str:
    """Prepare Slack text for Telegram.

    Delegates to the shared cleaner so Telegram and WhatsApp cannot drift
    apart: Slack's ``<...>`` markup is unwrapped (``<https://x.com>`` ->
    ``https://x.com``) and ``:shortcodes:`` become real emoji.
    """
    return for_client(text)

# IMPROVEMENT #1: Background event processor
async def process_slack_event_async(event: dict, bot_token: str):
    if tg_is_stopped():
        tg_note(outbound_blocked=1)
        logging.warning("🛑 BLOCKED by Telegram emergency stop: message not sent to customer.")
        return
    # --- Slack-to-Telegram edit functionality ---
    if event.get("type") == "message" and event.get("subtype") == "message_changed":
        channel_id = event.get("channel")
        message = event.get("message", {})
        ts = message.get("ts")
        new_text = message.get("text", "")
        map_key = f"{channel_id}:{ts}"
        mapping = slack_to_telegram_message_map.get(map_key)
        if mapping:
            telegram_id = mapping.get("telegram_id") or mapping.get("external_id")
            if isinstance(telegram_id, str) and (telegram_id.isdigit() or (telegram_id.startswith('-') and telegram_id[1:].isdigit())):
                telegram_id = int(telegram_id)
            telegram_msg_id = mapping.get("telegram_msg_id")
            try:
                # Same treatment as the initial send: an edit that adds :smile:
                # must not leave the shortcode in the Telegram message.
                await telethon_client.edit_message(
                    telegram_id, telegram_msg_id, clean_slack_formatting(new_text)
                )
                logging.info(f"Edited Telegram message {telegram_msg_id} in chat {telegram_id} due to Slack edit event")
            except Exception as e:
                logging.error(f"Failed to edit Telegram message: {e}")
        return

    # --- Slack-to-Telegram delete functionality ---
    if event.get("type") == "message" and event.get("subtype") == "message_deleted":
        channel_id = event.get("channel")
        deleted_ts = event.get("deleted_ts")
        map_key = f"{channel_id}:{deleted_ts}"
        mapping = slack_to_telegram_message_map.get(map_key)
        if mapping:
            telegram_id = mapping.get("telegram_id") or mapping.get("external_id")
            if isinstance(telegram_id, str) and (telegram_id.isdigit() or (telegram_id.startswith('-') and telegram_id[1:].isdigit())):
                telegram_id = int(telegram_id)
            telegram_msg_id = mapping.get("telegram_msg_id")
            try:
                await telethon_client.delete_messages(telegram_id, telegram_msg_id)
                logging.info(f"Deleted Telegram message {telegram_msg_id} in chat {telegram_id} due to Slack delete event")
                del slack_to_telegram_message_map[map_key]
                save_message_map()
            except Exception as e:
                logging.error(f"Failed to delete Telegram message: {e}")
        return

    """
    Async processor for Slack events. Runs in background queue.
    """
    global event_count
    event_count += 1

    try:
        event_type = event.get("type")
        subtype = event.get("subtype")
        channel_id = event.get("channel")

        if event_type != "message":
            return

        # Accept normal text messages (subtype None), file shares, thread broadcasts, and bot messages (Manager Bot)
        if subtype and subtype not in ["file_share", "thread_broadcast", "bot_message"]:
            return

        ts = event.get("ts")
        user = event.get("user")
        bot_id = event.get("bot_id")
        username = event.get("username")
        icons = event.get("icons", {})

        if not channel_id or not ts:
            return

        # 1. Block any message that has username set (messages forwarded to Slack on behalf of external clients)
        if username:
            logging.debug(f"[SKIP ECHO] Dropping message with username='{username}' ch={channel_id}")
            return

        # 2. Block icons that indicate a bridged client message
        if icons and isinstance(icons, dict):
            emoji = icons.get("emoji", "")
            if emoji in [":bust_in_silhouette:", ":robot_face:"]:
                logging.debug(f"[SKIP ECHO] Dropping message with bridge icon '{emoji}' ch={channel_id}")
                return

        # 3. Block self-echoes from this and all known bridge bots
        KNOWN_BRIDGE_BOT_IDS = {slack_bot_id, "B0C3L7FGLRY", "B0C3FBURUE9"} - {None}
        KNOWN_BRIDGE_USER_IDS = {slack_bot_user_id, "U0C3L7FKA7L", "U0C3E3DRC9H"} - {None}
        if bot_id and bot_id in KNOWN_BRIDGE_BOT_IDS:
            return
        if user and user in KNOWN_BRIDGE_USER_IDS:
            return

        # 4. Block messages whose (channel, ts) were posted by the bridge
        if (channel_id, ts) in posted_telegram_bridge_events:
            return

        # 5. Check for bot messages: ONLY allow the Manager Bot (for AI replies / snippets)
        if bot_id or subtype == "bot_message":
            master_bot_user_id = os.getenv("MASTER_SLACK_BOT_ID")
            is_manager = False
            if manager_bot_id and bot_id == manager_bot_id:
                is_manager = True
            elif manager_bot_user_id and user == manager_bot_user_id:
                is_manager = True
            elif master_bot_user_id and (user == master_bot_user_id or bot_id == master_bot_user_id):
                is_manager = True

            if not is_manager:
                return

            msg_text = event.get("text", "") or ""
            if any(marker in msg_text for marker in [
                "Lead Claimed:", "Lead Handed Off:", "New Telegram Lead", "Sales Summary", "Claim Lead"
            ]):
                logging.info("[SKIP] Ignoring internal Manager Bot notification.")
                return

        # Deduplication
        client_msg_id = event.get("client_msg_id")
        if client_msg_id:
            dedup_key = f"client:{client_msg_id}"
        else:
            dedup_key = f"ts:{channel_id}:{ts}"

        if dedup_key in processed_slack_events:
            return
        processed_slack_events.append(dedup_key)

        # Map to Telegram
        mapping = slack_to_telegram_map.get(channel_id)
        if not mapping:
            return

        telegram_id = mapping.get("telegram_id") or mapping.get("external_id")
        loop = asyncio.get_running_loop()

        # Check delivery handoff & paused status
        is_handed_off = bool(mapping.get("delivery_handed_off", False))
        if not is_handed_off and mapping.get("type") == "sales":
            is_handed_off = await loop.run_in_executor(None, db_store.is_client_in_delivery, str(telegram_id or ""))

        if is_handed_off:
            logging.info(f"Sales channel {channel_id} is handed over to delivery. Blocking Slack→Telegram message.")
            try:
                await slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=ts,
                    text="*Info:* This client has been handed over to delivery. The sales channel is permanently paused and cannot send or receive messages. Please communicate via the delivery channel.",
                    username="Bitlink Bridge Info"
                )
            except Exception as e:
                logging.error(f"Failed to post handoff info to Slack: {e}")
            return

        # CHECK IF CHANNEL IS PAUSED
        if mapping.get("paused", False):
            logging.info(f"Channel {channel_id} is paused. Blocking Slack→Telegram message.")
            await send_paused_notification(channel_id, ts)
            return

        telegram_id = mapping.get("telegram_id") or mapping.get("external_id")
        if not telegram_id:
            logging.error(f"Missing telegram_id in mapping for channel {channel_id}")
            return
        if isinstance(telegram_id, str) and (telegram_id.isdigit() or (telegram_id.startswith('-') and telegram_id[1:].isdigit())):
            telegram_id = int(telegram_id)

        client_name = mapping.get("client_name", "Telegram Client")
        text_caption = event.get("text", "") or ""

        # 1. CLEAN SLACK FORMATTING
        text_caption = clean_slack_formatting(text_caption)

        # '!!' prefix: message must remain in the channel and must not forward to the client
        if text_caption.strip().startswith('!!'):
            logging.info(f"[NOTE '!!'] Message kept in Slack channel {channel_id}, not forwarded to Telegram: \"{text_caption[:60]}\"")
            return

        # Mention replacement logic
        potential_names = re.findall(r'@([A-Za-z][A-Za-z0-9_\-]+)', text_caption)
        if potential_names:
            try:
                loop = asyncio.get_running_loop()
                sheet_mappings = await loop.run_in_executor(None, get_client_mappings, "Telegram")
                name_to_username = {}
                for item in sheet_mappings:
                    c_name = item.get("client_name")
                    t_username = item.get("external_id")
                    if c_name and t_username:
                        if not str(t_username).startswith('@') and not str(t_username).isdigit():
                            t_username = f"@{t_username}"
                        name_to_username[c_name] = str(t_username)

                for name in potential_names:
                    target_username = name_to_username.get(name)
                    if target_username:
                        text_caption = re.sub(rf'@{name}\b', target_username, text_caption)
                        logging.info(f"Mention swapped: @{name} -> {target_username}")
            except Exception as e:
                logging.error(f"Error processing Telegram mentions: {e}")

        logging.info(f"[Socket Mode] <- Slack message for '{client_name}'. Forwarding to Telegram...")

        telegram_msg_id = None

        # Files Handling
        files = event.get("files", []) or []
        if files:
            is_first = True
            for f in files:
                file_url = f.get("url_private_download") or f.get("url_private")
                filename = f.get("name", "file.bin")
                filetype = f.get("mimetype", "")
                filesize = f.get("size", 0)
                caption = text_caption if is_first else ""

                if not file_url:
                    logging.error(f"No download URL for file {filename}")
                    continue

                MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", 500)) * 1024 * 1024
                if filesize > MAX_FILE_SIZE:
                    warn_msg = f"⚠️ File transfer failed: `{filename}` ({filesize / 1024 / 1024:.1f} MB) exceeds the {MAX_FILE_SIZE // (1024 * 1024)} MB limit."
                    await slack_client.chat_postMessage(channel=channel_id, thread_ts=ts, text=warn_msg)
                    continue

                # Download file using Telegram bot token
                file_data = None
                tokens_to_try = [
                    SLACK_BOT_TOKEN,
                    bot_token
                ]
                for tok in tokens_to_try:
                    if not tok:
                        continue
                    try:
                        async with aiohttp_session.get(
                            file_url,
                            headers={"Authorization": f"Bearer {tok}"},
                            timeout=aiohttp.ClientTimeout(total=300)
                        ) as resp:
                            if resp.status == 200:
                                file_data = await resp.read()
                                break
                            else:
                                logging.warning(f"Download with token failed: status={resp.status}")
                    except Exception as dl_err:
                        logging.warning(f"Download attempt error: {dl_err}")

                if not file_data:
                    logging.error(f"Failed to download file {filename} from Slack with available tokens.")
                    await slack_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=ts,
                        text=f"❌ Failed to download `{filename}` from Slack. Please check file permissions."
                    )
                    continue

                try:
                    file_bytes = BytesIO(file_data)
                    file_bytes.name = filename
                    is_image = filetype.startswith("image/") and filetype not in ["image/svg+xml", "image/webp"]

                    msg = None
                    if is_image:
                        try:
                            msg = await telethon_client.send_file(
                                telegram_id, file_bytes, caption=caption, force_document=False
                            )
                        except Exception:
                            file_bytes.seek(0)
                            msg = await telethon_client.send_file(
                                telegram_id, file_bytes, caption=caption, force_document=True
                            )
                    else:
                        msg = await telethon_client.send_file(
                            telegram_id, file_bytes, caption=caption, force_document=True
                        )

                    if is_first and msg:
                        telegram_msg_id = msg.id
                    logging.info(f"✅ Forwarded file '{filename}' to Telegram chat {telegram_id} for client '{client_name}'")
                    await add_slack_reaction_async(channel_id, ts, "white_check_mark")

                except Exception as send_error:
                    logging.error(f"Failed to send file {filename} to Telegram: {send_error}", exc_info=True)
                    saved_path = None
                    try:
                        saved_path = str(DOWNLOAD_DIR / f"outbox_{uuid.uuid4().hex[:8]}_{filename}")
                        with open(saved_path, "wb") as sf:
                            sf.write(file_data)
                    except Exception as save_err:
                        logging.error(f"Failed to save outbox file to disk: {save_err}")

                    if saved_path:
                        await queue_telegram_outbox({
                            "channel_id": channel_id,
                            "slack_ts": ts,
                            "telegram_id": telegram_id,
                            "is_file": True,
                            "file_path": saved_path,
                            "filename": filename,
                            "caption": caption,
                            "client_name": client_name
                        })
                    await add_slack_reaction_async(channel_id, ts, "x")
                    try:
                        await slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=ts,
                            text=f"🔴 Failed to send `{filename}` to Telegram. It has been queued and will be retried automatically."
                        )
                    except Exception:
                        pass
                finally:
                    if 'file_bytes' in locals():
                        file_bytes.close()
                    del file_data

                is_first = False
        else:
            # Text-only
            if text_caption:
                try:
                    msg = await telethon_client.send_message(telegram_id, text_caption)
                    if msg:
                        telegram_msg_id = msg.id
                    logging.info(f"   ✅ Text sent to Telegram chat {telegram_id}")
                    await add_slack_reaction_async(channel_id, ts, "white_check_mark")
                except Exception as send_error:
                    logging.error(f"Failed to send text to Telegram: {send_error}", exc_info=True)
                    await queue_telegram_outbox({
                        "channel_id": channel_id,
                        "slack_ts": ts,
                        "telegram_id": telegram_id,
                        "text": text_caption,
                        "is_file": False,
                        "client_name": client_name
                    })
                    await add_slack_reaction_async(channel_id, ts, "x")
                    try:
                        await slack_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=ts,
                            text="🔴 Failed to send message to Telegram. It has been queued and will be retried automatically."
                        )
                    except Exception:
                        pass

        # Map for edits/deletes
        if telegram_msg_id:
            map_key = f"{channel_id}:{ts}"
            slack_to_telegram_message_map[map_key] = {
                "telegram_id": telegram_id,
                "telegram_msg_id": telegram_msg_id
            }
            if len(slack_to_telegram_message_map) % 10 == 0:
                save_message_map()

    except Exception as e:
        logging.error(f"Event processing error: {e}", exc_info=True)
# IMPROVEMENT #1: Background worker loop
async def event_worker():
    """Background worker that processes events from the queue."""
    logging.info("🔧 Event worker started")
    while True:
        try:
            event, bot_token = await event_queue.get()
            await process_slack_event_async(event, bot_token)
            event_queue.task_done()
        except Exception as e:
            logging.error(f"Worker error: {e}", exc_info=True)

def start_slack_socket_mode():
    """Starts Socket Mode with non-blocking event handling."""
    global socket_mode_active

    if not SLACK_APP_TOKEN or not SLACK_BOT_TOKEN:
        logging.error("Missing SLACK_APP_TOKEN_TELEGRAM or SLACK_BOT_TOKEN_TELEGRAM")
        return False

    try:
        logging.info("🔌 Initializing Slack Socket Mode for Telegram Bridge...")
        web_client_sync = WebClient(token=SLACK_BOT_TOKEN)
        socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client_sync)

        def handler(client, req):
            try:
                # Acknowledge immediately (non-blocking)
                client.send_socket_mode_response({"envelope_id": req.envelope_id})

                payload = req.payload or {}
                payload_type = payload.get("type")

                # Accept both "events_api" and "event_callback"
                if payload_type in ["events_api", "event_callback"]:
                    event = payload.get("event", {})
                    # FIX: Use thread-safe coroutine scheduling
                    if event_queue and main_loop:
                        try:
                            # Schedule the put operation in the event loop from this thread
                            asyncio.run_coroutine_threadsafe(
                                event_queue.put((event, web_client_sync.token)),
                                main_loop
                            )
                        except Exception as e:
                            logging.error(f"Failed to queue event: {e}")

                # Handle interactive events (button clicks, modal submissions) from lead welcome cards
                elif req.type == "interactive":
                    try:
                        import bitlink_manager
                        bitlink_manager.handle_slack_block_actions(client, req, web_client_sync)
                        bitlink_manager.handle_modal_submission(client, req, web_client_sync)
                    except Exception as ie:
                        logging.error(f"Interactive event handler error: {ie}")

            except Exception as e:
                logging.error(f"SocketMode handler error: {e}", exc_info=True)

        socket_client.socket_mode_request_listeners.append(handler)

        def connect_socket():
            try:
                socket_client.connect()
                logging.info("✅ Slack Socket Mode connected successfully!")
            except Exception as e:
                logging.error(f"❌ Socket Mode connection failed: {e}", exc_info=True)

        thread = threading.Thread(target=connect_socket, daemon=True)
        thread.start()

        import time
        time.sleep(2)

        if socket_client.is_connected():
            socket_mode_active = True
            logging.info("✅ Socket Mode is ACTIVE - Real-time forwarding enabled!")
            return True
        else:
            logging.error("❌ Socket Mode failed to connect")
            return False

    except Exception as e:
        logging.error(f"❌ Socket Mode initialization error: {e}", exc_info=True)
        return False

# --- 7. MAIN EXECUTION ---

async def main():
    global main_loop, aiohttp_session, sent_alerts, slack_bot_user_id, event_queue, slack_to_telegram_message_map, telegram_to_slack_message_map

    sent_alerts = load_sent_alerts()
    slack_to_telegram_message_map = load_message_map()
    telegram_to_slack_message_map = load_reverse_map()
    main_loop = asyncio.get_running_loop()

    # IMPROVEMENT #1: Initialize event queue
    event_queue = asyncio.Queue(maxsize=1000)

    # Get bot IDs and identities before loading channels
    try:
        global slack_bot_user_id, slack_bot_id, manager_bot_id, manager_bot_user_id
        auth_test = await slack_client.auth_test()
        slack_bot_user_id = auth_test.get("user_id")
        slack_bot_id = auth_test.get("bot_id")
        logging.info(f"Telegram bridge Slack bot user ID: {slack_bot_user_id}, bot ID: {slack_bot_id}")

        manager_token = os.getenv("MANAGER_SLACK_BOT_TOKEN")
        if manager_token:
            try:
                m_client = AsyncWebClient(token=manager_token)
                m_auth = await m_client.auth_test()
                manager_bot_user_id = m_auth.get("user_id")
                manager_bot_id = m_auth.get("bot_id")
                logging.info(f"Manager bot user ID: {manager_bot_user_id}, bot ID: {manager_bot_id}")
            except Exception as me:
                logging.warning(f"Could not fetch manager bot identity: {me}")
    except Exception as e:
        logging.error(f"Could not fetch Slack bot identity: {e}")

    await reload_config()

    async with aiohttp.ClientSession() as session:
        aiohttp_session = session

        refresh_server_task = asyncio.create_task(run_refresh_server())

        # IMPROVEMENT #1: Start background worker
        worker_task = asyncio.create_task(event_worker())

        # Honour the dashboard emergency stop toggle. Read it synchronously
        # before the client connects so a bridge restarted while stopped
        # cannot deliver anything during the watcher's first poll interval.
        try:
            import db_store as _db_store
            tg_set_stopped(bool(_db_store.get_sales_settings().get("kill_switch_autocreate", False)))
        except Exception as e:
            logging.error(f"[TG STOP] Could not read initial kill switch state: {e}")
        stop_watcher_task = asyncio.create_task(telegram_emergency_stop_watcher())

        logging.info("Starting Telethon client...")
        await telethon_client.start()
        me = await telethon_client.get_me()
        if me is None:
            logging.warning("Telethon get_me() returned None — session may not be fully authorized yet. Retrying once...")
            await asyncio.sleep(2)
            me = await telethon_client.get_me()
        if me is not None:
            logging.info(f"Logged in to Telegram as: {me.first_name} (@{me.username})")
        else:
            logging.error("Telethon could not resolve the logged-in account. The session file may be corrupted or unauthorized. Delete sessions/ and restart to re-authenticate.")

        # Startup scan
        async def startup_scan():
            try:
                count = 0
                async for dialog in telethon_client.iter_dialogs():
                    chat = dialog.entity
                    chat_id = getattr(chat, 'id', None)
                    if chat_id is None:
                        continue
                    chat_id_str = str(chat_id)
                    if chat_id_str in telegram_to_slack_map or chat_id_str in sent_alerts:
                        continue
                    if isinstance(chat, (types.User, types.Chat, types.Channel)):
                        await send_new_chat_alert_to_slack(
                            chat=chat,
                            chat_id=chat_id,
                            action_description="I have discovered a new chat during startup scan."
                        )
                        count += 1
                if count > 0:
                    logging.info(f"Startup scan: Found {count} unmapped chats")
                else:
                    logging.info("Startup scan: All chats are mapped")
            except Exception as e:
                logging.error(f"Startup scan failed: {e}")

        startup_scan_task = asyncio.create_task(startup_scan())

        # Start Socket Mode
        logging.info("Starting Socket Mode...")
        try:
            success = await asyncio.get_running_loop().run_in_executor(None, start_slack_socket_mode)
            if success:
                logging.info("=" * 70)
                logging.info("🚀 TELEGRAM BRIDGE FULLY OPERATIONAL")
                logging.info("   ✅ Telegram → Slack: ACTIVE")
                logging.info("   ✅ Slack → Telegram: ACTIVE (Socket Mode + Background Queue)")
                logging.info("   ✅ Message tracking: ENABLED (edit/delete ready)")
                logging.info("   ✅ Advanced deduplication: ENABLED")
                logging.info("=" * 70)
            else:
                logging.error("❌ Socket Mode failed")
        except Exception as e:
            logging.error(f"❌ Socket Mode error: {e}", exc_info=True)

        # Periodic message map save
        async def periodic_save():
            while True:
                await asyncio.sleep(300)  # Save every 5 minutes
                save_message_map()
                save_reverse_map()

        save_task = asyncio.create_task(periodic_save())

        outbox_task = asyncio.create_task(periodic_telegram_outbox_worker())
        catchup_task = asyncio.create_task(sync_slack_downtime_telegram())

        await asyncio.gather(
            refresh_server_task,
            worker_task,
            startup_scan_task,
            save_task,
            outbox_task,
            catchup_task,
            telethon_client.run_until_disconnected()
        )

if __name__ == "__main__":
    critical_alerts.install_excepthook("telegram_bridge")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down the Telegram bridge.")
        # Final save on shutdown
        save_message_map()