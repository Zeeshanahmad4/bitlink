# main.py - FINAL VERSION (with MIME type and file corruption fix)

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
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
DISCORD_SUPER_PROPERTIES = os.getenv("DISCORD_SUPER_PROPERTIES")
DISCORD_COOKIE = os.getenv("DISCORD_COOKIE")
DISCORD_USER_AGENT = os.getenv("DISCORD_USER_AGENT")

CLIENT_MAPPINGS_STR = os.getenv("CLIENT_MAPPINGS", "[]")
CLIENT_MAPPINGS = json.loads(CLIENT_MAPPINGS_STR)
discord_to_slack_map = {
    item["discord_user_id"]: item
    for item in CLIENT_MAPPINGS
}
slack_to_discord_map = {
    item["slack_channel_id"]: item
    for item in CLIENT_MAPPINGS
}

slack_web_client = WebClient(token=SLACK_BOT_TOKEN)
slack_socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN,
                                       web_client=slack_web_client)
discord_client = discord.Client(self_bot=True)


# --- 2. THE MASTER send_dm FUNCTION (HANDLES TEXT & FILES) ---
async def send_dm(target_user_id, content, files=None):
    try:
        # API Call 1: Create or Get the DM Channel
        dm_creation_url = "https://discord.com/api/v9/users/@me/channels"
        headers = {
            "Authorization": DISCORD_TOKEN,
            "Content-Type": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
            "Cookie": DISCORD_COOKIE,
            "X-Super-Properties": DISCORD_SUPER_PROPERTIES
        }
        dm_payload = {"recipients": [str(target_user_id)]}
        dm_channel_id = None

        async with aiohttp.ClientSession(headers=headers) as session:
            print("Attempting to get DM channel via manual API call...")
            async with session.post(dm_creation_url,
                                    json=dm_payload) as response:
                if 200 <= response.status < 300:
                    response_json = await response.json()
                    dm_channel_id = response_json.get("id")
                    print(f"Successfully got DM channel ID: {dm_channel_id}")
                else:
                    response_text = await response.text()
                    print(
                        f"CRITICAL: Failed to get DM channel ID. Status: {response.status}, Response: {response_text}"
                    )
                    return

        if not dm_channel_id:
            return

        # API Call 2: Send the Message (with or without files)
        message_url = f"https://discord.com/api/v9/channels/{dm_channel_id}/messages"

        if files:
            print("Constructing multipart request for file upload...")
            data = aiohttp.FormData()
            for i, file in enumerate(files):
                # --- THIS IS THE FINAL FIX ---
                # We pass the raw bytes from file["data"] directly, removing the io.BytesIO() wrapper.
                data.add_field(f'files[{i}]',
                               file["data"],
                               filename=file["filename"],
                               content_type=file.get(
                                   "mime_type", "application/octet-stream"))
            payload = {
                "content": content,
                "tts": False,
                "nonce": str(int(time.time() * 1000))
            }
            data.add_field('payload_json',
                           json.dumps(payload),
                           content_type='application/json')

            del headers["Content-Type"]

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(message_url, data=data) as response:
                    if 200 <= response.status < 300:
                        print(
                            f"SUCCESS: Successfully sent DM with file(s) to {target_user_id}"
                        )
                    else:
                        response_text = await response.text()
                        print(
                            f"API CALL FAILED (File Upload). Status: {response.status}, Response: {response_text}"
                        )
        else:
            print("Constructing JSON request for text-only message...")
            payload = {
                "content": content,
                "tts": False,
                "nonce": str(int(time.time() * 1000))
            }

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(message_url, json=payload) as response:
                    if 200 <= response.status < 300:
                        print(
                            f"SUCCESS: Successfully sent text DM to {target_user_id}"
                        )
                    else:
                        response_text = await response.text()
                        print(
                            f"API CALL FAILED (Text). Status: {response.status}, Response: {response_text}"
                        )
    except Exception as e:
        print(f"An exception occurred in send_dm function: {e}")


# --- 3. DISCORD EVENT HANDLER (Discord -> Slack) ---
@discord_client.event
async def on_message(message: discord.Message):
    if not isinstance(
            message.channel,
            discord.DMChannel) or message.author.id == discord_client.user.id:
        return
    sender_id = str(message.author.id)
    if sender_id not in discord_to_slack_map:
        return

    client_info = discord_to_slack_map[sender_id]
    client_name = client_info["client_name"]
    target_slack_channel = client_info["slack_channel_id"]

    print(
        f"Received DM from '{client_name}'. Forwarding to Slack channel {target_slack_channel}..."
    )
    try:
        if message.content:
            slack_web_client.chat_postMessage(
                channel=target_slack_channel,
                text=message.content,
                username=f"{client_name} (via Discord)",
                icon_emoji=":desktop_computer:")
        if message.attachments:
            print(
                f"Found {len(message.attachments)} attachment(s). Downloading and re-uploading..."
            )
            for attachment in message.attachments:
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            file_data = await resp.read()
                            print(
                                f"Successfully downloaded {attachment.filename}. Now uploading to Slack..."
                            )
                            slack_web_client.files_upload_v2(
                                channel=target_slack_channel,
                                file=file_data,
                                filename=attachment.filename,
                                initial_comment=f"File from {client_name}:",
                                username=f"{client_name} (via Discord)",
                                icon_emoji=":desktop_computer:")
                        else:
                            print(
                                f"Failed to download attachment from Discord URL: {attachment.url}"
                            )
    except Exception as e:
        print(f"Error forwarding Discord -> Slack: {e}")


# --- 4. SLACK EVENT HANDLER (Slack -> Discord) ---
def handle_slack_message(client: SocketModeClient, req: SocketModeRequest):
    if req.type != "events_api":
        return
    event = req.payload.get("event", {})
    if event.get("type") != "message" or event.get(
            "bot_id") or "user" not in event:
        return
    channel_id = event.get("channel")
    if channel_id not in slack_to_discord_map:
        return

    client_info = slack_to_discord_map[channel_id]
    target_discord_user_id = int(client_info["discord_user_id"])
    message_text = event.get("text", "")
    print(
        f"Received message from Slack channel {channel_id}. Forwarding to Discord user {target_discord_user_id}..."
    )

    async def process_and_send():
        files_to_send = []
        if "files" in event:
            print(f"Found {len(event['files'])} file(s) in Slack message.")
            for file_info in event["files"]:
                url = file_info.get("url_private_download")
                if url:
                    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
                    async with aiohttp.ClientSession(
                            headers=headers) as session:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                file_data = await resp.read()
                                files_to_send.append({
                                    "data":
                                    file_data,
                                    "filename":
                                    file_info.get("name", "file.dat"),
                                    "mime_type":
                                    file_info.get("mimetype",
                                                  "application/octet-stream")
                                })
                                print(
                                    f"Successfully downloaded file: {file_info.get('name')}"
                                )
                            else:
                                print(f"Failed to download Slack file: {url}")

        await send_dm(target_discord_user_id, message_text, files_to_send)

    asyncio.run_coroutine_threadsafe(process_and_send(), discord_client.loop)
    client.send_socket_mode_response(
        SocketModeResponse(envelope_id=req.envelope_id))


slack_socket_client.socket_mode_request_listeners.append(handle_slack_message)


# --- 5. MAIN APPLICATION RUNNER ---
async def main():
    print("Starting integration bridge...")
    try:
        slack_socket_client.connect()
        print("Slack SocketMode client connected.")
        print("Connecting Discord client...")
        await discord_client.start(DISCORD_TOKEN)
    except Exception as e:
        print(f"An error occurred during startup: {e}")
        if slack_socket_client.is_connected():
            slack_socket_client.close()


if __name__ == "__main__":
    if not all([DISCORD_TOKEN, SLACK_BOT_TOKEN, SLACK_APP_TOKEN]):
        print(
            "FATAL ERROR: One or more required tokens are missing in Secrets.")
        exit(1)
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Shutting down the bridge.")
