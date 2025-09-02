# management_server.py - FINAL, INSTANT REFRESH VERSION

import os
import gspread
import shlex
import threading
import requests
import json
from flask import Flask, request, jsonify
from slack_sdk.signature import SignatureVerifier
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# --- NEW: Function to send the "refresh" signal to the running bridges ---
def send_refresh_signals():
    """Sends a POST request to the bridge servers to trigger a config reload."""
    # Read the port numbers from the .env file, with defaults
    whatsapp_port = os.getenv("WHATSAPP_REFRESH_PORT", 8001)
    discord_port = os.getenv("DISCORD_REFRESH_PORT", 8002)
    
    headers = {'Content-Type': 'application/json'}
    urls = [
        f"http://localhost:{whatsapp_port}/refresh",
        f"http://localhost:{discord_port}/refresh"
    ]
    
    print("Sending refresh signals to bridge services...")
    for url in urls:
        try:
            # We don't need to wait for a response, just fire and forget.
            # A short timeout prevents this from hanging if a service is offline.
            requests.post(url, headers=headers, timeout=2)
            print(f"Successfully sent refresh signal to {url}")
        except requests.exceptions.RequestException:
            # This is not an error. It just means that bridge is not currently running.
            print(f"Could not send refresh signal to {url}. Bridge may be offline (this is normal).")

# --- MODIFIED: The background task now sends the refresh signal ---
def process_and_respond(response_url, command_text):
    """
    Writes to Google Sheets, sends the refresh signal, and responds to Slack.
    """
    try:
        parts = shlex.split(command_text)
        if len(parts) != 4:
            raise ValueError("Invalid format. Use: /add-client [platform] \"[Client Name]\" [external_id] [slack_channel_id]")
        
        platform, client_name, external_id, slack_channel_id = parts

        # 1. Write to Google Sheets
        gc = gspread.service_account(filename='credentials/service_account.json')
        spreadsheet = gc.open("BitLink Client Mappings")
        worksheet = spreadsheet.sheet1
        new_row = [client_name, platform, external_id, slack_channel_id]
        worksheet.append_row(new_row)
        
        # --- THIS IS THE NEW PART ---
        # 2. Tell the running bridges to reload their configuration
        send_refresh_signals()
        
        # 3. Prepare the success message for Slack
        payload = {
            "response_type": "in_channel",
            "text": f"✅ New client mapping added successfully! The bridges will refresh momentarily.",
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
        print(f"Error in background thread: {e}")
        payload = {"response_type": "ephemeral", "text": f"❌ An error occurred: {e}"}

    # 4. Send the final result back to Slack
    requests.post(response_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'})


# --- The rest of the file is unchanged ---
@app.route('/slack/commands/add-client', methods=['POST'])
def add_client_command():
    verifier = SignatureVerifier(os.environ.get("SLACK_SIGNING_SECRET"))
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return "Invalid request signature", 403

    thread = threading.Thread(
        target=process_and_respond,
        args=(request.form['response_url'], request.form['text'])
    )
    thread.start()

    return jsonify({
        "response_type": "ephemeral",
        "text": "Got it! Adding client and signaling bridges to refresh..."
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)