# g_sheets_client.py
import gspread
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s')
CREDENTIALS_FILE = 'credentials/service_account.json'
SPREADSHEET_NAME = 'BitLink Client Mappings'

def get_client_mappings(platform: str) -> list:
    logging.info(f"Contacting the records department (Google Sheets) for '{platform}' client list...")
    try:
        gc = gspread.service_account(filename=CREDENTIALS_FILE)
        spreadsheet = gc.open(SPREADSHEET_NAME)
        worksheet = spreadsheet.sheet1
        all_clients = worksheet.get_all_records()
        platform_clients = [
            client for client in all_clients 
            if client.get('platform', '').lower() == platform.lower()
        ]
        if not platform_clients:
            logging.warning(f"No client files found for '{platform}' in the records.")
            return []
        logging.info(f"Successfully received {len(platform_clients)} client file(s) for {platform}.")
        for client in platform_clients:
            client['external_id'] = str(client['external_id'])
        return platform_clients
    except Exception as e:
        logging.error(f"FATAL: A critical error occurred while accessing the records department: {e}")
        return []