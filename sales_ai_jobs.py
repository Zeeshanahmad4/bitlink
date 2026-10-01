import time
import logging
import schedule
import argparse
import db_store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)

def run_weekly_stage_inference():
    """
    Scans the open leads and uses AI to infer the new stage.
    """
    logging.info("Starting Weekly Stage AI inference job...")
    
    try:
        records = db_store.get_sales_leads()
        for idx, lead in enumerate(records, start=2):
            stage = str(lead.get("stage", "")).strip().lower()
            slack_channel_id = str(lead.get("slack_channel_id", "")).strip()
            
            # Skip if closed or no slack channel
            if stage in ["won", "lost"] or not slack_channel_id:
                continue
                
            logging.info(f"Processing lead '{lead.get('client_name')}' in channel {slack_channel_id} (current stage: {stage})")
            
            # In a real scenario, this is where we would:
            # 1. Pull the last 50 messages from Slack
            # 2. Call Gemini API to infer the next stage
            # 3. Update the stage in PostgreSQL and post a message to Slack
            
            # Simulated delay for rate limit protection
            time.sleep(2)
            
        logging.info("Finished Weekly Stage AI inference job.")
    except Exception as e:
        logging.error(f"Error during Weekly Stage AI inference: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sales AI Background Jobs")
    parser.add_argument("--run-now", action="store_true", help="Run the jobs immediately instead of waiting for the schedule")
    args = parser.parse_args()
    
    if args.run_now:
        run_weekly_stage_inference()
    else:
        # Schedule the job to run every week (e.g., Sunday at 2:00 AM)
        schedule.every().sunday.at("02:00").do(run_weekly_stage_inference)
        logging.info("Scheduled Weekly Stage AI to run every Sunday at 02:00")
        
        while True:
            schedule.run_pending()
            time.sleep(60)
