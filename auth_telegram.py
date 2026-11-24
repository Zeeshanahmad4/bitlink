#!/usr/bin/env python3
"""
Interactive Telegram Authentication Helper for BitLink.
Runs a synchronous Telethon client to prompt for Phone Number, SMS/App Code, and optional 2FA password.
Saves the session file directly to sessions/bitlink_telegram.session for tg-bridge.
"""
import os
import sys
from pathlib import Path
from telethon.sync import TelegramClient

def main():
    api_id_val = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    sessions_dir = os.getenv("SESSIONS_DIR", "sessions")
    session_name = os.getenv("SESSION_NAME", "bitlink_telegram")

    if not api_id_val or not api_hash:
        print("❌ Error: API_ID or API_HASH environment variables are not set in .env")
        sys.exit(1)

    try:
        api_id = int(api_id_val)
    except ValueError:
        print(f"❌ Error: API_ID must be an integer, got: {api_id_val}")
        sys.exit(1)

    Path(sessions_dir).mkdir(parents=True, exist_ok=True)
    session_path = os.path.join(sessions_dir, session_name)

    print("=" * 60)
    print("🚀 BitLink Telegram Interactive Authentication")
    print(f"📁 Session file path: {session_path}.session")
    print("=" * 60)

    with TelegramClient(session_path, api_id, api_hash) as client:
        client.start()
        me = client.get_me()
        if me:
            print("\n" + "=" * 60)
            print(f"🎉 Successfully authorized Telegram account!")
            print(f"👤 Name  : {me.first_name} {me.last_name or ''}".strip())
            print(f"📱 Phone : +{me.phone}")
            print(f"🆔 Username: @{me.username}" if me.username else "🆔 Username: (None)")
            print("💾 Session file saved successfully.")
            print("=" * 60)
        else:
            print("⚠️ Connected, but could not retrieve account info.")

if __name__ == "__main__":
    main()
