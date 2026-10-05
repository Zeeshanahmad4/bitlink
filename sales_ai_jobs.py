import os
import time
import logging
import schedule
import argparse
import db_store
from dotenv import load_dotenv

load_dotenv()

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


# ---------------------------------------------------------------------------
# Auto-Archive Job
# ---------------------------------------------------------------------------

def run_auto_archive_job(web_client=None):
    """Archive Slack channels for deals closed or handed to delivery 30+ days ago.

    The threshold is read from the ``archive_after_days`` setting (default 30).
    Each eligible channel receives a farewell notice before being archived so
    the audit trail is preserved in the Slack channel history.

    Channels created by the WhatsApp bridge are archived using SLACK_BOT_TOKEN;
    channels created by the Telegram bridge are archived using
    SLACK_BOT_TOKEN_TELEGRAM.  A bot can only archive channels it is a member of,
    so using the wrong token would silently fail with ``not_in_channel``.

    ``web_client`` is an optional slack_sdk.WebClient for the WhatsApp bot.  If
    not supplied the function builds one from SLACK_BOT_TOKEN.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    # Build a client per platform.  Telegram channels need their own bot token.
    wa_token = os.getenv("SLACK_BOT_TOKEN")
    tg_token = os.getenv("SLACK_BOT_TOKEN_TELEGRAM")

    if web_client is None:
        if not wa_token:
            logging.error("[AUTO-ARCHIVE] SLACK_BOT_TOKEN not set — cannot archive WhatsApp channels.")
        web_client = WebClient(token=wa_token) if wa_token else None

    tg_client = WebClient(token=tg_token) if tg_token else None
    if not tg_client:
        logging.warning(
            "[AUTO-ARCHIVE] SLACK_BOT_TOKEN_TELEGRAM not set — "
            "Telegram channels will be archived with the WhatsApp bot token as fallback."
        )

    def _client_for(platform: str) -> WebClient:
        """Return the correct Slack bot client for the given platform."""
        if str(platform).strip().lower() == "telegram" and tg_client:
            return tg_client
        # WhatsApp, Discord, or any unknown platform — use the primary WA client
        return web_client

    if not web_client and not tg_client:
        logging.error("[AUTO-ARCHIVE] No Slack tokens available — aborting.")
        return

    try:
        settings = db_store.get_sales_settings()
        after_days = int(settings.get("archive_after_days", 30) or 30)
    except Exception as e:
        logging.error(f"[AUTO-ARCHIVE] Could not read settings: {e}")
        after_days = 30

    logging.info(f"[AUTO-ARCHIVE] Running auto-archive scan (threshold: {after_days} days)...")

    try:
        candidates = db_store.get_archivable_channels(after_days=after_days)
    except Exception as e:
        logging.error(f"[AUTO-ARCHIVE] Failed to fetch archivable channels: {e}")
        return

    if not candidates:
        logging.info("[AUTO-ARCHIVE] No channels eligible for archiving.")
        return

    logging.info(f"[AUTO-ARCHIVE] {len(candidates)} channel(s) eligible for archiving.")

    archived = 0
    skipped = 0
    already_done = 0

    for ch in candidates:
        channel_id = ch["slack_channel_id"]
        client_name = ch.get("client_name") or channel_id
        platform = str(ch.get("platform") or "whatsapp").strip().lower()
        reason = ch.get("reason", "deal_closed")
        closed_at = ch.get("closed_at") or "unknown date"

        client = _client_for(platform)
        if client is None:
            logging.warning(
                f"[AUTO-ARCHIVE] No Slack client available for platform '{platform}' — "
                f"skipping {channel_id} ({client_name})."
            )
            skipped += 1
            continue

        # Check if already archived — avoid a redundant API call on every run
        try:
            info_resp = client.conversations_info(channel=channel_id)
            channel_data = info_resp.get("channel", {}) or {}
            if channel_data.get("is_archived"):
                already_done += 1
                logging.debug(f"[AUTO-ARCHIVE] {channel_id} ({client_name}) already archived — skipping.")
                continue
        except SlackApiError as e:
            err = e.response.get("error", "")
            if err in ("channel_not_found", "is_archived"):
                already_done += 1
                continue
            logging.warning(f"[AUTO-ARCHIVE] Could not inspect channel {channel_id}: {err} — skipping.")
            skipped += 1
            continue
        except Exception as e:
            logging.warning(f"[AUTO-ARCHIVE] Unexpected error inspecting {channel_id}: {e} — skipping.")
            skipped += 1
            continue

        # Post a farewell notice before archiving so history is readable
        platform_label = "Telegram" if platform == "telegram" else "WhatsApp"
        if reason == "delivery_handoff":
            notice = (
                f"📦 *This channel has been automatically archived.*\n"
                f"Client *{client_name}* ({platform_label}) was handed over to the Delivery team "
                f"on {closed_at} and the {after_days}-day retention window has passed.\n"
                f"Channel history is preserved and can be unarchived by an admin if needed."
            )
        else:
            notice = (
                f"📁 *This channel has been automatically archived.*\n"
                f"The deal with *{client_name}* ({platform_label}) was closed on {closed_at} "
                f"and the {after_days}-day retention window has passed.\n"
                f"Channel history is preserved and can be unarchived by an admin if needed."
            )

        try:
            client.chat_postMessage(
                channel=channel_id,
                text=notice,
                username="Bitlink Auto-Archive"
            )
        except SlackApiError as e:
            # Not fatal — post the message best-effort; still archive even if notice fails
            logging.warning(
                f"[AUTO-ARCHIVE] Could not post notice to {channel_id} ({client_name}): "
                f"{e.response.get('error', e)}"
            )
        except Exception as e:
            logging.warning(f"[AUTO-ARCHIVE] Notice post to {channel_id} failed: {e}")

        # Archive the channel
        try:
            client.conversations_archive(channel=channel_id)
            archived += 1
            logging.info(
                f"[AUTO-ARCHIVE] ✅ Archived {channel_id} ({client_name}, {platform_label}) "
                f"— reason: {reason}, closed: {closed_at}"
            )
        except SlackApiError as e:
            err = e.response.get("error", "")
            if err == "already_archived":
                already_done += 1
                logging.debug(f"[AUTO-ARCHIVE] {channel_id} was already archived.")
            elif err == "cant_archive_general":
                logging.warning(f"[AUTO-ARCHIVE] Cannot archive {channel_id} (it is the #general channel).")
                skipped += 1
            elif err == "not_in_channel":
                logging.warning(
                    f"[AUTO-ARCHIVE] Bot is not in {channel_id} ({platform_label}) — cannot archive. "
                    f"Invite the bot to this channel or archive it manually."
                )
                skipped += 1
            else:
                logging.error(
                    f"[AUTO-ARCHIVE] Failed to archive {channel_id} ({client_name}, {platform_label}): {err}"
                )
                skipped += 1
        except Exception as e:
            logging.error(f"[AUTO-ARCHIVE] Unexpected error archiving {channel_id}: {e}")
            skipped += 1

        # Small delay to stay within Slack rate limits (Tier 2: 20 req/min)
        time.sleep(3)

    logging.info(
        f"[AUTO-ARCHIVE] Scan complete — "
        f"archived: {archived}, already archived: {already_done}, skipped/errors: {skipped}."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sales AI Background Jobs")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run all jobs immediately instead of waiting for the schedule"
    )
    parser.add_argument(
        "--archive-now",
        action="store_true",
        help="Run only the auto-archive job immediately"
    )
    args = parser.parse_args()

    if args.archive_now:
        run_auto_archive_job()
    elif args.run_now:
        run_weekly_stage_inference()
        run_auto_archive_job()
    else:
        # Weekly stage inference: Sundays at 02:00
        schedule.every().sunday.at("02:00").do(run_weekly_stage_inference)
        logging.info("Scheduled Weekly Stage AI to run every Sunday at 02:00")

        # Auto-archive: daily at 03:00 AM so it runs quietly overnight
        schedule.every().day.at("03:00").do(run_auto_archive_job)
        logging.info("Scheduled Auto-Archive job to run every day at 03:00")

        while True:
            schedule.run_pending()
            time.sleep(60)
