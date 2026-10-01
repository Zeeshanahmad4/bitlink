import os
import sys
import json
import shlex
import logging
import threading
from io import BytesIO
from pathlib import Path
import requests
import db_store
import ai_context
import ai_prompts
from gemini_client import call_gemini_api
from dotenv import load_dotenv
load_dotenv()

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.request import SocketModeRequest

import sales_store

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
MANAGER_BOT_TOKEN = os.getenv("MANAGER_SLACK_BOT_TOKEN")
MANAGER_APP_TOKEN = os.getenv("MANAGER_SLACK_APP_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not MANAGER_BOT_TOKEN or not MANAGER_APP_TOKEN:
    logging.error("Missing MANAGER_SLACK_BOT_TOKEN or MANAGER_SLACK_APP_TOKEN in .env. Exiting.")
    sys.exit(1)

WHATSAPP_REFRESH_PORT = int(os.getenv("WHATSAPP_REFRESH_PORT", 8101))
TELEGRAM_REFRESH_PORT = int(os.getenv("TELEGRAM_REFRESH_PORT", 8003))

from concurrent.futures import ThreadPoolExecutor
thread_pool = ThreadPoolExecutor(max_workers=10)

web_client = WebClient(token=MANAGER_BOT_TOKEN)
try:
    auth_test = web_client.auth_test()
    slack_bot_user_id = auth_test["user_id"]
    logging.info(f"Manager Bot User ID: {slack_bot_user_id}")
except SlackApiError as e:
    logging.error(f"Error authenticating with Slack: {e}")
    sys.exit(1)

# Function to trigger refresh on all apps
def trigger_refresh_all_apps():
    for port in [WHATSAPP_REFRESH_PORT, TELEGRAM_REFRESH_PORT]:
        try:
            requests.post(f"http://127.0.0.1:{port}/refresh", json={}, timeout=2)
            logging.info(f"Triggered refresh on port {port}")
        except Exception as e:
            logging.warning(f"Failed to refresh port {port}: {e}")

def handle_slack_slash_commands(client: SocketModeClient, req, web_client: WebClient):
    """Unified handler for all Slack slash commands with instant (< 5ms) SocketModeResponse ACK."""
    if req.type != "slash_commands":
        return

    logging.info(f"👉 RECEIVED SLASH COMMAND PAYLOAD: {req.payload.get('command')}")

    # 1. Instantly ACK envelope back to Slack (< 5ms) to prevent any timeout
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    payload = req.payload or {}
    command = payload.get("command")
    trigger_id = payload.get("trigger_id")
    channel_id = payload.get("channel_id")
    user_id = payload.get("user_id")
    response_url = payload.get("response_url")
    text = (payload.get("text") or "").strip()

    # --- COMMAND 1: /claim ---
    if command == "/claim":
        def run_claim():
            try:
                sales_store.update_lead_owner_by_channel(channel_id=channel_id, new_owner_slack_id=user_id)
                web_client.chat_postMessage(
                    channel=channel_id,
                    text=f"🎯 *Lead Claimed:* <@{user_id}> is now the primary owner of this lead."
                )
                if response_url:
                    requests.post(response_url, json={"response_type": "ephemeral", "text": "✅ You have successfully claimed this lead."}, timeout=3)
            except Exception as e:
                logging.error(f"Error executing /claim: {e}")
                if response_url:
                    requests.post(response_url, json={"response_type": "ephemeral", "text": f"❌ Error claiming lead: {e}"}, timeout=3)
        thread_pool.submit(run_claim)

    # --- COMMAND 2: /handoff ---
    elif command == "/handoff":
        try:
            pool = sales_store.get_sales_pool(active_only=True)
            options = []
            for rep in pool:
                rep_id = rep.get("slack_user_id")
                rep_name = rep.get("name") or rep_id
                if rep_id:
                    options.append({
                        "text": {"type": "plain_text", "text": f"{rep_name} (@{rep_id})", "emoji": True},
                        "value": rep_id
                    })

            rep_picker_element = {
                "type": "static_select",
                "action_id": "selected_rep",
                "placeholder": {"type": "plain_text", "text": "Select sales team member", "emoji": True},
                "options": options[:100]
            } if options else {
                "type": "users_select",
                "action_id": "selected_rep",
                "placeholder": {"type": "plain_text", "text": "Select user from workspace", "emoji": True}
            }

            modal_view = {
                "type": "modal",
                "callback_id": "handoff_modal_submission",
                "private_metadata": f"{channel_id}|",
                "title": {"type": "plain_text", "text": "Handoff Lead", "emoji": True},
                "submit": {"type": "plain_text", "text": "Transfer Ownership", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Transfer ownership of this lead to another sales rep. Both of you will be notified in the channel."
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "rep_select_block",
                        "label": {"type": "plain_text", "text": "New Primary Owner", "emoji": True},
                        "element": rep_picker_element
                    },
                    {
                        "type": "input",
                        "block_id": "note_block",
                        "optional": True,
                        "label": {"type": "plain_text", "text": "Handover Note (Optional)", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "handoff_note",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "e.g. Needs pricing quotation or follow-up call...", "emoji": True}
                        }
                    }
                ]
            }
            web_client.views_open(trigger_id=trigger_id, view=modal_view)
        except Exception as e:
            logging.error(f"Error opening handoff modal from command: {e}")

    # --- COMMAND 3: /suggest ---
    elif command == "/suggest":
        try:
            loading_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "✨ AI Suggest Reply", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ *Reading conversation context & drafting best sales reply with Gemini...*\nPlease wait a moment."
                        }
                    }
                ]
            }
            open_resp = web_client.views_open(trigger_id=trigger_id, view=loading_modal)
            view_id = open_resp.get("view", {}).get("id")
            if view_id:
                thread_pool.submit(
                    async_generate_and_update_suggest_modal,
                    view_id,
                    channel_id,
                    slack_bot_user_id,
                    web_client
                )
        except Exception as e:
            logging.error(f"Error opening suggest modal from command: {e}")

    # --- COMMAND 4: /summary ---
    elif command == "/summary":
        try:
            summary_loading_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "📝 Deal Summary", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ *Analyzing conversation history and synthesizing deal brief...*\nPlease wait a moment."
                        }
                    }
                ]
            }
            open_resp = web_client.views_open(trigger_id=trigger_id, view=summary_loading_modal)
            view_id = open_resp.get("view", {}).get("id")
            if view_id:
                thread_pool.submit(
                    async_generate_and_update_summary_modal,
                    view_id,
                    channel_id,
                    slack_bot_user_id,
                    web_client
                )
        except Exception as e:
            logging.error(f"Error opening summary modal from command: {e}")

    # --- COMMAND 5: /reply-with-ai ---
    elif command == "/reply-with-ai":
        try:
            loading_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "✨ AI Suggest Reply", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ *Reading conversation context & drafting best sales reply with Gemini...*\nPlease wait a moment."
                        }
                    }
                ]
            }
            open_resp = web_client.views_open(trigger_id=trigger_id, view=loading_modal)
            view_id = open_resp.get("view", {}).get("id")
            if view_id:
                thread_pool.submit(
                    async_generate_and_update_suggest_modal,
                    view_id,
                    channel_id,
                    slack_bot_user_id,
                    web_client
                )
        except Exception as e:
            logging.error(f"Error opening AI suggest loading modal: {e}")

    # --- COMMAND 6: /sales-snippet or /snippet-sales ---
    elif command in ["/sales-snippet", "/snippet-sales"]:
        try:
            loading_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Insert Snippet", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ *Fetching your sales snippets...*\nPlease wait a moment."
                        }
                    }
                ]
            }
            open_resp = web_client.views_open(trigger_id=trigger_id, view=loading_modal)
            view_id = open_resp.get("view", {}).get("id")
            if view_id:
                def async_load_snippets():
                    try:
                        snippets = sales_store.get_snippets()
                        if not snippets:
                            web_client.views_update(
                                view_id=view_id,
                                view={
                                    "type": "modal",
                                    "title": {"type": "plain_text", "text": "Insert Snippet", "emoji": True},
                                    "close": {"type": "plain_text", "text": "Close", "emoji": True},
                                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "No snippets available. Add some in the BitLink Dashboard."}}]
                                }
                            )
                            return

                        options = []
                        for s in snippets:
                            options.append({
                                "text": {"type": "plain_text", "text": str(s.get("title", ""))[:75], "emoji": True},
                                "value": str(s.get("id"))
                            })
                        
                        modal_view = {
                            "type": "modal",
                            "callback_id": "snippet_modal_submission",
                            "private_metadata": f"{channel_id}|",
                            "title": {"type": "plain_text", "text": "Insert Snippet", "emoji": True},
                            "submit": {"type": "plain_text", "text": "Send to Client", "emoji": True},
                            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                            "blocks": [
                                {
                                    "type": "input",
                                    "block_id": "snippet_select_block",
                                    "label": {"type": "plain_text", "text": "Select a Sales Snippet", "emoji": True},
                                    "element": {
                                        "type": "static_select",
                                        "action_id": "selected_snippet",
                                        "placeholder": {"type": "plain_text", "text": "Choose a snippet...", "emoji": True},
                                        "options": options[:100]
                                    }
                                }
                            ]
                        }
                        web_client.views_update(view_id=view_id, view=modal_view)
                    except Exception as e:
                        logging.error(f"Error updating snippet modal: {e}")
                
                thread_pool.submit(async_load_snippets)
        except Exception as e:
            logging.error(f"Error opening snippet loading modal: {e}")

    # --- LEGACY ADMIN COMMANDS: /add-client, /pause-channel, /resume-channel ---
    elif command == "/add-client":
        def run_add_client():
            try:
                parts = shlex.split(text)
                if len(parts) != 4:
                    raise ValueError("Invalid format. Use: /add-client [platform] \"[Client Name]\" [external_id] [slack_channel_id]")
                platform, client_name, external_id, target_ch = parts
                db_store.add_client_mapping(platform, client_name, external_id, target_ch)
                trigger_refresh_all_apps()
                if response_url:
                    requests.post(response_url, json={"response_type": "in_channel", "text": f"✅ New client mapping added: {client_name} ({platform})"}, timeout=3)
            except Exception as e:
                logging.error(f"Error in /add-client: {e}")
                if response_url:
                    requests.post(response_url, json={"response_type": "ephemeral", "text": f"❌ Error: {e}"}, timeout=3)
        thread_pool.submit(run_add_client)

    elif command in ["/pause-channel", "/resume-channel"]:
        is_pause = (command == "/pause-channel")
        def run_toggle():
            try:
                target_ch = text or channel_id
                if db_store.set_client_paused(target_ch, is_pause):
                    trigger_refresh_all_apps()
                    status_text = "paused" if is_pause else "resumed"
                    if response_url:
                        requests.post(response_url, json={"response_type": "ephemeral", "text": f"✅ Channel {target_ch} {status_text}."}, timeout=3)
                elif response_url:
                    requests.post(response_url, json={"response_type": "ephemeral", "text": f"❌ Channel ID {target_ch} not found."}, timeout=3)
            except Exception as e:
                logging.error(f"Error in {command}: {e}")
                if response_url:
                    requests.post(response_url, json={"response_type": "ephemeral", "text": f"❌ Error: {e}"}, timeout=3)
        thread_pool.submit(run_toggle)


def handle_modal_submission(client: SocketModeClient, req, web_client: WebClient):
    """Handles modal submissions (Enhance message and Handoff lead)."""
    if req.type != "interactive":
        return

    payload = req.payload
    if payload.get("type") != "view_submission":
        return

    callback_id = payload.get("view", {}).get("callback_id")

    # 1. Enhance Modal Submission
    if callback_id == "enhance_modal_submission":
        client.send_socket_mode_response({"envelope_id": req.envelope_id})
        channel_id = payload.get("view", {}).get("private_metadata", "")
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        final_message = view_state.get("enhanced_text_block", {}).get("enhanced_text_input", {}).get("value")

        if not channel_id or not final_message:
            logging.error("Could not extract channel_id or message from modal submission.")
            return

        try:
            web_client.chat_postMessage(
                channel=channel_id,
                text=final_message
            )
            logging.info(f"Successfully posted enhanced message to channel {channel_id}")
        except Exception as e:
            logging.error(f"Failed to post message from modal to {channel_id}: {e}")

    # 2. Handoff Lead Modal Submission
    elif callback_id == "handoff_modal_submission":
        client.send_socket_mode_response({"envelope_id": req.envelope_id})
        
        user = payload.get("user", {})
        from_user_id = user.get("id")
        
        # Extract private metadata: format "channel_id|external_id"
        metadata = payload.get("view", {}).get("private_metadata", "")
        meta_parts = metadata.split("|")
        channel_id = meta_parts[0] if len(meta_parts) > 0 else ""
        external_id = meta_parts[1] if len(meta_parts) > 1 else ""

        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        
        # Extract selected rep
        rep_select_block = view_state.get("rep_select_block", {}).get("selected_rep", {})
        new_owner_id = rep_select_block.get("selected_user") or (rep_select_block.get("selected_option") or {}).get("value")

        # Extract optional note
        note_block = view_state.get("note_block", {}).get("handoff_note", {})
        note_text = (note_block.get("value") or "").strip()

        if not channel_id or not new_owner_id:
            logging.error(f"Missing channel_id ({channel_id}) or new_owner_id ({new_owner_id}) in handoff submission.")
            return

        # Update CRM sheet
        sales_store.update_lead_owner_by_channel(
            channel_id=channel_id,
            new_owner_slack_id=new_owner_id,
            external_id=external_id
        )

        # Notify channel
        try:
            msg_text = f"🤝 *Lead Handed Off:* <@{from_user_id}> transferred ownership of this lead to <@{new_owner_id}>."
            if note_text:
                msg_text += f"\n📝 *Handover Note:* _{note_text}_"
            
            web_client.chat_postMessage(
                channel=channel_id,
                text=msg_text
            )
            logging.info(f"Posted handoff transfer confirmation to channel {channel_id} (New Owner: {new_owner_id})")
        except Exception as e:
            logging.error(f"Failed to post handoff notification to {channel_id}: {e}")

    # 3. AI Suggest Reply Modal Submission (Send Draft)
    elif callback_id == "ai_suggest_modal_submission":
        client.send_socket_mode_response({"envelope_id": req.envelope_id})
        channel_id = payload.get("view", {}).get("private_metadata", "")
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        draft_block = view_state.get("draft_text_block", {}).get("ai_draft_input", {})
        final_message = draft_block.get("value")

        if not channel_id or not final_message:
            logging.error("Could not extract channel_id or message from AI suggest submission.")
            return

        try:
            post_resp = web_client.chat_postMessage(
                channel=channel_id,
                text=final_message
            )
            logging.info(f"Successfully posted AI suggested reply to channel {channel_id}")

            # Explicitly relay this AI-approved message to WhatsApp
            user_obj = payload.get("user", {})
            user_id = user_obj.get("id", "")
            slack_ts = post_resp.get("ts")
            synthetic_event = {
                "channel": channel_id,
                "ts": slack_ts,
                "text": final_message,
                "user": user_id,
                "files": []
            }
            logging.info(f"📤 [AI POST] Manager Bot posted AI reply to {channel_id}. Bridge apps will auto-forward it.")
        except Exception as e:
            logging.error(f"Failed to post/relay AI message to {channel_id}: {e}")

    # 4. Snippet Modal Submission (Send Snippet)
    elif callback_id == "snippet_modal_submission":
        client.send_socket_mode_response({"envelope_id": req.envelope_id})
        metadata = payload.get("view", {}).get("private_metadata", "")
        channel_id = metadata.split("|")[0] if metadata else ""
        view_state = payload.get("view", {}).get("state", {}).get("values", {})
        
        selected = view_state.get("snippet_select_block", {}).get("selected_snippet", {}).get("selected_option", {})
        row_idx_str = selected.get("value")
        
        if not channel_id or not row_idx_str:
            return
            
        try:
            row_idx = int(row_idx_str)
            snippets = sales_store.get_snippets()
            snippet_content = ""
            for s in snippets:
                if s.get("id") == row_idx:
                    snippet_content = s.get("content")
                    break
                    
            if not snippet_content:
                logging.error(f"Snippet at row {row_idx} not found.")
                return
                
            web_client.chat_postMessage(
                channel=channel_id,
                text=snippet_content
            )
            logging.info(f"Successfully posted Snippet to channel {channel_id}")
        except Exception as e:
            logging.error(f"Failed to send snippet to {channel_id}: {e}")


def generate_sales_reply_ai(chat_history: str, raw_draft: str = "") -> str:
    """Generates a warm, natural, ready-to-send reply using Gemini.

    ``chat_history`` is the labelled transcript from ``ai_context``; every
    line is tagged CLIENT or SALES REP, so the prompt can tell the model which
    side of the conversation it is writing for.
    """
    try:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured")

        if not chat_history.strip():
            chat_history = "CLIENT (the client): (Inbound inquiry just started)"

        prompt = ai_prompts.build_reply_prompt(chat_history, raw_draft)

        reply = call_gemini_api(prompt, timeout=30)
        if reply:
            return reply

        return "Hi! Thanks for reaching out. How can we help you get started with this today?"
    except Exception as e:
        logging.error(f"Gemini sales reply generation failed: {e}")
        return "Hi! Thanks for reaching out. How can we help you get started with this today?"


def format_sales_chat_history(messages, bot_user_id, client_names=()):
    """Extracts genuine conversation messages and labels each speaker.

    Kept as a thin wrapper so existing callers keep working; the speaker
    detection itself lives in ``ai_context``, where it is covered by tests.
    """
    formatted = ai_context.build_transcript(messages, client_names=client_names)
    logging.info(f"📜 [AI CONTEXT] Formatted transcript for Gemini:\n{formatted}")
    return formatted


def get_client_names_for_channel(channel_id: str) -> list:
    """Best-effort lookup of this channel's client display name(s)."""
    try:
        for platform in ("WhatsApp", "Telegram"):
            for item in db_store.get_client_mappings(platform) or []:
                if item.get("slack_channel_id") == channel_id and item.get("client_name"):
                    return [item["client_name"]]
    except Exception as e:
        logging.debug(f"No client mapping for {channel_id}: {e}")
    return []


def build_channel_transcript(web_client, channel_id: str, name_cache=None) -> str:
    """Fetch the whole channel and return it as a labelled transcript."""
    messages = ai_context.fetch_conversation(web_client, channel_id)
    return ai_context.build_transcript(
        messages,
        client_names=get_client_names_for_channel(channel_id),
        name_cache=name_cache,
    )


def async_generate_and_update_suggest_modal(view_id: str, channel_id: str, bot_user_id: str, web_client: WebClient):
    """Background worker: fetches chat history, generates AI draft, and updates modal view."""
    try:
        # Fetch the whole channel, not a 15-message slice.
        name_cache = ai_context._UserNameCache(web_client)
        formatted_history = build_channel_transcript(web_client, channel_id, name_cache=name_cache)

        draft = generate_sales_reply_ai(formatted_history)

        updated_view = {
            "type": "modal",
            "callback_id": "ai_suggest_modal_submission",
            "private_metadata": channel_id,
            "title": {"type": "plain_text", "text": "✨ AI Suggest Reply", "emoji": True},
            "submit": {"type": "plain_text", "text": "Send to Channel", "emoji": True},
            "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🤖 *AI Draft Generated from Recent Conversation:*\nReview and edit the message below before sending. Clicking *Send to Channel* will post it."
                    }
                },
                {
                    "type": "input",
                    "block_id": "draft_text_block",
                    "label": {"type": "plain_text", "text": "Reply Message", "emoji": True},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "ai_draft_input",
                        "multiline": True,
                        "initial_value": draft
                    }
                }
            ]
        }

        web_client.views_update(
            view_id=view_id,
            view=updated_view
        )
        logging.info(f"Successfully updated AI Suggest modal with draft for channel {channel_id}")
    except Exception as e:
        logging.error(f"Failed to update AI suggest modal: {e}")


def generate_chat_summary_ai(chat_history: str) -> str:
    """Generates a concise 3-bullet executive summary of the lead's conversation so far."""
    try:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured")

        if not chat_history.strip():
            return "• *Status:* No customer messages found in this channel yet."

        prompt = (
            "You are an elite Sales Operations Lead reviewing an active WhatsApp/Telegram conversation.\n"
            "Below is the conversation history with the prospect/client.\n\n"
            "=== CHAT HISTORY ===\n"
            f"{chat_history}\n"
            "====================\n\n"
            "Task: Generate a concise, high-signal executive deal summary with exactly 3 bullet points:\n"
            "1. 🎯 *Client Goal / Request:* (What the client needs or is asking about)\n"
            "2. 💡 *Key Details / Context:* (Any budget, timeline, package, or specific requirements mentioned)\n"
            "3. 📌 *Current Status & Next Step:* (Where the conversation currently stands and what should happen next)\n\n"
            "Guidelines:\n"
            "- Be direct and factual based ONLY on the chat history.\n"
            "- Use clean Slack mrkdwn formatting (*bold* and bullet points).\n"
            "- Output only the 3 bullet points without extra conversational preamble or closing."
        )

        summary = call_gemini_api(prompt, timeout=15)
        if summary:
            return summary

        return "• *Summary:* Unable to extract summary from recent chat context."
    except Exception as e:
        logging.error(f"Gemini chat summary generation failed: {e}")
        return f"• *Summary Error:* Could not generate summary ({e})"


def async_generate_and_update_summary_modal(view_id: str, channel_id: str, bot_user_id: str, web_client: WebClient):
    """Background worker: fetches chat history, generates AI summary, and updates modal view."""
    try:
        # The deal summary should read the whole conversation too, otherwise
        # it summarises a 25-message window that may not hold the whole deal.
        name_cache = ai_context._UserNameCache(web_client)
        formatted_history = build_channel_transcript(web_client, channel_id, name_cache=name_cache)

        summary_text = generate_chat_summary_ai(formatted_history)

        updated_view = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "📝 Deal Summary", "emoji": True},
            "close": {"type": "plain_text", "text": "Done", "emoji": True},
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Deal & Conversation Brief",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": summary_text
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "🤖 _Generated on-demand via Gemini from recent conversation history._"
                        }
                    ]
                }
            ]
        }

        web_client.views_update(
            view_id=view_id,
            view=updated_view
        )
        logging.info(f"Successfully updated Summary modal for channel {channel_id}")
    except Exception as e:
        logging.error(f"Failed to update Summary modal: {e}")


def handle_slack_block_actions(client: SocketModeClient, req, web_client: WebClient):
    """Handles Slack interactive block actions (e.g. Claim Lead, Handoff Lead, AI Suggest, Summarize buttons)."""
    if req.type != "interactive":
        return

    payload = req.payload
    if payload.get("type") != "block_actions":
        return

    # Immediately acknowledge the interaction (< 5ms)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    user = payload.get("user", {})
    user_id = user.get("id")
    user_name = user.get("name") or user.get("username") or user_id
    channel = payload.get("channel", {})
    channel_id = channel.get("id")
    trigger_id = payload.get("trigger_id")

    actions = payload.get("actions", [])
    for act in actions:
        action_id = act.get("action_id")
        external_id = act.get("value")

        # --- ACTION 1: Claim Lead Button ---
        if action_id == "claim_lead_btn":
            logging.info(f"🎯 User {user_name} ({user_id}) clicked [Claim Lead] in channel {channel_id} (value={external_id})")

            sales_store.update_lead_owner_by_channel(
                channel_id=channel_id,
                new_owner_slack_id=user_id,
                external_id=external_id
            )

            try:
                web_client.chat_postMessage(
                    channel=channel_id,
                    text=f"🎯 *Lead Claimed:* <@{user_id}> is now the primary owner of this lead."
                )
                logging.info(f"Posted claim confirmation in channel {channel_id}")
            except Exception as e:
                logging.error(f"Failed to post claim message to {channel_id}: {e}")

        # --- ACTION 2: Handoff Lead Button (Opens Modal) ---
        elif action_id == "handoff_lead_btn":
            logging.info(f"🤝 User {user_name} ({user_id}) clicked [Handoff Lead] in channel {channel_id}. Opening modal...")

            # Fetch active sales pool
            pool = sales_store.get_sales_pool(active_only=True)
            options = []
            for rep in pool:
                rep_id = rep.get("slack_user_id")
                rep_name = rep.get("name") or rep_id
                if rep_id:
                    options.append({
                        "text": {"type": "plain_text", "text": f"{rep_name} (@{rep_id})", "emoji": True},
                        "value": rep_id
                    })

            # Construct modal view
            if options:
                rep_picker_element = {
                    "type": "static_select",
                    "action_id": "selected_rep",
                    "placeholder": {"type": "plain_text", "text": "Select sales team member", "emoji": True},
                    "options": options[:100]
                }
            else:
                rep_picker_element = {
                    "type": "users_select",
                    "action_id": "selected_rep",
                    "placeholder": {"type": "plain_text", "text": "Select user from workspace", "emoji": True}
                }

            modal_view = {
                "type": "modal",
                "callback_id": "handoff_modal_submission",
                "private_metadata": f"{channel_id}|{external_id or ''}",
                "title": {"type": "plain_text", "text": "Handoff Lead", "emoji": True},
                "submit": {"type": "plain_text", "text": "Transfer Ownership", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Transfer ownership of this lead to another sales rep. Both of you will be notified in the channel."
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "rep_select_block",
                        "label": {"type": "plain_text", "text": "New Primary Owner", "emoji": True},
                        "element": rep_picker_element
                    },
                    {
                        "type": "input",
                        "block_id": "note_block",
                        "optional": True,
                        "label": {"type": "plain_text", "text": "Handover Note (Optional)", "emoji": True},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "handoff_note",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "e.g. Needs pricing quotation or follow-up call...", "emoji": True}
                        }
                    }
                ]
            }

            try:
                web_client.views_open(
                    trigger_id=trigger_id,
                    view=modal_view
                )
                logging.info(f"Successfully opened Handoff Lead modal for trigger_id {trigger_id}")
            except Exception as e:
                logging.error(f"Failed to open Handoff modal: {e}")

        # --- ACTION 3: AI Suggest Reply Button (Opens Loading Modal -> Updates with Draft) ---
        elif action_id == "ai_suggest_reply_btn":
            logging.info(f"✨ User {user_name} ({user_id}) requested [AI Suggest Reply] in channel {channel_id}...")

            loading_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "✨ AI Suggest Reply", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ *Reading conversation context & drafting best sales reply with Gemini...*\nPlease wait a moment."
                        }
                    }
                ]
            }

            try:
                open_resp = web_client.views_open(
                    trigger_id=trigger_id,
                    view=loading_modal
                )
                view_id = open_resp.get("view", {}).get("id")
                if view_id:
                    thread_pool.submit(
                        async_generate_and_update_suggest_modal,
                        view_id,
                        channel_id,
                        slack_bot_user_id,
                        web_client
                    )
            except Exception as e:
                logging.error(f"Failed to open AI Suggest loading modal: {e}")

        # --- ACTION 4: Summarize Chat Button (Opens Loading Modal -> Updates with Summary) ---
        elif action_id == "summarize_chat_btn":
            logging.info(f"📝 User {user_name} ({user_id}) requested [Summarize Chat] in channel {channel_id}...")

            summary_loading_modal = {
                "type": "modal",
                "title": {"type": "plain_text", "text": "📝 Deal Summary", "emoji": True},
                "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ *Analyzing conversation history and synthesizing deal brief...*\nPlease wait a moment."
                        }
                    }
                ]
            }

            try:
                open_resp = web_client.views_open(
                    trigger_id=trigger_id,
                    view=summary_loading_modal
                )
                view_id = open_resp.get("view", {}).get("id")
                if view_id:
                    thread_pool.submit(
                        async_generate_and_update_summary_modal,
                        view_id,
                        channel_id,
                        slack_bot_user_id,
                        web_client
                    )
            except Exception as e:
                logging.error(f"Failed to open Summarize Chat loading modal: {e}")



def handle_slack_delete_event(client: SocketModeClient, req, web_client: WebClient):
    """Handle when a message is deleted in Slack"""
    if req.type != "events_api":
        return

    event = req.payload.get("event", {})
    if event.get("type") != "message" or event.get("subtype") != "message_deleted":
        return

    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    deleted_ts = event.get("previous_message", {}).get("ts")
    channel_id = event.get("channel")
    
    logging.info(f"🗑️ SLACK DELETE DETECTED - TS: {deleted_ts}, Channel: {channel_id}")
    
    # Check if we have a WhatsApp message ID mapped to this Slack message
    whatsapp_msg_id = slack_to_whatsapp_msg_map.get(deleted_ts)
    if whatsapp_msg_id:
        logging.info(f"🗑️ Attempting to delete corresponding WhatsApp message: {whatsapp_msg_id}")
        if delete_whatsapp_message(whatsapp_msg_id):
            logging.info(f"✅ Successfully deleted WhatsApp message: {whatsapp_msg_id}")
            slack_to_whatsapp_msg_map.delete(deleted_ts)
        else:
            logging.error(f"❌ Failed to delete WhatsApp message: {whatsapp_msg_id}")
    else:
        logging.info(f"ℹ️ No WhatsApp message mapping found for Slack TS: {deleted_ts}")




def process_socket_mode_events():
    socket_client = SocketModeClient(app_token=MANAGER_APP_TOKEN, web_client=web_client)

    def process(client: SocketModeClient, req: SocketModeRequest):
        try:
            if req.type == "slash_commands":
                handle_slack_slash_commands(client, req, web_client)
            elif req.type == "interactive":
                payload_type = req.payload.get("type")
                if payload_type == "view_submission":
                    handle_modal_submission(client, req, web_client)
                elif payload_type == "block_actions":
                    handle_slack_block_actions(client, req, web_client)
        except Exception as e:
            logging.error(f"Error in SocketMode process: {e}", exc_info=True)

    socket_client.socket_mode_request_listeners.append(process)
    
    try:
        socket_client.connect()
        logging.info("🚀 BitLink Manager Connected to Slack Socket Mode!")
        from threading import Event
        Event().wait()
    except Exception as e:
        logging.error(f"Socket Mode connection failed: {e}", exc_info=True)

if __name__ == "__main__":
    process_socket_mode_events()
