# slack_log_handler.py
import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import os

class SlackLogHandler(logging.Handler):
    def __init__(self, slack_token, channel_id):
        super().__init__()
        self.client = WebClient(token=slack_token)
        self.channel_id = channel_id

    def emit(self, record):
        log_entry = self.format(record)
        try:
            self.client.chat_postMessage(channel=self.channel_id, text=log_entry)
        except SlackApiError as e:
            print(f"Failed to send log to Slack: {e.response['error']}")

def setup_slack_logging():
    SLACK_INFO_CHANNEL = os.getenv("SLACK_INFO_CHANNEL")
    SLACK_ERROR_CHANNEL = os.getenv("SLACK_ERROR_CHANNEL")
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    if SLACK_INFO_CHANNEL and SLACK_BOT_TOKEN:
        info_handler = SlackLogHandler(SLACK_BOT_TOKEN, SLACK_INFO_CHANNEL)
        info_handler.setLevel(logging.INFO)
        info_handler.addFilter(lambda record: record.levelno == logging.INFO)
        info_handler.setFormatter(formatter)
        logging.getLogger().addHandler(info_handler)

    if SLACK_ERROR_CHANNEL and SLACK_BOT_TOKEN:
        error_handler = SlackLogHandler(SLACK_BOT_TOKEN, SLACK_ERROR_CHANNEL)
        error_handler.setLevel(logging.ERROR)
        error_handler.addFilter(lambda record: record.levelno >= logging.ERROR)
        error_handler.setFormatter(formatter)
        logging.getLogger().addHandler(error_handler)