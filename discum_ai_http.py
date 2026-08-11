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
DISCORD_REFRESH_PORT = int(os.getenv("DISCORD_REFRESH_PORT", 8102))

slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
bot = discum.Client(token=DISCORD_TOKEN, log=False)

MY_USER_ID, main_loop, aiohttp_session = None, None, None
discord_id_to_slack_map, slack_to_discord_map = {}, {}
slack_channel_state = {}

# --- NEW: The async function that gets called on-demand to refresh the config ---
# REPLACE your old reload_config function with this one.

# In main_discord.py, REPLACE the reload_config function with this:

async def reload_config():
    global discord_id_to_slack_map, slack_to_discord_map, slack_channel_state
    loop = asyncio.get_running_loop()
    logging.info("(Discord Bridge) Refresh signal received! Reloading config...")
    
    # Now we can simply call our new, smarter function!
    all_discord_mappings_raw = await loop.run_in_executor(
        None, get_client_mappings, "Discord", True # The 'True' tells it to match "Discord" and "Discord-Channel"
    )
    
    if all_discord_mappings_raw:
        new_mappings = []
        for c in all_discord_mappings_raw:
            platform = c.get("platform", "")
            mapping = {
                "client_name": c.get("client_name"),
                "external_id": c.get("external_id"),
                "slack_channel_id": c.get("slack_channel_id"),
                # This is where we save our "label" for later
                "type": "channel" if platform == "Discord-Channel" else "dm"
            }
            new_mappings.append(mapping)

        new_discord_map = {item["external_id"]: item for item in new_mappings if item.get("external_id")}
        
        new_slack_map = {
            item["slack_channel_id"]: {
                "discord_id": item["external_id"],
                "client_name": item["client_name"],
                "type": item["type"]
            } 
            for item in new_mappings if item.get("slack_channel_id")
        }
        
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
        logging.info(f"(Discord Bridge) Configuration reloaded. Now tracking {len(discord_id_to_slack_map)} clients (DMs and Channels).")
# --- NEW: The aiohttp server and its endpoint ---
async def handle_refresh(request):
    """Endpoint handler that triggers the config reload as a background task."""
    asyncio.create_task(reload_config())
    return web.Response(text="Refresh signal received.")

async def handle_health(request):
    return web.json_response({"ok": True, "bridge": "discord", "clients": len(discord_id_to_slack_map)})

async def run_refresh_server():
    """Runs the aiohttp server to listen for the refresh signal."""
    app = web.Application()
    app.add_routes([
        web.post('/refresh', handle_refresh),
        web.get('/health', handle_health)
    ])
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
# PASTE THESE TWO NEW FUNCTIONS INTO main_discord.py

async def send_to_discord_channel(channel_id, content):
    """Sends a text message to a specific Discord channel."""
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    payload = {"content": content}
    headers = {"Authorization": DISCORD_TOKEN, "Content-Type": "application/json", "User-Agent": DISCORD_USER_AGENT}
    async with aiohttp_session.post(url, json=payload, headers=headers) as response:
        return response.status == 200

async def send_to_discord_channel_with_file(channel_id, content, file_url, filename):
    """Downloads a file from Slack and sends it to a specific Discord channel."""
    async with aiohttp_session.get(file_url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}) as response:
        if response.status != 200: return False
        file_data = await response.read()

    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    form_data = aiohttp.FormData()
    form_data.add_field('file', file_data, filename=filename)
    form_data.add_field('payload_json', f'{{"content": "{content}"}}')
    headers = {"Authorization": DISCORD_TOKEN, "User-Agent": DISCORD_USER_AGENT}
    async with aiohttp_session.post(url, data=form_data, headers=headers) as msg_response:
        return msg_response.status == 200
    
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
# The definitive, debug-enabled version for main_discord.py

@bot.gateway.command
# In main_discord.py, replace your on_discord_message function with this one.

def on_discord_message(resp):
    global MY_USER_ID, main_loop
    if resp.event.ready:
        user = bot.gateway.session.user
        MY_USER_ID = user['id']
        logging.info(f"Discord Userbot is LIVE. Logged in as: {user['username']}#{user['discriminator']}")
        return
        
    if resp.event.message:
        message_dict = resp.parsed.auto()
        
        is_dm = 'guild_id' not in message_dict
        
        if is_dm:
            lookup_id = str(message_dict['author']['id'])
        else:
            lookup_id = str(message_dict['channel_id'])
            
        if message_dict['author']['id'] == MY_USER_ID:
            return

        logging.info("==============================================================")
        logging.info(f"DEBUG: Received a message from Discord. DM: {is_dm}")
        
        current_mapping_keys = list(discord_id_to_slack_map.keys())

        logging.info(f"DEBUG: INCOMING DISCORD LOOKUP ID: '{lookup_id}'")
        logging.info(f"DEBUG: CURRENT MAPPING KEYS: {current_mapping_keys}")
        
        is_managed = lookup_id in current_mapping_keys
        
        if not is_managed:
            logging.warning("DEBUG: Mismatch found! The incoming ID is NOT in our mapping.")
            logging.warning("ACTION: Please verify the External ID in your Google Sheet is correct.")
            logging.info("==============================================================")
            return
        
        logging.info("DEBUG: ID is mapped. Proceeding to forward to Slack.")
        logging.info("==============================================================")
        
        client_info = discord_id_to_slack_map[lookup_id]
        
        # --- THIS IS THE ONLY PART THAT CHANGES ---
        
        # 1. Determine the correct name to show in Slack.
        if is_dm:
            # For a DM, the name is the client's name from the sheet.
            name_to_display = client_info['client_name']
        else:
            # For a server message, it's the actual sender's Discord username.
            name_to_display = message_dict['author'].get('username', 'Unknown User')

        # 2. Call the forwarding function with the correct name.
        coro = process_discord_to_slack(message_dict, client_info, name_to_display)
        asyncio.run_coroutine_threadsafe(coro, main_loop)
# In main_discord.py, replace your function with this corrected version.

# CHANGE 1: Added 'author_name' at the end
# In main_discord.py, REPLACE this entire function with the final version below.

async def process_discord_to_slack(message_dict, client_info, author_name):
    try:
        content = message_dict.get('content', '')
        attachments = message_dict.get('attachments', [])
        author = message_dict.get('author', {}) # <-- NEW: Get the whole author object

        # --- NEW: Construct the user's avatar URL ---
        avatar_url = None
        if author.get('avatar'):
            user_id = author.get('id')
            avatar_hash = author.get('avatar')
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        else:
            # Fallback for users with no custom avatar
            discriminator = author.get('discriminator', '0')
            avatar_url = f"https://cdn.discordapp.com/embed/avatars/{int(discriminator) % 5}.png"
        # --- END OF NEW LOGIC ---

        # --- Case 1: Message has attachments (files) ---
        # Slack API Limitation: We CANNOT change the bot name for file uploads.
        # So we keep the old behavior here: the name stays in the comment.
        if attachments:
            logging.info(f"Processing {len(attachments)} file(s) from Discord for '{author_name}'...")
            for i, attachment in enumerate(attachments):
                try:
                    async with aiohttp_session.get(attachment['url']) as response:
                        if response.status == 200:
                            file_data = await response.read()
                            comment = ""
                            if i == 0:
                                comment = f"*{author_name}:*\n{content}"
                            
                            await slack_client.files_upload_v2(
                                channel=client_info["slack_channel_id"],
                                content=file_data,
                                filename=attachment['filename'],
                                initial_comment=comment
                            )
                        else:
                            logging.error(f"Failed to download file from Discord. Status: {response.status}")
                except Exception as e:
                    logging.error(f"Error processing a file from Discord: {e}", exc_info=True)
        
        # --- Case 2: Message is text-only (This is where the magic happens) ---
        elif content:
            await slack_client.chat_postMessage(
                channel=client_info["slack_channel_id"],
                # The text is now CLEAN, no name prefix needed.
                text=f"{author_name}: {content}", 
                # --- NEW PARAMETERS TO OVERRIDE THE BOT'S APPEARANCE ---
                username=author_name,          # Use the Discord user's name
                icon_url=avatar_url,           # Use their Discord avatar
                # Use the clean text as a fallback for push notifications
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": content}}] 
            )
            logging.info("✅ Text message forwarded to Slack with custom user profile.")

    except Exception as e:
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
# REPLACE your old poll_slack_and_forward function with this one.

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
                        user, ts, subtype = message.get("user"), message.get("ts"), message.get("subtype")
                        if not user or user == slack_bot_user_id or ts == last_known_ts: continue
                        if subtype and subtype not in ["file_share", "thread_broadcast"]: continue
                        
                        logging.info(f"<- Slack message for '{client_info['client_name']}'. Deciding how to send...")
                        text = message.get("text", "")
                        files = message.get("files", [])
                        
                        # Here is the smart check!
                        discord_id = client_info["discord_id"]
                        connection_type = client_info["type"]

                        if connection_type == "channel":
                            # Use the NEW server channel tools
                            if files:
                                for i, f in enumerate(files):
                                    await retry_async_request(send_to_discord_channel_with_file, 3, discord_id, (text if i == 0 else ""), f.get("url_private_download"), f.get("name"))
                            elif text:
                                await retry_async_request(send_to_discord_channel, 3, discord_id, text)
                        else: # Otherwise, it's a DM
                            # Use the OLD DM tools
                            if files:
                                for i, f in enumerate(files):
                                    await retry_async_request(send_discord_dm_with_file, 3, discord_id, (text if i == 0 else ""), f.get("url_private_download"), f.get("name"))
                            elif text:
                                await retry_async_request(send_discord_dm, 3, discord_id, text)
                    
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