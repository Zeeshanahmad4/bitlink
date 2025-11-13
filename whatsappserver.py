# main_whatsapp.py - WHATSAPP BRIDGE ONLY (ENHANCE REMOVED)
import os
import time
import requests
import sys
import logging
import base64
import threading
import subprocess
import string
from flask import request, jsonify 
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.errors import SlackApiError
from flask import Flask
from collections import deque

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
NODE_API_URL = os.getenv("NODE_API_URL", "http://127.0.0.1:3001")
WHATSAPP_REFRESH_PORT = os.getenv("WHATSAPP_REFRESH_PORT", 8001)
web_client = WebClient(token=SLACK_BOT_TOKEN)

# --- Global State Variables ---
config_lock = threading.Lock()
stop_event = threading.Event()
active_threads = []
processed_slack_events = deque(maxlen=500)
processed_whatsapp_events = deque(maxlen=500)
whatsapp_to_slack_map = {}
slack_to_whatsapp_map = {}
slack_to_whatsapp_msg_map = {}

# --- Helper Functions ---
import re

# --- Slack Link Cleaner ---
def clean_slack_links(text):
    """Replace any Slack link format <scheme:...|display> with display text only."""
    # Handles http, https, mailto, and any other scheme
    return re.sub(r'<[^|>]+\|([^>]+)>', r'\1', text)

def sanitize_id(external_id):
    if not external_id: return None
    printable_chars = set(string.printable)
    clean_id = "".join(filter(lambda char: char in printable_chars, str(external_id)))
    return clean_id.strip()

def get_whatsapp_messages():
    try:
        response = requests.get(f"{NODE_API_URL}/get-messages")
        if response.status_code == 200: return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error connecting to WhatsApp service: {e}")
    return []

def send_whatsapp_message(chat_id, message, media=None):
    try:
        payload = {"chatId": chat_id, "message": message, "media": media}
        response = requests.post(f"{NODE_API_URL}/send-message", json=payload)
        if response and response.status_code == 200:
            return response.json()
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending message via WhatsApp service: {e}")
    return None

def delete_whatsapp_message(message_id):
    logging.info(f"Attempting to delete WhatsApp message: {message_id}")
    try:
        payload = {"messageId": message_id}
        response = requests.post(f"{NODE_API_URL}/delete-message", json=payload)
        logging.info(f"Delete request sent. Status code: {response.status_code}, Response: {response.text}")
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Error deleting WhatsApp message: {e}")
    return False

# --- Core Logic ---

def reload_config():
    global whatsapp_to_slack_map, slack_to_whatsapp_map
    logging.info("(WhatsApp Bridge) Refresh signal received! Reloading config...")
    client_mappings_raw = get_client_mappings("WhatsApp")
    if client_mappings_raw:
        new_mappings = [{
            "client_name": c.get("client_name"), 
            "whatsapp_chat_id": sanitize_id(c.get("external_id")), 
            "slack_channel_id": c.get("slack_channel_id"),
            "paused": c.get("paused", False)
        } for c in client_mappings_raw]
        with config_lock:
            whatsapp_to_slack_map.clear()
            slack_to_whatsapp_map.clear()
            whatsapp_to_slack_map.update({item["whatsapp_chat_id"]: item for item in new_mappings if item.get("whatsapp_chat_id")})
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
        with config_lock:
            current_clients = dict(whatsapp_to_slack_map)
        new_messages = get_whatsapp_messages()
        for msg in new_messages:
            event_id = msg.get('messageId')
            chat_id = msg.get('chatId')
            if not event_id or not chat_id:
                continue

            if event_id not in processed_whatsapp_events and chat_id in current_clients:
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

                # --- This is the modified section ---
                if has_media:
                    filename = msg['media'].get('filename', 'file.bin')
                    message_text += f"Sent a file: `{filename}`\n"
                    if content.strip():
                        message_text += content
                else:
                    message_text += content
                # ------------------------------------

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
                        # This part remains for text-only messages
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
    logging.info("WhatsApp polling worker is shutting down.")

def handle_slack_edit_event(client: SocketModeClient, req, web_client: WebClient):
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    event = req.payload.get("event", {})

    if event.get("type") == "message" and event.get("subtype") == "message_changed":
        new_msg = event.get("message", {})
        slack_ts = new_msg.get("ts")
        channel_id = event.get("channel")
        new_text = new_msg.get("text", "")

        if not slack_ts or not new_text:
            return

        if slack_ts in slack_to_whatsapp_msg_map:
            whatsapp_msg_id = slack_to_whatsapp_msg_map[slack_ts]
            logging.info(f"✏️ Slack message edited → editing WhatsApp message {whatsapp_msg_id}")

            success = edit_whatsapp_message(whatsapp_msg_id, new_text)
            if success:
                logging.info(f"✅ WhatsApp message {whatsapp_msg_id} updated successfully.")
            else:
                logging.error(f"❌ Failed to update WhatsApp message {whatsapp_msg_id}.")
        else:
            logging.info(f"ℹ️ No WhatsApp mapping found for edited Slack message {slack_ts}.")

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
        if deleted_ts in slack_to_whatsapp_msg_map:
            whatsapp_msg_id = slack_to_whatsapp_msg_map[deleted_ts]
            logging.info(f"🗑️ Attempting to delete corresponding WhatsApp message: {whatsapp_msg_id}")
            
            # Delete the WhatsApp message
            if delete_whatsapp_message(whatsapp_msg_id):
                logging.info(f"✅ Successfully deleted WhatsApp message: {whatsapp_msg_id}")
                # Remove from mapping
                del slack_to_whatsapp_msg_map[deleted_ts]
            else:
                logging.error(f"❌ Failed to delete WhatsApp message: {whatsapp_msg_id}")
        else:
            logging.info(f"ℹ️ No WhatsApp message mapping found for Slack TS: {deleted_ts}")
    else:
        logging.debug(f"ℹ️ Ignoring non-delete event: {event.get('type')}, {event.get('subtype')}")

def handle_slack_message(client: SocketModeClient, req, web_client: WebClient):
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    event = req.payload.get("event", {})
    event_id = (event.get("channel"), event.get("ts"))
   
    # MODIFIED FILTER - Allow messages from your specific bot
    if (event.get("type") != "message" or 
        (event.get("bot_id") and event.get("bot_id") != "B09BJQ8HBNZ") or  # ← Your Bot User ID
        (event.get("subtype") and event.get("subtype") != "file_share")):
        return
    
    channel_id, ts = event.get("channel"), event.get("ts")
    event_id = (channel_id, ts)
    
    # --- This is the corrected, definitive de-duplication logic ---
    with config_lock:
        is_managed_channel = channel_id in slack_to_whatsapp_map
        is_new_event = event_id not in processed_slack_events
        
        # Only proceed if the channel is managed AND the event is new.
        if is_managed_channel and is_new_event:
            # "Claim" the event immediately.
            processed_slack_events.append(event_id)
            
            # <<< The thread is now started safely inside the condition >>>
            thread = threading.Thread(target=process_slack_to_whatsapp, args=(event, web_client.token))
            active_threads.append(thread)
            thread.start()

def process_slack_to_whatsapp(event, bot_token):
    global active_threads
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

        whatsapp_chat_id = mapping["whatsapp_chat_id"]
        client_name = mapping["client_name"]
        text_caption = event.get("text", "")

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
                        # [UNCHANGED] Logic to map message ID of the first file
                        if is_first_file:
                            slack_to_whatsapp_msg_map[slack_ts] = response.get("messageId")
                        logging.info(f"Forwarded file '{file_name}' to WhatsApp user '{client_name}'")
                    else:
                        # [NEW] Better user feedback on send failure
                        error_from_service = response.get('error', 'unknown reason') if response else 'connection failed'
                        logging.error(f"Failed to forward file '{file_name}' via Node.js service: {error_from_service}")
                        web_client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=slack_ts,
                            text=f"🔴 Failed to send `{file_name}` to WhatsApp. Reason: {error_from_service}"
                        )
                    
                    is_first_file = False
                    time.sleep(1)

                except Exception as e:
                    logging.error(f"An error occurred while processing a file for '{client_name}': {e}", exc_info=True)

        # --- Case 2: The message is text-only (100% UNCHANGED) ---
        else:
            # Clean Slack link formatting before forwarding
            cleaned_text = clean_slack_links(text_caption)
            response = send_whatsapp_message(whatsapp_chat_id, cleaned_text, None)
            web_client = WebClient(token=bot_token)
            if response and response.get("success"):
                slack_to_whatsapp_msg_map[slack_ts] = response.get("messageId")
                logging.info(f"Forwarded Slack text message to WhatsApp user '{client_name}'")
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response from WhatsApp service.'
                logging.error(f"Failed to forward Slack text message to WhatsApp user '{client_name}'. Reason: {error_msg}")
                try:
                    web_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=slack_ts,
                        text=f"🔴 Failed to forward this message to WhatsApp. Reason: {error_msg}",
                        username="Bitlink Bridge Info"
                    )
                except Exception as e:
                    logging.error(f"Failed to notify Slack thread of WhatsApp send error: {e}")

    except Exception as e:
        logging.error(f"A critical error occurred in process_slack_to_whatsapp: {e}", exc_info=True)
    finally:
        if threading.current_thread() in active_threads:
            active_threads.remove(threading.current_thread())
def edit_whatsapp_message(message_id, new_text):
    logging.info(f"Attempting to edit WhatsApp message: {message_id}")
    try:
        payload = {"messageId": message_id, "newText": new_text}
        response = requests.post(f"{NODE_API_URL}/edit-message", json=payload)
        if response.status_code == 200:
            return True
        else:
            logging.error(f"Edit failed. Status {response.status_code}: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error editing WhatsApp message: {e}")
    return False

def debug_all_events(client: SocketModeClient, req):
    """Debug all incoming events"""
    logging.debug(f"DEBUG EVENT: type={req.type}, envelope_id={req.envelope_id}")
    if hasattr(req, 'payload'):
        payload = req.payload
        if 'command' in payload:
            logging.info(f"COMMAND: {payload.get('command')} - {payload.get('text')}")          

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
    @app.route('/whatsapp-edit', methods=['POST'])
    def whatsapp_edit_endpoint():
        data = request.get_json()
        message_id = data.get("messageId")
        new_text = data.get("newText")

        logging.info(f"✏️ WhatsApp edit received: {message_id} → {new_text}")

        for slack_ts, wa_id in slack_to_whatsapp_msg_map.items():
            if wa_id == message_id:
                try:
                    mapping = next((m for m in slack_to_whatsapp_map.values() if m["slack_channel_id"]), None)
                    if not mapping:
                        logging.warning("No channel mapping found for edited WhatsApp message.")
                        return "No mapping", 404

                    slack_channel = mapping["slack_channel_id"]
                    web_client.chat_update(channel=slack_channel, ts=slack_ts, text=new_text)
                    logging.info(f"✅ Updated Slack message {slack_ts} with new WhatsApp text.")
                    return "Edit synced.", 200
                except Exception as e:
                    logging.error(f"❌ Failed to update Slack message: {e}")
                    return "Error", 500

        return "No matching message", 404

    logging.info(f"WhatsApp refresh server listening on port {WHATSAPP_REFRESH_PORT}")
    app.run(host='0.0.0.0', port=int(WHATSAPP_REFRESH_PORT))
def main():
    reload_config() 
    
    socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)
    whatsapp_poller_thread = threading.Thread(target=poll_whatsapp_and_forward, args=(web_client,))
    whatsapp_poller_thread.daemon = True
    whatsapp_poller_thread.start()
    refresh_server_thread = threading.Thread(target=run_refresh_server)
    refresh_server_thread.daemon = True
    refresh_server_thread.start()
    
    # REGISTER ONLY WHATSAPP-RELATED LISTENERS (ENHANCE REMOVED)
    socket_client.socket_mode_request_listeners.append(debug_all_events)
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_delete_event(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_message(client, req, web_client)
    )
    socket_client.socket_mode_request_listeners.append(
    lambda client, req: handle_slack_edit_event(client, req, web_client)
)

    logging.info("Connecting to Slack and entering listener loop...")
    socket_client.connect()
    
    while not stop_event.is_set():
        time.sleep(1)

if __name__ == "__main__":
    if not all([SLACK_APP_TOKEN, SLACK_BOT_TOKEN]):
        sys.exit("FATAL: SLACK_APP_TOKEN or SLACK_BOT_TOKEN is missing.")
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown signal received. Waiting for threads to finish...")
        stop_event.set()
        for thread in active_threads:
            thread.join()
        logging.info("All processing threads have finished. Bridge shut down.")