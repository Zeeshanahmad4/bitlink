# main_whatsapp.py - YOUR ORIGINAL CODE + SIMPLE DELETION FUNCTIONALITY

import time
import requests
import os
import sys
import logging
import base64
import threading
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.errors import SlackApiError
from flask import Flask
from collections import deque

from g_sheets_client import get_client_mappings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

load_dotenv()
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
NODE_API_URL = os.getenv("NODE_API_URL", "http://localhost:3000")
WHATSAPP_REFRESH_PORT = os.getenv("WHATSAPP_REFRESH_PORT", 8001)

config_lock = threading.Lock()
stop_event = threading.Event()
active_threads = []
processed_slack_events = deque(maxlen=500)
processed_whatsapp_events = deque(maxlen=500)
whatsapp_to_slack_map = {}
slack_to_whatsapp_map = {}

# ⭐ ONLY NEW ADDITION: Simple message mapping for deletion
slack_to_whatsapp_msg_map = {}

def reload_config():
    """Fetches the latest mappings from Google Sheets and safely updates the global maps."""
    global whatsapp_to_slack_map, slack_to_whatsapp_map
    logging.info("(WhatsApp Bridge) Refresh signal received! Reloading config...")
    client_mappings_raw = get_client_mappings("WhatsApp")
    if client_mappings_raw:
        new_mappings = [{"client_name": c.get("client_name"), "whatsapp_chat_id": c.get("external_id"), "slack_channel_id": c.get("slack_channel_id")} for c in client_mappings_raw]
        with config_lock:
            whatsapp_to_slack_map.clear()
            slack_to_whatsapp_map.clear()
            whatsapp_to_slack_map.update({item["whatsapp_chat_id"]: item for item in new_mappings if item.get("whatsapp_chat_id")})
            slack_to_whatsapp_map.update({item["slack_channel_id"]: item for item in new_mappings if item.get("slack_channel_id")})
        logging.info(f"Configuration reloaded. Now tracking {len(whatsapp_to_slack_map)} clients.")
    return "Configuration reloaded.", 200

def run_refresh_server():
    """Runs a simple Flask server to listen for the /refresh webhook."""
    app = Flask(__name__)
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    @app.route('/refresh', methods=['POST'])
    def refresh_endpoint():
        threading.Thread(target=reload_config).start()
        return "Refresh signal received.", 200
    
    logging.info(f"WhatsApp refresh server listening on port {WHATSAPP_REFRESH_PORT}")
    app.run(port=int(WHATSAPP_REFRESH_PORT))

def main():
    reload_config() 
    web_client = WebClient(token=SLACK_BOT_TOKEN)
    socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)
    whatsapp_poller_thread = threading.Thread(target=poll_whatsapp_and_forward, args=(web_client,))
    whatsapp_poller_thread.daemon = True
    whatsapp_poller_thread.start()
    refresh_server_thread = threading.Thread(target=run_refresh_server)
    refresh_server_thread.daemon = True
    refresh_server_thread.start()
    
    socket_client.socket_mode_request_listeners.append(
        lambda client, req: handle_slack_message(client, req, web_client)
    )
    
    logging.info("Connecting to Slack and entering listener loop...")
    socket_client.connect()
    
    while not stop_event.is_set():
        time.sleep(1)

# --- Helper functions (YOUR ORIGINAL + MINIMAL ADDITIONS) ---
def get_whatsapp_messages():
    try:
        response = requests.get(f"{NODE_API_URL}/get-messages")
        if response.status_code == 200: return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error connecting to WhatsApp service: {e}")
    return []

# ⭐ MODIFIED: Now returns response to get messageId for deletion mapping
def send_whatsapp_message(chat_id, message, media=None):
    try:
        payload = {"chatId": chat_id, "message": message, "media": media}
        response = requests.post(f"{NODE_API_URL}/send-message", json=payload)
        if response.status_code == 200:
            return response.json()  # Return full response to get messageId
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending message via WhatsApp service: {e}")
    return None

# ⭐ NEW: Simple deletion function (no queues, direct call like your style)
def delete_whatsapp_message(message_id):
    try:
        payload = {"messageId": message_id}
        response = requests.post(f"{NODE_API_URL}/delete-message", json=payload)
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Error deleting WhatsApp message: {e}")
    return False

def poll_whatsapp_and_forward(web_client: WebClient):
    logging.info("WhatsApp polling worker has started.")
    while not stop_event.is_set():
        with config_lock:
            current_clients = dict(whatsapp_to_slack_map)
        new_messages = get_whatsapp_messages()
        for msg in new_messages:
            chat_id, ts = msg.get('chatId'), msg.get('timestamp')
            if not ts or not chat_id: continue
            event_id = (chat_id, ts)
            if event_id not in processed_whatsapp_events and chat_id in current_clients:
                processed_whatsapp_events.append(event_id)
                client_info = current_clients[chat_id]
                slack_channel, client_name = client_info["slack_channel_id"], client_info["client_name"]
                content = msg.get('body', '')
                quoted_body = msg.get('quotedBody')
                final_text = ""
                if quoted_body:
                    final_text += f"> {quoted_body}\n"
                final_text += f"*{client_name}:*\n{content}"
                try:
                    if msg.get('media') and msg['media'].get('data'):
                        file_content = base64.b64decode(msg['media']['data'])
                        web_client.files_upload_v2(channel=slack_channel, content=file_content, filename=msg['media'].get('filename', 'file.bin'), initial_comment=final_text)
                    else:
                        web_client.chat_postMessage(channel=slack_channel, text=final_text)
                    logging.info(f"Forwarded WhatsApp message from '{client_name}' to Slack")
                except SlackApiError as e:
                    logging.error(f"Slack API error forwarding from '{client_name}': {e.response['error']}")
        time.sleep(1)
    logging.info("WhatsApp polling worker is shutting down.")

# ⭐ MODIFIED: Added deletion detection (keeping your fast direct threading style)
def handle_slack_message(client: SocketModeClient, req, web_client: WebClient):
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    
    event = req.payload.get("event", {})
    event_type = event.get("type")
    
    # Handle message deletion (simple and fast)
    if event_type == "message" and event.get("subtype") == "message_deleted":
        deleted_ts = event.get("deleted_ts")
        if deleted_ts and deleted_ts in slack_to_whatsapp_msg_map:
            whatsapp_msg_id = slack_to_whatsapp_msg_map.pop(deleted_ts)
            logging.info(f"🗑️ Deleting WhatsApp message: {whatsapp_msg_id}")
            # Keep your style: direct threading, no queues
            threading.Thread(target=delete_whatsapp_message, args=(whatsapp_msg_id,), daemon=True).start()
        return
    
    # Regular message handling (YOUR ORIGINAL CODE)
    if event.get("type") != "message" or event.get("bot_id"): return
    channel_id, ts = event.get("channel"), event.get("ts")
    event_id = (channel_id, ts)
    with config_lock:
        is_managed_channel = channel_id in slack_to_whatsapp_map
    if is_managed_channel and event_id not in processed_slack_events:
        processed_slack_events.append(event_id)
        thread = threading.Thread(target=process_slack_to_whatsapp, args=(event, web_client.token))
        active_threads.append(thread)
        thread.start()

# ⭐ MODIFIED: Store message mapping for deletion (minimal change to your function)
def process_slack_to_whatsapp(event, bot_token):
    try:
        channel_id = event.get("channel")
        slack_ts = event.get("ts")  # ⭐ Get Slack timestamp for mapping
        
        with config_lock:
            if channel_id not in slack_to_whatsapp_map: return
            mapping = slack_to_whatsapp_map[channel_id]
        whatsapp_chat_id, client_name = mapping["whatsapp_chat_id"], mapping["client_name"]
        text = event.get("text", "")
        media_payload = None
        if "files" in event:
            file_info = event["files"][0]
            file_url = file_info.get("url_private_download")
            file_response = requests.get(file_url, headers={"Authorization": f"Bearer {bot_token}"})
            if file_response.status_code == 200:
                file_content_base64 = base64.b64encode(file_response.content).decode('utf-8')
                media_payload = {"mimetype": file_info.get("mimetype"), "filename": file_info.get("name"), "data": file_content_base64}
        
        # ⭐ MODIFIED: Get response to store mapping
        response = send_whatsapp_message(whatsapp_chat_id, text, media_payload)
        if response and response.get("success"):
            # ⭐ Store mapping for future deletion (simple dict, no complexity)
            whatsapp_msg_id = response.get("messageId")
            if whatsapp_msg_id:
                slack_to_whatsapp_msg_map[slack_ts] = whatsapp_msg_id
            logging.info(f"Forwarded Slack message to WhatsApp user '{client_name}'")
        else:
            logging.error(f"Failed to forward Slack message to WhatsApp user '{client_name}'")
    except Exception as e:
        logging.error(f"Unhandled exception in process_slack_to_whatsapp: {e}", exc_info=True)
    finally:
        active_threads.remove(threading.current_thread())

if __name__ == "__main__":
    if not SLACK_APP_TOKEN:
        sys.exit("FATAL: SLACK_APP_TOKEN is missing from .env file.")
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown signal received. Waiting for threads to finish...")
        stop_event.set()
        for thread in active_threads:
            thread.join()
        logging.info("All processing threads have finished. Bridge shut down.")