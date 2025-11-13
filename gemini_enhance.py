import google.generativeai as genai

API_KEY = "AIzaSyA6PymEguEq_HpR1enCnDJORNZkQsXR51E"

MASTER_PROMPT = """
YOUR TASK:
Please take my notes and the prior chat. Write me a complete, ready-to-send message for the client.
The message should always:
    •   Be polite, professional, friendly, and respectful.
    •   Use clear, natural, human language.
    •   Be effortlessly engaging and approachable.
    •   Be simple and conversational.
    •   Vary sentence lengths naturally (short and long mixed).
    •   Use common idioms or informal fillers (like “just,” “honestly,” “you know”) sparingly and authentically.
    •   Limit and casually explain any professional jargon.
    •   Show that I am managing coordination between client and dev smoothly.
    •   Include details about timelines, milestones, deliverables, or next steps if needed.
    •   Address any questions or issues from the previous chat.
    •   Add anything helpful so the client feels informed and supported.
    •   Include a polite, natural closing.
    •   Keep the style spontaneous, dynamic, and authentically human.
    •   Maintain a conversational tone—even when explaining technical topics.
    •   Do not use emojis.
IMPORTANT RULES:
    •   Be concise and to the point. Aim to match the length and complexity of my raw draft.
    •   Directly address the core question or statement from my draft first, before adding any extra detail.
    •   Do not over-explain unless my draft specifically asks for a detailed explanation.
    •   Do not include any introductory or framing lines that talk about delivering the message, such as “Sure! Here’s your client-ready message,” or anything similar.
    •   Do not include any closing lines or sign-offs like “Cheers,” “Regards,” “Thanks,” or my name.
    •   The output should be only the client-ready message itself—clean, direct, and ready to paste straight into the chat thread without any added explanation or wrapper.
"""

def enhance_message(chat_history, raw_draft, master_prompt=MASTER_PROMPT, api_key=API_KEY):
    """
    Calls the Google Gemini API to enhance a client message based on chat history and a raw draft.
    Returns the enhanced message as a string, or an error message if the API call fails.
    """
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-pro-latest')
        final_prompt = (
            f"{master_prompt}\n\n---\nHi, I want to prepare a client message. Below I’m sharing:\n\n"
            f"1. Previous chat with the client:\n---\n{chat_history}\n---\n\n"
            f"2. My raw draft or bullet points of what I want to say next:\n---\n{raw_draft}\n---"
        )
        response = model.generate_content(final_prompt)
        return response.text.strip()
    except Exception as e:
        return f"Sorry, I couldn't enhance the message using Gemini. Error: {e}"

if __name__ == "__main__":
    # Example usage for standalone testing
    chat_history = "Client: Hi, can you send the report?\nMe: Sure, I will send it by EOD."
    raw_draft = "I'll send the report soon."
    result = enhance_message(chat_history, raw_draft)
    print("Enhanced message:\n", result)
