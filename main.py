# main.py

import os
import io
import json
import asyncio
import aiohttp
import discord
from dotenv import load_dotenv
from slack_sdk.web import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

# --- 1. INITIAL SETUP & CONFIGURATION ---

# Load environment variables. In Replit, this is handled by the "Secrets" tool.
load_dotenv()

# Load credentials from Secrets
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

# Load and parse client mappings from Secrets
CLIENT_MAPPINGS_STR = os.getenv("CLIENT_MAPPINGS", "[]")
CLIENT_MAPPINGS = json.loads(CLIENT_MAPPINGS_STR)

# Create mapping dictionaries for quick lookups
discord_to_slack_map = {item["discord_user_id"]: item for item in CLIENT_MAPPINGS}
slack_to_discord_map = {item["slack_channel_id"]: item for item in CLIENT_MAPPINGS}

# Initialize Slack clients
slack_web_client = WebClient(token=SLACK_BOT_TOKEN)
slack_socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=slack_web_client)

# Initialize Discord client for self-botting
# Note: 'self_bot=True' is against Discord's ToS and can get the account banned.
discord_client = discord.Client(self_bot=True)

# --- 2. DISCORD EVENT HANDLER (Receives DMs from Clients) ---

@discord_client.event
async def on_message(message: discord.Message):
    if not isinstance(message.channel, discord.DMChannel) or message.author.id == discord_client.user.id:
        return

    sender_id = str(message.author.id)
    if sender_id not in discord_to_slack_map:
        return

    client_info = discord_to_slack_map[sender_id]
    client_name = client_info["client_name"]
    target_slack_channel = client_info["slack_channel_id"]

    print(f"Received DM from '{client_name}'. Forwarding to Slack channel {target_slack_channel}...")

    try:
        # Forward text content
        if message.content:
            slack_web_client.chat_postMessage(
                channel=target_slack_channel,
                text=message.content,
                username=f"{client_name} (via Discord)",
                icon_emoji=":desktop_computer:"
            )
        # Forward attachments
        if message.attachments:
            for attachment in message.attachments:
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            file_data = await resp.read()
                            slack_web_client.files_upload_v2(
                                channel=target_slack_channel,
                                file=file_data,
                                filename=attachment.filename,
                                initial_comment=f"File from {client_name}:",
                                username=f"{client_name} (via Discord)",
                                icon_emoji=":desktop_computer:"
                            )
    except Exception as e:
        print(f"Error forwarding Discord -> Slack: {e}")

# --- 3. SLACK EVENT HANDLER (Receives messages from Developers) ---

def handle_slack_message(client: SocketModeClient, req: SocketModeRequest):
    print(f"Received Slack event: {req.type}")
    
    if req.type != "events_api":
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return
    
    event = req.payload.get("event", {})
    print(f"Event details: {event}")
    
    # Skip bot messages and messages without a user
    if event.get("type") != "message" or event.get("bot_id") or "user" not in event:
        print(f"Skipping event - type: {event.get('type')}, bot_id: {event.get('bot_id')}, has_user: {'user' in event}")
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    channel_id = event.get("channel")
    if channel_id not in slack_to_discord_map:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    client_info = slack_to_discord_map[channel_id]
    target_discord_user_id = int(client_info["discord_user_id"])
    message_text = event.get("text", "")

    print(f"Received message from Slack channel {channel_id}. Forwarding to Discord user {target_discord_user_id}...")

    async def send_dm():
        try:
            # Get the target Discord user object
            target_user = await discord_client.fetch_user(target_discord_user_id)
            if not target_user:
                print(f"Could not find Discord user with ID: {target_discord_user_id}")
                return
            
            # Ensure a DM channel exists with the user
            dm_channel = await target_user.create_dm()

            # Send the message using a manual HTTP request to look more human
            url = f"https://discord.com/api/v9/channels/{dm_channel.id}/messages"
            
            headers = {
                "Authorization": DISCORD_TOKEN,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            
            payload = {
                "content": message_text,
                "tts": False
            }

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, json=payload) as response:
                    if response.status >= 200 and response.status < 300:
                        print(f"Successfully sent DM to {target_discord_user_id}")
                    else:
                        # Print the exact error from Discord's server
                        response_text = await response.text()
                        print(f"Error sending DM via HTTP. Status: {response.status}, Response: {response_text}")

            # Note: We are not handling file forwarding in this new manual method yet.
            # Let's get text working first.

        except Exception as e:
            print(f"An exception occurred in send_dm function: {e}")

    asyncio.run_coroutine_threadsafe(send_dm(), discord_client.loop)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

slack_socket_client.socket_mode_request_listeners.append(handle_slack_message)

# --- 4. MAIN APPLICATION RUNNER ---

async def main():
    print("Starting integration bridge...")
    try:
        slack_socket_client.connect()
        print("Slack SocketMode client connected.")
        print("Connecting Discord client...")
        if DISCORD_TOKEN is None:
            raise ValueError("DISCORD_TOKEN is not set")
        await discord_client.start(DISCORD_TOKEN)
    except Exception as e:
        print(f"An error occurred during startup: {e}")
        # Add a more robust shutdown sequence if needed
        if slack_socket_client.is_connected():
            slack_socket_client.close()

if __name__ == "__main__":
    # A simple check to ensure tokens are set
    if not all([DISCORD_TOKEN, SLACK_BOT_TOKEN, SLACK_APP_TOKEN]):
        print("FATAL ERROR: One or more required tokens are missing in Secrets.")
        exit(1)
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Shutting down the bridge.")