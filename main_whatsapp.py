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
import json
import pathlib
import uuid

import google.generativeai as genai
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.errors import SlackApiError
from slack_sdk.models.views import View
from slack_sdk.models.blocks import InputBlock, SectionBlock, PlainTextObject
from slack_sdk.models.blocks import PlainTextInputElement

from g_sheets_client import get_client_mappings

# Convert MB to bytes for easy comparison
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_MB", 50)) * 1024 * 1024

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

app = Flask(__name__)


class BoundedMessageMap:
    """Bounded message map with TTL and size limits to prevent memory leaks."""

    def __init__(self, max_size=50000, ttl_hours=48):
        self.map = OrderedDict()
        self.timestamps = {}
        self.max_size = max_size
        self.ttl_seconds = ttl_hours * 3600
        self.lock = threading.Lock()

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


# --- Global State Variables ---
config_lock = threading.Lock()
stop_event = threading.Event()
processed_slack_events = deque(maxlen=1000)
processed_whatsapp_events = deque(maxlen=500)
whatsapp_to_slack_map = {}
slack_to_whatsapp_map = {}
slack_to_whatsapp_msg_map = BoundedMessageMap(max_size=50000, ttl_hours=48)
thread_pool = ThreadPoolExecutor(max_workers=20)
slack_bot_user_id = None

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


MASTER_PROMPT = """
YOUR TASK:
Please take my notes and the prior chat. Write me a complete, ready-to-send message for the client.
The message should always:
	•	Be polite, professional, friendly, and respectful.
	•	Use clear, natural, human language.
	•	Be effortlessly engaging and approachable.
	•	Be simple and conversational.
	•	Vary sentence lengths naturally (short and long mixed).
	•	Use common idioms or informal fillers (like “just,” “honestly,” “you know”) sparingly and authentically.
	•	Limit and casually explain any professional jargon.
	•	Show that I am managing coordination between client and dev smoothly.
	•	Include details about timelines, milestones, deliverables, or next steps if needed.
	•	Address any questions or issues from the previous chat.
	•	Add anything helpful so the client feels informed and supported.
	•	Include a polite, natural closing.
	•	Keep the style spontaneous, dynamic, and authentically human.
	•	Maintain a conversational tone—even when explaining technical topics.
	•	Do not use emojis.
IMPORTANT RULES:
	•	Be concise and to the point. Aim to match the length and complexity of my raw draft.
	•	Directly address the core question or statement from my draft first, before adding any extra detail.
	•	Do not over-explain unless my draft specifically asks for a detailed explanation.
	•	Do not include any introductory or framing lines that talk about delivering the message, such as “Sure! Here’s your client-ready message,” or anything similar.
	•	Do not include any closing lines or sign-offs like “Cheers,” “Regards,” “Thanks,” or my name.
	•	The output should be only the client-ready message itself—clean, direct, and ready to paste straight into the chat thread without any added explanation or wrapper.
"""

def format_chat_history(messages, bot_user_id):
    """Formats Slack message history into a clean 'Me:' and 'Client:' format for the AI."""
    history = []
    for msg in reversed(messages):  
        user_name = "Client"
        # Check if the message is from our team (the bot/bridge)
        if 'bot_id' in msg or msg.get('user') == bot_user_id:
            user_name = "Me"
        
        # If it's a message posted with a custom username (from your bridge) it's a client
        if 'username' in msg:
             user_name = "Client"
        
        text = msg.get('text', '')
        if text:
            history.append(f"{user_name}: {text}")
    return "\n".join(history)
   
    
def get_enhanced_message(chat_history, raw_draft):
    """Calls the Google Gemini API to get the rewritten message without project context."""
    try:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not set")
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-pro-latest')
        
        
        final_prompt = f"{MASTER_PROMPT}\n\n---\nHi, I want to prepare a client message. Below I’m sharing:\n\n1. Previous chat with the client:\n---\n{chat_history}\n---\n\n2. My raw draft or bullet points of what I want to say next:\n---\n{raw_draft}\n---"
        
        response = model.generate_content(final_prompt)
        return response.text.strip()
        
    except Exception as e:
        logging.error(f"Google Gemini API call failed: {e}")
        return f"Sorry, I couldn't enhance the message using Gemini. Error: {e}"

def process_enhancement_in_background(channel_id, user_id, raw_draft, bot_user_id, web_client_token):
    """Fetches history, calls AI, and sends the private reply. Runs in a background thread."""
    web_client = WebClient(token=web_client_token)
    try:
        history_response = web_client.conversations_history(channel=channel_id, limit=5)
        chat_history = format_chat_history(history_response.get('messages', []), bot_user_id)
        enhanced_message = get_enhanced_message(chat_history, raw_draft)
        web_client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"Here is your enhanced message:\n\n{enhanced_message}"
        )
    except Exception as e:
        logging.error(f"Error processing enhancement: {e}")
        web_client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"An error occurred while enhancing your message: {e}"
        )




import re


def clean_slack_links(text):
    """Replace any Slack link format <scheme:...|display> with display text only."""
    # Handles http, https, mailto, and any other scheme
    return re.sub(r'<[^|>]+\|([^>]+)>', r'\1', text)

def extract_wa_mentions(text):
    """
    Extract WhatsApp mention IDs from Slack message text.
    Supports both @number and @name mentions.
    Looks up WhatsApp IDs for names using Google Sheet.
    """
    # 1. Extract @number mentions (e.g., @923001234567)
    numbers = re.findall(r'@([0-9]{10,15})', text)
    mention_ids = [f"{n}@c.us" for n in numbers]

    # 2. Extract @name mentions (e.g., @Ali)
    names = re.findall(r'@([A-Za-z][A-Za-z0-9_\-]+)', text)
    # Remove any numbers accidentally picked up as names
    names = [n for n in names if not re.match(r'^[0-9]{10,15}$', n)]

    # 3. Lookup WhatsApp IDs for names using Google Sheet
    if names:
        try:
            from g_sheets_client import get_client_mappings
            client_mappings = get_client_mappings("WhatsApp")
            name_to_id = {c.get("client_name"): sanitize_id(c.get("external_id")) for c in client_mappings if c.get("client_name") and c.get("external_id")}
            for name in names:
                wa_id = name_to_id.get(name)
                if wa_id:
                    mention_ids.append(wa_id)
        except Exception as e:
            logging.error(f"Error looking up WhatsApp IDs for names: {e}")
    return mention_ids
def sanitize_id(external_id):
    if not external_id: return None
    printable_chars = set(string.printable)
    clean_id = "".join(filter(lambda char: char in printable_chars, str(external_id)))
    return clean_id.strip()

def get_whatsapp_messages():
    try:
        response = requests.get(f"{NODE_API_URL}/get-messages", timeout=10)
        if response.status_code == 200: return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error connecting to WhatsApp service: {e}")
    return []

def send_whatsapp_message(chat_id, message, media=None, mentions=None, max_retries=3):
    """Send WhatsApp message with retry logic and detailed error reporting."""
    for attempt in range(max_retries):
        try:
            payload = {"chatId": chat_id, "message": message, "media": media}
            if mentions:
                payload["mentions"] = mentions

            response = requests.post(f"{NODE_API_URL}/send-message", json=payload, timeout=10)

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
    logging.info(f"Attempting to delete WhatsApp message: {message_id}")
    try:
        payload = {"messageId": message_id}
        response = requests.post(f"{NODE_API_URL}/delete-message", json=payload, timeout=10)
        logging.info(f"Delete request sent. Status code: {response.status_code}, Response: {response.text}")
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Error deleting WhatsApp message: {e}")
    return False


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
                    # notify Slack thread of delivery
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
            success = edit_whatsapp_message(whatsapp_msg_id, new_text)
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
                  f"• Action: Add this ID to Google Sheet for the relevant client")
        )
        logging.info(f"Sent new WhatsApp chat alert for {chat_id}")
    except Exception as e:
        logging.error(f"Failed to send WhatsApp chat alert: {e}")


# --- Core Logic ---

def reload_config():
    global whatsapp_to_slack_map, slack_to_whatsapp_map
    logging.info("(WhatsApp Bridge) Refresh signal received! Reloading config...")
    client_mappings_raw = get_client_mappings("WhatsApp")
    if client_mappings_raw:
        new_mappings = [{
            "client_name": c.get("client_name"), 
            "whatsapp_chat_id": sanitize_id(c.get("external_id")), 
            "lid": sanitize_id(c.get("lid")),
            "slack_channel_id": c.get("slack_channel_id"),
            "paused": c.get("paused", False)
        } for c in client_mappings_raw]
        with config_lock:
            whatsapp_to_slack_map.clear()
            slack_to_whatsapp_map.clear()
            for item in new_mappings:
                if item.get("whatsapp_chat_id"):
                    whatsapp_to_slack_map[item["whatsapp_chat_id"]] = item
                if item.get("lid"):
                    whatsapp_to_slack_map[item["lid"]] = item
            slack_to_whatsapp_map.update({item["slack_channel_id"]: item for item in new_mappings if item.get("slack_channel_id")})
        logging.info(f"Configuration reloaded. Now tracking {len(whatsapp_to_slack_map)} clients.")
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
    return False
    
def poll_whatsapp_and_forward(web_client: WebClient):
    logging.info("WhatsApp polling worker has started.")
    while not stop_event.is_set():
        try:
            with config_lock:
                current_clients = dict(whatsapp_to_slack_map)
            new_messages = get_whatsapp_messages()
            for msg in new_messages:
                event_id = msg.get('messageId')
                chat_id = msg.get('chatId')
                if not event_id or not chat_id:
                    continue

                if event_id not in processed_whatsapp_events:
                    if chat_id not in current_clients:
                        sender_name = msg.get('senderName')
                        logging.info(f"WA-IN SKIP: chat_id={chat_id!r} NOT in keys={list(current_clients.keys())[:5]}")
                        send_new_wa_chat_alert(chat_id, web_client, sender_name=sender_name)
                        continue
                    processed_whatsapp_events.append(event_id)
                    client_info = current_clients[chat_id]
                    slack_channel = client_info["slack_channel_id"]
                    # Check paused status before forwarding
                    if client_info.get('paused', False):
                        logging.info(f"Channel {slack_channel} is paused. Skipping forwarding.")
                        continue

                    sender_name = msg.get('senderName')
                    mapped_name = client_info["client_name"]
                    display_name = sender_name if sender_name else mapped_name

                    content = msg.get('body', '')
                    quoted_body = msg.get('quotedBody')

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
                            web_client.files_upload_v2(
                                channel=slack_channel, 
                                content=file_content, 
                                filename=msg['media'].get('filename', 'file.bin'),
                                initial_comment=final_text_payload
                            )
                        else:
                            notification_text = f"{display_name}: {content}"
                            message_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
                            web_client.chat_postMessage(
                                channel=slack_channel,
                                text=notification_text,
                                blocks=message_blocks,
                                username=display_name,
                            )
                        logging.info(f"Forwarded WhatsApp message from '{display_name}' to Slack")
                    except SlackApiError as e:
                        logging.error(f"Slack API error forwarding from '{display_name}': {e.response['error']}")

            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Poller loop error, continuing in 5s: {e}")
            time.sleep(5)
    logging.info("WhatsApp polling worker is shutting down.")



def process_and_update_modal(channel_id, raw_draft, bot_user_id, view_id, web_client_token):
    """Fetches AI result and then UPDATES the existing 'loading' modal with the final view."""
    web_client = WebClient(token=web_client_token)
    try:
        # --- 1. Get Context and Enhanced Message (Slow part) ---
        history_response = web_client.conversations_history(channel=channel_id, limit=5)
        chat_history = format_chat_history(history_response.get('messages', []), bot_user_id)
        enhanced_message = get_enhanced_message(chat_history, raw_draft)

        # --- 2. Build the FINAL, interactive modal view ---
        final_view = View(
            type="modal",
            callback_id="enhance_modal_submission",
            title=PlainTextObject(text="Review & Send"),
            submit=PlainTextObject(text="Send"),
            close=PlainTextObject(text="Cancel"),
            private_metadata=channel_id, # Pass channel_id for the submission handler
            blocks=[
                SectionBlock(
                    text=PlainTextObject(text="Review the enhanced message below. You can edit it before sending.")
                ),
                InputBlock(
                    block_id="enhanced_text_block",
                    element=PlainTextInputElement(
                        action_id="enhanced_text_input",
                        multiline=True,
                        initial_value=enhanced_message
                    ),
                    label=PlainTextObject(text="Client-Ready Message"),
                ),
            ]
        )

        # --- 3. Update the existing modal with the final content ---
        web_client.views_update(
            view_id=view_id,
            view=final_view
        )

    except Exception as e:
        logging.error(f"Error updating modal view: {e}")
        # Optionally, update the modal to show an error
        error_view = View(
            type="modal",
            title=PlainTextObject(text="Error"),
            close=PlainTextObject(text="Close"),
            blocks=[SectionBlock(text=f"Sorry, an error occurred: {e}")]
        )
        try:
            web_client.views_update(view_id=view_id, view=error_view)
        except Exception as update_err:
            logging.error(f"Failed to update modal with error message: {update_err}")


def handle_enhance_command_socket_mode(client: SocketModeClient, req, web_client: WebClient):
    """Handles the /enhance command by INSTANTLY opening a loading modal."""
    client.send_socket_mode_response({"envelope_id": req.envelope_id})

    if req.payload.get("command") == "/enhance":
        data = req.payload
        trigger_id = data.get('trigger_id')
        channel_id = data.get('channel_id')
        user_id = data.get('user_id')
        raw_draft = data.get('text')

        try:
            # 1. Create a simple "loading" view
            loading_view = View(
                type="modal",
                callback_id="enhance_modal_submission", # Callback ID is still needed for the final view
                title=PlainTextObject(text="Enhancing..."),
                close=PlainTextObject(text="Cancel"),
                blocks=[
                    SectionBlock(text="Please wait while I enhance your message with Gemini... 🤖")
                ]
            )

            # 2. Open the loading view IMMEDIATELY. This is the key change.
            view_response = web_client.views_open(
                trigger_id=trigger_id,
                view=loading_view
            )
            
            # 3. Get the view_id from the response. We need this to update the modal later.
            view_id = view_response.get("view", {}).get("id")

            # 4. Get bot_user_id for context formatting
            auth_response = web_client.auth_test()
            bot_user_id = auth_response.get("user_id")

            # 5. Start the background thread, passing the new view_id
            thread = threading.Thread(
                target=process_and_update_modal, # Note the function name change
                args=(channel_id, raw_draft, bot_user_id, view_id, web_client.token)
            )
            thread.start()

        except SlackApiError as e:
            logging.error(f"Error opening loading modal: {e.response['error']}")
# In main_whatsapp.py
# ADD this new function to handle the "Send" button click

def handle_modal_submission(client: SocketModeClient, req, web_client: WebClient):
    """Handles the submission of the 'enhance' modal."""
    # An empty response is a special instruction to close the modal.
    client.send_socket_mode_response({"envelope_id": req.envelope_id})

    payload = req.payload
    # 1. Check if this is the submission from our specific modal
    if payload.get("type") == "view_submission" and payload.get("view", {}).get("callback_id") == "enhance_modal_submission":
        
        # 2. Extract the data we need
        # Get the channel_id we stored in the metadata
        channel_id = payload.get("view", {}).get("private_metadata", "")
        
        # Get the state of the input block
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        
        # Extract the final text from the input block using the IDs we defined
        final_message = view_state.get("enhanced_text_block", {}).get("enhanced_text_input", {}).get("value")

        if not channel_id or not final_message:
            logging.error("Could not extract channel_id or message from modal submission.")
            return

        # 3. Post the final message to the original channel
        try:
            web_client.chat_postMessage(
                channel=channel_id,
                text=final_message
                # Note: The message will be posted by your bot.
            )
            logging.info(f"Successfully posted enhanced message to channel {channel_id}")
        except Exception as e:
            logging.error(f"Failed to post message from modal to {channel_id}: {e}")

def handle_slack_delete_event(client: SocketModeClient, req, web_client: WebClient):
    """Handle when a message is deleted in Slack"""
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
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
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    event = req.payload.get("event", {})

    if event.get("type") != "message":
        return

    bot_id = event.get("bot_id")
    if bot_id and bot_id == slack_bot_user_id:
        return

    subtype = event.get("subtype")
    if bot_id or (subtype and subtype != "file_share"):
        logging.debug(f"[SKIP] bot_id={bot_id} subtype={subtype} ch={event.get('channel')}")
        return

    channel_id = event.get("channel")
    ts = event.get("ts")
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
    try:
        channel_id, slack_ts = event.get("channel"), event.get("ts")
        
        with config_lock:
            if channel_id not in slack_to_whatsapp_map:
                logging.warning(f"Thread exiting: Channel {channel_id} not in mapping.")
                return
            mapping = slack_to_whatsapp_map[channel_id]

        # Two-way blocking: check paused status
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

        whatsapp_chat_id = mapping["whatsapp_chat_id"] or mapping.get("lid")
        client_name = mapping["client_name"]
        text_caption = event.get("text", "")

        # dev prefix: post to Slack only, don't forward to WhatsApp
        if text_caption.strip().startswith('dev '):
            logging.info(f"[DEV] \"{text_caption[:60]}\" kept in Slack, not forwarded")
            return

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
                    with requests.Session() as s:
                        s.headers.update({"Authorization": f"Bearer {bot_token}"})
                        file_response = s.get(file_url, timeout=30)
                    
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

            # 2. Lookup WhatsApp IDs for names using Google Sheet
            name_to_id = {}
            if names:
                try:
                    from g_sheets_client import get_client_mappings
                    client_mappings = get_client_mappings("WhatsApp")
                    name_to_id = {c.get("client_name"): sanitize_id(c.get("external_id")) for c in client_mappings if c.get("client_name") and c.get("external_id")}
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
            web_client = WebClient(token=bot_token)
            if response and response.get("success"):
                data = response.get("data", {})
                slack_to_whatsapp_msg_map.set(slack_ts, data.get("messageId"))
                logging.info(f"Forwarded Slack text message to WhatsApp user '{client_name}'")
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

            
# --- Service Management ---

def run_refresh_server():
    app = Flask(__name__)
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    @app.route('/refresh', methods=['POST'])
    def refresh_endpoint():
        threading.Thread(target=reload_config).start()
        return "Refresh signal received.", 200
    logging.info(f"WhatsApp refresh server listening on port {WHATSAPP_REFRESH_PORT}")
    app.run(host='0.0.0.0', port=int(WHATSAPP_REFRESH_PORT))


# --- Flask endpoint for WhatsApp→Slack edit sync ---
from flask import request

@app.route('/whatsapp-edit', methods=['POST'])
def whatsapp_edit_endpoint():
    data = request.get_json(force=True)
    message_id = data.get('messageId')
    new_text = data.get('newText')
    if not message_id or not new_text:
        return jsonify({'success': False, 'error': 'Missing messageId or newText'}), 400
    # Find the Slack message TS for this WhatsApp message
    slack_ts = None
    slack_channel = None
    with config_lock:
        for ts, wa_id in list(slack_to_whatsapp_msg_map.map.items()):
            if wa_id == message_id:
                slack_ts = ts
                for channel, mapping in slack_to_whatsapp_map.items():
                    if mapping and ts in slack_to_whatsapp_msg_map.map:
                        slack_channel = channel
                        break
                break
    if not slack_ts or not slack_channel:
        logging.warning(f"No Slack mapping found for WhatsApp message {message_id}")
        return jsonify({'success': False, 'error': 'No Slack mapping found'}), 404
    try:
        web_client = WebClient(token=SLACK_BOT_TOKEN)
        web_client.chat_update(channel=slack_channel, ts=slack_ts, text=new_text)
        logging.info(f"✅ Updated Slack message {slack_ts} with new WhatsApp text.")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Failed to update Slack message: {e}")
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
        global slack_bot_user_id
        slack_bot_user_id = auth_test["user_id"]
        logging.info(f"Slack bot user ID: {slack_bot_user_id}")
    except Exception as e:
        logging.warning(f"Could not fetch bot user ID: {e}")
    socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)
    thread_pool.submit(poller_with_restart, web_client)
    thread_pool.submit(periodic_outbox_worker, web_client)
    thread_pool.submit(run_refresh_server)
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_enhance_command_socket_mode(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_delete_event(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_message(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_modal_submission(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_edit_event(client, req, web_client)
    )
    logging.info("Connecting to Slack and entering listener loop...")
    socket_client.connect()
    
    while not stop_event.is_set():
        time.sleep(1)

if __name__ == "__main__":
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