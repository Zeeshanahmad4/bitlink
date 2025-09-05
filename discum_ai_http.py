# main_discord.py - FINAL, INSTANT REFRESH VERSION

import asyncio
import aiohttp
import os
import sys
import logging
from dotenv import load_dotenv
from slack_sdk.web.async_client import AsyncWebClient
import discum
from aiohttp import web # <-- NEW IMPORT

from g_sheets_client import get_client_mappings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
DISCORD_USER_AGENT = os.getenv("DISCORD_USER_AGENT", "Mozilla/5.0")
# --- NEW: Port for this bridge's refresh server ---
DISCORD_REFRESH_PORT = int(os.getenv("DISCORD_REFRESH_PORT", 8002))

slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
bot = discum.Client(token=DISCORD_TOKEN, log=False)

MY_USER_ID, main_loop, aiohttp_session = None, None, None
discord_id_to_slack_map, slack_to_discord_map = {}, {}
slack_channel_state = {}

# --- NEW: The async function that gets called on-demand to refresh the config ---
async def reload_config():
    global discord_id_to_slack_map, slack_to_discord_map, slack_channel_state
    loop = asyncio.get_running_loop()
    logging.info("(Discord Bridge) Refresh signal received! Reloading config...")
    
    # Run the synchronous gspread function in a separate thread to avoid blocking asyncio
    client_mappings_raw = await loop.run_in_executor(None, get_client_mappings, "Discord")
    
    if client_mappings_raw:
        new_mappings = [{"client_name": c.get("client_name"), "discord_user_id": c.get("external_id"), "slack_channel_id": c.get("slack_channel_id")} for c in client_mappings_raw]
        new_discord_map = {item["discord_user_id"]: item for item in new_mappings if item.get("discord_user_id")}
        new_slack_map = {item["slack_channel_id"]: item for item in new_mappings if item.get("slack_channel_id")}

        # Check for newly added Slack channels to initialize their state
        for new_channel_id in new_slack_map:
            if new_channel_id not in slack_to_discord_map:
                logging.info(f"New client channel found: {new_channel_id}. Initializing state.")
                try:
                    response = await slack_client.conversations_history(channel=new_channel_id, limit=1)
                    if response.get("messages"):
                        slack_channel_state[new_channel_id] = response["messages"][0]['ts']
                except Exception as e:
                     logging.error(f"Could not initialize state for new channel {new_channel_id}: {e}")
        
        discord_id_to_slack_map = new_discord_map
        slack_to_discord_map = new_slack_map
        logging.info(f"(Discord Bridge) Configuration reloaded. Now tracking {len(discord_id_to_slack_map)} clients.")

# --- NEW: The aiohttp server and its endpoint ---
async def handle_refresh(request):
    """Endpoint handler that triggers the config reload as a background task."""
    asyncio.create_task(reload_config())
    return web.Response(text="Refresh signal received.")

async def run_refresh_server():
    """Runs the aiohttp server to listen for the refresh signal."""
    app = web.Application()
    app.add_routes([web.post('/refresh', handle_refresh)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', DISCORD_REFRESH_PORT)
    await site.start()
    logging.info(f"Discord refresh server listening on port {DISCORD_REFRESH_PORT}")
    # Keep the server running in the background indefinitely
    while True:
        await asyncio.sleep(3600)

async def main():
    global main_loop, aiohttp_session
    main_loop = asyncio.get_running_loop()
    
    # Perform the initial config load on startup
    await reload_config()

    async with aiohttp.ClientSession() as session:
        aiohttp_session = session
        
        # --- MODIFIED: Start all background tasks concurrently ---
        slack_polling_task = asyncio.create_task(poll_slack_and_forward())
        refresh_server_task = asyncio.create_task(run_refresh_server()) # <-- NEW
        
        logging.info("Starting Discum gateway in a separate thread...")
        discum_thread_task = main_loop.run_in_executor(None, discum_wrapper)

        # Run all tasks together. If one fails, the others will be cancelled.
        await asyncio.gather(slack_polling_task, refresh_server_task, discum_thread_task)

# --- All other Discord bridge functions (discum_wrapper, on_discord_message, etc.) remain unchanged ---
async def retry_async_request(func, max_retries=3, *args, **kwargs):
    for i in range(max_retries):
        try: return await func(*args, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning(f"Request failed: {e}. Retrying in {2**i} seconds...")
            await asyncio.sleep(2**i)
    logging.error(f"Request failed after {max_retries} retries. Giving up.")
    return None
async def forward_file_to_slack(message_obj, client_info):
    attachment = message_obj.attachments[0]
    logging.info(f"Downloading file '{attachment.filename}' from Discord...")
    async with aiohttp_session.get(attachment.url) as response:
        if response.status == 200:
            file_data = await response.read()
            await slack_client.files_upload_v2(channel=client_info["slack_channel_id"], content=file_data, filename=attachment.filename, initial_comment=f"*{client_info['client_name']}:*\n{message_obj.content}")
            logging.info("File forwarded to Slack successfully.")
        else: logging.error(f"Failed to download file from Discord. Status: {response.status}")
async def send_discord_dm_with_file(recipient_id, content, file_url, filename):
    async with aiohttp_session.get(file_url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}) as response:
        if response.status != 200:
            logging.error(f"Failed to download file from Slack. Status: {response.status}")
            return False
        file_data = await response.read()
    url = "https://discord.com/api/v9/users/@me/channels"
    payload = {"recipients": [str(recipient_id)]}
    headers = {"Authorization": DISCORD_TOKEN, "User-Agent": DISCORD_USER_AGENT}
    async with aiohttp_session.post(url, json=payload, headers=headers) as dm_response:
        if dm_response.status in [200, 201]:
            channel = await dm_response.json()
            msg_url = f"https://discord.com/api/v9/channels/{channel['id']}/messages"
            form_data = aiohttp.FormData()
            form_data.add_field('file', file_data, filename=filename)
            form_data.add_field('payload_json', f'{{"content": "{content}"}}')
            async with aiohttp_session.post(msg_url, data=form_data, headers={"Authorization": DISCORD_TOKEN, "User-Agent": DISCORD_USER_AGENT}) as msg_response:
                if msg_response.status == 200: return True
    logging.error(f"Failed to forward file to Discord.")
    return False
async def send_discord_dm(recipient_id, content):
    url = "https://discord.com/api/v9/users/@me/channels"
    payload = {"recipients": [str(recipient_id)]}
    headers = {"Authorization": DISCORD_TOKEN, "Content-Type": "application/json", "User-Agent": DISCORD_USER_AGENT}
    async with aiohttp_session.post(url, json=payload, headers=headers) as response:
        if response.status in [200, 201]:
            channel = await response.json()
            msg_url = f"https://discord.com/api/v9/channels/{channel['id']}/messages"
            msg_payload = {"content": content}
            await aiohttp_session.post(msg_url, json=msg_payload, headers=headers)
            return True
    return False
@bot.gateway.command
def on_discord_message(resp):
    global MY_USER_ID, main_loop
    if resp.event.ready:
        user = bot.gateway.session.user; MY_USER_ID = user['id']
        logging.info(f"Discord Userbot is LIVE. Logged in as: {user['username']}#{user['discriminator']}")
        return
    if resp.event.message:
        message_dict = resp.parsed.auto()
        author_id = message_dict['author']['id']
        if 'guild_id' not in message_dict and str(author_id) in discord_id_to_slack_map and author_id != MY_USER_ID:
            client_info = discord_id_to_slack_map[str(author_id)]
            logging.info(f"-> Discord DM received from '{client_info['client_name']}'. Forwarding to Slack...")
            coro = process_discord_to_slack(message_dict, client_info)
            asyncio.run_coroutine_threadsafe(coro, main_loop)
async def process_discord_to_slack(message_dict, client_info):
    try:
        if message_dict.get('attachments'):
            class Attachment:
                def __init__(self, data): self.url, self.filename = data['url'], data['filename']
            class Message:
                def __init__(self, data, attachment): self.content, self.attachments = data.get('content', ''), [attachment]
            attachment_obj = Attachment(message_dict['attachments'][0])
            message_obj = Message(message_dict, attachment_obj)
            await retry_async_request(forward_file_to_slack, 3, message_obj, client_info)
        else:
            await slack_client.chat_postMessage(channel=client_info["slack_channel_id"], text=f"*{client_info['client_name']}:*\n{message_dict.get('content', '')}")
    except Exception:
        logging.error("An exception occurred in process_discord_to_slack:", exc_info=True)
async def initialize_slack_state():
    for channel_id in slack_to_discord_map.keys():
        try:
            response = await slack_client.conversations_history(channel=channel_id, limit=1)
            messages = response.get("messages", [])
            if messages: slack_channel_state[channel_id] = messages[0]['ts']
        except Exception as e:
            logging.error(f"Could not initialize state for Slack channel {channel_id}: {e}")
    logging.info(f"Slack state initialized for {len(slack_channel_state)} channels.")
async def poll_slack_and_forward():
    try:
        auth_test = await slack_client.auth_test()
        slack_bot_user_id = auth_test["user_id"]
    except Exception:
        logging.critical("Could not fetch Slack bot user ID. Exiting.", exc_info=True)
        return
    await initialize_slack_state()
    logging.info("Slack polling loop is running...")
    while True:
        current_slack_map = dict(slack_to_discord_map)
        for slack_channel_id, client_info in current_slack_map.items():
             try:
                last_known_ts = slack_channel_state.get(slack_channel_id)
                response = await slack_client.conversations_history(channel=slack_channel_id, oldest=last_known_ts, limit=20)
                messages = response.get("messages", [])
                if messages:
                    messages.reverse()
                    for message in messages:
                        user, text, ts = message.get("user"), message.get("text", ""), message.get("ts")
                        if user and user != slack_bot_user_id and ts != last_known_ts:
                            logging.info(f"<- Slack message received for '{client_info['client_name']}'. Forwarding to Discord...")
                            discord_user_id = client_info["discord_user_id"]
                            if message.get("files"):
                                file_info = message["files"][0]
                                file_url, filename = file_info.get("url_private_download"), file_info.get("name")
                                await retry_async_request(send_discord_dm_with_file, 3, discord_user_id, text, file_url, filename)
                            else:
                                await retry_async_request(send_discord_dm, 3, discord_user_id, text)
                    slack_channel_state[slack_channel_id] = messages[-1]['ts']
             except Exception:
                logging.error(f"An exception occurred while polling channel {slack_channel_id}:", exc_info=True)
        await asyncio.sleep(2)

def discum_wrapper():
    bot.gateway.run(auto_reconnect=True)

if __name__ == "__main__":
    if not all([DISCORD_TOKEN, SLACK_BOT_TOKEN]):
        sys.exit("FATAL ERROR: One or more required tokens are missing in the .env file.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Discord bridge shutting down.")
    finally:
        if bot.gateway.READY:
            logging.info("Closing Discum gateway...")
            bot.gateway.close()