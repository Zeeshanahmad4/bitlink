"""Normalise Slack message text before forwarding it to WhatsApp/Telegram.

Two separate problems are handled here, both of which only show up on the
outbound Slack -> client path:

1. Slack's markup wrappers. Slack stores an auto-linked URL as
   ``<https://example.com>`` and a labelled link as ``<https://example.com|click>``.
   Those angle brackets are real characters in the payload, so forwarding the
   text verbatim makes the client display ``<https://example.com>`` instead of a
   link. Only the ``<scheme:...>`` forms are unwrapped, so ordinary text
   containing a bare ``<`` is left alone.

2. Emoji shortcodes. Slack stores a typed ``:smile:`` as literal text and only
   renders it client side. WhatsApp and Telegram understand Unicode, not Slack
   shortcodes, so the shortcode is forwarded and the client sees ``:smile:``.

The reverse direction (WhatsApp/Telegram -> Slack) needs neither, because those
platforms already send clean Unicode.

Both bridges share this module so their outbound behaviour cannot drift apart.
"""

import logging
import re

try:
    import emoji as _emoji
except ImportError:  # pragma: no cover - dependency should be in requirements
    _emoji = None
    logging.warning(
        "The 'emoji' package is not installed, so Slack :shortcodes: will be "
        "forwarded to clients unconverted. Run: pip install emoji"
    )

# 'alias' covers the GitHub/Slack-flavoured shortcodes (:smile:, :+1:,
# :white_check_mark:) as well as the standard :shortcode: set.
_EMOJI_LANGUAGE = "alias"

# A scheme is required, which is what keeps "a < b" or "x <y> z" untouched.
# Schemes where the scheme name is noise in a chat message get it stripped
# (<mailto:a@b.com> is "a@b.com", not "mailto:a@b.com"); http(s) keeps it.
_OTHER_SCHEMES = "mailto|tel|sms|skype"
_URL_SCHEMES = "https?|ftp"

_LABELLED_NON_URL_RE = re.compile(
    rf"<({_OTHER_SCHEMES}):[^|>]+\|([^>]+)>", re.IGNORECASE
)
_BARE_NON_URL_RE = re.compile(rf"<({_OTHER_SCHEMES}):([^>]+)>", re.IGNORECASE)
_LABELLED_LINK_RE = re.compile(
    rf"<({_URL_SCHEMES}):([^|>]+)\|([^>]+)>", re.IGNORECASE
)
_BARE_LINK_RE = re.compile(rf"<({_URL_SCHEMES}):([^>]+)>", re.IGNORECASE)
_LABELLED_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]+)>")
_BARE_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)>")
# <!here>, <!channel>, <!everyone> and <!subteam^S123|@team> all render as
# "@name" in Slack, so that is what clients should see.
_BROADCAST_RE = re.compile(r"<!(?:subteam\^[^|>]+\|@([^>]+)|([A-Za-z0-9_.@-]+))>")
# <@U123|name> carries a label; the bare form is left for the mention pipeline.
_LABELLED_MENTION_RE = re.compile(r"<@([A-Z0-9]+)\|([^>]+)>")
_BARE_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def to_unicode_emoji(text):
    """Replace Slack ``:shortcode:`` sequences with real emoji.

    Text without shortcodes is returned untouched, including times and ports
    like ``10:30`` or ``localhost:8080`` -- only names that exist as an emoji
    are substituted.
    """
    if not text or _emoji is None or ":" not in text:
        return text
    try:
        return _emoji.emojize(text, language=_EMOJI_LANGUAGE)
    except Exception as e:
        # Never let a formatting nicety block message delivery.
        logging.debug(f"emoji conversion skipped: {e}")
        return text


def _format_labelled_link(match):
    scheme = match.group(1)
    url_body = match.group(2)
    label = match.group(3)
    full_url = f"{scheme}:{url_body}"

    url_without_scheme = re.sub(r'^(?:https?|ftp)://', '', full_url, flags=re.IGNORECASE)
    clean_label = re.sub(r'[\.\u2026]+$', '', label)

    # 1. Exact match with full_url or url_without_scheme
    if label in (full_url, url_without_scheme, full_url.rstrip('/'), url_without_scheme.rstrip('/')):
        return full_url

    # 2. Label is a truncated prefix of full_url or url_without_scheme (Slack auto-shortening)
    if clean_label and (full_url.startswith(clean_label) or url_without_scheme.startswith(clean_label)):
        return full_url

    # 3. Label starts with http(s), www, or host domain and matches the start of url_without_scheme
    domain = url_without_scheme.split('/')[0].split('?')[0].split('#')[0]
    if label.startswith(('http://', 'https://', 'www.', domain)) and clean_label.startswith(domain):
        return full_url

    # 4. Otherwise it is a custom text label (e.g. <https://ex.com|the doc>)
    return label


def strip_slack_markup(text):
    """Unwrap Slack's ``<...>`` markup so clients see plain text.

    ``<https://x.com|label>`` and ``<https://x.com>`` both become readable text,
    ``<#C123|general>`` becomes ``#general``, and ``<!here>`` becomes ``@here``.

    User mentions are deliberately left as ``<@U123>``: they carry a Slack user
    ID that only the mention pipeline can resolve, and silently dropping the
    brackets would leave a raw ID in front of the client.
    """
    if not text or "<" not in text:
        return text

    text = _LABELLED_NON_URL_RE.sub(lambda m: m.group(2), text)
    text = _BARE_NON_URL_RE.sub(lambda m: m.group(2), text)
    text = _LABELLED_LINK_RE.sub(_format_labelled_link, text)
    text = _BARE_LINK_RE.sub(lambda m: f"{m.group(1)}:{m.group(2)}", text)
    text = _LABELLED_CHANNEL_RE.sub(lambda m: f"#{m.group(2)}", text)
    text = _BARE_CHANNEL_RE.sub(lambda m: f"#{m.group(1)}", text)
    text = _LABELLED_MENTION_RE.sub(lambda m: f"@{m.group(2)}", text)
    text = _BROADCAST_RE.sub(lambda m: f"@{m.group(1) or m.group(2)}", text)
    return text


def for_client(text):
    """Full outbound cleanup: Slack markup out, real emoji in."""
    if not text:
        return text
    return to_unicode_emoji(strip_slack_markup(text))
