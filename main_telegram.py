import os
import sys
import logging
import asyncio
import requests
from pathlib import Path
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
event_count = 0  # NEW: Track events received

DOWNLOAD_DIR = Path("temp_downloads_telegram")
DOWNLOAD_DIR.mkdir(exist_ok=True)

from collections import deque
processed_slack_events = deque(maxlen=500)

# --- 3. CONFIGURATION MANAGEMENT ---

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
    """Fetches mappings from Google Sheets and updates the global maps."""
    global telegram_to_slack_map, slack_to_telegram_map, slack_channel_state
    loop = asyncio.get_running_loop()
    logging.info("(Telegram Bridge) Refresh signal received! Reloading config...")
    
    mappings_raw = await loop.run_in_executor(None, get_client_mappings, "Telegram")
    
    if mappings_raw:
        new_telegram_map = {
            str(c["external_id"]): c for c in mappings_raw if c.get("external_id")
        }
        new_slack_map = {
            c["slack_channel_id"]: {
                "telegram_id": int(c["external_id"]),
                "client_name": c["client_name"]
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
        
        # Log mapped channels
        logging.info("Mapped Slack channels:")
        for ch_id, info in slack_to_telegram_map.items():
            logging.info(f"  • {info['client_name']}: {ch_id} → Telegram {info['telegram_id']}")

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

async def run_refresh_server():
    """Runs the aiohttp server to listen for the refresh signal."""
    app = web.Application()
    app.add_routes([
        web.post('/refresh', handle_refresh),
        web.post('/test-alert', handle_test_alert)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', TELEGRAM_REFRESH_PORT)
    await site.start()
    logging.info(f"Telegram refresh server listening on port {TELEGRAM_REFRESH_PORT}")
    while True:
        await asyncio.sleep(3600)

# --- 4. CORE LOGIC (Telegram -> Slack) ---

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
    if event.out:
        return

    chat_id = str(event.chat_id)
    if chat_id not in telegram_to_slack_map:
        return

    client_info = telegram_to_slack_map[chat_id]
    slack_channel_id = client_info["slack_channel_id"]
    
    sender_name, pfp_path = await get_sender_details(event)
    message_text = event.message.text
    
    try:
        if event.message.media:
            logging.info(f"Media message received from '{sender_name}' ({chat_id}). Downloading...")
            file_path = await telethon_client.download_media(event.message, file=DOWNLOAD_DIR)
            
            comment = message_text or f"Sent a file: {Path(file_path).name}"
            
            await slack_client.files_upload_v2(
                channel=slack_channel_id,
                file=file_path,
                title=Path(file_path).name,
                initial_comment=f"*{sender_name}:*\n{comment}"
            )
            os.remove(file_path)
            logging.info(f"Forwarded file from '{sender_name}' to Slack.")

        elif message_text:
            await slack_client.chat_postMessage(
                channel=slack_channel_id,
                text=f"{sender_name}: {message_text}",
                username=sender_name,
                icon_url=None,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message_text}}]
            )
            logging.info(f"Forwarded text from '{sender_name}' to Slack.")
            
        if pfp_path and os.path.exists(pfp_path):
            os.remove(pfp_path)

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

# --- 5. SLACK -> TELEGRAM (Socket Mode) WITH DIAGNOSTICS ---

def process_slack_event_socket_mode(event: dict, bot_token: str):
    """Process Slack message event and forward to Telegram."""
    global event_count
    event_count += 1
    
    try:
        # Log ALL events for debugging
        event_type = event.get("type")
        subtype = event.get("subtype")
        channel_id = event.get("channel")
        
        logging.info(f"🔔 [Event #{event_count}] Received: type={event_type}, subtype={subtype}, channel={channel_id}")
        
        if event_type != "message":
            logging.info(f"   ↳ Ignoring: Not a message event")
            return
            
        if subtype and subtype not in ["file_share", "thread_broadcast"]:
            logging.info(f"   ↳ Ignoring: Subtype '{subtype}' not supported")
            return

        ts = event.get("ts")
        user = event.get("user")
        
        if not channel_id or not ts or not user:
            logging.warning(f"   ↳ Missing required fields: channel={channel_id}, ts={ts}, user={user}")
            return

        # Filter out bot's own messages
        if user == slack_bot_user_id:
            logging.info(f"   ↳ Ignoring: Bot's own message")
            return

        # Dedupe
        key = (channel_id, ts)
        if key in processed_slack_events:
            logging.info(f"   ↳ Ignoring: Duplicate (already processed)")
            return
        processed_slack_events.append(key)

        # Map to Telegram
        mapping = slack_to_telegram_map.get(channel_id)
        if not mapping:
            logging.warning(f"   ↳ No mapping found for channel {channel_id}")
            logging.warning(f"   ↳ Available mappings: {list(slack_to_telegram_map.keys())}")
            return
            
        telegram_id = mapping["telegram_id"]
        client_name = mapping["client_name"]
        text_caption = event.get("text", "")

        logging.info(f"   ✅ [Socket Mode] Forwarding to '{client_name}' (Telegram: {telegram_id})")

        # Files
        files = event.get("files", []) or []
        if files:
            is_first = True
            for f in files:
                file_url = f.get("url_private_download")
                filename = f.get("name", "file.bin")
                caption = text_caption if is_first else ""
                
                with requests.Session() as s:
                    s.headers.update({"Authorization": f"Bearer {bot_token}"})
                    resp = s.get(file_url, timeout=30)
                    if resp.status_code != 200:
                        logging.error(f"Failed to download file: {filename} status={resp.status_code}")
                        continue
                    temp_path = DOWNLOAD_DIR / filename
                    temp_path.write_bytes(resp.content)
                
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        telethon_client.send_file(telegram_id, temp_path, caption=caption),
                        main_loop
                    )
                    fut.result(timeout=60)
                    logging.info(f"   ✅ File sent to Telegram: {filename}")
                finally:
                    if temp_path.exists():
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                is_first = False
        else:
            # Text-only
            if text_caption:
                fut = asyncio.run_coroutine_threadsafe(
                    telethon_client.send_message(telegram_id, text_caption),
                    main_loop
                )
                fut.result(timeout=60)
                logging.info(f"   ✅ Text sent to Telegram: {text_caption[:50]}...")
                
    except Exception as e:
        logging.error(f"   ❌ SocketMode processing error: {e}", exc_info=True)

def start_slack_socket_mode():
    """Starts Socket Mode with verbose logging."""
    global socket_mode_active
    
    if not SLACK_APP_TOKEN:
        logging.error("=" * 70)
        logging.error("❌ Socket Mode CANNOT start: SLACK_APP_TOKEN_TELEGRAM is missing!")
        logging.error("=" * 70)
        return False
        
    if not SLACK_BOT_TOKEN:
        logging.error("❌ Socket Mode CANNOT start: SLACK_BOT_TOKEN_TELEGRAM is missing!")
        return False

    try:
        logging.info("🔌 Initializing Slack Socket Mode for Telegram Bridge...")
        web_client_sync = WebClient(token=SLACK_BOT_TOKEN)
        socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client_sync)

        def handler(client, req):
            try:
                # Acknowledge immediately
                client.send_socket_mode_response({"envelope_id": req.envelope_id})
                
                # Log raw request
                logging.info(f"📨 Socket Mode Request: type={req.type}")
                
                payload = req.payload or {}
                payload_type = payload.get("type")
                
                # Accept both "events_api" and "event_callback" 
                if payload_type in ["events_api", "event_callback"]:
                    event = payload.get("event", {})
                    process_slack_event_socket_mode(event, web_client_sync.token)
                else:
                    logging.info(f"   ↳ Payload type: {payload_type} (not an event)")
                    
            except Exception as e:
                logging.error(f"SocketMode handler error: {e}", exc_info=True)

        socket_client.socket_mode_request_listeners.append(handler)
        
        # Connect in separate thread
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
            logging.info("✅ Socket Mode is ACTIVE and listening for events!")
            return True
        else:
            logging.error("❌ Socket Mode failed to connect")
            return False
            
    except Exception as e:
        logging.error(f"❌ Socket Mode initialization error: {e}", exc_info=True)
        return False

# --- 6. MAIN EXECUTION ---

async def main():
    global main_loop, aiohttp_session, sent_alerts, slack_bot_user_id
    
    sent_alerts = load_sent_alerts()
    main_loop = asyncio.get_running_loop()
    
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
                logging.info("🎧 Socket Mode is LISTENING for Slack events")
                logging.info("   Send a message in Slack channel C09M0APC6DT to test")
                logging.info("=" * 70)
            else:
                logging.error("❌ Socket Mode failed")
        except Exception as e:
            logging.error(f"❌ Socket Mode error: {e}", exc_info=True)

        await asyncio.gather(
            refresh_server_task, 
            startup_scan_task, 
            telethon_client.run_until_disconnected()
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down the Telegram bridge.")