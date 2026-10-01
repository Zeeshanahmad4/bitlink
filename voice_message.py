"""Helpers for turning Telegram/WhatsApp voice notes into native Slack voice messages.

Slack does not render a plain audio upload as a voice message. Native Slack voice
messages are identified by the ``audio_message*`` filename prefix combined with an
audio mimetype, which makes Slack tag the file with ``subtype: "slack_audio"`` and
render an inline playable bubble instead of a generic file attachment.

Telegram and WhatsApp voice notes are already Ogg/Opus, so no transcoding is
required -- only the filename/mimetype need normalising.
"""

import logging

SLACK_VOICE_FILENAME = "audio_message.ogg"

# Mimetypes Telegram/WhatsApp use for voice notes. The codec parameter is
# stripped before comparison because WhatsApp sends "audio/ogg; codecs=opus".
VOICE_MIMETYPES = {
    "audio/ogg",
    "audio/opus",
    "audio/x-opus",
    "audio/webm",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
}

VOICE_EXTENSIONS = {".ogg", ".oga", ".opus", ".webm", ".weba", ".m4a"}

# Audio that is not a voice note, even though it shares a container with one.
# Telegram music tracks arrive as audio/ogg or audio/mp4 too, and WhatsApp
# documents can carry any of these extensions.
NON_VOICE_MIMETYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/aac",
    "audio/wav",
    "audio/x-wav",
    "audio/flac",
    "audio/amr",
    "audio/3gpp",
}

NON_VOICE_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".amr", ".3gp", ".m4r", ".wma"}

# Slack's own marker for a voice message. Used when a voice note arrives from
# Slack (or was renamed by this module) and is on its way back to a client.
SLACK_VOICE_PREFIX = "audio_message"


def normalise_mimetype(mimetype):
    """Strip parameters and casing: "audio/ogg; codecs=opus" -> "audio/ogg"."""
    if not mimetype:
        return ""
    return str(mimetype).split(";")[0].strip().lower()


def _extension(filename):
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].strip().lower()


def is_voice_note(mimetype=None, filename=None, ptt=False):
    """Return True when the media is a voice note rather than music or a file.

    ``ptt`` is the platform flag (WhatsApp's ``audioMessage.ptt`` or Telegram's
    ``message.voice``) and is authoritative when present. The mimetype/extension
    check is the fallback for platforms that do not expose it.
    """
    if ptt:
        return True

    mime = normalise_mimetype(mimetype)
    ext = _extension(filename)

    if mime in NON_VOICE_MIMETYPES or ext in NON_VOICE_EXTENSIONS:
        return False

    if mime in VOICE_MIMETYPES and (not ext or ext in VOICE_EXTENSIONS):
        return True

    # No usable mimetype: fall back to the extension alone.
    return not mime and ext in VOICE_EXTENSIONS


def is_slack_voice_message(file_obj):
    """Return True for a Slack file object that is a native voice message."""
    if not file_obj:
        return False
    if (file_obj.get("subtype") or "").strip().lower() == "slack_audio":
        return True
    name = (file_obj.get("name") or "").strip().lower()
    return name.startswith(SLACK_VOICE_PREFIX)


def to_slack_voice_filename():
    """The filename Slack expects for a native voice message."""
    return SLACK_VOICE_FILENAME


def voice_caption(name):
    """Fallback text so the message is still announced/accessible."""
    return f"*{name}* sent a voice message"


def log_detection(bridge, filename, mimetype, detected, ptt=False):
    logging.info(
        f"[{bridge}] Voice check for '{filename}' (mimetype={mimetype!r}, ptt={ptt}) -> "
        f"{'VOICE MESSAGE' if detected else 'regular media'}"
    )
