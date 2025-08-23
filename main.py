# main.py

import os
import io
import json
import asyncio
import aiohttp
import discord
import time 
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
DISCORD_SUPER_PROPERTIES = os.getenv("DISCORD_SUPER_PROPERTIES") 
DISCORD_COOKIE = os.getenv("DISCORD_COOKIE") # <-- ADD THIS
DISCORD_USER_AGENT = os.getenv("DISCORD_USER_AGENT") # <-- ADD THIS
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
        print(f"Attempting to send DM to Discord user {target_discord_user_id}")
        
        # Try to get DM channel ID via Discord API directly
        dm_channel_id = None
        
        try:
            # First try to create/get DM channel via API
            dm_url = f"https://discord.com/api/v9/users/@me/channels"
            headers = {
                "Authorization": DISCORD_TOKEN,
                "Content-Type": "application/json"
            }
            
            if DISCORD_USER_AGENT:
                headers["User-Agent"] = DISCORD_USER_AGENT
            if DISCORD_COOKIE:
                headers["Cookie"] = DISCORD_COOKIE
            if DISCORD_SUPER_PROPERTIES:
                headers["X-Super-Properties"] = DISCORD_SUPER_PROPERTIES
            
            dm_payload = {
                "recipient_id": str(target_discord_user_id)
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(dm_url, headers=headers, json=dm_payload) as dm_response:
                    if 200 <= dm_response.status < 300:
                        dm_data = await dm_response.json()
                        dm_channel_id = dm_data.get("id")
                        print(f"Successfully created/retrieved DM channel: {dm_channel_id}")
                    else:
                        dm_error = await dm_response.text()
                        print(f"Failed to create DM channel. Status: {dm_response.status}, Error: {dm_error}")
                        return
            
            if not dm_channel_id:
                print("Could not get DM channel ID")
                return
            
            # Now send the message
            message_url = f"https://discord.com/api/v9/channels/{dm_channel_id}/messages"
            nonce = str(int(time.time() * 1000))
            
            message_payload = {
                "content": message_text,
                "tts": False,
                "nonce": nonce
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(message_url, headers=headers, json=message_payload) as response:
                    if 200 <= response.status < 300:
                        print(f"SUCCESS: Message sent to Discord user {target_discord_user_id}")
                    else:
                        response_text = await response.text()
                        print(f"Failed to send message. Status: {response.status}, Response: {response_text}")
                        
        except Exception as e:
            print(f"Exception in send_dm function: {e}")
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