# g_sheets_client.py
import gspread
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s')
CREDENTIALS_FILE = 'credentials/service_account.json'
SPREADSHEET_NAME = 'BitLink Client Mappings'

# In g_sheets_client.py, REPLACE the function with this:

def get_client_mappings(platform: str, match_startswith: bool = False) -> list:
    """
    Retrieves client mappings from Google Sheets.

    Args:
        platform (str): The platform to filter by (e.g., "Discord", "WhatsApp").
        match_startswith (bool): If True, matches platforms that start with the
                                 given string. Defaults to False (exact match).
    """
    logging.info(f"Contacting Google Sheets for '{platform}' client list (match_startswith={match_startswith})...")
    try:
        gc = gspread.service_account(filename=CREDENTIALS_FILE)
        spreadsheet = gc.open(SPREADSHEET_NAME)
        worksheet = spreadsheet.sheet1
        all_clients = worksheet.get_all_records()

        # --- THIS IS THE NEW, SMARTER LOGIC ---
        if match_startswith:
            # Find all clients where the platform starts with the given string
            platform_clients = [
                client for client in all_clients
                if client.get('platform', '').lower().startswith(platform.lower())
            ]
        else:
            # The original logic for an exact match
            platform_clients = [
                client for client in all_clients
                if client.get('platform', '').lower() == platform.lower()
            ]
        # --- END OF NEW LOGIC ---

        if not platform_clients:
            logging.warning(f"No client files found for '{platform}' in the records.")
            return []
            
        logging.info(f"Successfully received {len(platform_clients)} client file(s) for {platform}.")
        for client in platform_clients:
            client['external_id'] = str(client['external_id'])
        return platform_clients
    except Exception as e:
        logging.error(f"FATAL: A critical error occurred while accessing Google Sheets: {e}")
        return []