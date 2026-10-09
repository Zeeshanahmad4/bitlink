# main_whatsapp.py
import time
import requests
import os
import sys
import logging
import base64
import threading
import subprocess
import string
import re
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import pathlib
import uuid
from typing import Optional

from flask import Flask, request, jsonify
from dotenv import load_dotenv

# --- Environment Setup ---
if '--env' in sys.argv:
    env_index = sys.argv.index('--env') + 1
    if env_index < len(sys.argv):
        env_path = sys.argv[env_index]
        print(f"--- INFO: Loading custom environment from '{env_path}' ---")
        load_dotenv(dotenv_path=env_path)
    else:
        print("--- WARNING: --env flag used but no file specified. Using default '.env'. ---")
        load_dotenv()
else:
    load_dotenv()

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.errors import SlackApiError

import sales_provisioner
import sales_store
import db_store
import critical_alerts
import ai_prompts
import traceback
import gemini_client
import sales_insights
from slack_format import for_client

from g_sheets_client import get_client_mappings

# Environment setup moved to top

# Convert MB to bytes for easy comparison
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_MB", 500)) * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("main_whatsapp3.log"),
        logging.StreamHandler()
    ]
)

SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
NODE_API_URL = os.getenv("NODE_API_URL", "http://127.0.0.1:3101")
WHATSAPP_REFRESH_PORT = os.getenv("WHATSAPP_REFRESH_PORT", 8101)

app = Flask(__name__)


class BoundedMessageMap:
    """Bounded message map with TTL and size limits to prevent memory leaks, with optional persistence."""

    def __init__(self, max_size=50000, ttl_hours=48, persistence_file=None):
        self.map = OrderedDict()
        self.timestamps = {}
        self.max_size = max_size
        self.ttl_seconds = ttl_hours * 3600
        self.lock = threading.Lock()
        self.persistence_file = persistence_file
        if persistence_file:
            self._load()

    def _load(self):
        if not self.persistence_file or not os.path.exists(self.persistence_file):
            return
        try:
            with open(self.persistence_file, 'r') as f:
                data = json.load(f)
            now = time.time()
            for k, entry in data.items():
                if isinstance(entry, dict) and "v" in entry:
                    ts = entry.get("t", now)
                    if now - ts <= self.ttl_seconds:
                        self.map[k] = entry["v"]
                        self.timestamps[k] = ts
                else:
                    self.map[k] = entry
                    self.timestamps[k] = now
        except Exception as e:
            logging.debug(f"Could not load {self.persistence_file}: {e}")

    def _save(self):
        if not self.persistence_file:
            return
        try:
            data = {}
            for k, v in self.map.items():
                data[k] = {"v": v, "t": self.timestamps.get(k, time.time())}
            tmp = f"{self.persistence_file}.tmp"
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self.persistence_file)
        except Exception as e:
            logging.debug(f"Could not save {self.persistence_file}: {e}")

    def _purge_expired(self):
        current_time = time.time()
        expired_keys = [
            key for key, timestamp in self.timestamps.items()
            if current_time - timestamp > self.ttl_seconds
        ]
        for key in expired_keys:
            self.map.pop(key, None)
            self.timestamps.pop(key, None)

    def set(self, key, value):
        with self.lock:
            self._purge_expired()
            if key in self.map:
                self.map.pop(key, None)
            elif len(self.map) >= self.max_size:
                oldest = next(iter(self.map))
                self.map.pop(oldest, None)
                self.timestamps.pop(oldest, None)
            self.map[key] = value
            self.timestamps[key] = time.time()
            self._save()

    def get(self, key, default=None):
        with self.lock:
            if key not in self.map:
                return default
            if time.time() - self.timestamps.get(key, 0) > self.ttl_seconds:
                self.map.pop(key, None)
                self.timestamps.pop(key, None)
                return default
            return self.map[key]

    def delete(self, key):
        with self.lock:
            self.map.pop(key, None)
            self.timestamps.pop(key, None)
            self._save()


# --- Global State Variables ---
config_lock = threading.Lock()
stop_event = threading.Event()
processed_slack_events = deque(maxlen=1000)
processed_whatsapp_events = deque(maxlen=500)
whatsapp_to_slack_map = {}
slack_to_whatsapp_map = {}
slack_to_whatsapp_msg_map = BoundedMessageMap(max_size=50000, ttl_hours=48, persistence_file="slack_to_whatsapp_msg_map.json")
wa_to_slack_map = BoundedMessageMap(max_size=50000, ttl_hours=48, persistence_file="whatsapp_to_slack_msg_map.json")  # reverse: wa_msg_id -> {channel, ts}
posted_bridge_slack_events = deque(maxlen=2000)
thread_pool = ThreadPoolExecutor(max_workers=20)
slack_bot_user_id = None
slack_bot_id = None
manager_bot_id = None
manager_bot_user_id = None

# Outbox (JSONL) configuration
OUTBOX_PATH = pathlib.Path(os.getenv("OUTBOX_PATH", "whatsapp_outbox.jsonl"))
OUTBOX_FLUSH_INTERVAL = int(os.getenv("OUTBOX_FLUSH_INTERVAL", 60))
OUTBOX_MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", 5))
OUTBOX_LOCK = threading.Lock()

WHATSAPP_ALERTS_FILE = os.getenv("WHATSAPP_ALERTS_FILE", "sent_whatsapp_alerts.json")
sent_wa_alerts = set()


def load_sent_wa_alerts():
    if not os.path.exists(WHATSAPP_ALERTS_FILE):
        return set()
    try:
        with open(WHATSAPP_ALERTS_FILE, 'r') as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent_wa_alerts():
    try:
        with open(WHATSAPP_ALERTS_FILE, 'w') as f:
            json.dump(list(sent_wa_alerts), f)
    except IOError:
        logging.error(f"Could not write {WHATSAPP_ALERTS_FILE}")


import re


def clean_slack_links(text):
    """Prepare Slack message text for WhatsApp.

    Delegates to the shared cleaner so WhatsApp and Telegram cannot drift
    apart: Slack's ``<...>`` markup is unwrapped (``<https://x.com>`` ->
    ``https://x.com``) and ``:shortcodes:`` become real emoji, which is what
    WhatsApp renders.
    """
    return for_client(text)

def extract_wa_mentions(text):
    """
    Extract WhatsApp mention IDs from Slack message text.
    Supports both @number and @name mentions.
    Looks up WhatsApp IDs for names from the database.
    """
    # 1. Extract @number mentions (e.g., @923001234567)
    numbers = re.findall(r'@([0-9]{10,15})', text)
    mention_ids = [f"{n}@c.us" for n in numbers]

    # 2. Extract @name mentions (e.g., @Ali)
    names = re.findall(r'@([A-Za-z][A-Za-z0-9_\-]+)', text)
    # Remove any numbers accidentally picked up as names
    names = [n for n in names if not re.match(r'^[0-9]{10,15}$', n)]

    # 3. Lookup WhatsApp IDs for names using cached map
    if names:
        try:
            name_to_id = {c.get("client_name"): sanitize_id(c.get("whatsapp_chat_id")) for c in slack_to_whatsapp_map.values() if c.get("client_name") and c.get("whatsapp_chat_id")}
            for name in names:
                wa_id = name_to_id.get(name)
                if wa_id:
                    mention_ids.append(wa_id)
        except Exception as e:
            logging.error(f"Error looking up WhatsApp IDs for names: {e}")
    return mention_ids
def sanitize_id(external_id):
    if not external_id: return None
    # Strictly allow only digits, letters, @, ., and - for valid JIDs
    clean_id = re.sub(r'[^a-zA-Z0-9@.\-]', '', str(external_id))
    if clean_id and not ("@" in clean_id):
        # Automatically fix manually added phone numbers missing the WhatsApp suffix
        clean_id += "@s.whatsapp.net"
    return clean_id


# ==============================================================================
# EMERGENCY STOP
# ==============================================================================
# Driven by the dashboard's kill_switch_autocreate toggle. When ON this halts
# the bridge completely in both directions:
#   * outbound - nothing is sent to any customer (send/edit/delete all gated)
#   * inbound  - client messages are drained from the gateway and discarded, so
#                they never reach Slack and never auto-provision a channel
# Inbound is drained rather than left queued on purpose: the gateway buffers
# messages, so skipping the read would mean a flood the moment we resume, and
# the buffer would grow without bound while stopped.
#
# The process stays alive while stopped so the dashboard can switch it back on.
# Held messages are counted so the operator can see what was missed.

emergency_stop = threading.Event()
_emergency_lock = threading.Lock()
_emergency_state = {
    "active": False,
    "since": None,
    "inbound_dropped": 0,
    "outbound_blocked": 0,
    "outbox_held": 0,
}


def is_emergency_stopped() -> bool:
    return emergency_stop.is_set()


def _emergency_note(**deltas):
    with _emergency_lock:
        for k, v in deltas.items():
            _emergency_state[k] = _emergency_state.get(k, 0) + v


def _emergency_set_current(key, value):
    """Set an absolute value (e.g. how many messages are held right now)."""
    with _emergency_lock:
        _emergency_state[key] = value


def emergency_snapshot() -> dict:
    with _emergency_lock:
        return dict(_emergency_state)


def _set_emergency_stop(active: bool):
    with _emergency_lock:
        already = _emergency_state["active"]
        if already == active:
            return
        _emergency_state["active"] = active
        if active:
            _emergency_state["since"] = datetime.now().isoformat(timespec="seconds")
            _emergency_state["inbound_dropped"] = 0
            _emergency_state["outbound_blocked"] = 0
            _emergency_state["outbox_held"] = 0
    if active:
        emergency_stop.set()
        logging.critical(
            "🛑 EMERGENCY STOP ENGAGED - all WhatsApp traffic halted. "
            "Inbound messages will be discarded and nothing will be sent to customers."
        )
        _report("EMERGENCY_STOP_ON",
                "Client intake is paused. Client messages are not being delivered.",
                "The kill switch engaged; all WhatsApp traffic is halted.",
                {"client_key": "emergency_stop"})
    else:
        emergency_stop.clear()
        snap = emergency_snapshot()
        logging.critical(
            f"▶️ EMERGENCY STOP RELEASED - bridge resumed. "
            f"{snap.get('inbound_dropped', 0)} inbound message(s) were "
            f"discarded while stopped and {snap.get('outbound_blocked', 0)} "
            f"outbound send(s) were blocked."
        )
        _report("EMERGENCY_STOP_OFF",
                "Client intake has resumed.",
                f"The kill switch was released. {snap.get('inbound_dropped', 0)} inbound "
                f"message(s) were discarded and {snap.get('outbound_blocked', 0)} "
                f"outbound send(s) were blocked while stopped.",
                {"client_key": "emergency_stop", "inbound_dropped": snap.get("inbound_dropped", 0),
                 "outbound_blocked": snap.get("outbound_blocked", 0)},
                severity="info")
        held = snap.get("outbox_held", 0)
        if held:
            # Be explicit: releasing the stop re-arms the outbox, so anything
            # queued beforehand will go out on the next flush unless cleared.
            logging.warning(
                f"⚠️ {held} message(s) queued BEFORE the stop are still pending and WILL be "
                f"delivered on the next outbox flush. Review or remove them from "
                f"{OUTBOX_PATH} first if they must not reach customers."
            )


def emergency_stop_watcher(stop_event: threading.Event):
    """Polls the dashboard toggle and flips the bridge in/out of the stopped state."""
    logging.info("🚦 Emergency stop watcher initialized.")
    while not stop_event.is_set():
        try:
            active = bool(db_store.get_sales_settings().get("kill_switch_autocreate", False))
            _set_emergency_stop(active)
        except Exception as e:
            logging.error(f"[STOP] Could not read kill switch state: {e}")
        stop_event.wait(5)


def get_whatsapp_messages():
    try:
        response = requests.get(f"{NODE_API_URL}/get-messages", timeout=10)
        if response.status_code == 200: return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error connecting to WhatsApp service: {e}")
    return []

def send_whatsapp_message(chat_id, message, media=None, mentions=None, max_retries=3):
    """Send WhatsApp message with retry logic and detailed error reporting."""
    if is_emergency_stopped():
        _emergency_note(outbound_blocked=1)
        logging.warning(
            f"🛑 BLOCKED by emergency stop: refusing to send to {chat_id}. "
            f"Message not delivered."
        )
        return {"success": False, "error": "Emergency stop is active - sending is halted",
                "status_code": None, "retry_after": None, "blocked_by_emergency_stop": True}
    for attempt in range(max_retries):
        try:
            payload = {"chatId": chat_id, "message": message, "media": media}
            if mentions:
                payload["mentions"] = mentions

            req_timeout = 600 if media else 10
            response = requests.post(f"{NODE_API_URL}/send-message", json=payload, timeout=req_timeout)

            with open("process_debug.log", "a") as f: f.write(f"Node response: {response.status_code} {response.text[:100]}\n")

            if response.status_code == 200:
                return {"success": True, "data": response.json()}
            if response.status_code == 503 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {
                "success": False,
                "error": response.text[:200] if response.text else "HTTP error",
                "status_code": response.status_code,
                "retry_after": 60 if response.status_code == 503 else None,
            }
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {"success": False, "error": "Request timeout", "status_code": None, "retry_after": None}
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            logging.error(f"Error sending message via WhatsApp service: {e}")
            return {"success": False, "error": str(e), "status_code": None, "retry_after": None}

def delete_whatsapp_message(message_id):
    if is_emergency_stopped():
        _emergency_note(outbound_blocked=1)
        logging.warning(f"🛑 BLOCKED by emergency stop: refusing to delete message {message_id}.")
        return False
    logging.info(f"Attempting to delete WhatsApp message: {message_id}")
    try:
        payload = {"messageId": message_id}
        response = requests.post(f"{NODE_API_URL}/delete-message", json=payload, timeout=10)
        logging.info(f"Delete request sent. Status code: {response.status_code}, Response: {response.text}")
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Error deleting WhatsApp message: {e}")
    return False


# --- Slack Reaction Helpers ---
def add_slack_reaction(client: WebClient, channel: str, ts: str, name: str = "white_check_mark"):
    """Safely adds an emoji reaction to a Slack message."""
    if not channel or not ts:
        return
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception as e:
        err = getattr(e, "response", {})
        if isinstance(err, dict) and err.get("error") == "already_reacted":
            return
        logging.debug(f"Slack reaction {name} could not be added to {ts}: {e}")

def remove_slack_reaction(client: WebClient, channel: str, ts: str, name: str = "x"):
    """Safely removes an emoji reaction from a Slack message."""
    if not channel or not ts:
        return
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name=name)
    except Exception:
        pass


# --- Outbox helpers (JSONL) ---
def queue_outbox(entry):
    entry = dict(entry)
    entry.setdefault("id", str(uuid.uuid4()))
    entry.setdefault("attempts", 0)
    entry.setdefault("created_at", time.time())
    line = json.dumps(entry, ensure_ascii=False)
    with OUTBOX_LOCK:
        with OUTBOX_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def flush_outbox(web_client: WebClient):
    """Attempt to send queued messages; remove delivered ones atomically."""
    if not OUTBOX_PATH.exists():
        return
    # Never drain the outbox while the bridge is halted. Messages queued before
    # an emergency stop must not silently land on customers the moment the
    # bridge comes back, so they stay on disk for a human to review.
    if is_emergency_stopped():
        try:
            pending = sum(1 for ln in OUTBOX_PATH.read_text(encoding="utf-8").splitlines() if ln.strip())
        except Exception:
            pending = -1
        _emergency_set_current("outbox_held", pending)
        if pending:
            logging.warning(
                f"🛑 Emergency stop active: holding {pending} queued outbound message(s). "
                f"They were queued before the stop and are still on disk."
            )
        return
    with OUTBOX_LOCK:
        try:
            lines = OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            logging.error(f"Failed to read outbox: {e}")
            return

        remaining = []
        for ln in lines:
            if not ln.strip():
                continue
            try:
                item = json.loads(ln)
            except Exception:
                logging.warning("Skipping corrupt outbox line")
                continue

            # Skip items that exceeded attempts
            if item.get("attempts", 0) >= OUTBOX_MAX_ATTEMPTS:
                add_slack_reaction(web_client, item.get("slack_channel"), item.get("slack_ts"), "x")
                try:
                    web_client.chat_postMessage(
                        channel=item.get("slack_channel"),
                        thread_ts=item.get("slack_ts"),
                        text=f"⚠️ Message queued {item.get('id')} failed after {OUTBOX_MAX_ATTEMPTS} attempts."
                    )
                except Exception:
                    logging.exception("Failed to notify Slack about permanent outbox failure")
                continue

            # Try sending
            try:
                resp = send_whatsapp_message(item.get("chat_id"), item.get("message"), item.get("media"), item.get("mentions"))
                if resp and resp.get("success"):
                    # notify Slack thread of delivery and update reactions
                    remove_slack_reaction(web_client, item.get("slack_channel"), item.get("slack_ts"), "x")
                    add_slack_reaction(web_client, item.get("slack_channel"), item.get("slack_ts"), "white_check_mark")
                    try:
                        web_client.chat_postMessage(
                            channel=item.get("slack_channel"),
                            thread_ts=item.get("slack_ts"),
                            text=f"✅ Previously queued message delivered to WhatsApp."
                        )
                    except Exception:
                        logging.exception("Failed to post delivery notification to Slack")
                    continue
                else:
                    item["attempts"] = item.get("attempts", 0) + 1
                    remaining.append(json.dumps(item, ensure_ascii=False))
            except Exception as e:
                logging.error(f"Error flushing outbox item: {e}")
                item["attempts"] = item.get("attempts", 0) + 1
                remaining.append(json.dumps(item, ensure_ascii=False))

        # Write remaining atomically
        try:
            tmp = OUTBOX_PATH.with_suffix(".tmp")
            tmp.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
            tmp.replace(OUTBOX_PATH)
        except Exception as e:
            logging.error(f"Failed to write outbox file atomically: {e}")


def periodic_outbox_worker(web_client: WebClient):
    logging.info("Outbox worker started")
    while not stop_event.is_set():
        try:
            flush_outbox(web_client)
        except Exception:
            logging.exception("Outbox worker error")
        for _ in range(OUTBOX_FLUSH_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)
    logging.info("Outbox worker stopping")


# --- WhatsApp Edit Message Function ---
def edit_whatsapp_message(message_id, new_text):
    """Send a request to the Node.js service to edit a WhatsApp message."""
    if is_emergency_stopped():
        _emergency_note(outbound_blocked=1)
        logging.warning(f"🛑 BLOCKED by emergency stop: refusing to edit message {message_id}.")
        return False
    try:
        payload = {"messageId": message_id, "newText": new_text}
        response = requests.post(f"{NODE_API_URL}/edit-message", json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("success"):
            logging.info(f"✅ WhatsApp message {message_id} updated successfully.")
            return True
        else:
            logging.error(f"❌ Failed to update WhatsApp message {message_id}. Response: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Error editing WhatsApp message: {e}")
        return False

# --- Slack Edit Event Handler ---
def handle_slack_edit_event(client: SocketModeClient, req, web_client: WebClient):
    """Handle Slack message edit events and update WhatsApp messages if mapped."""
    if req.type != "events_api":
        return
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    event = req.payload.get("event", {})
    if event.get("type") == "message" and event.get("subtype") == "message_changed":
        channel_id = event.get("channel")
        new_text = event.get("message", {}).get("text", "")
        slack_ts = event.get("message", {}).get("ts")
        if not (channel_id and slack_ts and new_text):
            return
        # Look up WhatsApp message ID
        whatsapp_msg_id = slack_to_whatsapp_msg_map.get(slack_ts)
        if whatsapp_msg_id:
            logging.info(f"✏️ Slack message edited → editing WhatsApp message {whatsapp_msg_id}")
            # Same treatment as the initial send: an edit that adds :smile:
            # must not leave the shortcode sitting in the WhatsApp message.
            success = edit_whatsapp_message(whatsapp_msg_id, clean_slack_links(new_text))
            if success:
                logging.info(f"✅ WhatsApp message {whatsapp_msg_id} updated successfully.")
            else:
                logging.error(f"❌ Failed to update WhatsApp message {whatsapp_msg_id}.")
        else:
            logging.info(f"ℹ️ No WhatsApp mapping found for edited Slack message {slack_ts}.")

def send_new_wa_chat_alert(chat_id, web_client, sender_name=None):
    global sent_wa_alerts
    if chat_id.endswith('@g.us'):
        return
    if chat_id in sent_wa_alerts:
        return
    sent_wa_alerts.add(chat_id)
    save_sent_wa_alerts()
    admin_channel = os.getenv("SLACK_ADMIN_CHANNEL_ID")
    if not admin_channel:
        return
    try:
        name_line = f"• Name: *{sender_name}*\n" if sender_name else ""
        web_client.chat_postMessage(
            channel=admin_channel,
            text=(f"🔔 New Unmapped WhatsApp Chat Detected\n\n"
                  f"{name_line}"
                  f"• Chat ID: `{chat_id}`\n"
                  f"• Action: Add this ID to the dashboard for the relevant client")
        )
        logging.info(f"Sent new WhatsApp chat alert for {chat_id}")
    except Exception as e:
        logging.error(f"Failed to send WhatsApp chat alert: {e}")


# --- Core Logic ---

def reload_config():
    global whatsapp_to_slack_map, slack_to_whatsapp_map
    logging.info("(WhatsApp Bridge) Refresh signal received! Reloading config...")
    
    # 1. Base Layer: Load Sales Leads
    sales_mappings = []
    try:
        sales_leads = db_store.get_sales_leads()
        for lead in sales_leads:
            if str(lead.get("platform", "")).strip().lower() != "whatsapp":
                continue
            ext_id = sanitize_id(lead.get("external_id"))
            if ext_id and lead.get("slack_channel_id"):
                sales_mappings.append({
                    "client_name": lead.get("client_name"),
                    "whatsapp_chat_id": ext_id,
                    "lid": None,
                    "slack_channel_id": lead.get("slack_channel_id"),
                    "paused": str(lead.get("paused", False)).strip().lower() in ["true", "1", "yes"],
                    "delivery_handed_off": bool(lead.get("delivery_handed_off")),
                    "type": "sales"
                })
    except Exception as e:
        logging.warning(f"Could not load Sales Leads during reload_config: {e}")

    # 2. Priority Layer: Load Delivery Mappings from client_mappings
    delivery_mappings = []
    client_mappings_raw = get_client_mappings("WhatsApp") or []
    for c in client_mappings_raw:
        ext_id = sanitize_id(c.get("external_id"))
        lid = sanitize_id(c.get("lid"))
        ch_id = c.get("slack_channel_id")
        if (ext_id or lid) and ch_id:
            delivery_mappings.append({
                "client_name": c.get("client_name"), 
                "whatsapp_chat_id": ext_id, 
                "lid": lid,
                "slack_channel_id": ch_id,
                "paused": c.get("paused", False),
                "type": "delivery"
            })

    with config_lock:
        whatsapp_to_slack_map.clear()
        slack_to_whatsapp_map.clear()

        # Step A: Register all Sales Leads first
        for item in sales_mappings:
            if item.get("whatsapp_chat_id"):
                whatsapp_to_slack_map[item["whatsapp_chat_id"]] = item
            slack_to_whatsapp_map[item["slack_channel_id"]] = item

        # Step B: Register Delivery Mappings on top (active mappings registered last to win inbound routing)
        for item in sorted(delivery_mappings, key=lambda m: 1 if not m.get("paused") else 0):
            if item.get("whatsapp_chat_id"):
                whatsapp_to_slack_map[item["whatsapp_chat_id"]] = item
            if item.get("lid"):
                whatsapp_to_slack_map[item["lid"]] = item
            slack_to_whatsapp_map[item["slack_channel_id"]] = item

    logging.info(f"Configuration reloaded. Now tracking {len(whatsapp_to_slack_map)} active routes.")
    return "Configuration reloaded.", 200



def send_to_slack_with_retry(action, max_retries=3, delay_seconds=5, **kwargs):
    """
    Attempts to execute a Slack API call, retrying on failure.
    
    :param action: The Slack client method to call (e.g., web_client.files_upload_v2).
    :param max_retries: The maximum number of times to try.
    :param delay_seconds: The time to wait between retries.
    :param kwargs: The arguments to pass to the action.
    :return: True on success, False on failure after all retries.
    """
    for attempt in range(max_retries):
        try:
            # Attempt to perform the action
            action(**kwargs)
            # If it succeeds, return True immediately
            return True
        except Exception as e:
            # If it fails, log the attempt and wait
            logging.warning(
                f"Slack API call failed (Attempt {attempt + 1}/{max_retries}): {e}. "
                f"Retrying in {delay_seconds} seconds..."
            )
            if attempt < max_retries - 1:
                time.sleep(delay_seconds)
    
    # If the loop finishes without returning True, all retries have failed
    logging.error(f"Failed to send to Slack after {max_retries} attempts.")
    _report("SLACK_POST_EXHAUSTED",
            "Messages to the team channel are not going through.",
            f"{getattr(action, '__name__', 'slack_action')} failed after {max_retries} "
            f"attempts. kwargs keys: {sorted(kwargs.keys())}",
            {"client_key": f"exhausted:{getattr(action, '__name__', 'slack_action')}"})
    return False
    
def _report(code: str, summary_plain: str, technical_detail: str, context: dict,
            severity: str = "critical", tb_text: Optional[str] = None) -> None:
    """Push a bridge failure to the developer channel.

    Wrapped so that alerting can never become the reason the bridge stops. Any
    error inside critical_alerts is already swallowed there; this is the second
    layer that keeps this call from disturbing the poller's own control flow.
    """
    try:
        critical_alerts.report(code=code, severity=severity, component="whatsapp_bridge",
                               summary_plain=summary_plain, technical_detail=technical_detail,
                               context=context, key=context.get("client_key"), tb_text=tb_text)
    except Exception as e:
        logging.error(f"Alert reporting for {code} failed: {e}")


def _current_traceback() -> Optional[str]:
    """traceback of the exception being handled, or None when there is not one.

    critical_alerts would otherwise call traceback.format_exc() itself, but only
    when tb_text is left unset; passing 'NoneType: None' through explicitly would
    overwrite a real traceback on a repeat of the same alert.
    """
    return traceback.format_exc() if sys.exc_info()[0] is not None else None


def _alert_slack_forward_failed(display_name, chat_id, event_id, slack_channel,
                                exc: Exception, has_media: bool) -> None:
    _report("SLACK_FORWARD_FAILED",
            f"A message from {display_name} could not be delivered to the team channel.",
            f"{type(exc).__name__}: {getattr(exc, 'response', {}).get('error', exc)}",
            {"display_name": display_name, "chat_id": chat_id, "event_id": event_id,
             "slack_channel": slack_channel, "had_media": bool(has_media),
             "client_key": str(chat_id)},
            tb_text=traceback.format_exc())


def _alert_media_upload_failed(display_name, chat_id, event_id, slack_channel,
                               exc: Exception) -> None:
    _report("MEDIA_UPLOAD_FAILED",
            f"A file from {display_name} could not be processed.",
            f"{type(exc).__name__}: {getattr(exc, 'response', {}).get('error', exc)}",
            {"display_name": display_name, "chat_id": chat_id, "event_id": event_id,
             "slack_channel": slack_channel, "client_key": str(chat_id)},
            tb_text=traceback.format_exc())


def _alert_poller_error(exc: Exception) -> None:
    _report("POLLER_LOOP_ERROR",
            "The WhatsApp connection is unstable and may have dropped a message.",
            f"{type(exc).__name__}: {exc}",
            {"client_key": "poller"},
            tb_text=traceback.format_exc())


def _post_text_without_media(web_client: WebClient, slack_channel: str,
                             display_name: str, message_text: str) -> None:
    """Deliver a client's text when their attachment could not be processed."""
    try:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
        web_client.chat_postMessage(channel=slack_channel,
                                    text=f"{display_name}: {message_text}",
                                    blocks=blocks, username=display_name,
                                    icon_emoji=":bust_in_silhouette:")
        logging.info(f"Forwarded text from '{display_name}' without its attachment")
    except SlackApiError as e:
        logging.error(f"Slack also rejected the text-only fallback for '{display_name}': "
                      f"{e.response.get('error')}")
        _alert_slack_forward_failed(display_name, "", "", slack_channel, e, False)


def poll_whatsapp_and_forward(web_client: WebClient):
    logging.info("WhatsApp polling worker has started.")
    while not stop_event.is_set():
        try:
            with config_lock:
                current_clients = dict(whatsapp_to_slack_map)
            new_messages = get_whatsapp_messages()

            if is_emergency_stopped():
                # Drain and discard. The gateway already handed us these
                # messages, so they are counted as dropped and never reach
                # Slack, never auto-provision, and never block a mapped client
                # from being reached later.
                if new_messages:
                    _emergency_note(inbound_dropped=len(new_messages))
                    logging.warning(
                        f"🛑 Emergency stop: discarded {len(new_messages)} inbound WhatsApp "
                        f"message(s). Clients cannot reach the team while stopped."
                    )
                    _report("EMERGENCY_INBOUND_DROPPED",
                            f"{len(new_messages)} client message(s) were not delivered "
                            f"because intake is paused.",
                            f"Emergency stop discarded {len(new_messages)} inbound message(s).",
                            {"client_key": "emergency_stop", "dropped": len(new_messages)})
                time.sleep(1)
                continue

            for msg in new_messages:
                event_id = msg.get('messageId')
                chat_id = msg.get('chatId')
                if not event_id or not chat_id:
                    continue

                if event_id not in processed_whatsapp_events:
                    # Global Blocklist Check
                    if sales_store.is_external_id_blocked(chat_id):
                        processed_whatsapp_events.append(event_id)
                        logging.info(f"🛡️ [BLOCKLIST] Sender {chat_id} is in Blocklist. Dropping inbound message.")
                        continue

                    if chat_id not in current_clients:
                        # Check if client belongs to Delivery
                        delivery_rec = db_store.find_delivery_mapping_by_external_id(chat_id)
                        if delivery_rec and delivery_rec.get("slack_channel_id"):
                            del_ch = delivery_rec["slack_channel_id"]
                            client_info = {
                                "client_name": delivery_rec.get("client_name", "Client"),
                                "platform": "whatsapp",
                                "whatsapp_chat_id": chat_id,
                                "external_id": chat_id,
                                "slack_channel_id": del_ch,
                                "paused": delivery_rec.get("paused", False),
                                "type": "delivery"
                            }
                            with config_lock:
                                whatsapp_to_slack_map[chat_id] = client_info
                                slack_to_whatsapp_map[del_ch] = client_info
                                current_clients[chat_id] = client_info
                            logging.info(f"✅ Routed incoming WhatsApp message from delivery client {chat_id} directly to Delivery channel {del_ch}")
                        else:
                            sender_name = msg.get('senderName')
                            logging.info(f"WA-IN NEW LEAD: chat_id={chat_id!r} (sender={sender_name}). Triggering Sales Provisioner...")
                            provision_res = sales_provisioner.provision_whatsapp_lead(msg, web_client)
                            status = provision_res.get("status")

                            if status in ["provisioned", "deduplicated"]:
                                slack_channel = provision_res.get("channel_id")
                                client_info = provision_res.get("lead") or {
                                    "client_name": provision_res.get("channel_name", f"sale-wa-{chat_id}"),
                                    "platform": "whatsapp",
                                    "whatsapp_chat_id": chat_id,
                                    "external_id": chat_id,
                                    "slack_channel_id": slack_channel,
                                    "paused": False
                                }
                                # Register in-memory mappings immediately for zero-drop real-time routing
                                with config_lock:
                                    whatsapp_to_slack_map[chat_id] = client_info
                                    slack_to_whatsapp_map[slack_channel] = client_info
                                    current_clients[chat_id] = client_info
                                logging.info(f"✅ Real-time mapping established: {chat_id} <-> {slack_channel}")
                            else:
                                logging.info(f"Lead intake status for {chat_id}: {status}. Skipping forward.")
                                continue
                    processed_whatsapp_events.append(event_id)
                    client_info = current_clients[chat_id]
                    slack_channel = client_info["slack_channel_id"]

                    # Check delivery handoff & paused status before forwarding
                    if client_info.get('type') == 'sales':
                        if client_info.get('delivery_handed_off') or db_store.is_client_in_delivery(chat_id):
                            logging.info(f"Sales channel {slack_channel} is handed off to delivery. Inbound message not forwarded to sales channel.")
                            continue
                        if client_info.get('paused', False):
                            logging.info(f"Sales channel {slack_channel} is paused. Skipping forwarding.")
                            continue
                    elif client_info.get('paused', False):
                        logging.info(f"Delivery channel {slack_channel} is paused. Skipping forwarding.")
                        continue

                    sender_name = msg.get('senderName')
                    mapped_name = client_info["client_name"]
                    display_name = sender_name if sender_name else mapped_name

                    content = msg.get('body', '')
                    quoted_body = msg.get('quotedBody')
                    
                    # Convert incoming @12345 mentions to @Name if mapped
                    def replace_incoming_mention(match):
                        num = match.group(1)
                        c = whatsapp_to_slack_map.get(f"{num}@c.us") or whatsapp_to_slack_map.get(f"{num}@s.whatsapp.net") or whatsapp_to_slack_map.get(num)
                        return f"@{c['client_name']}" if c and c.get('client_name') else match.group(0)
                    
                    content = re.sub(r'@([0-9]{10,15})', replace_incoming_mention, content)
                    if quoted_body:
                        quoted_body = re.sub(r'@([0-9]{10,15})', replace_incoming_mention, quoted_body)

                    message_text = ""
                    if quoted_body:
                        message_text += f"> {quoted_body}\n"

                    has_media = bool(msg.get('media') and msg['media'].get('data'))

                    if has_media:
                        filename = msg['media'].get('filename', 'file.bin')
                        message_text += f"Sent a file: `{filename}`\n"
                        if content.strip():
                            message_text += content
                    else:
                        message_text += content

                    if not message_text.strip() and not has_media:
                        logging.info(f"Ignoring empty event from '{display_name}' (e.g., a reaction).")
                        continue

                    final_text_payload = f"*{display_name}:*\n{message_text}"

                    try:
                        if has_media:
                            file_content = base64.b64decode(msg['media']['data'])
                            # Step 1: Upload file without sharing it to a channel
                            try:
                                upload_resp = web_client.files_upload_v2(
                                    content=file_content, 
                                    filename=msg['media'].get('filename', 'file.bin')
                                )
                            except SlackApiError as e:
                                # An unsupported file type raises here. Because upload
                                # and channel-post share one try, letting this propagate
                                # used to take the whole message down with it, losing the
                                # client's text as well as the file. Post the text on its
                                # own and record why the attachment is missing.
                                logging.error(f"Slack media upload failed for '{display_name}': "
                                              f"{e.response.get('error')}")
                                _alert_media_upload_failed(display_name, chat_id, event_id,
                                                           slack_channel, e)
                                if message_text.strip():
                                    _post_text_without_media(web_client, slack_channel,
                                                             display_name, message_text)
                                    record_client_message_for_sla(slack_channel)
                                continue
                            file_url = upload_resp.get("file", {}).get("permalink")
                            
                            # Step 2: Append the file permalink with invisible text so it unfurls without showing the ugly URL
                            if message_text:
                                message_text += f"\n<{file_url}|\u200B>"
                            else:
                                message_text = f"<{file_url}|[Attached Media]>"
                                
                            notification_text = f"{display_name} sent a file"
                            message_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
                            
                            resp = web_client.chat_postMessage(
                                channel=slack_channel,
                                text=notification_text,
                                blocks=message_blocks,
                                username=display_name,
                                icon_emoji=":bust_in_silhouette:",
                                unfurl_links=True,
                                unfurl_media=True
                            )
                            slack_ts = resp.get("ts")
                            if slack_ts:
                                posted_bridge_slack_events.append((slack_channel, slack_ts))
                                if event_id:
                                    wa_to_slack_map.set(event_id, {"channel": slack_channel, "ts": slack_ts})
                        else:
                            notification_text = f"{display_name}: {content}"
                            message_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
                            resp = web_client.chat_postMessage(
                                channel=slack_channel,
                                text=notification_text,
                                blocks=message_blocks,
                                username=display_name,
                                icon_emoji=":bust_in_silhouette:",
                            )
                            slack_ts = resp.get("ts")
                            if slack_ts:
                                posted_bridge_slack_events.append((slack_channel, slack_ts))
                                if event_id:
                                    wa_to_slack_map.set(event_id, {"channel": slack_channel, "ts": slack_ts})
                        # Record incoming client message timestamp for SLA silence watch timer
                        record_client_message_for_sla(slack_channel)
                        # Check and dispatch Sunday-only auto-reply if today is Sunday
                        handle_sunday_autoreply(chat_id, slack_channel, web_client)
                        logging.info(f"Forwarded WhatsApp message from '{display_name}' to Slack")
                    except SlackApiError as e:
                        # The message is dropped here and polling continues, with no
                        # record that it was ever lost. Report it before it vanishes.
                        logging.error(f"Slack API error forwarding from '{display_name}': {e.response['error']}")
                        _alert_slack_forward_failed(display_name, chat_id, event_id,
                                                   slack_channel, e, has_media)

            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Poller loop error, continuing in 5s: {e}")
            _alert_poller_error(e)
            time.sleep(5)
    logging.info("WhatsApp polling worker is shutting down.")



def handle_slack_delete_event(client: SocketModeClient, req, web_client: WebClient):
    """Handle when a message is deleted in Slack"""
    if req.type != "events_api":
        return
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    try:
        with open("slack_events.log", "a") as f:
            f.write(json.dumps(req.to_dict()) + "\n")
    except Exception:
        pass
    event = req.payload.get("event", {})
    
    # Check if this is a message deletion event
    if event.get("type") == "message" and event.get("subtype") == "message_deleted":
        deleted_ts = event.get("previous_message", {}).get("ts")
        channel_id = event.get("channel")
        
        logging.info(f"🗑️ SLACK DELETE DETECTED - TS: {deleted_ts}, Channel: {channel_id}")
        
        # Check if we have a WhatsApp message ID mapped to this Slack message
        whatsapp_msg_id = slack_to_whatsapp_msg_map.get(deleted_ts)
        if whatsapp_msg_id:
            logging.info(f"🗑️ Attempting to delete corresponding WhatsApp message: {whatsapp_msg_id}")
            
            # Delete the WhatsApp message
            if delete_whatsapp_message(whatsapp_msg_id):
                logging.info(f"✅ Successfully deleted WhatsApp message: {whatsapp_msg_id}")
                # Remove from mapping
                slack_to_whatsapp_msg_map.delete(deleted_ts)
            else:
                logging.error(f"❌ Failed to delete WhatsApp message: {whatsapp_msg_id}")
        else:
            logging.info(f"ℹ️ No WhatsApp message mapping found for Slack TS: {deleted_ts}")
    else:
        logging.debug(f"ℹ️ Ignoring non-delete event: {event.get('type')}, {event.get('subtype')}")


def handle_slack_message(client: SocketModeClient, req, web_client: WebClient):
    if req.type != "events_api":
        return
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    event = req.payload.get("event", {})

    if event.get("type") != "message":
        return

    channel_id = event.get("channel")
    ts = event.get("ts")
    bot_id = event.get("bot_id")
    user_id = event.get("user")
    subtype = event.get("subtype")
    text = event.get("text") or ""
    username = event.get("username")
    icons = event.get("icons", {})

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
    KNOWN_BRIDGE_BOT_IDS = {slack_bot_id, "B0C3FBURUE9", "B0C3L7FGLRY"} - {None}
    KNOWN_BRIDGE_USER_IDS = {slack_bot_user_id, "U0C3E3DRC9H", "U0C3L7FKA7L"} - {None}
    if bot_id and bot_id in KNOWN_BRIDGE_BOT_IDS:
        return
    if user_id and user_id in KNOWN_BRIDGE_USER_IDS:
        return

    # 4. Block messages whose (channel, ts) were posted by the bridge
    if (channel_id, ts) in posted_bridge_slack_events:
        return

    # 5. Check for bot messages: ONLY allow the Manager Bot (for AI replies / snippets)
    if bot_id or subtype == "bot_message":
        master_bot_user_id = os.getenv("MASTER_SLACK_BOT_ID")
        is_manager = False
        if manager_bot_id and bot_id == manager_bot_id:
            is_manager = True
        elif manager_bot_user_id and user_id == manager_bot_user_id:
            is_manager = True
        elif master_bot_user_id and (user_id == master_bot_user_id or bot_id == master_bot_user_id):
            is_manager = True

        if not is_manager:
            logging.debug(f"[SKIP] bot_id={bot_id} user={user_id} subtype={subtype} (not authorized manager bot) ch={channel_id}")
            return

        # Ensure we don't forward internal Manager Bot admin notifications
        if any(marker in text for marker in [
            "Lead Claimed:", "Lead Handed Off:", "New WhatsApp Lead", "Sales Summary", "Claim Lead"
        ]):
            logging.info(f"[SKIP] Ignoring internal Manager Bot notification in ch={channel_id}")
            return

    # 6. Only allow valid message subtypes (normal messages have subtype None, files have file_share, bot messages have bot_message)
    if subtype and subtype not in ["file_share", "bot_message"]:
        logging.debug(f"[SKIP] bot_id={bot_id} subtype={subtype} ch={channel_id}")
        return

    if not channel_id or not ts:
        return

    event_key = (channel_id, ts)

    with config_lock:
        if channel_id not in slack_to_whatsapp_map:
            logging.info(f"[SKIP] channel {channel_id} not mapped")
            return

        if event_key in processed_slack_events:
            return

        processed_slack_events.append(event_key)
        text_preview = (event.get("text") or "")[:60]
        logging.info(f"<- Slack: \"{text_preview}\" -> WA (ch={channel_id})")
        thread_pool.submit(process_slack_to_whatsapp, event, web_client.token)


def process_slack_to_whatsapp(event, bot_token):
    if is_emergency_stopped():
        _emergency_note(outbound_blocked=1)
        logging.warning("🛑 BLOCKED by emergency stop: agent message not sent to WhatsApp.")
        return
    try:
        channel_id, slack_ts = event.get("channel"), event.get("ts")
        
        with config_lock:
            if channel_id not in slack_to_whatsapp_map:
                with open("process_debug.log", "a") as f: f.write(f"Early exit: {channel_id} not in map\n")
                logging.warning(f"Thread exiting: Channel {channel_id} not in mapping.")
                return
            mapping = slack_to_whatsapp_map[channel_id]

        whatsapp_chat_id = mapping.get("whatsapp_chat_id") or mapping.get("external_id") or mapping.get("lid")

        # Global Blocklist Check (Outbound)
        if sales_store.is_external_id_blocked(whatsapp_chat_id):
            logging.warning(f"🛡️ [BLOCKLIST] Recipient {whatsapp_chat_id} is in BLOCKLIST. Outbound message dropped.")
            return

        # Two-way blocking: check if client was handed over to delivery
        is_handed_off = bool(mapping.get('delivery_handed_off', False))
        if not is_handed_off and mapping.get('type') == 'sales':
            is_handed_off = db_store.is_client_in_delivery(whatsapp_chat_id)

        if is_handed_off:
            logging.info(f"Sales channel {channel_id} is handed over to delivery. Blocking Slack-to-WhatsApp message.")
            web_client = WebClient(token=bot_token)
            try:
                web_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=slack_ts,
                    text="*Info:* This client has been handed over to delivery. The sales channel is permanently paused and cannot send or receive messages. Please communicate via the delivery channel.",
                    username="Bitlink Bridge Info"
                )
            except Exception as e:
                logging.error(f"Failed to post handoff info message to Slack: {e}")
            return

        # Check paused status
        if mapping.get('paused', False):
            logging.info(f"Channel {channel_id} is paused. Blocking Slack-to-WhatsApp message.")
            web_client = WebClient(token=bot_token)
            try:
                web_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=slack_ts,
                    text="*Info:* This channel is currently paused. Outgoing Slack messages are not forwarded to WhatsApp.",
                    username="Bitlink Bridge Info"
                )
            except Exception as e:
                logging.error(f"Failed to post paused info message to Slack: {e}")
            return

        whatsapp_chat_id = mapping.get("whatsapp_chat_id") or mapping.get("external_id") or mapping.get("lid")
        client_name = mapping.get("client_name", "Unknown")
        # Cleaned up front so both the file-caption branch below and the
        # text-only branch get real emoji instead of Slack :shortcodes:.
        text_caption = clean_slack_links(event.get("text", ""))

        # '!!' prefix: message must remain in the channel and must not forward to the client
        if text_caption.strip().startswith('!!'):
            logging.info(f"[NOTE '!!'] Message kept in Slack channel {channel_id}, not forwarded to WhatsApp: \"{text_caption[:60]}\"")
            return

        with open("process_debug.log", "a") as f:
            f.write(f"Preparing to send message to {whatsapp_chat_id} (Client: {client_name})\n")
            f.write(f"Text caption: {text_caption}\n")
            
        logging.info(f"Preparing to send message to {whatsapp_chat_id} (Client: {client_name})")

        # --- Case 1: The message has files ---
        if "files" in event and event["files"]:
            is_first_file = True
            web_client = WebClient(token=bot_token) # For sending replies back to Slack

            for file_info in event["files"]:
                try:
                    file_size = file_info.get("size", 0)
                    file_name = file_info.get("name", "the file")

                    # [NEW] Pre-emptive validation of file size
                    if file_size > MAX_FILE_SIZE_BYTES:
                        error_message = (
                            f"⚠️ File transfer failed: `{file_name}` is "
                            f"{file_size / 1024 / 1024:.1f} MB, which exceeds the "
                            f"{MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB limit."
                        )
                        logging.warning(f"Blocked oversized file from Slack: {file_name} ({file_size} bytes)")
                        web_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=slack_ts,
                            text=error_message
                        )
                        continue # Skip this oversized file and continue to the next

                    # [UNCHANGED] File download logic
                    file_url = file_info.get("url_private_download")
                    if not file_url:
                        error_msg = f"❌ Could not download `{file_name}`. Your Slack App is missing the `files:read` scope! Please add it in the Slack API Dashboard and reinstall the app."
                        logging.error(error_msg)
                        web_client.chat_postMessage(channel=channel_id, thread_ts=slack_ts, text=error_msg)
                        continue

                    with requests.Session() as s:
                        s.headers.update({"Authorization": f"Bearer {bot_token}"})
                        file_response = s.get(file_url, timeout=300)
                    
                    if file_response.status_code != 200:
                        logging.error(f"Failed to download file from Slack: {file_name}. Status: {file_response.status_code}")
                        continue

                    # [UNCHANGED] Payload preparation
                    media_payload = {
                        "mimetype": file_info.get("mimetype"), 
                        "filename": file_name, 
                        "data": base64.b64encode(file_response.content).decode('utf-8')
                    }
                    
                    # [UNCHANGED] Logic to send caption only with the first file
                    current_caption = text_caption if is_first_file else ""
                    response = send_whatsapp_message(whatsapp_chat_id, current_caption, media_payload)

                    if response and response.get("success"):
                        data = response.get("data", {})
                        # [UNCHANGED] Logic to map message ID of the first file
                        if is_first_file:
                            slack_to_whatsapp_msg_map.set(slack_ts, data.get("messageId"))
                        logging.info(f"Forwarded file '{file_name}' to WhatsApp user '{client_name}'")
                        add_slack_reaction(web_client, channel_id, slack_ts, "white_check_mark")
                    else:
                        # [NEW] Better user feedback on send failure
                        error_from_service = response.get('error', 'unknown reason') if response else 'connection failed'
                        logging.error(f"Failed to forward file '{file_name}' via Node.js service: {error_from_service}")
                        # queue the failed file for retry
                        queue_outbox({
                            "chat_id": whatsapp_chat_id,
                            "message": current_caption,
                            "media": media_payload,
                            "mentions": None,
                            "slack_channel": channel_id,
                            "slack_ts": slack_ts,
                        })
                        add_slack_reaction(web_client, channel_id, slack_ts, "x")
                        web_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=slack_ts,
                            text=f"🔴 Failed to send `{file_name}` to WhatsApp. It has been queued and will be retried automatically."
                        )
                    
                    is_first_file = False
                    time.sleep(1)

                except Exception as e:
                    logging.error(f"An error occurred while processing a file for '{client_name}': {e}", exc_info=True)

        # --- Case 2: The message is text-only (FIXED FOR MENTIONS) ---
        else:
            # WhatsApp needs @mentions in the text to match mention IDs (must be @number)
            raw_text = text_caption
            mention_ids = extract_wa_mentions(raw_text)

            # Replace @name in text with @number (external_id) for WhatsApp mentions
            # 1. Extract @name mentions
            name_pattern = r'@([A-Za-z][A-Za-z0-9_\-]+)'
            names = re.findall(name_pattern, raw_text)
            # Remove any numbers accidentally picked up as names
            names = [n for n in names if not re.match(r'^[0-9]{10,15}$', n)]

            # 2. Lookup WhatsApp IDs for names using cached map
            name_to_id = {}
            if names:
                try:
                    name_to_id = {c.get("client_name"): sanitize_id(c.get("whatsapp_chat_id")) for c in slack_to_whatsapp_map.values() if c.get("client_name") and c.get("whatsapp_chat_id")}
                except Exception as e:
                    logging.error(f"Error looking up WhatsApp IDs for names: {e}")

            # 3. Replace @name with @number in the text
            final_text = raw_text
            for name in names:
                wa_id = name_to_id.get(name)
                if wa_id:
                    wa_number = wa_id.split('@')[0]
                    final_text = re.sub(rf'@{name}\b', f'@{wa_number}', final_text)

            # Clean Slack links (but keep @mentions)
            final_text = clean_slack_links(final_text)

            # Debug logging
            logging.info(f"📤 WhatsApp sending - Text: '{final_text[:100]}...', Mentions: {mention_ids}")
            response = send_whatsapp_message(whatsapp_chat_id, final_text, None, mention_ids)
            # Satisfy and reset SLA silence watch timer for this channel
            record_team_reply_for_sla(channel_id)
            web_client = WebClient(token=bot_token)
            if response and response.get("success"):
                data = response.get("data", {})
                slack_to_whatsapp_msg_map.set(slack_ts, data.get("messageId"))
                logging.info(f"Forwarded Slack text message to WhatsApp user '{client_name}'")
                add_slack_reaction(web_client, channel_id, slack_ts, "white_check_mark")
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response from WhatsApp service.'
                logging.error(f"Failed to forward Slack text message to WhatsApp user '{client_name}'. Reason: {error_msg}")
                # queue the failed text for retry and notify thread
                queue_outbox({
                    "chat_id": whatsapp_chat_id,
                    "message": final_text,
                    "media": None,
                    "mentions": mention_ids,
                    "slack_channel": channel_id,
                    "slack_ts": slack_ts,
                })
                add_slack_reaction(web_client, channel_id, slack_ts, "x")
                try:
                    web_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=slack_ts,
                        text=f"🔴 Failed to forward this message to WhatsApp. It has been queued and will be retried automatically.",
                        username="Bitlink Bridge Info"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify Slack thread of WhatsApp send error: {e}")

    except Exception as e:
        logging.error(f"A critical error occurred in process_slack_to_whatsapp: {e}", exc_info=True)


def sync_slack_downtime_messages(web_client: WebClient):
    """
    On startup, inspects active WhatsApp channels for any messages sent by agents
    while the BitLink bridge was down (messages that have no ✅ reaction).
    """
    time.sleep(3)
    # Never replay downtime messages onto an emergency-stopped bridge: that
    # would dump up to 24h of queued agent messages onto customers the instant
    # the process starts.
    if is_emergency_stopped():
        logging.warning(
            "🛑 Emergency stop is active - skipping downtime catch-up. "
            "Queued agent messages will not be replayed to customers."
        )
        return
    logging.info("🔄 [CATCH-UP] Scanning Slack for messages sent to WhatsApp during downtime...")
    with config_lock:
        channel_ids = list(slack_to_whatsapp_map.keys())

    recovered_count = 0
    for channel_id in channel_ids:
        try:
            res = web_client.conversations_history(
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

                text = msg.get("text", "")
                if text.strip().startswith("!!"):
                    continue

                if slack_to_whatsapp_msg_map.get(ts):
                    continue

                logging.info(f"🔄 [CATCH-UP] Recovered unsent message {ts} in channel {channel_id}. Forwarding to WhatsApp...")
                msg_copy = dict(msg)
                msg_copy["channel"] = channel_id
                process_slack_to_whatsapp(msg_copy, SLACK_BOT_TOKEN)
                recovered_count += 1
                time.sleep(0.5)
        except Exception as e:
            logging.warning(f"[CATCH-UP] Error checking downtime messages in channel {channel_id}: {e}")

    if recovered_count > 0:
        logging.info(f"✅ [CATCH-UP] Delivered {recovered_count} messages sent during downtime to WhatsApp.")
    else:
        logging.info("✅ [CATCH-UP] No unsent downtime messages found for WhatsApp.")

            
# --- Feature #20 / #40: SLA Response Timer & Silence Watcher ---
# Ported from the `add-ons` branch. Data access goes through db_store
# (PostgreSQL-first) instead of the old direct spreadsheet reads.
sla_pending_channels = {}  # {channel_id: {"last_client_msg_time": float, "warned_levels": set()}}
sla_lock = threading.Lock()
_sla_last_good = None  # last sla_minutes read from the dashboard, used if PG is briefly unavailable

# --- Feature #27: Sunday-Only Auto-Reply ---
# {f"{chat_id}_{YYYY-MM-DD}": timestamp} - one acknowledgment per client per Sunday.
sunday_replied_chats = {}
sunday_lock = threading.Lock()


def _sunday_prune_locked():
    """Keep only today's entries so the map cannot grow without bound."""
    today = datetime.now().strftime("%Y-%m-%d")
    for k in [k for k in sunday_replied_chats if not k.endswith(today)]:
        sunday_replied_chats.pop(k, None)


def handle_sunday_autoreply(chat_id: str, slack_channel: str, web_client: WebClient):
    """Sunday-only out-of-office acknowledgment to a client.

    Skipped while the emergency stop is active, since this sends outbound to a
    customer. Only marks the client as replied once the send actually
    succeeded, so a failed send is retried on their next message instead of
    being silently lost.
    """
    now = datetime.now()
    if now.weekday() != 6:  # Monday=0 ... Sunday=6
        return
    if not chat_id or not slack_channel:
        return
    if is_emergency_stopped():
        logging.warning("🛑 Sunday auto-reply suppressed by emergency stop.")
        return

    try:
        settings = db_store.get_sales_settings()
        if not settings.get("sunday_autoreply_enabled", False):
            return

        date_key = f"{chat_id}_{now.strftime('%Y-%m-%d')}"
        with sunday_lock:
            _sunday_prune_locked()
            if date_key in sunday_replied_chats:
                return  # already acknowledged this client today

        reply_text = settings.get("sunday_autoreply_text") or (
            "Thank you for reaching out! Our team is currently away for Sunday. "
            "We have received your message and will reach out to you first thing Monday morning."
        )

        resp = send_whatsapp_message(chat_id, reply_text)
        if not (resp and resp.get("success")):
            logging.error(
                f"📅 [SUNDAY AUTO-REPLY] Failed to send acknowledgment to {chat_id}: "
                f"{(resp or {}).get('error', 'unknown error')}. Will retry on their next message."
            )
            return

        with sunday_lock:
            sunday_replied_chats[date_key] = time.time()

        try:
            web_client.chat_postMessage(
                channel=slack_channel,
                text=f"ℹ️ *Sunday Auto-Reply:* Sent automated out-of-office acknowledgment to client:\n> _{reply_text}_",
                username="Bitlink Sunday Bot"
            )
        except Exception as e:
            logging.error(f"[SUNDAY AUTO-REPLY] Client notified but Slack record failed: {e}")
        logging.info(f"📅 [SUNDAY AUTO-REPLY] Acknowledgment sent to {chat_id} in {slack_channel}")
    except Exception as e:
        logging.error(f"Error dispatching Sunday auto-reply to {chat_id}: {e}")


def record_client_message_for_sla(channel_id: str):
    """Records an incoming client message timestamp to start or update the SLA timer."""
    if not channel_id:
        return
    with sla_lock:
        sla_pending_channels[channel_id] = {
            "last_client_msg_time": time.time(),
            "warned_levels": set()
        }
    logging.debug(f"⏱️ [SLA] Recorded client message for channel {channel_id}")


def record_team_reply_for_sla(channel_id: str):
    """Resets the SLA response timer when a sales rep replies to the client."""
    if not channel_id:
        return
    with sla_lock:
        if channel_id in sla_pending_channels:
            sla_pending_channels.pop(channel_id, None)
            logging.info(f"✅ [SLA] Team replied in channel {channel_id}. SLA timer satisfied & reset.")


def _lead_owner_for_channel(channel_id: str) -> str:
    """Look up a lead's owner, from PostgreSQL with the Sheets fallback."""
    try:
        for lead in db_store.get_sales_leads():
            if str(lead.get("slack_channel_id", "")).strip() == channel_id:
                return str(lead.get("owner_slack_id", "")).strip()
    except Exception as e:
        logging.debug(f"Error fetching lead owner for SLA alert: {e}")
    return ""


def _format_slack_user_tag(raw_id: str) -> str:
    """Render a Slack ID as a real mention, or fall back to bold text.

    Mentioning a non-ID string renders as a blank pill in Slack, so only IDs
    matching the documented shape get the <@...> treatment.
    """
    import re
    clean = raw_id.replace('<', '').replace('>', '').replace('@', '').strip() if raw_id else ""
    if clean and re.match(r'^[UW][0-9A-Z]{8,10}$', clean):
        return f"<@{clean}>"
    return f"*{clean}*" if clean else "<!channel>"


def sla_silence_watcher(web_client: WebClient):
    """Feature #20 / #40: Background SLA Response Timer & Silence Watcher.

    Posts a warning tagging the lead owner when a client has waited longer
    than the configured sla_minutes without a team reply.
    """
    logging.info("⏱️ SLA Silence Watcher worker initialized and active.")
    while not stop_event.is_set():
        try:
            # SLA warnings are posted into the client's own channel, so they are
            # customer-visible traffic and must stop with everything else.
            if is_emergency_stopped():
                stop_event.wait(20)
                continue
            # The dashboard is the only supported way to set the timer, so the
            # value is read straight from PostgreSQL. Falling back to the
            # spreadsheet here would let a stale row silently change the timer
            # during a database outage; instead we keep the last good value and
            # say so loudly.
            global _sla_last_good
            try:
                sla_mins = db_store.get_sales_settings(force_refresh=True).get("sla_minutes")
            except Exception as e:
                logging.error(f"[SLA] Could not read sla_minutes from PostgreSQL: {e}")
                sla_mins = None

            if isinstance(sla_mins, (int, float)) and sla_mins > 0:
                if sla_mins != _sla_last_good:
                    logging.info(f"[SLA] Response timer set to {sla_mins} min (from dashboard settings).")
                    _sla_last_good = sla_mins
            elif _sla_last_good is not None:
                logging.warning(
                    f"[SLA] Dashboard timer unavailable - continuing with last known value "
                    f"of {_sla_last_good} min."
                )
                sla_mins = _sla_last_good
            else:
                logging.warning("[SLA] No valid sla_minutes available yet; skipping this cycle.")
                stop_event.wait(20)
                continue

            now = time.time()
            with sla_lock:
                pending_snapshot = list(sla_pending_channels.items())

            for ch_id, data in pending_snapshot:
                last_msg_ts = data.get("last_client_msg_time", now)
                elapsed_mins = int((now - last_msg_ts) / 60)
                warned_levels = data.get("warned_levels", set())

                if elapsed_mins >= sla_mins and 1 not in warned_levels:
                    warned_levels.add(1)
                    data["warned_levels"] = warned_levels

                    owner_id = _lead_owner_for_channel(ch_id)
                    owner_tag = _format_slack_user_tag(owner_id)

                    warning_blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"⚠️ *SLA Response Warning:* {owner_tag}, this lead has been waiting for *{elapsed_mins} minutes* without a team reply!\n_Please review the chat and respond to keep our response time fast._"
                            }
                        }
                    ]

                    try:
                        web_client.chat_postMessage(
                            channel=ch_id,
                            text=f"⚠️ SLA Warning: {owner_tag}, this lead has been waiting for {elapsed_mins} minutes without a reply!",
                            blocks=warning_blocks
                        )
                        logging.info(f"🚨 [SLA WARNING] Posted warning in {ch_id} (elapsed: {elapsed_mins}m, owner: {owner_id})")
                    except Exception as post_err:
                        logging.error(f"Failed to post SLA warning to {ch_id}: {post_err}")

        except Exception as e:
            logging.error(f"Error in SLA Silence Watcher loop: {e}")

        stop_event.wait(30)


# --- Feature #32: Daily Sales Digest ---

def generate_and_post_sales_digest(web_client: WebClient, target_channel: str = None) -> bool:
    """Generates and posts the Daily Sales Digest to the designated Slack channel."""
    try:
        settings = db_store.get_sales_settings()
        dest_channel = target_channel or settings.get("digest_channel_id")

        # Auto-discover #sales-digest or #sales-general if not explicitly set
        if not dest_channel:
            try:
                chans = web_client.conversations_list(types="public_channel,private_channel", limit=100)
                for ch in chans.get("channels", []):
                    if ch["name"] in ["sales-digest", "sales-general", "sales"]:
                        dest_channel = ch["id"]
                        break
            except Exception as e:
                logging.debug(f"Could not auto-discover sales digest channel: {e}")

        if not dest_channel:
            logging.warning("⚠️ Daily Sales Digest skipped: No digest_channel_id configured in Settings or found.")
            return False

        records = db_store.get_sales_leads()

        # The pool is the roster: every active member is listed in the digest,
        # including those with nothing open, so the summary always reflects the
        # real team size rather than whoever happens to top a leaderboard.
        active_pool = {}
        for rep in db_store.get_sales_pool(active_only=True):
            uid = str(rep.get("slack_user_id", "")).strip()
            if uid:
                active_pool[uid] = str(rep.get("name", "")).strip()

        # Day boundaries are Pakistan days, so the digest's "today" matches the
        # business day the team is actually in.
        now = _now_pkt()
        today_str = now.strftime("%Y-%m-%d")

        new_leads_24h = 0
        stages_count = {"new": 0, "contacted": 0, "qualified": 0, "proposal": 0, "negotiation": 0, "won": 0, "lost": 0}
        won_today = 0
        total_pipeline_val = 0.0
        won_revenue_today = 0.0
        rep_counts = {}

        for lead in records:
            stage = str(lead.get("stage", "new")).strip().lower()
            if stage in stages_count:
                stages_count[stage] += 1
            else:
                stages_count[stage] = 1

            created_at = str(lead.get("created_at", "") or "")
            if created_at.startswith(today_str):
                new_leads_24h += 1

            # PostgreSQL returns NUMERIC as Decimal, so normalise via str()
            # before float() to keep values like 1000.00 parseable.
            deal_val_str = str(lead.get("deal_value", "") or "").replace("$", "").replace(",", "").strip()
            val = 0.0
            try:
                if deal_val_str:
                    val = float(deal_val_str)
            except ValueError:
                val = 0.0

            if stage not in ["won", "lost"]:
                total_pipeline_val += val

            if stage == "won":
                closed_at = str(lead.get("closed_at", "") or "")
                if closed_at.startswith(today_str):
                    won_today += 1
                    won_revenue_today += val

            # Rep distribution is scoped to the current sales pool. Leads still
            # owned by someone who has since left the pool are excluded here
            # (they remain in the pipeline totals above), and only open leads
            # are counted so the "active leads" label is accurate.
            owner = str(lead.get("owner_slack_id", "")).strip()
            if owner in active_pool and stage not in ("won", "lost"):
                rep_counts[owner] = rep_counts.get(owner, 0) + 1

        with sla_lock:
            pending_sla_channels = set(sla_pending_channels.keys())
        unanswered_count = len(pending_sla_channels)

        date_heading = now.strftime("%A, %B %d, %Y")
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📊 Daily Sales Digest — {date_heading}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"🆕 *New Leads (Today):*\n`{new_leads_24h}` leads"},
                    {"type": "mrkdwn", "text": f"💰 *Won Revenue (Today):*\n`${won_revenue_today:,.2f}` ({won_today} deals)"},
                    {"type": "mrkdwn", "text": f"📈 *Active Pipeline Value:*\n`${total_pipeline_val:,.2f}`"},
                    {"type": "mrkdwn", "text": f"⚠️ *Waiting Leads (SLA):*\n`{unanswered_count}` unreplied"}
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*📌 Pipeline Breakdown:*\n"
                        f"• 🟡 *Proposal / Negotiation:* `{stages_count.get('proposal', 0) + stages_count.get('negotiation', 0)}` deals\n"
                        f"• 🔵 *Qualified Leads:* `{stages_count.get('qualified', 0)}` leads\n"
                        f"• ⚪ *New / Contacted:* `{stages_count.get('new', 0) + stages_count.get('contacted', 0)}` leads\n"
                        f"• 🏆 *Total Won (All-Time):* `{stages_count.get('won', 0)}` deals"
                    )
                }
            }
        ]

        if active_pool:
            rep_lines = []
            for uid, name in sorted(active_pool.items(), key=lambda kv: rep_counts.get(kv[0], 0), reverse=True):
                tag = _format_slack_user_tag(uid)
                label = f"{_format_slack_user_tag(name)}" if name else tag
                rep_lines.append(f"• {label}: `{rep_counts.get(uid, 0)}` active leads")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*👥 Rep Distribution:*\n" + "\n".join(rep_lines)
                }
            })

        web_client.chat_postMessage(
            channel=dest_channel,
            text=f"📊 Daily Sales Digest — {date_heading}",
            blocks=blocks
        )
        logging.info(f"📢 [SALES DIGEST] Successfully posted digest to channel {dest_channel}")
        # Recorded here, not by the caller, so every route that sends a digest
        # (scheduler and manual trigger alike) counts against the same daily
        # allowance and none of them can produce a second digest the same day.
        if not db_store.set_setting(DIGEST_LAST_DAY_KEY, today_str,
                                    note="Last day the daily sales digest was posted (PKT)"):
            logging.warning(
                "⚠️ Digest was posted but the once-a-day marker could not be saved; "
                "the digest may be sent again until this succeeds.")
        return True
    except Exception as e:
        logging.error(f"Error generating sales digest: {e}", exc_info=True)
        return False


def post_sales_insights(web_client: WebClient, insights: str, facts: dict) -> bool:
    """Posts the weekly win/loss report to the digest channel (Feature #45).

    Plain text rather than blocks: the same string is shown in the dashboard
    modal, which renders it verbatim, so Slack-only formatting would show up as
    stray asterisks there.
    """
    settings = db_store.get_sales_settings()
    dest = settings.get("digest_channel_id") or settings.get("insights_channel_id")
    if not dest:
        try:
            for ch in web_client.conversations_list(
                types="public_channel,private_channel", limit=100
            ).get("channels", []):
                if ch["name"] in ("sales-insights", "sales-digest", "sales-general", "sales"):
                    dest = ch["id"]
                    break
        except Exception as e:
            logging.debug(f"Could not auto-discover insights channel: {e}")
    if not dest:
        logging.warning("⚠️ Win/Loss Insights skipped: no digest_channel_id configured.")
        return False
    try:
        web_client.chat_postMessage(
            channel=dest,
            text=insights,
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*🧠 Weekly Win/Loss Insights — {facts['window_label']}*\n"
                        f"_Closed: {facts['total_closed']} · "
                        f"Win rate: {'n/a' if facts['win_rate'] is None else str(facts['win_rate']) + '%'}_\n\n"
                        f"{insights}"
                    ),
                },
            }],
        )
        logging.info(f"🧠 [SALES INSIGHTS] Posted report to channel {dest}")
        return True
    except Exception as e:
        logging.error(f"Failed to post sales insights: {e}", exc_info=True)
        return False


def _parse_digest_time(raw):
    """Parse digest_time into (hour, minute), tolerating unpadded values.

    The Settings sheet contains a legacy unpadded '9:00' that never matches a
    'HH:MM' clock, which silently disabled the digest. Normalising here means
    both forms work, and a genuinely invalid value is rejected loudly instead
    of quietly never firing.
    """
    import re
    m = re.match(r'^\s*(\d{1,2})\s*:\s*(\d{2})\s*$', str(raw or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


# The digest is scheduled against Pakistan time, not whatever the host happens
# to be set to. A UTC host would otherwise fire the 09:00 digest at 14:00 PKT.
# Pakistan has no daylight saving, so the fixed +05:00 fallback below is exactly
# equivalent to Asia/Karachi and needs no tzdata installed.
try:
    from zoneinfo import ZoneInfo

    DIGEST_TZ = ZoneInfo("Asia/Karachi")
except Exception:  # pragma: no cover - only when tzdata is unavailable
    from datetime import timedelta, timezone as _tz

    DIGEST_TZ = _tz(timedelta(hours=5), "PKT")

# Persisted in the settings table rather than a local variable, so restarting
# the bridge cannot make the scheduler forget it already sent today's digest.
DIGEST_LAST_DAY_KEY = "digest_last_posted_day"


def _now_pkt() -> datetime:
    """Current time in Pakistan, regardless of how the host is configured."""
    return datetime.now(DIGEST_TZ)


def daily_sales_digest_worker(web_client: WebClient):
    """Feature #32: Background Daily Sales Digest Scheduler.

    Posts the digest at most once per day, at or after the configured
    digest_time, evaluated in Pakistan time.

    The "already sent today" marker lives in the settings table rather than in
    a local variable. It used to be local, which meant every bridge restart
    started the day from scratch and re-sent the digest — the observed symptom
    being two or three digests a day plus one whenever the script was started
    after the scheduled time. Reading the marker from the database on each pass
    makes the schedule survive restarts.

    A digest that would have been due while the bridge was not running is
    skipped, not sent late. Firing hours after the configured time is worse
    than not firing at all, so a bridge that starts after digest_time waits for
    tomorrow instead of posting a stale digest on its first pass.
    """
    logging.info("📊 Daily Sales Digest scheduler initialized and active "
                 f"(Pakistan time, currently {_now_pkt().strftime('%H:%M')}).")

    # Anything due before this instant is treated as deliberately missed: a
    # bridge started after digest_time must not fire a late digest for that day.
    started_at = _now_pkt()

    while not stop_event.is_set():
        wait_seconds = 60
        try:
            settings = db_store.get_sales_settings()
            if not settings.get("digest_enabled", True):
                stop_event.wait(60)
                continue

            now = _now_pkt()
            today_str = now.strftime("%Y-%m-%d")
            configured = _parse_digest_time(settings.get("digest_time", "09:00"))
            if configured is None:
                logging.error(
                    f"⏰ digest_time {settings.get('digest_time')!r} is not a valid time; digest not sent. "
                    f"Set it to e.g. '09:00' in the dashboard."
                )
                stop_event.wait(300)
                continue

            last_posted_day = db_store.get_setting(DIGEST_LAST_DAY_KEY).strip()
            if last_posted_day == today_str:
                stop_event.wait(60)
                continue

            # Compare whole timestamps, not minutes-since-midnight: a worker
            # started yesterday must still fire today's digest, which a bare
            # minutes comparison would get wrong.
            due_at = now.replace(hour=configured[0], minute=configured[1],
                                 second=0, microsecond=0)

            if now >= due_at:
                if started_at <= due_at:
                    generate_and_post_sales_digest(web_client)
                else:
                    logging.info(
                        f"⏰ Skipping today's digest: bridge started at "
                        f"{started_at.strftime('%H:%M')}, after digest_time "
                        f"{settings.get('digest_time')}. Next digest at the "
                        f"next configured time."
                    )
            else:
                # Not due yet: sleep right up to the scheduled minute so the
                # digest goes out on time rather than up to a minute late.
                remaining = (due_at - now).total_seconds()
                if 0 < remaining < wait_seconds:
                    wait_seconds = remaining

        except Exception as e:
            logging.error(f"Error in daily sales digest worker: {e}")

        stop_event.wait(wait_seconds)


def auto_archive_worker(web_client: WebClient):
    """Background thread that runs the auto-archive job once per day at 03:00.

    Reads ``archive_after_days`` from settings (default 30) and archives any
    Slack channels whose deal was closed or handed to delivery beyond that
    threshold.  Runs independently of sales_ai_jobs.py so the feature is always
    active as long as the WhatsApp bridge is running.

    Shares the digest scheduler's persisted per-day marker and Pakistan clock,
    for the same reason: a local ``last_run_day`` resets on every restart, so
    the job used to re-archive every time the bridge was restarted after 03:00.
    """
    from sales_ai_jobs import run_auto_archive_job
    logging.info("🗄️ Auto-Archive worker initialized (runs daily at 03:00 PKT).")
    last_key = "auto_archive_last_run_day"

    while not stop_event.is_set():
        try:
            now = _now_pkt()
            today_str = now.strftime("%Y-%m-%d")
            if db_store.get_setting(last_key).strip() == today_str:
                stop_event.wait(60)
                continue
            # Fire once per day at or after 03:00
            if now.hour * 60 + now.minute >= 3 * 60:
                run_auto_archive_job(web_client=web_client)
                db_store.set_setting(last_key, today_str,
                                     note="Last day auto-archive ran (PKT)")
        except Exception as e:
            logging.error(f"[AUTO-ARCHIVE] Worker error: {e}")

        stop_event.wait(60)


# --- Service Management ---

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Routes are defined at module level so they are registered on import rather
# than only once the server thread starts, which also makes them testable.
@app.route('/refresh', methods=['POST'])
def refresh_endpoint():
    threading.Thread(target=reload_config).start()
    return "Refresh signal received.", 200
@app.route('/health', methods=['GET'])
def health():
    snap = emergency_snapshot()
    return {
        "ok": not snap.get("active"),
        "bridge": "whatsapp",
        "clients": len(whatsapp_to_slack_map),
        "emergency_stop": snap,
    }
@app.route('/sales-digest/trigger', methods=['POST'])
def trigger_digest_endpoint():
    # Build the client here: run_refresh_server() runs on its own thread and
    # has no access to the one created in main().
    web_c = WebClient(token=SLACK_BOT_TOKEN)
    success = generate_and_post_sales_digest(web_c)
    return jsonify({"success": success, "message": "Digest dispatched." if success else "Failed or no channel configured."})
@app.route('/sales-insights/generate', methods=['POST'])
def generate_insights_endpoint():
    # Feature #45. control_server.py proxies to this route; it was missing
    # entirely, so the dashboard's "AI Win/Loss Insights" button 404'd.
    body = {}
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    try:
        if is_emergency_stopped():
            return jsonify({"success": False, "error": "Emergency stop is active"}), 503

        now = datetime.now()
        leads = db_store.get_sales_leads()
        closed, undated, window_days = sales_insights.select_window(leads, now)
        rep_names = {
            str(r.get("slack_user_id", "")).strip(): str(r.get("name", "")).strip()
            for r in db_store.get_sales_pool(active_only=True)
            if str(r.get("slack_user_id", "")).strip()
        }
        facts = sales_insights.build_insight_facts(closed, undated, rep_names, now, window_days)
        facts_block = sales_insights.render_facts_block(facts)

        posting = bool(body.get("post_to_slack"))
        # When the dashboard sends the report back, share exactly that text.
        # Regenerating here would post something the user never read, and would
        # spend another Gemini call on a report that is already on screen.
        reviewed = body.get("insights")
        reviewed = reviewed.strip() if isinstance(reviewed, str) else ""

        # Gemini is a bonus layer: the free tier is capped per day and runs
        # out often, so a failed call must not blank out a report that is
        # already fully computed.
        if posting and reviewed:
            report, ai_note = reviewed, None
        elif not facts["total_closed"]:
            # Nothing closed: there is no data to reason about, so skip the call
            # rather than spend quota asking the model to comment on silence.
            report, ai_note = sales_insights.render_facts_block(facts), None
        else:
            ai_narrative, ai_note = None, None
            try:
                window_note = (
                    f"The window was widened to {window_days} days because fewer than "
                    f"{sales_insights.MIN_DEALS_FOR_REPORT} deals closed in the last 7."
                    if window_days != 7 else ""
                )
                reply = gemini_client.call_gemini_api(
                    ai_prompts.build_insights_prompt(facts_block, window_note), timeout=60
                )
                if reply:
                    ai_narrative = reply
                else:
                    ai_note = "AI summary unavailable (empty response); showing computed statistics."
            except Exception as ai_err:
                logging.warning(f"Win/loss AI summary unavailable, using computed stats: {ai_err}")
                ai_note = f"AI summary unavailable ({ai_err}); showing computed statistics."
            report = sales_insights.render_report(facts, ai_narrative)

        if posting:
            web_c = WebClient(token=SLACK_BOT_TOKEN)
            posted = post_sales_insights(web_c, report, facts)
            if not posted:
                return jsonify({
                    "success": False,
                    "insights": report,
                    "error": "Report generated but could not be posted to Slack: no insights channel configured.",
                }), 500
        return jsonify({
            "success": True,
            "insights": report,
            "note": ai_note,
            "posted": posting,
            "deals_closed": facts["total_closed"],
            "window_days": window_days,
        })
    except Exception as e:
        logging.error(f"Error generating sales insights: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"Failed to generate insights: {e}"}), 500


def run_refresh_server():
    log.setLevel(logging.ERROR)
    logging.info(f"WhatsApp refresh server listening on port {WHATSAPP_REFRESH_PORT}")
    app.run(host='0.0.0.0', port=int(WHATSAPP_REFRESH_PORT))


# --- Flask endpoint for WhatsApp→Slack edit sync ---
from flask import request

@app.route('/whatsapp-edit', methods=['POST'])
def whatsapp_edit_endpoint():
    if is_emergency_stopped():
        _emergency_note(inbound_dropped=1)
        return jsonify({'success': False, 'error': 'Emergency stop is active'}), 503
    data = request.get_json(force=True)
    message_id = data.get('messageId')
    new_text = data.get('newText')
    if not message_id or not new_text:
        return jsonify({'success': False, 'error': 'Missing messageId or newText'}), 400
    mapping = wa_to_slack_map.get(message_id)
    if not mapping:
        logging.warning(f"No Slack mapping found for WhatsApp message {message_id}")
        return jsonify({'success': False, 'error': 'No Slack mapping found'}), 200
    try:
        web_client = WebClient(token=SLACK_BOT_TOKEN)
        new_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{new_text} *(edited)*"}}]
        web_client.chat_update(
            channel=mapping["channel"], 
            ts=mapping["ts"], 
            text=new_text,
            blocks=new_blocks
        )
        logging.info(f"✅ Updated Slack message {mapping['ts']} with new WhatsApp text.")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Failed to update Slack message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/whatsapp-delete', methods=['POST'])
def whatsapp_delete_endpoint():
    if is_emergency_stopped():
        _emergency_note(inbound_dropped=1)
        return jsonify({'success': False, 'error': 'Emergency stop is active'}), 503
    data = request.get_json(force=True)
    message_id = data.get('messageId')
    if not message_id:
        return jsonify({'success': False, 'error': 'Missing messageId'}), 400
    mapping = wa_to_slack_map.get(message_id)
    if not mapping:
        logging.warning(f"No Slack mapping found for WhatsApp msg {message_id}")
        return jsonify({'success': False, 'error': 'No Slack mapping found'}), 200
    try:
        web_client = WebClient(token=SLACK_BOT_TOKEN)
        web_client.chat_postMessage(
            channel=mapping["channel"],
            thread_ts=mapping["ts"],
            text="🗑️ This message was deleted by the client on WhatsApp.",
            username="Bitlink Bridge Info"
        )
        wa_to_slack_map.delete(message_id)
        logging.info(f"Posted delete notice for WhatsApp msg {message_id}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Failed to post delete notice: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def poller_with_restart(web_client):
    while not stop_event.is_set():
        try:
            poll_whatsapp_and_forward(web_client)
        except Exception as e:
            logging.error(f"Poller crashed: {e}. Restarting in 5s...")
            time.sleep(5)

def main():
    if not all([SLACK_APP_TOKEN, SLACK_BOT_TOKEN]):
        sys.exit("FATAL: SLACK_APP_TOKEN or SLACK_BOT_TOKEN is missing.")

    global sent_wa_alerts
    sent_wa_alerts = load_sent_wa_alerts()

    reload_config() 
    web_client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        auth_test = web_client.auth_test()
        global slack_bot_user_id, slack_bot_id, manager_bot_id, manager_bot_user_id
        slack_bot_user_id = auth_test.get("user_id")
        slack_bot_id = auth_test.get("bot_id")
        logging.info(f"Slack bridge bot user ID: {slack_bot_user_id}, bot ID: {slack_bot_id}")

        manager_token = os.getenv("MANAGER_SLACK_BOT_TOKEN")
        if manager_token:
            try:
                m_client = WebClient(token=manager_token)
                m_auth = m_client.auth_test()
                manager_bot_user_id = m_auth.get("user_id")
                manager_bot_id = m_auth.get("bot_id")
                logging.info(f"Manager bot user ID: {manager_bot_user_id}, bot ID: {manager_bot_id}")
            except Exception as me:
                logging.warning(f"Could not fetch manager bot info: {me}")
    except Exception as e:
        logging.warning(f"Could not fetch bot user ID: {e}")
    socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

    # Read the emergency stop synchronously BEFORE any worker starts, so a
    # bridge that is restarted while stopped cannot slip a message through
    # during the watcher's first poll interval.
    try:
        _set_emergency_stop(bool(db_store.get_sales_settings().get("kill_switch_autocreate", False)))
    except Exception as e:
        logging.error(f"[STOP] Could not read initial kill switch state: {e}")

    thread_pool.submit(poller_with_restart, web_client)
    thread_pool.submit(periodic_outbox_worker, web_client)
    thread_pool.submit(run_refresh_server)
    thread_pool.submit(emergency_stop_watcher, stop_event)
    # Feature #20/#40 SLA watcher and Feature #32 daily digest scheduler,
    # ported from the `add-ons` branch.
    thread_pool.submit(sla_silence_watcher, web_client)
    thread_pool.submit(daily_sales_digest_worker, web_client)
    thread_pool.submit(auto_archive_worker, web_client)
    def raw_event_logger(client, req):
        try:
            with open("raw_slack_events.log", "a") as f:
                f.write(f"REQ_TYPE: {req.type} | PAYLOAD: {str(req.payload)}\n")
        except:
            pass

    socket_client.socket_mode_request_listeners.append(raw_event_logger)
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_delete_event(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_message(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_edit_event(client, req, web_client)
    )
    # Handle interactive events (button clicks on lead welcome cards, modal submissions)
    # These arrive on the WhatsApp bridge socket because the buttons were posted by this bot.
    try:
        import bitlink_manager
        socket_client.socket_mode_request_listeners.append(
            lambda client, req: bitlink_manager.handle_slack_block_actions(client, req, web_client) if req.type == "interactive" else None
        )
        socket_client.socket_mode_request_listeners.append(
            lambda client, req: bitlink_manager.handle_modal_submission(client, req, web_client) if req.type == "interactive" else None
        )
        logging.info("✅ Registered interactive event handlers (block_actions, modal_submissions) on WhatsApp bridge socket.")
    except Exception as e:
        logging.error(f"Failed to register interactive handlers from bitlink_manager: {e}")
    logging.info("Connecting to Slack and entering listener loop...")
    socket_client.connect()
    thread_pool.submit(sync_slack_downtime_messages, web_client)
    
    while not stop_event.is_set():
        time.sleep(1)

if __name__ == "__main__":
    critical_alerts.install_excepthook("whatsapp_bridge")
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown signal received. Waiting for threads to finish...")
        stop_event.set()
        try:
            thread_pool.shutdown(wait=True, cancel_futures=False)
        except Exception as e:
            logging.warning(f"Thread pool shutdown issue: {e}")
        logging.info("All processing threads have finished. Bridge shut down.")