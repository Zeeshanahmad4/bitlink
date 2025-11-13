import os
import logging
from flask import Flask, request, jsonify
from gemini_enhance import enhance_message

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('enhance_slack_webhook.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)

@app.route('/slack/enhance', methods=['POST'])
def slack_enhance():
    # Slack sends data as form-urlencoded
    user = request.form.get('user_name', 'unknown')
    text = request.form.get('text', '')
    channel = request.form.get('channel_name', 'unknown')
    logging.info(f"Received /enhance from Slack user={user} channel={channel} text={text}")

    # For demo, treat the text as the raw draft, and use a placeholder chat history
    chat_history = "Client: Please update me.\nMe: Sure, will do."
    raw_draft = text
    enhanced = enhance_message(chat_history, raw_draft)
    logging.info(f"Enhanced message: {enhanced}")

    # Respond to Slack (ephemeral message)
    return jsonify({
        "response_type": "ephemeral",
        "text": f"*Enhanced message:*\n{enhanced}"
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"Starting Slack enhance webhook on port {port}")
    app.run(host="0.0.0.0", port=port)
