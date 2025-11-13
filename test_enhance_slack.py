import logging
from gemini_enhance import enhance_message

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('enhance_test.log'),
        logging.StreamHandler()
    ]
)

# Example Slack message simulation
def simulate_slack_enhancement(chat_history, raw_draft):
    logging.info('Simulating Slack enhancement...')
    logging.info(f'Chat history: {chat_history}')
    logging.info(f'Raw draft: {raw_draft}')
    enhanced = enhance_message(chat_history, raw_draft)
    logging.info(f'Enhanced message: {enhanced}')
    print('Enhanced message:', enhanced)
    return enhanced

if __name__ == "__main__":
    # Simulate a Slack event
    chat_history = "Client: Can you update me on the project?\nMe: Sure, I will send an update soon."
    raw_draft = "Project is on track, will share details."
    simulate_slack_enhancement(chat_history, raw_draft)
