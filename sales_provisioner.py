# sales_provisioner.py — WhatsApp Sales Lead Intake Engine
import datetime
import logging
import os
import re
import string
import time
from typing import Optional, Dict, Any, Tuple, List
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import sales_store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)


import json
import google.generativeai as genai
from gemini_enhance import API_KEY

def infer_geo_language(text: str) -> dict:
    """Uses Gemini API to extract guessed geo and language from the first message."""
    if not text or not text.strip():
        return {"geo": "Unknown", "language": "Unknown"}
        
    try:
        genai.configure(api_key=API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = (
            "Analyze the following text and determine the 'language' and a guessed 'geo' (geographical region or country) based on the language/context. "
            "Return ONLY a JSON object with keys 'geo' and 'language'. Do not use markdown blocks.\n\n"
            f"Text: \"{text}\""
        )
        response = model.generate_content(prompt)
        result_text = response.text.strip().replace('```json', '').replace('```', '').strip()
        data = json.loads(result_text)
        return {"geo": data.get("geo", "Unknown"), "language": data.get("language", "Unknown")}
    except Exception as e:
        logging.error(f"Failed to infer geo/language: {e}")
        return {"geo": "Unknown", "language": "Unknown"}


# --- 2.1 Channel Naming & Collision Resolution ---

def sanitize_lead_name(lead_name: Optional[str], external_id: str, max_len: int = 15) -> str:
    """
    Sanitizes a lead's name for Slack channel naming compliance:
    - Lowercase letters, numbers, and hyphens only
    - Strips special characters/spaces/emojis
    - Falls back to last digits of phone/ID if name is empty or generic
    """
    if lead_name:
        # Remove emojis, special characters, and non-alphanumeric (keep spaces and hyphens temporarily)
        clean = re.sub(r"[^a-zA-Z0-9\s-]", "", str(lead_name)).strip().lower()
        # Convert spaces to hyphens
        slug = re.sub(r"[\s_]+", "-", clean)
        # Collapse multiple hyphens
        slug = re.sub(r"-+", "-", slug).strip("-")
        
        # Avoid generic placeholder slugs
        if slug and slug not in ["new-client", "user", "new-telegram-lead", "telegram-user"]:
            return slug[:max_len].rstrip("-")

    # Fallback to last digits if name is unavailable or purely special characters
    return extract_last_digits(external_id, count=4)


def extract_last_digits(external_id: str, count: int = 3) -> str:
    """Extracts the last N numeric digits from a WhatsApp JID or phone number."""
    if not external_id:
        return "000"
    
    raw_user = str(external_id).split("@")[0]
    digits_only = re.sub(r"\D", "", raw_user)
    if len(digits_only) >= count:
        return digits_only[-count:]
    elif digits_only:
        return digits_only.zfill(count)
    else:
        clean_str = "".join(c for c in raw_user if c in string.ascii_lowercase or c.isdigit())
        return clean_str[-count:] if len(clean_str) >= count else "lead"


def get_base_channel_name(
    external_id: str, 
    lead_name: Optional[str] = None, 
    date_obj: Optional[datetime.datetime] = None, 
    platform: str = "whatsapp"
) -> str:
    """
    Generates canonical base channel name:
    sale-wa-{lead_name}-{YYYYMMDD} or sale-tg-{lead_name}-{YYYYMMDD}
    """
    name_slug = sanitize_lead_name(lead_name, external_id)
    dt = date_obj or datetime.datetime.now()
    date_str = dt.strftime("%Y%m%d")
    prefix = "sale-tg" if platform.lower() == "telegram" else "sale-wa"
    return f"{prefix}-{name_slug}-{date_str}".lower()


def get_collision_channel_name(base_name: str, date_obj: Optional[datetime.datetime] = None, include_seconds: bool = False) -> str:
    """Appends timestamp suffix to resolve channel name collisions."""
    dt = date_obj or datetime.datetime.now()
    suffix = dt.strftime("%H%M%S") if include_seconds else dt.strftime("%H%M")
    return f"{base_name}-{suffix}".lower()


def create_slack_channel_with_collision_retry(
    external_id: str,
    web_client: WebClient,
    lead_name: Optional[str] = None,
    is_private: bool = False,
    platform: str = "whatsapp"
) -> Tuple[Optional[str], Optional[str]]:
    """Creates a Slack channel with automated collision resolution on 'name_taken'."""
    now = datetime.datetime.now()
    base_name = get_base_channel_name(external_id, lead_name=lead_name, date_obj=now, platform=platform)
    attempts = [
        base_name,
        get_collision_channel_name(base_name, now, include_seconds=False),
        get_collision_channel_name(base_name, now, include_seconds=True)
    ]

    for candidate_name in attempts:
        try:
            logging.info(f"Attempting to create Slack channel: '{candidate_name}' (private={is_private})")
            response = web_client.conversations_create(
                name=candidate_name,
                is_private=is_private
            )
            ch = response.get("channel", {})
            channel_id = ch.get("id")
            channel_name = ch.get("name", candidate_name)
            logging.info(f"Successfully created Slack channel: {channel_name} (ID: {channel_id})")
            
            # --- AUTO-INVITE MASTER BOT FOR COMMANDS ---
            master_bot_id = os.getenv("MASTER_SLACK_BOT_ID")
            if master_bot_id:
                try:
                    web_client.conversations_invite(channel=channel_id, users=master_bot_id)
                    logging.info(f"Successfully invited Master Bot ({master_bot_id}) to {channel_name}")
                except Exception as e:
                    logging.error(f"Failed to auto-invite Master Bot to {channel_name}: {e}")

            return channel_id, channel_name
        except SlackApiError as e:
            error_code = e.response.get("error")
            if error_code == "name_taken":
                logging.warning(f"Slack channel name '{candidate_name}' already exists. Retrying with next collision suffix...")
                time.sleep(0.5)
                continue
            elif error_code == "invalid_name_specials":
                logging.warning(f"Slack rejected channel name '{candidate_name}'. Retrying with fallback numeric name...")
                fallback_base = get_base_channel_name(external_id, lead_name=None, date_obj=now, platform=platform)
                return create_slack_channel_with_collision_retry(external_id, web_client, lead_name=None, is_private=is_private, platform=platform)
            else:
                logging.error(f"Failed to create Slack channel '{candidate_name}': {e}")
                return None, None

    logging.error(f"Failed to create Slack channel after all collision attempts for '{external_id}'.")
    return None, None


# --- 2.2 Safety & Deduplication Guards ---

class GuardAction:
    PROCEED = "PROCEED"                         # Safe to create new channel
    DEDUPLICATED = "DEDUPLICATED"               # Lead already open -> reuse existing channel
    BLOCKED_BLOCKLIST = "BLOCKED_BLOCKLIST"     # Sender is in blocklist -> drop
    BLOCKED_KILLSWITCH = "BLOCKED_KILLSWITCH"   # Killswitch active -> halt auto-creation
    ROUTED_TRIAGE = "ROUTED_TRIAGE"             # Triage mode ON -> route to triage inbox


def evaluate_lead_guards(
    external_id: str,
    web_client: Optional[WebClient] = None,
    msg: Optional[Dict[str, Any]] = None
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Evaluates safety, blocklist, kill-switch, and deduplication rules."""
    clean_id = str(external_id).strip()

    # 1. Blocklist Check (#9)
    if sales_store.is_external_id_blocked(clean_id):
        logging.info(f"🛡️ [GUARD] Sender {clean_id} is in BLOCKLIST. Dropping lead.")
        return GuardAction.BLOCKED_BLOCKLIST, None, None

    # 2. Delivery Client Check: If already handed off to delivery, reuse delivery channel
    delivery_client = sales_store.find_delivery_mapping_by_external_id(clean_id)
    if delivery_client:
        ch_id = delivery_client.get("slack_channel_id")
        logging.info(f"🚚 [GUARD] Client {clean_id} is in DELIVERY ({ch_id}). Reusing Delivery Slack channel.")
        return GuardAction.DEDUPLICATED, ch_id, delivery_client

    # 3. Deduplication Check (#5)
    existing_lead = sales_store.find_existing_open_lead(clean_id)
    if existing_lead:
        channel_id = existing_lead.get("slack_channel_id")
        logging.info(f"🔄 [GUARD] Open lead already exists for {clean_id}. Reusing Slack channel: {channel_id}")
        return GuardAction.DEDUPLICATED, channel_id, existing_lead

    # 3. Settings Retrieval
    settings = sales_store.get_sales_settings()

    # 4. Kill-Switch Check (#52)
    if settings.get("kill_switch_autocreate", False):
        logging.warning(f"🛑 [GUARD] Kill-switch is ACTIVE! Halting auto-creation for lead {clean_id}.")
        # Prefer a dashboard-configured channel so this stays manageable from
        # the UI, then fall back to the environment.
        admin_channel = settings.get("kill_switch_alert_channel_id") or os.getenv("SLACK_ADMIN_CHANNEL_ID")
        if not admin_channel:
            # Nothing was posted, so without this the lead would vanish with no
            # trace outside this line. Make it unmissable.
            logging.critical(
                f"🚨 [GUARD] Lead {clean_id} was BLOCKED by the kill-switch but no alert channel "
                f"is configured, so nobody was notified. Set 'kill_switch_alert_channel_id' in "
                f"dashboard Settings, or set SLACK_ADMIN_CHANNEL_ID."
            )
        elif web_client:
            try:
                web_client.chat_postMessage(
                    channel=admin_channel,
                    text=f"⚠️ *Sales Kill-Switch Alert:* Inbound WhatsApp message from `{clean_id}` was blocked from auto-creating a channel because `kill_switch_autocreate` is ON."
                )
            except Exception as e:
                logging.error(f"Failed to post killswitch alert to admin channel: {e}")
        return GuardAction.BLOCKED_KILLSWITCH, None, None

    # 5. Manual Triage Check (#4)
    if settings.get("triage_mode_enabled", False):
        triage_channel = settings.get("triage_channel_id") or os.getenv("SLACK_ADMIN_CHANNEL_ID")
        platform_str = str(msg.get("platform", "whatsapp") if msg else "whatsapp").capitalize()
        if not triage_channel:
            # Without a destination there is nowhere to route the lead, and
            # returning ROUTED_TRIAGE here would make the caller skip
            # provisioning while nothing was actually posted - silently losing
            # the lead. Fail loudly instead.
            logging.critical(
                f"🚨 [GUARD] Triage mode is ENABLED but no triage channel is configured "
                f"({platform_str} lead {clean_id} would be dropped). Set 'triage_channel_id' "
                f"in the dashboard Settings, or set SLACK_ADMIN_CHANNEL_ID."
            )
            return GuardAction.BLOCKED_KILLSWITCH, None, None
        logging.info(f"📬 [GUARD] Triage mode is ENABLED. Routing {platform_str} lead {clean_id} to triage channel: {triage_channel}")
        if triage_channel and web_client:
            sender_name = (msg.get("senderName") if msg else None) or f"New {platform_str} Client"
            body_text = ((msg.get("body") or "") if msg else "")
            if not body_text:
                body_text = "(Attachment / Media)"
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"📋 New {platform_str} Lead in Triage",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"👤 *Sender:* {sender_name} (`{clean_id}`)\n"
                            f"🌐 *Platform:* {platform_str}\n"
                            f"💬 *Message:* {body_text}\n"
                            f"⚠️ *Status:* Auto-creation paused by Triage Mode. Review and approve manually."
                        )
                    }
                },
                {"type": "divider"}
            ]
            try:
                web_client.chat_postMessage(
                    channel=triage_channel,
                    text=f"📋 *New {platform_str} Lead in Triage:* {sender_name} (`{clean_id}`): {body_text}",
                    blocks=blocks
                )
                logging.info(f"Successfully posted {platform_str} triage notification to channel: {triage_channel}")
            except SlackApiError as e:
                err_code = e.response.get("error")
                if err_code == "not_in_channel":
                    try:
                        web_client.conversations_join(channel=triage_channel)
                        web_client.chat_postMessage(
                            channel=triage_channel,
                            text=f"📋 *New {platform_str} Lead in Triage:* {sender_name} (`{clean_id}`): {body_text}",
                            blocks=blocks
                        )
                        logging.info(f"Successfully joined and posted triage notification to channel: {triage_channel}")
                    except SlackApiError as join_err:
                        logging.error(f"⚠️ Bot is not in triage channel {triage_channel}. Please invite your Telegram/WhatsApp bot to channel {triage_channel} in Slack (Type '/invite @BotName' in that channel). Error: {join_err}")
                else:
                    logging.error(f"Failed to post triage alert to {triage_channel}: {e}")
            except Exception as e:
                logging.error(f"Failed to post triage alert to {triage_channel}: {e}")
        return GuardAction.ROUTED_TRIAGE, triage_channel, None

    # 6. All guards passed -> Safe to proceed
    logging.info(f"✅ [GUARD] All guards passed for {clean_id}. Proceeding with auto-provisioning.")
    return GuardAction.PROCEED, None, None


# --- 2.3 Slack Channel Creation & Member Pool Invite ---

def set_channel_topic(channel_id: str, external_id: str, web_client: WebClient, platform: str = "whatsapp") -> bool:
    """Sets the Slack channel topic to display the platform and client ID."""
    platform_label = "Telegram" if platform.lower() == "telegram" else "WhatsApp"
    topic_text = f"{platform_label} Lead: {external_id} | Auto-provisioned by Bitlink Sales"
    try:
        web_client.conversations_setTopic(channel=channel_id, topic=topic_text)
        logging.info(f"Set topic for channel {channel_id}: '{topic_text}'")
        return True
    except SlackApiError as e:
        logging.warning(f"Could not set topic for channel {channel_id}: {e}")
        return False


def invite_member_pool_to_channel(
    channel_id: str,
    web_client: WebClient,
    member_pool_user_ids: Optional[List[str]] = None
) -> List[str]:
    """Invites all active sales reps from the member pool into the created channel."""
    pool_ids = member_pool_user_ids
    if pool_ids is None:
        pool_ids = sales_store.get_active_sales_pool_user_ids()

    if not pool_ids:
        logging.info(f"No active sales pool members configured in 'Sales Pool' tab. Skipping pool invites.")
        return []

    invited_ids: List[str] = []
    skipped_ids: List[str] = []
    logging.info(f"Inviting {len(pool_ids)} sales reps to channel {channel_id}: {pool_ids}")

    def _invite_one(uid: str) -> bool:
        """Invite a single rep. Slack returns ok:false (no exception) for
        already_in_channel / user_not_found, so the response is inspected
        directly rather than relying on an exception."""
        try:
            res = web_client.conversations_invite(channel=channel_id, users=uid)
        except SlackApiError as e:
            err = e.response.get("error", "")
            if err in ("already_in_channel", "cant_invite_self"):
                return True
            logging.warning(f"Could not invite user {uid} to {channel_id}: {err}")
            return False
        if res.get("ok"):
            return True
        err = res.get("error", "")
        if err in ("already_in_channel", "cant_invite_self"):
            return True
        logging.warning(f"Could not invite user {uid} to {channel_id}: {err}")
        return False

    # Try the whole pool in one call first. A single stale or non-workspace
    # Slack ID (e.g. a rep who has left) makes Slack reject the entire batch,
    # so this is only ever an optimisation.
    try:
        response = web_client.conversations_invite(channel=channel_id, users=",".join(pool_ids))
        if response.get("ok"):
            logging.info(f"Successfully invited member pool to channel {channel_id}")
            return list(pool_ids)
        logging.warning(
            f"Batch pool invite rejected ('{response.get('error')}'). "
            f"Falling back to individual invites so one bad ID cannot block the rest."
        )
    except SlackApiError as e:
        logging.warning(
            f"Batch pool invite errored ('{e.response.get('error')}'). "
            f"Falling back to individual invites."
        )

    for uid in pool_ids:
        if _invite_one(uid):
            invited_ids.append(uid)
        else:
            skipped_ids.append(uid)

    if skipped_ids:
        logging.warning(
            f"Pool members not added to {channel_id} (check their Slack User ID in the "
            f"Sales Pool tab): {skipped_ids}. They will still be notified by DM."
        )
    logging.info(f"Invited {len(invited_ids)}/{len(pool_ids)} pool members to {channel_id}")
    return invited_ids


def provision_slack_channel_with_pool(
    external_id: str,
    web_client: WebClient,
    lead_name: Optional[str] = None,
    is_private: bool = False,
    platform: str = "whatsapp"
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Complete channel provisioning flow (#1, #2, #6, #8). Returns (channel_id, channel_name, invited_ids)."""
    channel_id, channel_name = create_slack_channel_with_collision_retry(
        external_id=external_id,
        web_client=web_client,
        lead_name=lead_name,
        is_private=is_private,
        platform=platform
    )
    if not channel_id:
        return None, None, []

    set_channel_topic(channel_id=channel_id, external_id=external_id, web_client=web_client, platform=platform)
    invited_ids = invite_member_pool_to_channel(channel_id=channel_id, web_client=web_client)

    return channel_id, channel_name, invited_ids


# --- 2.4 Round-Robin Owner Assignment (#3) ---

def assign_round_robin_owner() -> Tuple[Optional[str], Optional[str]]:
    """Selects the next primary lead owner from active sales reps using round-robin rotation."""
    pool = sales_store.get_sales_pool(active_only=True)
    if not pool:
        logging.warning("⚠️ No active sales pool members found for round-robin assignment.")
        return None, "Unassigned"

    settings = sales_store.get_sales_settings()
    current_cursor = settings.get("round_robin_cursor", 0)

    assigned_idx = current_cursor % len(pool)
    assigned_rep = pool[assigned_idx]
    owner_slack_id = assigned_rep.get("slack_user_id")
    owner_name = assigned_rep.get("name") or owner_slack_id

    next_cursor = (assigned_idx + 1) % len(pool)
    sales_store.update_round_robin_cursor(next_cursor)

    logging.info(f"🎯 [ROUND-ROBIN] Assigned Lead Owner: {owner_name} ({owner_slack_id}) [Cursor: {current_cursor} -> {next_cursor}]")
    return owner_slack_id, owner_name


# --- 2.5 Record Lead in CRM & Orchestrator ---

def record_new_lead_in_crm(
    client_name: str,
    channel_name: str,
    external_id: str,
    slack_channel_id: str,
    owner_slack_id: Optional[str] = None,
    lead_score: str = "warm",
    source: str = "wa",
    first_message_text: Optional[str] = None
) -> Dict[str, Any]:
    """
    Creates and saves a structured CRM lead record in the 'Sales Leads' data store.
    """
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    geo_lang = infer_geo_language(first_message_text or "")
    lead_record = {
        "client_name": client_name or channel_name,
        "platform": "whatsapp" if source == "wa" else "telegram",
        "external_id": external_id,
        "slack_channel_id": slack_channel_id,
        "tag": "sales",
        "owner_slack_id": owner_slack_id or "",
        "stage": "new",
        "lead_score": lead_score,
        "source": source,
        "geo": geo_lang.get("geo", "Unknown"),
        "language": geo_lang.get("language", "Unknown"),
        "created_at": now_ts,
        "last_client_msg_at": now_ts,
        "paused": False
    }
    sales_store.save_new_sales_lead(lead_record)
    return lead_record


def provision_whatsapp_lead(
    msg: Dict[str, Any],
    web_client: WebClient,
    is_private: bool = True
) -> Dict[str, Any]:
    """
    Master Orchestration for WhatsApp Inbound Lead Intake:
    1. Evaluates Safety, Blocklist, Kill-Switch, and Deduplication guards.
    2. Provisions Slack channel with collision retry & sets topic.
    3. Invites active Sales Member Pool.
    4. Assigns Round-Robin Owner.
    5. Records lead in CRM ('Sales Leads').
    
    Returns a result dict:
        {"status": "provisioned"|"deduplicated"|"blocked"|"triage", ...}
    """
    external_id = msg.get("chatId")
    if not external_id:
        return {"status": "error", "error": "Missing chatId in message"}

    # 1. Guard Evaluation
    action, target_channel, meta = evaluate_lead_guards(external_id, web_client, msg=msg)

    if action == GuardAction.BLOCKED_BLOCKLIST:
        return {"status": "blocked", "reason": "blocklisted", "external_id": external_id}

    if action == GuardAction.BLOCKED_KILLSWITCH:
        return {"status": "blocked", "reason": "kill_switch_active", "external_id": external_id}

    if action == GuardAction.ROUTED_TRIAGE:
        return {"status": "triage", "channel_id": target_channel, "external_id": external_id}

    if action == GuardAction.DEDUPLICATED:
        return {
            "status": "deduplicated",
            "channel_id": target_channel,
            "external_id": external_id,
            "lead": meta
        }

    # 2. Assign Round-Robin Owner
    owner_slack_id, owner_name = assign_round_robin_owner()

    sender_name = msg.get("senderName") or "New Client"

    # 3. Create Slack Channel (using fetched Lead Name) & Invite Member Pool
    channel_id, channel_name, invited_ids = provision_slack_channel_with_pool(
        external_id=external_id,
        web_client=web_client,
        lead_name=sender_name,
        is_private=is_private,
        platform="whatsapp"
    )
    if not channel_id:
        return {"status": "error", "error": "Failed to create Slack channel"}

    # 4. Record Lead in CRM
    lead_record = record_new_lead_in_crm(
        client_name=sender_name,
        channel_name=channel_name,
        external_id=external_id,
        slack_channel_id=channel_id,
        owner_slack_id=owner_slack_id,
        lead_score="warm",
        source="wa"
    )

    # 5. Post Welcome Card with Owner Mention (#12)
    post_lead_welcome_card(
        channel_id=channel_id,
        sender_name=sender_name,
        external_id=external_id,
        owner_slack_id=owner_slack_id,
        owner_name=owner_name,
        web_client=web_client,
        platform="whatsapp",
        invited_ids=invited_ids
    )

    # 6. Send DM notifications to pool members so they get Activity alerts
    notify_pool_members_dm(
        channel_id=channel_id,
        channel_name=channel_name,
        sender_name=sender_name,
        platform="whatsapp",
        invited_ids=invited_ids,
        owner_slack_id=owner_slack_id,
        web_client=web_client
    )

    logging.info(f"✨ [PROVISION COMPLETE] Created lead channel {channel_name} ({channel_id}) for {external_id}, Owner: {owner_name}")
    return {
        "status": "provisioned",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "owner_slack_id": owner_slack_id,
        "owner_name": owner_name,
        "lead": lead_record
    }


def provision_telegram_lead(
    msg: Dict[str, Any],
    web_client: WebClient,
    is_private: bool = True
) -> Dict[str, Any]:
    """
    Master Orchestration for Telegram Inbound Lead Intake:
    1. Evaluates Safety, Blocklist, Kill-Switch, and Deduplication guards.
    2. Provisions Slack channel (#sale-tg-{lead_name}-{date}) with collision retry & sets topic.
    3. Invites active Sales Member Pool.
    4. Assigns Round-Robin Owner.
    5. Records lead in CRM ('Sales Leads') with platform='telegram'.
    """
    external_id = str(msg.get("chatId") or msg.get("senderId") or "")
    if not external_id:
        return {"status": "error", "error": "Missing chatId/senderId in message"}

    # 1. Guard Evaluation
    action, target_channel, meta = evaluate_lead_guards(external_id, web_client, msg=msg)

    if action == GuardAction.BLOCKED_BLOCKLIST:
        return {"status": "blocked", "reason": "blocklisted", "external_id": external_id}

    if action == GuardAction.BLOCKED_KILLSWITCH:
        return {"status": "blocked", "reason": "kill_switch_active", "external_id": external_id}

    if action == GuardAction.ROUTED_TRIAGE:
        return {"status": "triage", "channel_id": target_channel, "external_id": external_id}

    if action == GuardAction.DEDUPLICATED:
        return {
            "status": "deduplicated",
            "channel_id": target_channel,
            "external_id": external_id,
            "lead": meta
        }

    # 2. Assign Round-Robin Owner
    owner_slack_id, owner_name = assign_round_robin_owner()

    sender_name = msg.get("senderName") or "New Telegram Lead"

    # 3. Create Slack Channel (using fetched Lead Name) & Invite Member Pool
    channel_id, channel_name, invited_ids = provision_slack_channel_with_pool(
        external_id=external_id,
        web_client=web_client,
        lead_name=sender_name,
        is_private=is_private,
        platform="telegram"
    )
    if not channel_id:
        return {"status": "error", "error": "Failed to create Slack channel"}

    # 4. Record Lead in CRM
    lead_record = record_new_lead_in_crm(
        client_name=sender_name,
        channel_name=channel_name,
        external_id=external_id,
        slack_channel_id=channel_id,
        owner_slack_id=owner_slack_id,
        lead_score="warm",
        source="tg"
    )

    # 5. Post Welcome Card with Owner Mention (#12)
    post_lead_welcome_card(
        channel_id=channel_id,
        sender_name=sender_name,
        external_id=external_id,
        owner_slack_id=owner_slack_id,
        owner_name=owner_name,
        web_client=web_client,
        platform="telegram",
        invited_ids=invited_ids
    )

    # 6. Send DM notifications to pool members so they get Activity alerts
    notify_pool_members_dm(
        channel_id=channel_id,
        channel_name=channel_name,
        sender_name=sender_name,
        platform="telegram",
        invited_ids=invited_ids,
        owner_slack_id=owner_slack_id,
        web_client=web_client
    )

    logging.info(f"✨ [TG PROVISION COMPLETE] Created lead channel {channel_name} ({channel_id}) for {external_id}, Owner: {owner_name}")
    return {
        "status": "provisioned",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "owner_slack_id": owner_slack_id,
        "owner_name": owner_name,
        "lead": lead_record
    }

def post_lead_welcome_card(
    channel_id: str,
    sender_name: str,
    external_id: str,
    owner_slack_id: Optional[str],
    owner_name: Optional[str],
    web_client: WebClient,
    platform: str = "whatsapp",
    invited_ids: Optional[List[str]] = None
) -> bool:
    """
    Posts the official Welcome Header Card into the newly created Sales Slack channel (#12).
    Includes formatted header, sender details, owner @mention, pool @mentions, and timestamp.
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
    display_sender = sender_name if sender_name else "Unknown Contact"
    platform_title = "Telegram" if platform.lower() == "telegram" else "WhatsApp"

    import re
    clean_owner = owner_slack_id.replace('<', '').replace('>', '').replace('@', '').strip() if owner_slack_id else ""
    if clean_owner and re.match(r'^[UW][0-9A-Z]{8,10}$', clean_owner):
        owner_display = f"<@{clean_owner}>"
    elif clean_owner:
        owner_display = f"*{clean_owner}*"
    elif owner_name and owner_name != "Unassigned":
        owner_display = f"*{owner_name}*"
    else:
        owner_display = "\u26a0\ufe0f *Unassigned (Team Pool Notified)*"

    # Build pool mentions so every invited member gets an Activity notification
    pool_mentions = ""
    if invited_ids:
        pool_mentions = " ".join([f"<@{uid}>" for uid in invited_ids])

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"\U0001f389 New {platform_title} Lead Detected",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"\U0001f464 *Sender:* {display_sender} (`{external_id}`)\n"
                    f"\U0001f3af *Primary Owner:* {owner_display}\n"
                    f"\U0001f4c5 *Received:* {now_str}"
                )
            }
        },
        {
            "type": "actions",
            "block_id": "sales_lead_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "\u26a1 Claim Lead",
                        "emoji": True
                    },
                    "style": "primary",
                    "action_id": "claim_lead_btn",
                    "value": external_id
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "\U0001f91d Handoff Lead",
                        "emoji": True
                    },
                    "action_id": "handoff_lead_btn",
                    "value": external_id
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "\u2728 AI Suggest Reply",
                        "emoji": True
                    },
                    "action_id": "ai_suggest_reply_btn",
                    "value": external_id
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "\U0001f4dd Summarize Chat",
                        "emoji": True
                    },
                    "action_id": "summarize_chat_btn",
                    "value": external_id
                }
            ]
        },
        {
            "type": "divider"
        }
    ]

    # Build the fallback text with all pool mentions so Slack sends Activity notifications
    fallback_text = f"\U0001f389 *New {platform_title} Lead Detected* from {display_sender} \u2014 Owner: {owner_display}"
    if pool_mentions:
        fallback_text += f"\n\U0001f465 *Team:* {pool_mentions} \u2014 a new lead has arrived!"

    try:
        web_client.chat_postMessage(
            channel=channel_id,
            text=fallback_text,
            blocks=blocks
        )
        logging.info(f"Posted welcome card to channel {channel_id} (Owner: {owner_display})")
        return True
    except SlackApiError as e:
        logging.error(f"Failed to post welcome card to channel {channel_id}: {e}")
        return False


def notify_pool_members_dm(
    channel_id: str,
    channel_name: str,
    sender_name: str,
    platform: str,
    invited_ids: List[str],
    owner_slack_id: Optional[str],
    web_client: WebClient
) -> None:
    """
    Sends a DM to every active sales rep about the new lead channel.

    Slack suppresses Activity notifications for messages inside a brand new
    channel, and a private-channel invite is easy to miss, so a DM is the
    reliable way to alert the team.

    This deliberately notifies the whole active pool rather than only the reps
    the channel invite succeeded for: a rep who could not be added to the
    channel still needs to know a lead exists. `invited_ids` is used only to
    decide whether the copy says "you were added" or "you were not added".
    """
    # Notify the full active pool, not just the invited subset. A rep whose
    # Slack ID failed to invite must still get the alert.
    notify_ids = [uid for uid in (sales_store.get_active_sales_pool_user_ids() or []) if uid]
    if not notify_ids:
        notify_ids = list(invited_ids or [])
    if not notify_ids:
        logging.warning("No active sales pool members to notify about a new lead.")
        return

    platform_title = "Telegram" if platform.lower() == "telegram" else "WhatsApp"
    channel_link = f"<#{channel_id}|{channel_name}>"
    invited_set = set(invited_ids or [])

    for uid in notify_ids:
        try:
            if owner_slack_id and uid == owner_slack_id:
                dm_text = (
                    f"\U0001f3af *You have been assigned a new {platform_title} lead!*\n"
                    f"\U0001f464 *Sender:* {sender_name}\n"
                    f"\U0001f4ac *Channel:* {channel_link}\n"
                    f"Head over to the channel to respond."
                )
            else:
                in_channel = uid in invited_set
                if in_channel:
                    detail = "You have been added to this lead channel."
                else:
                    detail = "You could not be added to the channel automatically - please open it from the link."
                # Mentioning the owner gives them a badge on a channel they may
                # not be watching, and tells the rest of the pool who is on it.
                owner_tag = f" *Owner:* <@{owner_slack_id}>" if owner_slack_id else ""
                dm_text = (
                    f"\U0001f389 *New {platform_title} Lead:* {sender_name}{owner_tag}\n"
                    f"\U0001f4ac *Channel:* {channel_link}\n"
                    f"{detail}"
                )

            res = web_client.chat_postMessage(channel=uid, text=dm_text)
            if not res.get("ok"):
                logging.warning(f"DM to {uid} not delivered: {res.get('error')}")
        except SlackApiError as e:
            err_code = e.response.get("error", "")
            if err_code == "channel_not_found":
                # Bot cannot DM this user (needs im:write scope or user hasn't opened DM)
                logging.warning(f"Cannot DM user {uid}: {err_code}")
            else:
                logging.warning(f"Failed to send DM notification to {uid}: {err_code}")
        except Exception as e:
            logging.warning(f"Error sending DM to {uid}: {e}")

    logging.info(
        f"Sent DM notifications to {len(notify_ids)} active pool members for channel {channel_name}"
    )
