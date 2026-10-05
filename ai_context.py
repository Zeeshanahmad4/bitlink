"""Shared conversation-context builder for the Gemini reply features.

Both reply flows need the same two things:

1. The *entire* client/sales-rep conversation, not a truncated slice.
2. Every line correctly attributed to either the client or the BitLink sales rep.

(2) is the part that was silently broken. A sales rep typing in Slack has no
``bot_id`` and no ``username``, so the old labeler fell through to its default
and stamped every human message as "Client" -- the model was being handed a
conversation where both sides were the client, and it drafted accordingly.

What actually identifies each speaker in a BitLink Slack channel, verified
against live channel history:

* Client message  -> posted by a BitLink bot, prefixed with the client's
                     display name. The bridge relays inbound WhatsApp/Telegram
                     text as ``"<name>: <text>"`` (or ``"*<name>* sent a
                     file"``) and, when it can, sets ``username`` to that name.
* Sales rep       -> a real human Slack user posting directly (no ``bot_id``).
* Sales rep       -> OR the bot itself, when a rep sends an AI-drafted reply
                     from the modal. ``main_whatsapp.handle_modal_submission``
                     posts it with the bot token, so a drafted reply looks
                     like a bot post. Name-prefix matching is what keeps that
                     from being misread as the client talking.
* Automation      -> BitLink's own system cards, SLA warnings, lead notices.
"""

import logging
import re
import threading

# Slack message subtypes that carry no conversational content.
SYSTEM_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "bot_add",
    "bot_remove", "message_deleted", "tombstone",
    "channel_convert_to_private", "channel_convert_to_public",
    "group_join", "group_leave", "group_topic", "group_purpose",
    "reminder_add",
})

# BitLink's own automation cards. These are posted as text by the bridge bots
# and would otherwise be mistaken for the client talking.
AUTOMATION_MARKERS = (
    "New WhatsApp Lead Detected",
    "New Telegram Lead Detected",
    "Lead Claimed:",
    "Lead Handed Off:",
    "SLA Warning:",
    "has joined the channel",
    "has left the channel",
    "set the channel purpose",
    "set the channel topic",
    "This channel is currently paused",
    "queued message delivered to WhatsApp",
    "Failed to forward this message",
    "This message was deleted by the client",
    "uploaded a file",
    "set a reminder",
)

BOT_NAME_PREFIXES = ("bitlink",)

# "Abdul: hello" / "**Abdul**: hello" / "*Abdul:* hello" -- the bridge's client relay format.
_CLIENT_PREFIX_RE = re.compile(
    r"^\s*(?:\*(?P<name>[^*\n:]{1,60}?):\*\s*|\*{0,2}(?P<name2>[^*\n:]{1,60}?)\*{0,2}\s*:\s*)"
)
# "*Abdul* sent a file: x.pdf" / "Abdul sent a photo"
_CLIENT_MEDIA_RE = re.compile(
    r"^\s*\*{0,2}(?P<name>[^*\n]{1,60}?)\*{0,2}\s+sent\s+(?:a|an|some)\b[^:\n]{0,30}:?\s*",
    re.IGNORECASE,
)
# "@Name" style prefix on outgoing system cards, not a client relay.
_TEAM_PREFIX_RE = re.compile(r"^@")

_SLACK_LINK_RE = re.compile(r"<[^<>|]+(?:\|([^<>]*))?>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")

# Keep the transcript well inside a safe context window. Oldest lines are
# dropped first; the recent turns are what the reply has to respond to.
MAX_TRANSCRIPT_CHARS = 48000


def _is_bitlink_bot_name(name):
    name = (name or "").strip().lower()
    return any(name.startswith(prefix) for prefix in BOT_NAME_PREFIXES)


def _name_variants(name):
    """Normalised forms of a display name, for tolerant comparison.

    WhatsApp push names are often truncated ("Umar\u2026"), so the bare name
    has to match the ellipsised one too.
    """
    name = (name or "").strip()
    if not name:
        return frozenset()
    variants = {name.casefold()}
    stripped = name.replace("\u2026", "").replace("...", "").strip()
    if stripped:
        variants.add(stripped.casefold())
    return frozenset(v for v in variants if v)


def clean_text(text):
    """Strip Slack markup so the model reads plain prose.

    ``<https://x|label>`` becomes ``label`` and ``<@U123>`` becomes
    ``@someone``; runs of spaces collapse.
    """
    if not text:
        return ""
    text = _SLACK_LINK_RE.sub(lambda m: m.group(1) or "", text)
    text = re.sub(r"<@[UWB][A-Z0-9]+>", "@someone", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def unwrap_edited(msg):
    """Return the current version of an edited message.

    ``message_changed`` wraps the live message in ``message`` and keeps the old
    text in ``previous_message``. Reading the wrapper directly loses the text
    entirely, so an edited reply would vanish from the conversation.
    """
    if (msg or {}).get("subtype") != "message_changed":
        return msg
    current = (msg.get("message") or {})
    if not current.get("text") and not current.get("files"):
        # Fall back to the wrapper rather than dropping the turn entirely.
        return msg
    return current


def extract_message_text(msg):
    """Extract message text from either 'text' or Slack 'blocks'."""
    text = (msg.get("text") or "").strip()
    if not text and msg.get("blocks"):
        extracted = []
        for block in msg.get("blocks", []):
            if not isinstance(block, dict):
                continue
            b_text = block.get("text")
            if isinstance(b_text, dict) and b_text.get("text"):
                extracted.append(b_text.get("text"))
            elif isinstance(b_text, str) and b_text:
                extracted.append(b_text)
            elif block.get("type") == "rich_text":
                for element in block.get("elements", []):
                    if isinstance(element, dict):
                        for sub in element.get("elements", []):
                            if isinstance(sub, dict) and sub.get("text"):
                                extracted.append(sub.get("text"))
        if extracted:
            text = " ".join(extracted).strip()
    return clean_text(text)


def _client_prefix_match(text):
    """Match the bridge's client relay prefix, returning the regex match."""
    for pattern in (_CLIENT_MEDIA_RE, _CLIENT_PREFIX_RE):
        match = pattern.match(text or "")
        if match:
            name = (match.group("name") or match.group("name2") or "").strip()
            if name and not _TEAM_PREFIX_RE.match(name) and not _is_bitlink_bot_name(name):
                return match
    return None


def _client_name_from_text(text):
    """Pull the client display name out of a bridge-relayed message."""
    match = _client_prefix_match(text)
    if not match:
        return ""
    return (match.group("name") or match.group("name2") or "").strip()


def classify(msg, client_names=frozenset()):
    """Return the speaker role for one Slack message.

    One of: ``"client"``, ``"sales_rep"``, ``"team"``, ``"system"`` or ``None``
    when the message should be dropped entirely.

    ``client_names`` is the set of display-name variants belonging to this
    channel's client (from the mapping table, plus the bridge's ``username``).
    """
    msg = unwrap_edited(msg)
    if msg.get("subtype") in SYSTEM_SUBTYPES:
        return "system"

    text = extract_message_text(msg)
    if any(marker in text for marker in AUTOMATION_MARKERS):
        return "system"

    username = (msg.get("username") or "").strip()
    is_bot = bool(msg.get("bot_id"))
    user = msg.get("user")

    # Candidates that the bridge may have used for the client.
    candidate = _client_name_from_text(text) or username
    known = _name_variants(candidate) & _name_variants_set(client_names)

    if is_bot:
        if known:
            # Bridge relaying inbound client text, tagged with their name.
            return "client"
        if _is_bitlink_bot_name(username):
            # A BitLink system app (bridge info, SLA cards, delivery receipts).
            return "system"
        if username:
            # Custom username on a bot post is the bridge tagging the sender.
            return "client"
        if candidate and not _name_variants_set(client_names):
            # No mapping for this channel; a name prefix is still the
            # strongest available signal for a relayed client turn.
            return "client"
        # No name at all on a bot post: a drafted reply or an automation card.
        return "team"

    if user and not is_bot:
        # A real human Slack user -- that is our sales rep.
        return "sales_rep"

    return "team"


def _name_variants_set(names):
    """Expand an iterable of names into a single set of comparable variants."""
    out = set()
    for name in names or ():
        out |= _name_variants(name)
    return out


class _UserNameCache:
    """Resolve Slack user IDs to display names, once per user.

    Without this every sales rep line reads "BitLink Team", which tells the
    model nothing about who is actually talking.
    """

    def __init__(self, web_client):
        self._client = web_client
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, user_id):
        if not user_id or user_id in ("USLACKBOT",):
            return ""
        with self._lock:
            if user_id in self._cache:
                return self._cache[user_id]
        name = ""
        try:
            info = (self._client.users_info(user=user_id) or {}).get("user") or {}
            profile = info.get("profile") or {}
            name = (
                profile.get("real_name")
                or profile.get("display_name")
                or profile.get("name")
                or info.get("name")
                or ""
            ).strip()
        except Exception as e:
            logging.debug(f"users_info failed for {user_id}: {e}")
        with self._lock:
            self._cache[user_id] = name
        return name


def _media_placeholder(msg):
    """Describe attachments so a voice note still carries meaning."""
    files = msg.get("files") or []
    kinds = []
    for f in files:
        mime = (f.get("mimetype") or "").lower()
        subtype = (f.get("subtype") or "").lower()
        if subtype == "slack_audio" or mime.startswith("audio/"):
            kinds.append("voice message")
        elif mime.startswith("image/"):
            kinds.append("photo")
        elif mime.startswith("video/"):
            kinds.append("video")
        else:
            kinds.append("file")
    if not kinds:
        return ""
    seen, uniq = set(), []
    for k in kinds:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return f"[sent a {' and '.join(uniq)}]"


def build_transcript(messages, client_names=(), name_cache=None, max_chars=MAX_TRANSCRIPT_CHARS):
    """Turn raw Slack messages into a chronological, correctly-labelled
    transcript for the model.

    ``messages`` must be oldest-first, which is what :func:`fetch_conversation`
    returns. ``name_cache`` is an optional ``_UserNameCache`` used to label
    sales reps with their real Slack name.
    """
    lines = []
    for raw in messages or []:
        msg = unwrap_edited(raw)
        role = classify(raw, client_names)
        if role in (None, "system"):
            continue

        text = extract_message_text(msg)
        who = ""
        if role == "client":
            match = _client_prefix_match(text)
            if match:
                who = (match.group("name") or match.group("name2") or "").strip()
                # Drop the "Name:" / "Name sent a file:" preamble -- the
                # transcript already labels the line with the speaker.
                text = (text[match.end():] or text).strip()
            if not who:
                who = (msg.get("username") or "").strip()

        placeholder = _media_placeholder(msg)
        if not text and not placeholder:
            continue
        if not text:
            text = placeholder
        elif placeholder and placeholder not in text:
            text = f"{text} {placeholder}"

        if role == "client":
            lines.append(f"CLIENT ({who or 'the client'}): {text}")
        else:
            if who == "" and name_cache is not None:
                who = name_cache.get(msg.get("user"))
            lines.append(f"SALES REP ({who or 'BitLink Team'}): {text}")

    return _truncate("\n".join(lines), max_chars)


def _truncate(transcript, max_chars):
    """Drop the oldest lines first if the transcript is too long."""
    if not transcript or len(transcript) <= max_chars:
        return transcript
    lines = transcript.split("\n")
    note = "[... earlier messages omitted for length ...]"
    kept, total = [], len(note)
    for line in reversed(lines):
        total += len(line) + 1
        if total > max_chars:
            break
        kept.append(line)
    kept.reverse()
    return "\n".join([note] + kept)


def _paginate(fetch, max_messages, page_size, **kwargs):
    """Walk a cursor-paginated Slack read, collecting newest-first."""
    collected = []
    cursor = None
    while len(collected) < max_messages:
        batch_size = min(page_size, max_messages - len(collected))
        try:
            response = fetch(
                **kwargs, limit=batch_size, **({"cursor": cursor} if cursor else {})
            )
        except Exception as e:
            logging.warning(f"Slack history read failed: {e}")
            break
        if not response or not response.get("ok"):
            logging.warning(f"Slack history read failed: {(response or {}).get('error')}")
            break
        batch = response.get("messages") or []
        if not batch:
            break
        collected.extend(batch)
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return collected[:max_messages]


def fetch_conversation(web_client, channel_id, max_messages=300, page_size=200,
                       include_threads=True, max_thread_replies=200, fallback_clients=None):
    """Page through a whole channel and return messages oldest-first.

    The API returns newest-first and pages with a cursor, so a single
    ``limit=N`` call only ever gave the model the last N messages. This walks
    the cursor until the channel is exhausted or ``max_messages`` is reached,
    then reverses so the transcript reads chronologically.

    ``include_threads`` also pulls replies to any thread started in the window.
    Plain ``conversations_history`` never returns them, so a conversation held
    in a thread would otherwise be missing entirely.
    """
    collected = _paginate(
        web_client.conversations_history, max_messages, page_size, channel=channel_id
    )

    # If the primary bot was not in the channel, attempt join or fallback clients
    if not collected:
        try:
            join_resp = web_client.conversations_join(channel=channel_id)
            if join_resp and join_resp.get("ok"):
                collected = _paginate(
                    web_client.conversations_history, max_messages, page_size, channel=channel_id
                )
        except Exception:
            pass

    if not collected and fallback_clients:
        for fb in fallback_clients:
            try:
                fb_collected = _paginate(
                    fb.conversations_history, max_messages, page_size, channel=channel_id
                )
                if fb_collected:
                    collected = fb_collected
                    web_client = fb
                    break
            except Exception as e:
                logging.debug(f"Fallback history read failed: {e}")

    if include_threads and collected:
        by_ts = {m.get("ts"): m for m in collected if m.get("ts")}
        for root in collected:
            if not root.get("thread_ts") or root.get("ts") != root.get("thread_ts"):
                continue
            try:
                replies = _paginate(
                    web_client.conversations_replies,
                    max_thread_replies,
                    page_size,
                    channel=channel_id,
                    ts=root["ts"],
                )
            except Exception as e:
                logging.debug(f"replies read failed for {root.get('ts')}: {e}")
                continue
            for reply in replies:
                ts = reply.get("ts")
                if ts and ts not in by_ts:
                    by_ts[ts] = reply
        collected = list(by_ts.values())

    # Everything so far is newest-first. Slack timestamps sort numerically
    # across both top-level messages and thread replies, so ordering on them
    # keeps a reply directly after the message it answers.
    try:
        collected.sort(key=lambda m: float(m.get("ts") or 0), reverse=True)
    except (TypeError, ValueError):
        pass
    collected.reverse()
    return collected
