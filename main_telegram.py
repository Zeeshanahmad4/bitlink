import os
import sys
import logging
import asyncio
import requests
import re
from pathlib import Path
from io import BytesIO
import json
from telethon.tl import types
import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from aiohttp import web
import threading
from collections import deque

# Import your existing Google Sheets client
from g_sheets_client import get_client_mappings

# --- 1. INITIALIZATION AND CONFIGURATION ---

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Load configuration from environment - TELEGRAM SPECIFIC TOKENS
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
SESSION_NAME = os.getenv('SESSION_NAME', 'bitlink_telegram')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN_TELEGRAM')
SLACK_APP_TOKEN = os.getenv('SLACK_APP_TOKEN_TELEGRAM')
TELEGRAM_REFRESH_PORT = int(os.getenv("TELEGRAM_REFRESH_PORT", 8003))
SLACK_ADMIN_CHANNEL_ID = os.getenv('SLACK_ADMIN_CHANNEL_ID_tele')
SENT_ALERTS_FILE = os.getenv("TELEGRAM_ALERTS_FILE", "sent_telegram_alerts.json")
MESSAGE_MAP_FILE = os.getenv("TELEGRAM_MESSAGE_MAP_FILE", "telegram_message_map.json")

if not all([API_ID, API_HASH, SLACK_BOT_TOKEN]):
    logging.critical("FATAL: Missing one or more required environment variables.")
    sys.exit(1)

# --- 2. GLOBAL VARIABLES AND CLIENTS ---

telethon_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

main_loop, aiohttp_session = None, None
telegram_to_slack_map, slack_to_telegram_map = {}, {}
slack_channel_state = {}
sent_alerts = set()
socket_mode_active = False
slack_bot_user_id = None
event_count = 0

DOWNLOAD_DIR = Path("temp_downloads_telegram")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# IMPROVEMENT #2: Better deduplication with client_msg_id
processed_slack_events = deque(maxlen=1000)  # Increased capacity

# IMPROVEMENT #3: Message ID mapping for edit/delete support
# Format: "channel_id:ts" -> telegram_message_id
slack_to_telegram_message_map = {}

# IMPROVEMENT #1: Background worker queue
event_queue = None  # Will be initialized as asyncio.Queue

# --- 3. MESSAGE MAPPING PERSISTENCE ---

def load_message_map():
    """Loads the Slack->Telegram message ID mapping from disk."""
    if not os.path.exists(MESSAGE_MAP_FILE):
        return {}
    try:
        with open(MESSAGE_MAP_FILE, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Could not load message map: {e}")
        return {}

def save_message_map():
    """Saves the message map to disk."""
    try:
        # Only keep last 10,000 mappings to prevent file from growing infinitely
        if len(slack_to_telegram_message_map) > 10000:
            # Keep only the most recent 5000
            items = list(slack_to_telegram_message_map.items())
            new_map = dict(items[-5000:])
            slack_to_telegram_message_map.clear()
            slack_to_telegram_message_map.update(new_map)
        
        with open(MESSAGE_MAP_FILE, 'w') as f:
            json.dump(slack_to_telegram_message_map, f, indent=2)
    except IOError as e:
        logging.error(f"Could not write message map: {e}")

# --- 4. CONFIGURATION MANAGEMENT ---

async def slack_post_message_with_retry(channel_id: str, text: str, max_retries: int = 5):
    """Posts a message to Slack with handling for 429 and transient errors."""
    delay = 1
    for attempt in range(max_retries):
        try:
            await slack_client.chat_postMessage(channel=channel_id, text=text)
            return True
        except SlackApiError as e:
            try:
                status = getattr(e.response, 'status_code', None)
                err = None
                try:
                    err = e.response.get('error')
                except Exception:
                    err = None
                if status == 429 or err == 'ratelimited':
                    retry_after = 0
                    try:
                        retry_after = int(e.response.headers.get('Retry-After', '0'))
                    except Exception:
                        retry_after = 0
                    wait_seconds = retry_after if retry_after > 0 else delay
                    logging.warning(f"Slack rate limit hit. Waiting {wait_seconds}s before retry.")
                    await asyncio.sleep(wait_seconds)
                    delay = min(delay * 2, 30)
                    continue
            except Exception:
                pass
            logging.warning(f"Slack post failed: {e}. Backing off {delay}s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as e:
            logging.warning(f"Unexpected error posting to Slack: {e}. Backing off {delay}s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    return False

async def reload_config():
    global telegram_to_slack_map, slack_to_telegram_map, slack_channel_state
    loop = asyncio.get_running_loop()
    logging.info("(Telegram Bridge) Refresh signal received! Reloading config...")
    
    # Use a thread executor for the synchronous gspread call
    mappings_raw = await loop.run_in_executor(None, get_client_mappings, "Telegram")
    
    if mappings_raw:
        new_telegram_map = {
            str(c["external_id"]): {
                "slack_channel_id": c["slack_channel_id"],
                "client_name": c["client_name"],
                "paused": c.get("paused", False)  # ADD THIS LINE
            } for c in mappings_raw if c.get("external_id")
        }
        new_slack_map = {
            c["slack_channel_id"]: {
                "telegram_id": int(c["external_id"]),
                "client_name": c["client_name"],
                "paused": c.get("paused", False)  # ADD THIS LINE
            } for c in mappings_raw if c.get("slack_channel_id")
        }
        
        for new_channel_id in new_slack_map:
            if new_channel_id not in slack_to_telegram_map:
                logging.info(f"New client channel found: {new_channel_id}. Initializing state.")
                try:
                    response = await slack_client.conversations_history(channel=new_channel_id, limit=1)
                    if response.get("messages"):
                        slack_channel_state[new_channel_id] = response["messages"][0]['ts']
                except Exception as e:
                    logging.error(f"Could not initialize state for new channel {new_channel_id}: {e}")

        telegram_to_slack_map = new_telegram_map
        slack_to_telegram_map = new_slack_map
        logging.info(f"(Telegram Bridge) Configuration reloaded. Now tracking {len(telegram_to_slack_map)} clients.")

async def handle_refresh(request):
    """Endpoint handler that triggers the config reload as a background task."""
    asyncio.create_task(reload_config())
    return web.Response(text="Refresh signal received.")

async def handle_test_alert(request):
    """Sends a test alert to the admin Slack channel to verify wiring."""
    try:
        if not SLACK_ADMIN_CHANNEL_ID:
            return web.Response(status=400, text="SLACK_ADMIN_CHANNEL_ID_tele not set")
        await slack_post_message_with_retry(SLACK_ADMIN_CHANNEL_ID, "🔧 Telegram bridge test: admin alert path is working.")
        return web.Response(text="Test alert sent.")
    except Exception as e:
        logging.error(f"Test alert failed: {e}")
        return web.Response(status=500, text=f"Error: {e}")
# Place this new function before run_refresh_server in main_telegram.py
async def handle_send_message(request):
    """Endpoint to receive a send command and forward to Telegram."""
    try:
        data = await request.json()
        recipient_id = data.get("recipient_id")
        text = data.get("text")

        if not recipient_id or not text:
            return web.Response(status=400, text="Missing recipient_id or text")

        # Use asyncio.create_task to send without blocking the server
        asyncio.create_task(telethon_client.send_message(int(recipient_id), text))
        
        logging.info(f"Received send command. Queued message for Telegram ID: {recipient_id}")
        return web.Response(text="Message queued for sending.")
    except Exception as e:
        logging.error(f"Error in handle_send_message: {e}")
        return web.Response(status=500, text=f"Error: {e}")
    
async def run_refresh_server():
    """Runs the aiohttp server to listen for the refresh signal."""
    app = web.Application()
    app.add_routes([
        web.post('/refresh', handle_refresh),
        web.post('/test-alert', handle_test_alert),
        web.post('/send', handle_send_message)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', TELEGRAM_REFRESH_PORT)
    await site.start()
    logging.info(f"Telegram refresh server listening on port {TELEGRAM_REFRESH_PORT}")
    while True:
        await asyncio.sleep(3600)

# --- 5. CORE LOGIC (Telegram -> Slack) ---

async def get_sender_details(event):
    """Gets sender's name and profile picture URL."""
    sender = await event.get_sender()
    name = "Unknown User"
    pfp_path = None
    
    if sender:
        name = sender.first_name or sender.username or "User"
        try:
            pfp_path = await telethon_client.download_profile_photo(
                sender, 
                file=DOWNLOAD_DIR / f"{sender.id}_pfp.jpg"
            )
        except Exception:
            pfp_path = None
    elif hasattr(event.chat, 'title'):
        name = event.chat.title
        try:
            pfp_path = await telethon_client.download_profile_photo(
                event.chat, 
                file=DOWNLOAD_DIR / f"{event.chat.id}_pfp.jpg"
            )
        except Exception:
            pfp_path = None

    return name, pfp_path

@telethon_client.on(events.NewMessage)
async def handle_telegram_message(event):
    """Listens for new Telegram messages and forwards them to Slack."""
    logging.info(f"DEBUG: New message received from Chat ID: {event.chat_id}")
    if event.out: return

    chat_id = str(event.chat_id)
    if chat_id not in telegram_to_slack_map: return

    client_info = telegram_to_slack_map[chat_id]
    slack_channel_id = client_info["slack_channel_id"]  # ← MOVE THIS LINE UP!

    # CHECK IF CHANNEL IS PAUSED - BLOCK TELEGRAM TO SLACK
    if client_info.get("paused", False):
        logging.info(f"Channel {slack_channel_id} is paused. Skipping Telegram→Slack forwarding.")
        return

    client_name = client_info["client_name"]  # ← CRITICAL: Get client name from mapping
    
    sender_name, pfp_path = await get_sender_details(event)
    message_text = event.message.text
    
    try:
        if event.message.media:
            logging.info(f"Media message received from '{sender_name}' ({chat_id}). Downloading...")
            file_path = await telethon_client.download_media(event.message, file=DOWNLOAD_DIR)
            
            # Prepare comment, use default if caption is empty
            comment = message_text or f"Sent a file: {Path(file_path).name}"
            
            await slack_client.files_upload_v2(
                channel=slack_channel_id,
                file=file_path,
                title=Path(file_path).name,
                # Use CLIENT name instead of sender name
                initial_comment=f"*{client_name}:*\n{comment}"  # ← CHANGED: client_name
            )
            os.remove(file_path) # Clean up
            logging.info(f"Forwarded file from '{client_name}' to Slack.")  # ← CHANGED: client_name

        elif message_text:
            # For text messages, we can customize the user profile
            await slack_client.chat_postMessage(
                channel=slack_channel_id,
                text=f"{client_name}: {message_text}", # ← CHANGED: client_name
                username=client_name,  # ← CHANGED: client_name (this overrides "telegram-bridge")
                icon_emoji=":robot_face:",  # ← ADDED: Optional emoji
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
            )
            logging.info(f"Forwarded text from '{client_name}' to Slack.")  # ← CHANGED: client_name
            
        if pfp_path and os.path.exists(pfp_path):
            os.remove(pfp_path) # Clean up profile picture

    except SlackApiError as e:
        logging.error(f"Slack API Error forwarding from Telegram: {e.response['error']}")
    except Exception as e:
        logging.error(f"An unexpected error occurred in handle_telegram_message: {e}", exc_info=True)

# --- NEW CHAT ALERTS ---

def load_sent_alerts():
    if not os.path.exists(SENT_ALERTS_FILE):
        return set()
    try:
        with open(SENT_ALERTS_FILE, 'r') as f:
            return set(json.load(f))
    except (IOError, json.JSONDecodeError):
        return set()

def save_sent_alerts(alerts_set):
    try:
        with open(SENT_ALERTS_FILE, 'w') as f:
            json.dump(list(alerts_set), f, indent=4)
    except IOError:
        logging.error(f"Could not write to {SENT_ALERTS_FILE}")

async def send_new_chat_alert_to_slack(chat, chat_id, action_description):
    chat_type = "Unknown"
    if isinstance(chat, types.User):
        chat_type = "Direct Message (DM)"
    elif isinstance(chat, types.Chat):
        chat_type = "Small Group"
    elif isinstance(chat, types.Channel):
        chat_type = "Supergroup" if chat.megagroup else "Channel"

    try:
        if isinstance(chat, types.User):
            name_display = chat.first_name or chat.username or "User"
        elif hasattr(chat, 'title') and chat.title:
            name_display = chat.title
        else:
            name_display = str(chat_id)
    except Exception:
        name_display = str(chat_id)

    message_text = (
        f"🔔 *New Telegram Chat Alert*\n\n"
        f"{action_description}\n\n"
        f"• *Name:* {name_display}\n"
        f"• *Type:* {chat_type}\n"
        f"• *Chat ID:* `{chat_id}`\n\n"
        f"To map this chat, add the ID to the Google Sheet."
    )

    try:
        sent_alerts.add(str(chat_id))
        save_sent_alerts(sent_alerts)

        if not SLACK_ADMIN_CHANNEL_ID:
            logging.warning("SLACK_ADMIN_CHANNEL_ID is not set. Cannot send alert.")
            return

        await slack_post_message_with_retry(SLACK_ADMIN_CHANNEL_ID, message_text)
        logging.info(f"Successfully sent new chat alert for '{name_display}'.")
        
    except Exception as e:
        logging.error(f"Failed to send new chat alert to Slack: {e}")

@telethon_client.on(events.NewMessage(pattern=None, forwards=False, outgoing=True))
async def handle_bot_created_chat(event):
    if not hasattr(event.message, 'action') or not isinstance(event.message.action, types.MessageActionChatCreate):
        return
        
    chat = await event.get_chat()
    chat_id = event.chat_id
    
    if str(chat_id) in telegram_to_slack_map or str(chat_id) in sent_alerts:
        return

    logging.info(f"Bot has created a new group: '{chat.title}' ({chat_id})")
    await send_new_chat_alert_to_slack(
        chat=chat,
        chat_id=chat_id,
        action_description="I have created a new group."
    )

@telethon_client.on(events.ChatAction)
async def handle_chat_actions_universal(event):
    try:
        me = await telethon_client.get_me()
        user_id = getattr(event, 'user_id', None)
        if not user_id or user_id != me.id:
            return
    except Exception:
        return

    chat = await event.get_chat()
    chat_id = event.chat_id
    
    if str(chat_id) in telegram_to_slack_map or str(chat_id) in sent_alerts:
        return

    logging.info(f"Bot has been added to a new chat: '{getattr(chat, 'title', chat_id)}' ({chat_id})")
    
    adder = await event.get_added_by()
    adder_name = "an unknown user"
    if adder:
        adder_name = f"{adder.first_name} (@{adder.username})" if adder.username else adder.first_name

    await send_new_chat_alert_to_slack(
        chat=chat,
        chat_id=chat_id,
        action_description=f"I have been added to a new chat by *{adder_name}*."
    )

@telethon_client.on(events.NewMessage)
async def discover_unmapped_on_message(event):
    if event.out:
        return
    chat_id = str(event.chat_id)
    if chat_id in telegram_to_slack_map or chat_id in sent_alerts:
        return
    try:
        chat = await event.get_chat()
        await send_new_chat_alert_to_slack(
            chat=chat,
            chat_id=chat_id,
            action_description="I have discovered a new chat from an incoming message."
        )
    except Exception as e:
        logging.error(f"Failed to send discovery alert: {e}")
async def send_paused_notification(slack_channel_id, thread_ts):
    """Send a thread notification when trying to send from a paused channel."""
    try:
        await slack_client.chat_postMessage(
            channel=slack_channel_id,
            thread_ts=thread_ts,
            text="*Info:* This channel is currently paused. Outgoing Slack messages are not forwarded to Telegram.",
            username="Bitlink Bridge Info"
        )
        logging.info(f"Sent paused notification to Slack channel {slack_channel_id}")
    except Exception as e:
        logging.error(f"Failed to send paused notification to Slack: {e}")
# --- 6. SLACK -> TELEGRAM (Background Worker) ---

def clean_slack_formatting(text: str) -> str:
    """
    Removes Slack's special formatting before sending to Telegram.
    
    Slack formats:
    - URLs: <http://example.com|example.com> → http://example.com
    - Simple URLs: <http://example.com> → http://example.com  
    - User mentions: <@U123456> → @username (keep as is for now)
    - Channel mentions: <#C123456|general> → #general
    """
    if not text:
        return text
    
    # Pattern 1: <URL|display_text> → display_text (usually the clean URL)
    text = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2', text)
    
    # Pattern 2: <URL> → URL (remove brackets)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    
    # Pattern 3: <#CHANNEL_ID|channel_name> → #channel_name
    text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)
    
    # Pattern 4: <!here>, <!channel>, <!everyone> → @here, @channel, @everyone
    text = text.replace('<!here>', '@here')
    text = text.replace('<!channel>', '@channel')
    text = text.replace('<!everyone>', '@everyone')
    
    return text

# IMPROVEMENT #1: Background event processor
async def process_slack_event_async(event: dict, bot_token: str):
    # --- Slack-to-Telegram edit functionality ---
    if event.get("type") == "message" and event.get("subtype") == "message_changed":
        channel_id = event.get("channel")
        message = event.get("message", {})
        ts = message.get("ts")
        new_text = message.get("text", "")
        map_key = f"{channel_id}:{ts}"
        mapping = slack_to_telegram_message_map.get(map_key)
        if mapping:
            telegram_id = mapping["telegram_id"]
            telegram_msg_id = mapping["telegram_msg_id"]
            try:
                await telethon_client.edit_message(telegram_id, telegram_msg_id, new_text)
                logging.info(f"Edited Telegram message {telegram_msg_id} in chat {telegram_id} due to Slack edit event")
            except Exception as e:
                logging.error(f"Failed to edit Telegram message: {e}")
        return
    # --- Slack-to-Telegram delete functionality ---
    if event.get("type") == "message" and event.get("subtype") == "message_deleted":
        channel_id = event.get("channel")
        deleted_ts = event.get("deleted_ts")
        map_key = f"{channel_id}:{deleted_ts}"
        mapping = slack_to_telegram_message_map.get(map_key)
        if mapping:
            telegram_id = mapping["telegram_id"]
            telegram_msg_id = mapping["telegram_msg_id"]
            try:
                await telethon_client.delete_messages(telegram_id, telegram_msg_id)
                logging.info(f"Deleted Telegram message {telegram_msg_id} in chat {telegram_id} due to Slack delete event")
                del slack_to_telegram_message_map[map_key]
                save_message_map()
            except Exception as e:
                logging.error(f"Failed to delete Telegram message: {e}")
        return
    """
    Async processor for Slack events. Runs in background queue.
    IMPROVEMENT #2: Uses client_msg_id for dedup
    IMPROVEMENT #3: Stores message mapping
    IMPROVEMENT #4: Guards against bot_id
    """
    global event_count
    event_count += 1
    
    try:
        event_type = event.get("type")
        subtype = event.get("subtype")
        channel_id = event.get("channel")
        
        if event_type != "message":
            return
            
        if subtype and subtype not in ["file_share", "thread_broadcast"]:
            return

        ts = event.get("ts")
        user = event.get("user")
        bot_id = event.get("bot_id")  # IMPROVEMENT #4
        
        if not channel_id or not ts:
            return

        # IMPROVEMENT #4: Filter out bot messages (including our own)
        if bot_id or (user and user == slack_bot_user_id):
            return

        # IMPROVEMENT #2: Better deduplication
        client_msg_id = event.get("client_msg_id")
        if client_msg_id:
            # Prefer client_msg_id (more reliable)
            dedup_key = f"client:{client_msg_id}"
        else:
            # Fallback to channel:ts
            dedup_key = f"ts:{channel_id}:{ts}"
        
        if dedup_key in processed_slack_events:
            return
        processed_slack_events.append(dedup_key)

    
         # Map to Telegram
        mapping = slack_to_telegram_map.get(channel_id)
        if not mapping:
            return
        
        # CHECK IF CHANNEL IS PAUSED - BLOCK SLACK TO TELEGRAM
        if mapping.get("paused", False):
            logging.info(f"Channel {channel_id} is paused. Blocking Slack→Telegram message.")
            await send_paused_notification(channel_id, ts)
            return
            
        telegram_id = mapping["telegram_id"]
        client_name = mapping["client_name"] 
    
        text_caption = event.get("text", "")
        
        # Clean Slack formatting before sending to Telegram
        text_caption = clean_slack_formatting(text_caption)

        logging.info(f"[Socket Mode] <- Slack message for '{client_name}'. Forwarding to Telegram...")

        telegram_msg_id = None  # Track the message ID we send

        # Files
        files = event.get("files", []) or []
        if files:
            is_first = True
            for f in files:
                file_url = f.get("url_private_download")
                filename = f.get("name", "file.bin")
                filetype = f.get("mimetype", "")
                filesize = f.get("size", 0)
                caption = text_caption if is_first else ""
                
                # Download file into memory
                try:
                    async with aiohttp_session.get(
                        file_url, 
                        headers={"Authorization": f"Bearer {bot_token}"},
                        timeout=aiohttp.ClientTimeout(total=60)
                    ) as resp:
                        if resp.status != 200:
                            logging.error(f"Failed to download file: {filename} status={resp.status}")
                            continue
                        
                        file_data = await resp.read()
                        
                        # Verify download completed
                        if not file_data:
                            logging.error(f"Empty file downloaded: {filename}")
                            continue
                        
                        # Verify size matches if available
                        if filesize > 0 and len(file_data) != filesize:
                            logging.warning(f"Size mismatch for {filename}: expected {filesize}, got {len(file_data)}")
                        
                        logging.info(f"   Downloaded {filename} ({len(file_data)} bytes)")
                
                except Exception as download_error:
                    logging.error(f"   ❌ Download failed for {filename}: {download_error}")
                    continue
                
                # Send file directly from memory (no disk write)
                try:
                    # Create in-memory file
                    file_bytes = BytesIO(file_data)
                    file_bytes.name = filename  # Telegram uses this for filename
                    
                    # Determine if it's an image
                    is_image = filetype.startswith("image/") and filetype not in ["image/svg+xml", "image/webp"]
                    
                    # Try sending as media first (for images)
                    if is_image:
                        try:
                            msg = await telethon_client.send_file(
                                telegram_id, 
                                file_bytes, 
                                caption=caption,
                                force_document=False,
                                attributes=[],
                            )
                            logging.info(f"   ✅ Image sent to Telegram: {filename}")
                        except Exception as img_error:
                            # If image fails, fallback to document
                            logging.warning(f"   ⚠️ Image send failed, trying as document: {str(img_error)[:100]}")
                            file_bytes.seek(0)  # Reset stream position
                            msg = await telethon_client.send_file(
                                telegram_id,
                                file_bytes,
                                caption=caption,
                                force_document=True,
                                attributes=[],
                            )
                            logging.info(f"   ✅ File sent as document: {filename}")
                    else:
                        # Non-images always send as document
                        msg = await telethon_client.send_file(
                            telegram_id,
                            file_bytes,
                            caption=caption,
                            force_document=True,
                            attributes=[],
                        )
                        logging.info(f"   ✅ File sent to Telegram: {filename}")
                    
                    # IMPROVEMENT #3: Store first message ID
                    if is_first and msg:
                        telegram_msg_id = msg.id
                        
                except Exception as send_error:
                    logging.error(f"   ❌ Failed to send file {filename}: {send_error}", exc_info=True)
                finally:
                    # Clean up memory
                    if 'file_bytes' in locals():
                        file_bytes.close()
                    del file_data
                
                is_first = False
        else:
            # Text-only
            if text_caption:
                msg = await telethon_client.send_message(telegram_id, text_caption)
                # IMPROVEMENT #3: Store message ID
                if msg:
                    telegram_msg_id = msg.id
                logging.info(f"   ✅ Text sent to Telegram")
        
        # IMPROVEMENT #3: Store the mapping for potential edit/delete
        if telegram_msg_id:
            map_key = f"{channel_id}:{ts}"
            slack_to_telegram_message_map[map_key] = {
                "telegram_id": telegram_id,
                "telegram_msg_id": telegram_msg_id
            }
            # Persist to disk every 10 messages
            if len(slack_to_telegram_message_map) % 10 == 0:
                save_message_map()
                
    except Exception as e:
        logging.error(f"Event processing error: {e}", exc_info=True)

# IMPROVEMENT #1: Background worker loop
async def event_worker():
    """Background worker that processes events from the queue."""
    logging.info("🔧 Event worker started")
    while True:
        try:
            event, bot_token = await event_queue.get()
            await process_slack_event_async(event, bot_token)
            event_queue.task_done()
        except Exception as e:
            logging.error(f"Worker error: {e}", exc_info=True)

def start_slack_socket_mode():
    """Starts Socket Mode with non-blocking event handling."""
    global socket_mode_active
    
    if not SLACK_APP_TOKEN or not SLACK_BOT_TOKEN:
        logging.error("Missing SLACK_APP_TOKEN_TELEGRAM or SLACK_BOT_TOKEN_TELEGRAM")
        return False

    try:
        logging.info("🔌 Initializing Slack Socket Mode for Telegram Bridge...")
        web_client_sync = WebClient(token=SLACK_BOT_TOKEN)
        socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client_sync)

        def handler(client, req):
            try:
                # Acknowledge immediately (non-blocking)
                client.send_socket_mode_response({"envelope_id": req.envelope_id})
                
                payload = req.payload or {}
                payload_type = payload.get("type")
                
                # Accept both "events_api" and "event_callback"
                if payload_type in ["events_api", "event_callback"]:
                    event = payload.get("event", {})
                    # FIX: Use thread-safe coroutine scheduling
                    if event_queue and main_loop:
                        try:
                            # Schedule the put operation in the event loop from this thread
                            asyncio.run_coroutine_threadsafe(
                                event_queue.put((event, web_client_sync.token)),
                                main_loop
                            )
                        except Exception as e:
                            logging.error(f"Failed to queue event: {e}")
                    
            except Exception as e:
                logging.error(f"SocketMode handler error: {e}", exc_info=True)

        socket_client.socket_mode_request_listeners.append(handler)
        
        def connect_socket():
            try:
                socket_client.connect()
                logging.info("✅ Slack Socket Mode connected successfully!")
            except Exception as e:
                logging.error(f"❌ Socket Mode connection failed: {e}", exc_info=True)
        
        thread = threading.Thread(target=connect_socket, daemon=True)
        thread.start()
        
        import time
        time.sleep(2)
        
        if socket_client.is_connected():
            socket_mode_active = True
            logging.info("✅ Socket Mode is ACTIVE - Real-time forwarding enabled!")
            return True
        else:
            logging.error("❌ Socket Mode failed to connect")
            return False
            
    except Exception as e:
        logging.error(f"❌ Socket Mode initialization error: {e}", exc_info=True)
        return False

# --- 7. MAIN EXECUTION ---

async def main():
    global main_loop, aiohttp_session, sent_alerts, slack_bot_user_id, event_queue, slack_to_telegram_message_map
    
    sent_alerts = load_sent_alerts()
    slack_to_telegram_message_map = load_message_map()  # IMPROVEMENT #3
    main_loop = asyncio.get_running_loop()
    
    # IMPROVEMENT #1: Initialize event queue
    event_queue = asyncio.Queue(maxsize=1000)
    
    await reload_config()

    # Get bot user ID
    try:
        auth_test = await slack_client.auth_test()
        slack_bot_user_id = auth_test["user_id"]
        logging.info(f"Slack bot user ID: {slack_bot_user_id}")
    except Exception as e:
        logging.error(f"Could not fetch Slack bot user ID: {e}")

    async with aiohttp.ClientSession() as session:
        aiohttp_session = session
        
        refresh_server_task = asyncio.create_task(run_refresh_server())
        
        # IMPROVEMENT #1: Start background worker
        worker_task = asyncio.create_task(event_worker())
        
        logging.info("Starting Telethon client...")
        await telethon_client.start()
        me = await telethon_client.get_me()
        logging.info(f"Logged in to Telegram as: {me.first_name} (@{me.username})")

        # Startup scan
        async def startup_scan():
            try:
                count = 0
                async for dialog in telethon_client.iter_dialogs():
                    chat = dialog.entity
                    chat_id = getattr(chat, 'id', None)
                    if chat_id is None:
                        continue
                    chat_id_str = str(chat_id)
                    if chat_id_str in telegram_to_slack_map or chat_id_str in sent_alerts:
                        continue
                    if isinstance(chat, (types.User, types.Chat, types.Channel)):
                        await send_new_chat_alert_to_slack(
                            chat=chat,
                            chat_id=chat_id,
                            action_description="I have discovered a new chat during startup scan."
                        )
                        count += 1
                if count > 0:
                    logging.info(f"Startup scan: Found {count} unmapped chats")
                else:
                    logging.info("Startup scan: All chats are mapped")
            except Exception as e:
                logging.error(f"Startup scan failed: {e}")

        startup_scan_task = asyncio.create_task(startup_scan())

        # Start Socket Mode
        logging.info("Starting Socket Mode...")
        try:
            success = await asyncio.get_running_loop().run_in_executor(None, start_slack_socket_mode)
            if success:
                logging.info("=" * 70)
                logging.info("🚀 TELEGRAM BRIDGE FULLY OPERATIONAL")
                logging.info("   ✅ Telegram → Slack: ACTIVE")
                logging.info("   ✅ Slack → Telegram: ACTIVE (Socket Mode + Background Queue)")
                logging.info("   ✅ Message tracking: ENABLED (edit/delete ready)")
                logging.info("   ✅ Advanced deduplication: ENABLED")
                logging.info("=" * 70)
            else:
                logging.error("❌ Socket Mode failed")
        except Exception as e:
            logging.error(f"❌ Socket Mode error: {e}", exc_info=True)

        # Periodic message map save
        async def periodic_save():
            while True:
                await asyncio.sleep(300)  # Save every 5 minutes
                save_message_map()
        
        save_task = asyncio.create_task(periodic_save())

        await asyncio.gather(
            refresh_server_task,
            worker_task,
            startup_scan_task,
            save_task,
            telethon_client.run_until_disconnected()
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down the Telegram bridge.")
        # Final save on shutdown
        save_message_map()