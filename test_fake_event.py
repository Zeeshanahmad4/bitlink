import os
from dotenv import load_dotenv
load_dotenv()
from main_whatsapp import process_slack_to_whatsapp, reload_config
import logging
logging.basicConfig(level=logging.INFO)

reload_config()

fake_event = {
    "channel": "C0C5TQ6FSKA",
    "ts": "1790606492.193589",
    "text": "Hi! Thanks for reaching out. Test message from AI."
}

process_slack_to_whatsapp(fake_event, os.getenv("SLACK_BOT_TOKEN"))
