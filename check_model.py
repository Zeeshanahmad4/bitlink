# check_models.py - A temporary script to find your available Gemini models

import os
import google.generativeai as genai
from dotenv import load_dotenv

# Load your .env file to get the key
load_dotenv()
GEMINI_API_KEY = "AIzaSyA6PymEguEq_HpR1enCnDJORNZkQsXR51E"

# Or, if you want to test with the hardcoded key, uncomment the line below
# GEMINI_API_KEY = "your-gemini-api-key-here"

print("--- Finding available Gemini models... ---")

try:
    genai.configure(api_key=GEMINI_API_KEY)

    print("Models that support the 'generateContent' method:")
    for m in genai.list_models():
      if 'generateContent' in m.supported_generation_methods:
        print(f"  - {m.name}")

except Exception as e:
    print(f"\nAn error occurred: {e}")

print("\n--- Script finished. ---")