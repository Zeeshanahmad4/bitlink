# test_slack_download.py

import requests
import os
from dotenv import load_dotenv

# --- CONFIGURATION ---
# Paste the File ID you copied from Slack here
# IMPORTANT: This must be a file your BOT has access to.
# Invite your @BitLink bot to the channel where the file is.
FILE_ID_TO_TEST = "F09GL9ZEEBW" 

# --- SCRIPT LOGIC ---
print("--- Slack File Download Test ---")

# Load the SLACK_BOT_TOKEN from your .env file
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

if not SLACK_BOT_TOKEN:
    print("❌ ERROR: SLACK_BOT_TOKEN not found in .env file. Please check your configuration.")
    exit()

if "YOUR_FILE_ID_HERE" in FILE_ID_TO_TEST:
    print("❌ ERROR: Please replace 'YOUR_FILE_ID_HERE' with a real File ID from Slack.")
    exit()

print(f"Attempting to get information for File ID: {FILE_ID_TO_TEST}")

# Use the files.info API method to get the file's private download URL
# This is the same method our bridge uses implicitly
api_url = "https://slack.com/api/files.info"
headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
params = {"file": FILE_ID_TO_TEST}

try:
    response = requests.get(api_url, headers=headers, params=params, timeout=15)
    
    if response.status_code == 200:
        json_response = response.json()
        
        if json_response.get("ok"):
            print("✅ SUCCESS: Successfully retrieved file info from Slack.")
            
            file_info = json_response.get("file", {})
            download_url = file_info.get("url_private_download")
            filename = file_info.get("name", "downloaded_file")

            if download_url:
                print(f"Attempting to download file: {filename}...")
                
                # Now, try to download the actual file content
                file_response = requests.get(download_url, headers=headers, timeout=30)
                
                if file_response.status_code == 200:
                    # Save the file to prove the download worked
                    with open(filename, "wb") as f:
                        f.write(file_response.content)
                    print("\n========================================================")
                    print(f"✅✅✅ SUCCESS! File '{filename}' downloaded and saved successfully.")
                    print("This confirms your SLACK_BOT_TOKEN is VALID and has permission.")
                    print("========================================================")
                else:
                    print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    print(f"❌ FAILED: Could not download the file content.")
                    print(f"   Slack responded with Status Code: {file_response.status_code}")
                    print(f"   Response Body: {file_response.text}")
                    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            else:
                print("❌ FAILED: File info retrieved, but no 'url_private_download' was provided.")
        else:
            print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print("❌ FAILED: Slack API call was not 'ok'.")
            print(f"   Error from Slack: {json_response.get('error')}")
            print("   This almost always means your SLACK_BOT_TOKEN is invalid or expired.")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    else:
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("❌ FAILED: Could not connect to Slack API.")
        print(f"   Status Code: {response.status_code}")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

except requests.exceptions.RequestException as e:
    print(f"❌ A network error occurred: {e}")