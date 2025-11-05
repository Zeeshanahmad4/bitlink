import os
from slack_sdk import WebClient
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
web_client = WebClient(token=SLACK_BOT_TOKEN)

try:
    response = web_client.auth_test()
    print("🎯 SUCCESS! Here are your IDs:")
    print(f"Bot User ID: {response.get('user_id')}")  # ← THIS IS WHAT YOU NEED
    print(f"Team ID: {response.get('team_id')}")
    print(f"Team: {response.get('team')}")
    print(f"User: {response.get('user')}")
    print(f"URL: {response.get('url')}")
except Exception as e:
    print(f"❌ Error: {e}")