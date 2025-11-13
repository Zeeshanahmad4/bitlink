import google.generativeai as genai
import os

def test_gemini_enhance():
    # Hardcoded Gemini API key (replace with your actual key if needed)
    api_key = "AIzaSyA6PymEguEq_HpR1enCnDJORNZkQsXR51E"
    genai.configure(api_key=api_key)
    
    MASTER_PROMPT = """
    Please rewrite the following message in a more professional and client-friendly tone.
    """
    chat_history = "Client: Hi, can you send the report?\nMe: Sure, I will send it by EOD."
    raw_draft = "I'll send the report soon."
    final_prompt = f"{MASTER_PROMPT}\n\n---\nChat history:\n{chat_history}\n---\nDraft:\n{raw_draft}\n---"
    try:
        model = genai.GenerativeModel('gemini-pro-latest')
        response = model.generate_content(final_prompt)
        print("Gemini API call succeeded!\n---\nResponse:\n", response.text.strip())
    except Exception as e:
        print("Gemini API call failed:", e)

if __name__ == "__main__":
    test_gemini_enhance()
