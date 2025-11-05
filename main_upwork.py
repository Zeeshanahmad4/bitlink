# main_upwork.py

import os
import sys
import logging
import imaplib
import email
import smtplib
import time
import re
from email.header import decode_header
from dotenv import load_dotenv
from slack_sdk.web import WebClient
from slack_sdk.errors import SlackApiError

# It's good practice to import your own modules last
from g_sheets_client import get_client_mappings

# --- Environment Setup ---
load_dotenv()

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- Load Configuration from .env ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
UPWORK_EMAIL_ADDRESS = os.getenv("UPWORK_EMAIL_ADDRESS")
UPWORK_EMAIL_PASSWORD = os.getenv("UPWORK_EMAIL_PASSWORD")
UPWORK_IMAP_SERVER = os.getenv("UPWORK_IMAP_SERVER")
UPWORK_SMTP_SERVER = os.getenv("UPWORK_SMTP_SERVER")
POLL_INTERVAL = int(os.getenv("UPWORK_POLL_INTERVAL", 30))

# --- Global State & Clients ---
# This dictionary is the "brain" that links a Slack message to an email for replying.
# Key: Slack message timestamp (ts) | Value: { 'message_id': '...', 'reply_to': '...', 'subject': '...' }
slack_ts_to_email_map = {}

# Initialize the Slack client
try:
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    # Verify the token is valid
    auth_test = slack_client.auth_test()
    logging.info(f"Successfully connected to Slack as bot '{auth_test['user']}'")
except SlackApiError as e:
    logging.critical(f"FATAL: Could not connect to Slack. Check your SLACK_BOT_TOKEN. Error: {e.response['error']}")
    sys.exit(1)

# --- Main Functions (will be filled in next) ---

# In main_upwork.py
# REPLACE the empty poll_upwork_emails function with this one

def poll_upwork_emails(upwork_mappings):
    """Connects to IMAP, fetches unread Upwork messages, and forwards to Slack."""
    logging.info("Polling for new Upwork emails...")
    
    try:
        # --- 1. Connect to IMAP server ---
        mail = imaplib.IMAP4_SSL(UPWORK_IMAP_SERVER)
        mail.login(UPWORK_EMAIL_ADDRESS, UPWORK_EMAIL_PASSWORD)
        mail.select("inbox")
        logging.info("IMAP connection successful.")

        # --- 2. Search for relevant unread emails ---
        # We search for emails that are UNSEEN. For testing, the FROM part is commented out.
        # In production, you would uncomment it: '(UNSEEN FROM "messages@upwork.com")'
        status, messages = mail.search(None, '(UNSEEN)')
        if status != "OK":
            logging.error("Failed to search inbox.")
            mail.logout()
            return
            
        email_ids = messages[0].split()
        if not email_ids:
            logging.info("No new unread emails found.")
            mail.logout()
            return

        logging.info(f"Found {len(email_ids)} new unread email(s).")

        # --- 3. Process each email ---
        for email_id in email_ids:
            status, data = mail.fetch(email_id, '(RFC822)')
            if status != 'OK':
                logging.error(f"Failed to fetch email ID {email_id}")
                continue

            msg = email.message_from_bytes(data[0][1])

            # Decode subject and from headers
            subject, encoding = decode_header(msg["Subject"])[0]
            if isinstance(subject, bytes):
                subject = subject.decode(encoding or 'utf-8')

            from_address = msg.get("From")
            
            # --- 4. Check if it's a message we care about ---
            if "New message from" not in subject:
                logging.info(f"Skipping email with subject: '{subject}' (doesn't match pattern).")
                continue

            # Extract client name from subject
            client_name_match = re.search(r'New message from (.*)', subject)
            if not client_name_match:
                continue
            
            client_name = client_name_match.group(1).strip()
            
            # Find the corresponding Slack channel from our mappings
            client_mapping = next((m for m in upwork_mappings if m['external_id'] == client_name), None)
            
            if not client_mapping:
                logging.warning(f"No Slack channel mapped for Upwork client: '{client_name}'. Skipping.")
                continue
            
            slack_channel_id = client_mapping["slack_channel_id"]

            # --- 5. Extract the email body ---
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                        body = part.get_payload(decode=True).decode('utf-8', 'ignore')
                        break
            else:
                body = msg.get_payload(decode=True).decode('utf-8', 'ignore')

            if not body:
                logging.warning(f"Could not extract a plain text body from email for '{client_name}'. Skipping.")
                continue

            # --- 6. Post to Slack ---
            try:
                message_text = f"📨 *New Upwork Message from {client_name}*\n\n>>> {body}"
                slack_response = slack_client.chat_postMessage(
                    channel=slack_channel_id,
                    text=message_text
                )
                
                # --- 7. Save metadata for replying (CRUCIAL) ---
                slack_ts = slack_response['ts']
                reply_to_address = msg.get("Reply-To") or from_address
                message_id_header = msg.get("Message-ID")
                
                slack_ts_to_email_map[slack_ts] = {
                    'message_id': message_id_header,
                    'reply_to': reply_to_address,
                    'subject': subject
                }
                logging.info(f"Successfully forwarded message from '{client_name}' to Slack channel {slack_channel_id}.")
                logging.info(f"Stored reply info for Slack message ts: {slack_ts}")

                # Mark the email as read so we don't process it again
                mail.store(email_id, '+FLAGS', '\\Seen')

            except SlackApiError as e:
                logging.error(f"Failed to post message to Slack for '{client_name}': {e.response['error']}")
        
        mail.logout()
        logging.info("IMAP connection closed.")

    except Exception as e:
        logging.error(f"An error occurred in poll_upwork_emails: {e}", exc_info=True)

def poll_slack_and_forward(upwork_mappings):
    """Checks for replies in Slack threads and sends them as emails."""
    # We will write the code for this function later.
    pass

# --- Main Execution Loop ---

def main():
    """The main loop that runs the bridge."""
    if not all([UPWORK_EMAIL_ADDRESS, UPWORK_EMAIL_PASSWORD, UPWORK_IMAP_SERVER, UPWORK_SMTP_SERVER]):
        logging.critical("FATAL: One or more Upwork email environment variables are missing.")
        sys.exit(1)
        
    logging.info("Starting BitLink Upwork Bridge...")
    
    while True:
        try:
            # Fetch the latest mappings from Google Sheets at the start of each cycle
            upwork_mappings = get_client_mappings("Upwork")
            if not upwork_mappings:
                logging.warning("No 'Upwork' platform mappings found in Google Sheet. Sleeping.")
            else:
                logging.info(f"Loaded {len(upwork_mappings)} Upwork client mappings.")
                poll_upwork_emails(upwork_mappings)
                poll_slack_and_forward(upwork_mappings)

        except Exception as e:
            logging.error(f"An unexpected error occurred in the main loop: {e}", exc_info=True)
            
        logging.info(f"Sleeping for {POLL_INTERVAL} seconds...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()