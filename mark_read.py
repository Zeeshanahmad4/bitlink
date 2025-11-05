# mark_all_read.py
# A one-time utility script to mark all emails in the inbox as read.

import os
import logging
import imaplib
from dotenv import load_dotenv

# --- Basic Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()

# --- Load Credentials ---
EMAIL_ADDRESS = os.getenv("UPWORK_EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("UPWORK_EMAIL_PASSWORD")
IMAP_SERVER = os.getenv("UPWORK_IMAP_SERVER")

def mark_all_emails_as_read():
    """Connects to the inbox and marks all messages as read."""
    
    if not all([EMAIL_ADDRESS, EMAIL_PASSWORD, IMAP_SERVER]):
        logging.critical("FATAL: Email environment variables are missing in your .env file.")
        return

    try:
        # --- Connect to the server ---
        logging.info(f"Connecting to {IMAP_SERVER}...")
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        
        logging.info(f"Logging in as {EMAIL_ADDRESS}...")
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        
        # --- Select the inbox ---
        status, messages = mail.select("inbox")
        if status != "OK":
            logging.error("Failed to select the inbox.")
            mail.logout()
            return
            
        total_messages = int(messages[0])
        logging.info(f"Inbox selected. Total messages: {total_messages}")

        # --- Search for ALL emails in the inbox ---
        status, message_ids_raw = mail.search(None, "ALL")
        if status != "OK":
            logging.error("Failed to search for emails.")
            mail.logout()
            return

        # The result is a list of space-separated IDs, convert to a comma-separated string for the store command
        message_ids_str = b",".join(message_ids_raw[0].split()).decode()
        
        if not message_ids_str:
            logging.info("No messages found in the inbox. Nothing to do.")
            mail.logout()
            return

        logging.info(f"Found {len(message_ids_raw[0].split())} emails to mark as read. This may take a moment...")

        # --- Mark all found emails as read ('\Seen') ---
        # Using mail.store() is the standard IMAP way to change flags
        status, response = mail.store(message_ids_str, '+FLAGS', '\\Seen')

        if status == "OK":
            logging.info("Successfully marked all emails as read!")
        else:
            logging.error(f"Failed to mark emails as read. Server response: {response}")

        # --- Clean up ---
        mail.close()
        mail.logout()
        logging.info("Connection closed.")

    except Exception as e:
        logging.error(f"An error occurred: {e}", exc_info=True)


if __name__ == "__main__":
    # Add a confirmation step to prevent accidental runs
    print("🚨 WARNING: This script will mark ALL emails in your inbox as read.")
    print(f"Target account: {EMAIL_ADDRESS}")
    
    user_confirmation = input("Are you sure you want to continue? (yes/no): ")
    
    if user_confirmation.lower() == 'yes':
        mark_all_emails_as_read()
    else:
        print("Operation cancelled by user.")