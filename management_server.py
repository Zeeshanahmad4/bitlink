# management_server.py — Socket Mode version

import os
import db_store
import shlex
import threading
import time
import sys
import logging
import requests
import json
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

# --- Send refresh signal to running bridges ---
def send_refresh_signals():
    whatsapp_port = os.getenv("WHATSAPP_REFRESH_PORT", 8101)
    discord_port = os.getenv("DISCORD_REFRESH_PORT", 8102)
    telegram_port = os.getenv("TELEGRAM_REFRESH_PORT", 8003)

    headers = {'Content-Type': 'application/json'}
    urls = [
        f"http://localhost:{whatsapp_port}/refresh",
        f"http://localhost:{discord_port}/refresh",
        f"http://localhost:{telegram_port}/refresh"
    ]

    logging.info("Sending refresh signals to bridge services...")
    for url in urls:
        try:
            requests.post(url, headers=headers, timeout=2)
            logging.info(f"Refresh sent to {url}")
        except requests.exceptions.RequestException:
            logging.info(f"Could not reach {url} (bridge may be offline)")

# --- /add-client command ---
def process_and_respond(response_url, command_text):
    try:
        parts = shlex.split(command_text)
        if len(parts) != 4:
            raise ValueError("Invalid format. Use: /add-client [platform] \"[Client Name]\" [external_id] [slack_channel_id]")

        platform, client_name, external_id, slack_channel_id = parts

        db_store.add_client_mapping(platform, client_name, external_id, slack_channel_id)

        send_refresh_signals()

        payload = {
            "response_type": "in_channel",
            "text": "✅ New client mapping added successfully! The bridges will refresh momentarily.",
            "attachments": [{
                "color": "#36a64f",
                "fields": [
                    {"title": "Client Name", "value": client_name, "short": True},
                    {"title": "Platform", "value": platform, "short": True},
                    {"title": "External ID", "value": external_id, "short": True},
                    {"title": "Slack Channel ID", "value": slack_channel_id, "short": True}
                ]
            }]
        }
    except Exception as e:
        logging.error(f"Error in add-client: {e}")
        payload = {"response_type": "ephemeral", "text": f"❌ An error occurred: {e}"}

    requests.post(response_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'})

# --- /pause-channel command ---
def process_pause(response_url, channel_id):
    try:
        if db_store.set_client_paused(channel_id, True):
            send_refresh_signals()
            payload = {"response_type": "ephemeral", "text": f"✅ Channel {channel_id} paused."}
        else:
            payload = {"response_type": "ephemeral", "text": f"❌ Channel ID {channel_id} not found."}
    except Exception as e:
        logging.error(f"Error in pause-channel: {e}")
        payload = {"response_type": "ephemeral", "text": f"❌ Error: {e}"}
        requests.post(response_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'})

# --- /resume-channel command ---
def process_resume(response_url, channel_id):
    try:
        if db_store.set_client_paused(channel_id, False):
            send_refresh_signals()
            payload = {"response_type": "ephemeral", "text": f"✅ Channel {channel_id} resumed."}
        else:
            payload = {"response_type": "ephemeral", "text": f"❌ Channel ID {channel_id} not found."}
    except Exception as e:
        logging.error(f"Error in resume-channel: {e}")
        payload = {"response_type": "ephemeral", "text": f"❌ Error: {e}"}
        requests.post(response_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'})

# --- Socket Mode command handlers ---
def handle_add_client(client, req):
    data = req.payload
    if data.get("command") != "/add-client":
        return
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    response_url = data.get("response_url")
    command_text = data.get("text")
    logging.info(f"Received /add-client: {command_text}")
    thread = threading.Thread(target=process_and_respond, args=(response_url, command_text))
    thread.start()

def handle_pause_channel(client, req):
    data = req.payload
    if data.get("command") != "/pause-channel":
        return
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    response_url = data.get("response_url")
    channel_id = data.get("text", "").strip()
    logging.info(f"Received /pause-channel: {channel_id}")
    thread = threading.Thread(target=process_pause, args=(response_url, channel_id))
    thread.start()

def handle_resume_channel(client, req):
    data = req.payload
    if data.get("command") != "/resume-channel":
        return
    client.send_socket_mode_response({"envelope_id": req.envelope_id})
    response_url = data.get("response_url")
    channel_id = data.get("text", "").strip()
    logging.info(f"Received /resume-channel: {channel_id}")
    thread = threading.Thread(target=process_resume, args=(response_url, channel_id))
    thread.start()

# --- Main ---
def main():
    web_client = WebClient(token=SLACK_BOT_TOKEN)
    socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)
    socket_client.socket_mode_request_listeners.append(handle_add_client)
    socket_client.socket_mode_request_listeners.append(handle_pause_channel)
    socket_client.socket_mode_request_listeners.append(handle_resume_channel)
    logging.info("Management server connecting via Socket Mode...")
    socket_client.connect()
    while True:
        time.sleep(1)

if __name__ == "__main__":
    if not all([SLACK_APP_TOKEN, SLACK_BOT_TOKEN]):
        sys.exit("FATAL: SLACK_APP_TOKEN or SLACK_BOT_TOKEN is missing.")
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Management server shut down.")
