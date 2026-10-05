# critical_alerts.py — reporting for operational failures.
#
# Two callers with very different needs share this module: the bridges push from
# inside a running event loop, and the dashboard watcher polls on a timer. Both
# use report() and neither needs to know how the other works.
#
# The governing rule is that alerting must never be the thing that breaks the
# system it reports on. Every failure inside report() is caught and logged; the
# function returns a dict describing what happened and never raises. A bridge
# that is already handling a message-loss bug must not be taken down by a failed
# alert about that bug.
#
# Sales and developers read different columns from the same rows. Sales sees
# summary_plain only; technical_detail and traceback live in alert_details,
# which no sales-facing code path reads.

import hashlib
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import db
    import db_store
except ImportError:  # pragma: no cover - import guard for partial deployments
    db = None  # type: ignore[assignment]
    db_store = None  # type: ignore[assignment]

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:  # pragma: no cover
    WebClient = None  # type: ignore[assignment]

    class SlackApiError(Exception):  # type: ignore[no-redef]
        def __init__(self, message: str = "", response: Optional[Dict[str, Any]] = None):
            super().__init__(message)
            self.response = response or {}


DEFAULT_ERROR_CHANNEL_NAME = "bitlink-dev-alerts"
FALLBACK_LOG = "critical_alerts_fallback.log"

# A repeat inside this window only bumps the counter and never posts again.
DEDUP_WINDOW_SECONDS = 300
# Hard ceiling on how many times any single alert may reach Slack, no matter how
# long it stays open or how often it is detected. Detection runs every 30s, so
# without this a long outage repeats the same message dozens of times and the
# channel becomes noise that people stop reading.
MAX_POSTS_PER_ALERT = 5
# Minimum gap between those repeat posts, so the five allowed posts are spread
# over the life of an outage instead of arriving as a burst in the first minutes.
REPOST_INTERVAL_SECONDS = 1800
# Absolute ceiling per component per window, regardless of distinct fingerprints.
RATE_CAP = 20

_client_cache: Dict[str, Any] = {}
_post_state: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Slack access
# ---------------------------------------------------------------------------

def _token() -> str:
    return (os.getenv("MANAGER_SLACK_BOT_TOKEN") or os.getenv("SLACK_BOT_TOKEN") or "").strip()


def get_client(token: Optional[str] = None) -> Optional[Any]:
    """A cached WebClient, or None when Slack is not configured or not installed."""
    if WebClient is None:
        return None
    tok = (token or _token())
    if not tok:
        return None
    if tok not in _client_cache:
        _client_cache[tok] = WebClient(token=tok)
    return _client_cache[tok]


def error_channel_ids() -> List[str]:
    """All active error channels. Critical alerts broadcast to all of them."""
    cids: List[str] = []
    if db_store is not None and hasattr(db_store, "list_alert_channels"):
        try:
            channels = db_store.list_alert_channels(active_only=True)
            for ch in channels:
                cid = str(ch.get("channel_id", "")).strip()
                if cid and cid not in cids:
                    cids.append(cid)
        except Exception as e:
            logging.error(f"Could not read active alert channels: {e}")
    if not cids:
        # Fallback to single setting or env var
        single = (os.getenv("CRITICAL_ALERTS_CHANNEL_ID") or "").strip()
        if not single and db_store is not None:
            try:
                single = str(db_store.get_sales_settings().get("critical_alerts_channel_id") or "").strip()
            except Exception:
                pass
        if single:
            cids.append(single)
    return cids


def error_channel_id() -> str:
    """Primary configured error channel, empty when unconfigured."""
    cids = error_channel_ids()
    return cids[0] if cids else ""


def channel_label(client: Any, channel_id: str) -> str:
    """Best-effort display name for a channel ID."""
    try:
        info = client.conversations_info(channel=channel_id)
        ch = info.get("channel", {})
        return f"#{ch.get('name', channel_id)}"
    except Exception:
        return channel_id


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def active_developer_ids(channel_id: Optional[str] = None) -> List[str]:
    if db_store is None:
        return []
    try:
        if channel_id:
            return db_store.get_developers_for_channel(channel_id)
        return db_store.get_active_developer_user_ids()
    except Exception as e:
        logging.error(f"Could not read the developer roster: {e}")
        return []


def _invite_batch(client: Any, channel_id: str, user_ids: List[str]) -> Dict[str, Any]:
    """Invite users one at a time, reporting per-user outcomes.

    A single batch conversations.invite fails as a unit, so one stale ID used to
    leave every valid member uninvited with no indication of which one failed.
    """
    invited: List[str] = []
    skipped: List[Dict[str, str]] = []
    for uid in user_ids:
        try:
            resp = client.conversations_invite(channel=channel_id, users=uid)
            if resp.get("ok") is False:
                skipped.append({"user_id": uid, "error": str(resp.get("error", "unknown"))})
            else:
                invited.append(uid)
        except SlackApiError as e:
            err = str(e.response.get("error", str(e)))
            if err in ("already_in_channel", "cant_invite_self"):
                invited.append(uid)
            elif err == "is_archived":
                try:
                    client.conversations_unarchive(channel=channel_id)
                    resp = client.conversations_invite(channel=channel_id, users=uid)
                    if resp.get("ok") is not False:
                        invited.append(uid)
                    else:
                        skipped.append({"user_id": uid, "error": str(resp.get("error", "unknown"))})
                except Exception as unarchive_err:
                    skipped.append({"user_id": uid, "error": f"is_archived (unarchive failed: {unarchive_err})"})
            else:
                skipped.append({"user_id": uid, "error": err})
        except Exception as e:
            skipped.append({"user_id": uid, "error": str(e)})
    return {"invited": invited, "skipped": skipped}


def kick_from_error_channel(slack_user_id: str, channel_id: Optional[str] = None,
                            client: Optional[Any] = None) -> Dict[str, Any]:
    """Exclude a member from the error channel via conversations.kick."""
    slack_user_id = (slack_user_id or "").strip()
    if not slack_user_id:
        return {"ok": False, "reason": "no_user_id"}
    client = client or get_client()
    channel_id = (channel_id or error_channel_id()).strip()
    if client is None or not channel_id:
        return {"ok": False, "reason": "slack_not_configured" if client is None else "no_channel"}

    master = (os.getenv("MASTER_SLACK_BOT_ID") or "").strip()
    if slack_user_id == master:
        return {"ok": False, "reason": "cannot_kick_bot"}

    try:
        resp = client.conversations_kick(channel=channel_id, user=slack_user_id)
        if resp.get("ok"):
            logging.info(f"Successfully excluded {slack_user_id} from error channel {channel_id}")
            return {"ok": True, "user_id": slack_user_id, "channel_id": channel_id}
        else:
            err = str(resp.get("error", "unknown"))
            logging.warning(f"Slack refused to exclude {slack_user_id} from {channel_id}: {err}")
            return {"ok": False, "error": err}
    except SlackApiError as e:
        err = str(e.response.get("error", str(e)))
        if err in ("not_in_channel", "already_kicked"):
            logging.info(f"User {slack_user_id} is not in channel {channel_id}")
            return {"ok": True, "user_id": slack_user_id, "note": err}
        if err == "cant_kick_self":
            return {"ok": False, "error": "cant_kick_self"}
        logging.warning(f"Slack API error excluding {slack_user_id} from {channel_id}: {err}")
        return {"ok": False, "error": err}
    except Exception as e:
        logging.warning(f"Could not exclude {slack_user_id} from {channel_id}: {e}")
        return {"ok": False, "error": str(e)}


def sync_channel_membership(client: Optional[Any] = None, channel_id: Optional[str] = None,
                            remove_inactive: bool = True) -> Dict[str, Any]:
    """Bring the error channel's membership in line with the developers table.

    Adds active developers and the master bot. Removal needs conversations.kick,
    which may not be granted; when it is missing the row is still deactivated and
    membership is corrected at the next successful sync rather than failing here.
    """
    client = client or get_client()
    channel_id = (channel_id or error_channel_id()).strip()
    if client is None or not channel_id:
        return {"ok": False, "reason": "slack_not_configured" if client is None else "no_channel"}

    wanted = active_developer_ids(channel_id=channel_id)
    master = (os.getenv("MASTER_SLACK_BOT_ID") or "").strip()
    if master:
        wanted.append(master)
    wanted = list(dict.fromkeys(wanted))

    result = _invite_batch(client, channel_id, wanted)

    removed: List[str] = []
    removal_failed = False
    if remove_inactive and db_store is not None:
        try:
            all_devs = db_store.list_developers()
            unwanted = [r["slack_user_id"] for r in all_devs
                        if (not r.get("active") or r.get("slack_user_id") not in wanted)
                        and r.get("slack_user_id")]
            for uid in unwanted:
                if uid == master:
                    continue
                kick = kick_from_error_channel(uid, channel_id=channel_id, client=client)
                if kick.get("ok") and not kick.get("note"):
                    removed.append(uid)
                elif not kick.get("ok"):
                    removal_failed = True
        except Exception as e:
            logging.error(f"Failed to read developers for removal from {channel_id}: {e}")

    if result["skipped"]:
        logging.warning(f"Error channel {channel_id} invite skipped: {result['skipped']}")
    return {"ok": True, "channel_id": channel_id, "invited": result["invited"],
            "skipped": result["skipped"], "removed": removed,
            "removal_incomplete": removal_failed}


def validate_slack_user(client: Optional[Any], slack_user_id: str) -> Dict[str, Any]:
    """Confirm a Slack ID resolves to a real user, before it is saved to a roster."""
    slack_user_id = (slack_user_id or "").strip()
    if not slack_user_id:
        return {"ok": False, "error": "A Slack user ID is required."}
    client = client or get_client()
    if client is None:
        return {"ok": False, "error": "Slack is not configured on this server.", "skipped": True}
    try:
        info = client.users_info(user=slack_user_id)
        u = info.get("user", {})
        if not u.get("id"):
            return {"ok": False, "error": "Slack did not return a user for that ID."}
        return {"ok": True, "user_id": u.get("id"),
                "name": u.get("real_name") or u.get("name") or u.get("id")}
    except SlackApiError as e:
        return {"ok": False, "error": str(e.response.get("error", str(e)))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Channel provisioning
# ---------------------------------------------------------------------------

def _get_admin_user_ids() -> List[str]:
    """Return Slack user IDs of dashboard users with role='admin'.

    Falls back to an empty list when the auth store is unavailable.
    """
    if db_store is None:
        return []
    try:
        rows = db.query_all(
            "SELECT id, role FROM dashboard_users WHERE role = 'admin'"
        )
        return [str(r["id"]).strip() for r in rows if r.get("id") and r.get("role") == "admin"]
    except Exception as e:
        logging.error(f"Could not read admin users: {e}")
        return []


def create_error_channel(name: str = DEFAULT_ERROR_CHANNEL_NAME,
                         client: Optional[Any] = None,
                         is_private: bool = True) -> Dict[str, Any]:
    """Create a private error channel and invite the full admin roster.

    The channel is created as a private channel (is_private=True) by default.
    After creation the function invites every member of the project‑managers roster,
    the error‑channel developers roster, and every dashboard user whose role is
    'admin'. A missing Slack scope or a rate‑limit is reported rather than
    raising, so the caller can fall back to the manual channel‑ID field.
    """
    client = client or get_client()
    if client is None:
        return {"ok": False, "error": "Slack is not configured on this server."}
    try:
        resp = client.conversations_create(name=name, is_private=is_private)
    except SlackApiError as e:
        code = e.response.get("error")
        if code == "missing_scope":
            return {"ok": False, "error": "missing_scope",
                    "detail": "This bot cannot create channels. Add conversations.create or groups:write, "
                              "or paste an existing channel ID into the field below."}
        if code == "name_taken":
            return {"ok": False, "error": "name_taken",
                    "detail": f"'{name}' already exists. Paste its ID into the field below."}
        return {"ok": False, "error": code or str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    ch = resp.get("channel", {}) or {}
    channel_id = ch.get("id")
    if not channel_id:
        return {"ok": False, "error": "Slack created the channel but returned no ID."}

    # ---- Invite everyone who should see technical failures ----
    invited: List[str] = []

    # 1. Project managers (client‑channel oversight)
    for uid in db_store.list_project_managers():
        if uid.get("slack_user_id"):
            invited.append(uid["slack_user_id"])

    # 2. Error‑channel developers
    for uid in db_store.get_active_developer_user_ids():
        invited.append(uid)

    # 3. Dashboard admins
    for uid in _get_admin_user_ids():
        invited.append(uid)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for uid in invited:
        if uid not in seen:
            seen.add(uid)
            deduped.append(uid)

    # Invite each user individually so one bad ID never blocks the whole batch
    for uid in deduped:
        try:
            client.conversations_invite(channel=channel_id, users=uid)
            invited.append(uid)  # track successfully invited
        except SlackApiError as e:
            # Log but do not abort the whole sync
            logging.warning(
                f"Could not invite {uid} to {channel_id}: {e.response.get('error')}"
            )
        except Exception as e:
            logging.warning(f"Unexpected error inviting {uid} to {channel_id}: {e}")

    sync = sync_channel_membership(client=client, channel_id=channel_id)
    if db_store is not None:
        try:
            db_store.set_setting("critical_alerts_channel_id", channel_id,
                                 note="Set from the Developers page")
        except Exception as e:
            logging.error(f"Failed to set critical_alerts_channel_id setting: {e}")
    return {"ok": True, "channel_id": channel_id,
            "channel_name": ch.get("name", name), "membership": sync,
            "invited": len(deduped),
            "invited_ids": deduped}


create_public_channel = create_error_channel


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def make_fingerprint(component: str, code: str, key: Optional[str] = None) -> str:
    """Stable identity for dedup.

    key should identify the specific thing that failed (a client external_id, a
    channel), not the individual message, so a broken client produces one alert
    with a rising counter rather than thousands of rows.
    """
    raw = f"{component}|{code}|{key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _buffer_to_file(entry: Dict[str, Any]) -> None:
    try:
        with open(FALLBACK_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logging.error(f"Could not buffer alert to {FALLBACK_LOG}: {e}")


def _post_to_channel(client: Any, channel_id: str, severity: str, code: str,
                     component: str, summary_plain: str, technical_detail: str,
                     tb_text: Optional[str], context: Dict[str, Any],
                     alert_id: Optional[int]) -> Optional[str]:
    header = f":rotating_light: *{severity.upper()}* — `{code}` ({component})"
    lines = [header, "", f"*Sales-facing:* {summary_plain}"]
    if context:
        ctx = json.dumps(context, default=str)
        if len(ctx) > 800:
            ctx = ctx[:800] + "…"
        lines.append(f"*Context:* ```{ctx}```")
    lines.append(f"*Technical:* ```{technical_detail}```")
    if tb_text:
        trimmed = tb_text if len(tb_text) <= 1500 else tb_text[-1500:]
        lines.append("*Traceback:*\n```" + trimmed + "```")
    if alert_id:
        lines.append(f"_Dashboard alert id: {alert_id}_")

    resp = client.chat_postMessage(channel=channel_id, text="\n".join(lines))
    if not resp.get("ok"):
        raise RuntimeError(f"chat_postMessage failed: {resp.get('error')}")
    ts = (resp.get("ts") or "").replace(".", "")
    return f"https://app.slack.com/archives/{channel_id}/p{ts}" if ts else None


def _should_post(state: Dict[str, Any], occurrences: int, is_new: bool) -> bool:
    """Apply the per-alert post budget and the component rate cap.

    The budget is counted in successful posts, not in detections. Detection runs
    every 30s, so keying the cap off occurrences would either allow far more
    than five messages or silence the alert almost immediately. Counting real
    posts means an alert is announced at most MAX_POSTS_PER_ALERT times and then
    stays quiet, while still being re-announced while it remains unresolved.

    ``last_post`` lives in an in-memory dict that resets on every process
    restart. When it is None it means either (a) the alert is brand new, or
    (b) this process has never posted it — in both cases we should post so
    that a restart does not permanently silence a pre-existing open alert.
    """
    if state.get("posts", 0) >= MAX_POSTS_PER_ALERT:
        return False
    now = time.time()
    last = state.get("last_post")
    # No posting history in this process — always notify so restarts don't
    # silence alerts that were already firing before the restart.
    if last is None:
        return True
    if now - last >= REPOST_INTERVAL_SECONDS:
        return True
    return False


def report(code: str, severity: str = "critical", component: str = "dashboard",
           summary_plain: str = "", technical_detail: str = "", context: Optional[Dict[str, Any]] = None,
           key: Optional[str] = None, tb_text: Optional[str] = None) -> Dict[str, Any]:
    """Record an operational failure and notify the developer channel.

    Never raises. The PostgreSQL write and the Slack post are attempted
    independently so that one being down does not suppress the other, and an
    unstoreable alert is buffered to a file for replay.

    summary_plain is what sales will read: no exception names, no IDs, no jargon.
    technical_detail is the full truth for the developer channel.
    """
    out: Dict[str, Any] = {"stored": False, "posted": False, "alert_id": None,
                          "occurrences": 1, "reason": None}
    context = context or {}
    summary_plain = summary_plain or "A system component failed unexpectedly."
    technical_detail = technical_detail or summary_plain
    if tb_text is None and sys.exc_info()[0] is not None:
        tb_text = traceback.format_exc()

    fingerprint = make_fingerprint(component, code, key)

    cap_key = f"cap:{component}"
    window = _post_state.setdefault(cap_key, {"count": 0, "window_start": time.time()})
    if time.time() - window["window_start"] > DEDUP_WINDOW_SECONDS:
        window["count"] = 0
        window["window_start"] = time.time()
    window["count"] += 1
    if window["count"] > RATE_CAP:
        logging.error(
            f"Alert rate cap hit for {component} ({code}); suppressing Slack post. "
            f"The failure is still being recorded.")
        out["reason"] = "rate_capped"
    cap_exceeded = out["reason"] == "rate_capped"

    try:
        rec = db_store.record_alert(
            fingerprint=fingerprint, code=code, severity=severity, component=component,
            summary_plain=summary_plain, context=context,
            technical_detail=technical_detail, traceback_text=tb_text)
        out.update({"stored": True, "alert_id": rec["alert_id"],
                    "occurrences": rec["occurrences"], "is_new": rec["is_new"]})
    except Exception as e:
        out["reason"] = out["reason"] or "store_failed"
        out["store_error"] = str(e)
        logging.error(f"Alert {code} could not be stored in PostgreSQL: {e}")
        _buffer_to_file({
            "at": datetime.now(timezone.utc).isoformat(), "code": code, "severity": severity,
            "component": component, "summary_plain": summary_plain,
            "technical_detail": technical_detail, "context": context,
            "fingerprint": fingerprint, "traceback": tb_text})

    channel_ids = error_channel_ids()
    client = get_client()
    if not channel_ids:
        if out["reason"] is None:
            out["reason"] = "no_channel"
        logging.error(
            f"Critical alert {code} recorded but no error channel is configured, "
            f"so nobody was notified. Set one on the Developers page.")
        return out
    if client is None or cap_exceeded:
        return out

    state = _post_state.setdefault(fingerprint, {"last_post": None, "posts": 0})
    if not _should_post(state, out["occurrences"], out.get("is_new", False)):
        out["reason"] = "deduped"
        return out

    permalinks: List[str] = []
    for cid in channel_ids:
        try:
            plink = _post_to_channel(client, cid, severity, code, component,
                                     summary_plain, technical_detail, tb_text, context,
                                     out["alert_id"])
            if plink:
                permalinks.append(plink)
        except Exception as e:
            out["reason"] = out["reason"] or "post_failed"
            out["post_error"] = str(e)
            logging.error(f"Alert {code} was stored but could not be posted to {cid}: {e}")

    if permalinks:
        state["last_post"] = time.time()
        state["posts"] = state.get("posts", 0) + 1
        out["posted"] = True
        out["permalink"] = permalinks[0]
        out["permalinks"] = permalinks
        if out["alert_id"]:
            db_store.set_alert_permalink(out["alert_id"], permalinks[0])

    return out


# ---------------------------------------------------------------------------
# Unhandled-exception capture
# ---------------------------------------------------------------------------
#
# A script dying on an unhandled exception is the most serious failure there is
# and the one most likely to go unnoticed: the traceback scrolls past in a log
# nobody is watching, and a supervisor restart overwrites it within seconds.
# These hooks turn every unhandled exception, in any thread, into a critical
# alert at the moment it happens.
#
# Two rules keep this from being worse than the problem it solves. The previous
# hooks always run afterwards, so traceback output and process-exit behaviour are
# unchanged. And every step is guarded: an alerting path that raised while
# reporting a crash would replace a loud failure with a silent one.

_excepthook_state = threading.local()


def _report_uncaught(exc_type, exc_value, exc_tb, component: str, thread_name: str) -> None:
    """Report one unhandled exception. Never raises."""
    if getattr(_excepthook_state, "busy", False):
        # Already reporting an exception on this thread; a failure inside
        # report() must not recurse into another alert.
        return
    _excepthook_state.busy = True
    try:
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        where = f" in thread '{thread_name}'" if thread_name and thread_name != "main" else ""
        try:
            frames = traceback.extract_tb(exc_tb)
            site = ""
            if frames:
                last = frames[-1]
                site = f"{os.path.basename(last.filename)}:{last.name}"
        except Exception:
            site = ""
        report(
            code="UNHANDLED_EXCEPTION",
            severity="critical",
            component=component,
            summary_plain="A part of the system hit an unexpected error and may have "
                          "stopped working. Messages could be delayed or missed.",
            technical_detail=f"Unhandled {exc_type.__name__}{where}: {exc_value}"
                             + (f" (raised at {site})" if site else ""),
            context={"exception": exc_type.__name__, "thread": thread_name, "site": site},
            key=f"{exc_type.__name__}@{site}" if site else exc_type.__name__,
            tb_text=tb_text,
        )
    except Exception:
        pass
    finally:
        _excepthook_state.busy = False


def install_excepthook(component: str) -> None:
    """Route unhandled exceptions from the main thread and all worker threads
    into critical alerts.

    Call once, as early as possible in a service's __main__. Safe to call more
    than once; the previously installed hooks are chained, not replaced.
    """
    previous_sys_hook = sys.excepthook
    previous_thread_hook = getattr(threading, "excepthook", None)

    def _sys_hook(exc_type, exc_value, exc_tb):
        _report_uncaught(exc_type, exc_value, exc_tb, component, "main")
        previous_sys_hook(exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        _report_uncaught(args.exc_type, args.exc_value, args.exc_traceback,
                         component, getattr(getattr(args, "thread", None), "name", "?"))
        if previous_thread_hook:
            previous_thread_hook(args)

    sys.excepthook = _sys_hook
    if previous_thread_hook:
        threading.excepthook = _thread_hook
    logging.info(f"Unhandled-exception alerting installed for {component}.")


# ---------------------------------------------------------------------------
# Conftest-style reset for tests
# ---------------------------------------------------------------------------

def reset_state() -> None:
    """Clear cached posting state. For tests and after a config change."""
    _post_state.clear()