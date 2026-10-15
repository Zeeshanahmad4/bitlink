# control_server.py — Bitlink Control Dashboard REST API Server
import os
import logging
import threading
import time
import requests
from flask import Flask, request, jsonify, render_template, session, send_from_directory, make_response
from dotenv import load_dotenv
load_dotenv()

import db
import db_store
import critical_alerts as alerts

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

from datetime import timedelta
import uuid
from typing import Any, Dict, Optional

# Data access lives in db_store, which is PostgreSQL-only. There is no fallback
# store, so a database outage surfaces to the client as a 500 rather than
# silently serving stale rows.

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Generate a new session key every time control_server.py starts so all clients must re-authenticate on restart
app.secret_key = str(uuid.uuid4())
app.permanent_session_lifetime = timedelta(hours=24)
DASHBOARD_PASSCODE = os.getenv("DASHBOARD_PASSCODE", "admin123")

# Native CORS handler (zero external pip dependencies)
@app.after_request
def enable_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route('/')
@app.route('/delivery')
def index():
    """Renders the main Control Dashboard web interface."""
    return render_template('index.html')

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def serve_sw():
    response = make_response(send_from_directory('static', 'sw.js', mimetype='application/javascript'))
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/offline.html')
def serve_offline():
    return render_template('offline.html')

# --- Authentication Endpoints ---

def verify_credentials(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Verifies dashboard credentials against the bcrypt hashes in PostgreSQL.

    Returns the account record (including its role) on success, None otherwise.

    The old hardcoded admin/admin bypass is gone. If credential lookup fails
    outright we deny the login rather than granting a default account.
    """
    try:
        return db_store.verify_login(username.strip(), password.strip())
    except Exception as e:
        logging.error(f"Error checking credentials: {e}")
        return None


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(force=True) if request.data else {}
    username = str(data.get("username") or data.get("id") or "").strip()
    password = str(data.get("password") or data.get("passcode") or "").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required."}), 400

    account = verify_credentials(username, password)
    if account:
        session.permanent = True
        session["authenticated"] = True
        session["user"] = username
        # A session issued before the role column existed carries no role. That
        # must read as non-admin, never as admin, so an upgrade cannot widen access.
        session["role"] = account.get("role") or "sales"
        session["login_time"] = time.time()
        logging.info(f"User '{username}' logged in as role '{session['role']}' (valid for 24 hours).")
        return jsonify({"success": True, "message": "Authentication successful.",
                        "user": username, "role": session["role"]})
    
    logging.warning(f"Failed login attempt for user '{username}'.")
    return jsonify({"success": False, "error": "Invalid username or password."}), 401


@app.route('/api/auth/check', methods=['GET'])
def auth_check():
    is_auth = session.get("authenticated", False)
    login_time = session.get("login_time", 0)
    # Check 24 hours (86400 seconds) timeout
    if is_auth and (time.time() - login_time > 86400):
        session.pop("authenticated", None)
        session.pop("user", None)
        session.pop("login_time", None)
        session.pop("role", None)
        is_auth = False
        user = ""
        role = "sales"
    else:
        user = session.get("user", "")
        role = session.get("role", "sales") or "sales"
    return jsonify({"authenticated": is_auth, "user": user, "role": role})


def is_admin() -> bool:
    """True only for an explicitly admin session.

    A missing or unrecognised role is non-admin. Never widen this default: the
    Developers page exposes technical detail and channel credentials.
    """
    return session.get("authenticated", False) is True and session.get("role") == "admin"


def require_admin() -> Optional[Any]:
    """Guard for every admin-only endpoint. Returns a 403 response or None."""
    if is_admin():
        return None
    return jsonify({"success": False,
                    "error": "This area is restricted to administrator accounts."}), 403


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    user = session.pop("user", "User")
    session.pop("authenticated", None)
    logging.info(f"User '{user}' logged out.")
    return jsonify({"success": True, "message": "Logged out successfully."})

PORT = int(os.getenv("CONTROL_DASHBOARD_PORT", 5050))

# --- Bridge Host & Port Discovery ---
# In Docker, containers reach each other via service hostnames ('wa-bridge', 'tg-bridge', 'gateway').
# Outside Docker, fallback to 127.0.0.1.
WA_BRIDGE_HOST = os.getenv("WHATSAPP_BRIDGE_HOST", "wa-bridge" if os.path.exists("/.dockerenv") else "127.0.0.1")
TG_BRIDGE_HOST = os.getenv("TELEGRAM_BRIDGE_HOST", "tg-bridge" if os.path.exists("/.dockerenv") else "127.0.0.1")
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "gateway" if os.path.exists("/.dockerenv") else "127.0.0.1")
WA_PORT = int(os.getenv("WHATSAPP_REFRESH_PORT", 8102))
TG_PORT = int(os.getenv("TELEGRAM_REFRESH_PORT", 8004))
GATEWAY_PORT = int(os.getenv("PORT", 3101))


# --- Bridge Auto-Refresh Dispatcher ---

def dispatch_bridge_refresh():
    """Dispatches background refresh requests to running bridge services."""
    def _worker():
        targets = [
            ("WhatsApp", WA_BRIDGE_HOST, WA_PORT),
            ("Telegram", TG_BRIDGE_HOST, TG_PORT),
        ]
        headers = {'Content-Type': 'application/json'}
        for name, host, port in targets:
            url = f"http://{host}:{port}/refresh"
            try:
                requests.post(url, headers=headers, timeout=2.0)
                logging.info(f"⚡ [REFRESH] Signal sent successfully to {name} bridge at {url}")
            except requests.exceptions.RequestException:
                if host != "127.0.0.1":
                    try:
                        requests.post(f"http://127.0.0.1:{port}/refresh", headers=headers, timeout=1.0)
                        logging.info(f"⚡ [REFRESH] Signal sent successfully to {name} bridge on 127.0.0.1:{port}")
                        continue
                    except Exception:
                        pass
                logging.debug(f"ℹ️ [REFRESH] Could not reach {name} bridge at {url} (may be offline)")

    threading.Thread(target=_worker, daemon=True).start()


# ==============================================================================
# 1. SYSTEM HEALTH API
# ==============================================================================

@app.route('/api/health', methods=['GET'])
def get_system_health():
    """Returns real-time health of all running bridges and gateway services."""
    bridges = {}

    # WhatsApp Node Gateway
    gateway_url = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/health"
    try:
        r = requests.get(gateway_url, timeout=1.5)
        details = r.json() if r.status_code == 200 else {}
        ready = str(details.get("status", "")).lower() == "ready"
        bridges["whatsapp_gateway"] = {"online": r.status_code == 200 and ready, "details": details}
    except Exception:
        try:
            r = requests.get(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=1.0)
            details = r.json() if r.status_code == 200 else {}
            ready = str(details.get("status", "")).lower() == "ready"
            bridges["whatsapp_gateway"] = {"online": r.status_code == 200 and ready, "details": details}
        except Exception:
            bridges["whatsapp_gateway"] = {"online": False}

    # WhatsApp Python Bridge
    wa_url = f"http://{WA_BRIDGE_HOST}:{WA_PORT}/health"
    try:
        r = requests.get(wa_url, timeout=1.5)
        details = r.json() if r.status_code == 200 else {}
        bridges["whatsapp_bridge"] = {
            "online": r.status_code == 200,
            "emergency_stop": details.get("emergency_stop", {"active": False}),
            "details": details,
        }
    except Exception:
        try:
            r = requests.get(f"http://127.0.0.1:{WA_PORT}/health", timeout=1.0)
            details = r.json() if r.status_code == 200 else {}
            bridges["whatsapp_bridge"] = {
                "online": r.status_code == 200,
                "emergency_stop": details.get("emergency_stop", {"active": False}),
                "details": details,
            }
        except Exception:
            bridges["whatsapp_bridge"] = {"online": False, "emergency_stop": {"active": False}}

    # Telegram Python Bridge
    tg_url = f"http://{TG_BRIDGE_HOST}:{TG_PORT}/health"
    try:
        r = requests.get(tg_url, timeout=1.5)
        bridges["telegram_bridge"] = {"online": r.status_code == 200, "details": r.json() if r.status_code == 200 else {}}
    except Exception:
        try:
            r = requests.get(f"http://127.0.0.1:{TG_PORT}/health", timeout=1.0)
            bridges["telegram_bridge"] = {"online": r.status_code == 200, "details": r.json() if r.status_code == 200 else {}}
        except Exception:
            bridges["telegram_bridge"] = {"online": False}

    # PostgreSQL — a dashboard that cannot reach its own store is not healthy
    try:
        bridges["postgres"] = {"online": db.is_available()}
    except Exception:
        bridges["postgres"] = {"online": False}

    return jsonify({"status": "ok", "bridges": bridges, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")})


# ==============================================================================
# 2. DELIVERY MAPPINGS API (client_mappings table)
# ==============================================================================

@app.route('/api/delivery/mappings', methods=['GET'])
def list_delivery_mappings():
    """Lists all delivery client mappings. 'row_idx' is the stable database id."""
    try:
        records = db_store.list_all_client_mappings()
        mappings = [{
            "row_idx": r["id"],
            "client_name": r.get("client_name", ""),
            "platform": r.get("platform", "WhatsApp"),
            "external_id": str(r.get("external_id", "")),
            "slack_channel_id": str(r.get("slack_channel_id", "")),
            "paused": bool(r.get("paused"))
        } for r in records]
        return jsonify({"success": True, "data": mappings, "count": len(mappings)})
    except Exception as e:
        logging.error(f"Error fetching delivery mappings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/delivery/mappings', methods=['POST'])
def add_delivery_mapping():
    """Adds a new delivery client mapping by auto-provisioning a new private Slack channel."""
    data = request.get_json(force=True)
    client_name = data.get("client_name", "").strip()
    platform = data.get("platform", "WhatsApp").strip()
    external_id = data.get("external_id", "").strip()
    slack_channel_name = data.get("slack_channel_name", "").strip()
    paused = bool(data.get("paused"))

    if not client_name or not external_id:
        return jsonify({"success": False, "error": "Missing required fields: client_name, external_id"}), 400

    provisioned_channel_id = None
    try:
        from slack_sdk import WebClient
        import re

        # Use appropriate token based on platform
        bot_token = os.getenv("SLACK_BOT_TOKEN") if platform.lower() == "whatsapp" else os.getenv("SLACK_BOT_TOKEN_TELEGRAM", os.getenv("SLACK_BOT_TOKEN"))
        wc = WebClient(token=bot_token)

        if slack_channel_name:
            ch_name = re.sub(r"[^a-z0-9-]", "", slack_channel_name.lower())
        else:
            # Generate channel slug fallback: del-client-1234
            clean_name = re.sub(r"[^a-zA-Z0-9\s-]", "", client_name).strip().lower()
            slug = re.sub(r"[\s_]+", "-", clean_name)[:15]
            if not slug:
                slug = external_id[-4:] if len(external_id) >= 4 else "client"
            ch_name = f"del-{slug}-{external_id[-4:]}"

        logging.info(f"Auto-provisioning delivery channel: {ch_name} (Private)")
        response = wc.conversations_create(name=ch_name, is_private=True)
        slack_channel_id = response["channel"]["id"]
        provisioned_channel_id = slack_channel_id

        # Auto-invite Manager Bot and Project Managers
        master_bot = os.getenv("MASTER_SLACK_BOT_ID")
        users_to_invite = []
        if master_bot: users_to_invite.append(master_bot)

        try:
            for rec in db_store.get_project_managers():
                if rec.get("active") and rec.get("slack_user_id"):
                    users_to_invite.append(str(rec["slack_user_id"]).strip())
        except Exception as e:
            logging.warning(f"Failed to fetch project managers: {e}")

        # If no PMs found, fallback to ENV var just in case
        if len(users_to_invite) == (1 if master_bot else 0):
            pm_env = os.getenv("PROJECT_MANAGER_SLACK_ID")
            if pm_env: users_to_invite.append(pm_env)

        users_to_invite = list(set(users_to_invite))
        if users_to_invite:
            try:
                wc.conversations_invite(channel=slack_channel_id, users=",".join(users_to_invite))
            except Exception as e:
                logging.warning(f"Failed to auto-invite users {users_to_invite} to delivery channel: {e}")
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to auto-provision channel: {e}"}), 500

    try:
        # 1. Idempotency check
        for rec in db_store.list_all_client_mappings():
            if str(rec.get("external_id", "")) == external_id and \
               str(rec.get("slack_channel_id", "")) == slack_channel_id:
                return jsonify({"success": False, "error": "Client is already mapped to Delivery."}), 409

        db_store.add_client_mapping(platform, client_name, external_id,
                                    slack_channel_id, paused)

        # 2. Auto-pause the matching sales lead and lock it as handed over
        sales_lead_id = data.get("sales_lead_id") or data.get("salesRowToPause")
        if sales_lead_id:
            try:
                db_store.update_sales_lead(int(sales_lead_id), {"paused": True, "delivery_handed_off": True})
            except Exception as e:
                logging.error(f"Failed to auto-pause sales lead by id {sales_lead_id}: {e}")

        try:
            db_store.update_sales_lead_by_external_id(external_id, {"paused": True, "delivery_handed_off": True})
        except Exception as e:
            logging.error(f"Failed to auto-pause sales lead by external_id {external_id}: {e}")

        dispatch_bridge_refresh()
        logging.info(f"Created delivery mapping: {client_name} ({external_id}) -> {slack_channel_id}")
        return jsonify({"success": True, "message": "Delivery mapping created successfully.", "slack_channel_id": slack_channel_id}), 201
    except Exception as e:
        logging.error(f"Error adding delivery mapping: {e}")
        if provisioned_channel_id:
            try:
                from slack_sdk import WebClient as _WC
                bot_token = os.getenv("SLACK_BOT_TOKEN") if platform.lower() == "whatsapp" \
                    else os.getenv("SLACK_BOT_TOKEN_TELEGRAM", os.getenv("SLACK_BOT_TOKEN"))
                _WC(token=bot_token).conversations_archive(channel=provisioned_channel_id)
                logging.warning(f"Archived orphaned Slack channel {provisioned_channel_id} after the mapping failed to save.")
            except Exception as e2:
                logging.error(f"Could not archive orphaned channel {provisioned_channel_id}: {e2}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/delivery/mappings/<int:row_idx>', methods=['PUT'])
def update_delivery_mapping(row_idx: int):
    """Updates an existing delivery client mapping. If slack_channel_name is provided, creates a new channel and pauses old."""
    data = request.get_json(force=True)
    try:
        current = db_store.get_client_mapping_by_id(row_idx)
        if not current:
            return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404

        client_name = data.get("client_name", current.get("client_name", "")).strip()
        platform = data.get("platform", current.get("platform", "WhatsApp")).strip()
        external_id = data.get("external_id", current.get("external_id", "")).strip()
        old_channel_id = current.get("slack_channel_id", "")
        new_channel_name = (data.get("slack_channel_name") or "").strip()

        fields = {
            "client_name": client_name,
            "platform": platform,
            "external_id": external_id,
            "slack_channel_id": old_channel_id,
        }
        if "paused" in data:
            fields["paused"] = bool(data["paused"])

        # If a new channel name was entered, provision the new channel and pause the old one
        if new_channel_name:
            from slack_sdk import WebClient
            import re

            bot_token = os.getenv("SLACK_BOT_TOKEN") if platform.lower() == "whatsapp" else os.getenv("SLACK_BOT_TOKEN_TELEGRAM", os.getenv("SLACK_BOT_TOKEN"))
            wc = WebClient(token=bot_token)

            ch_name = re.sub(r"[^a-z0-9-]", "", new_channel_name.lower())
            if not ch_name:
                clean_name = re.sub(r"[^a-zA-Z0-9\s-]", "", client_name).strip().lower()
                slug = re.sub(r"[\s_]+", "-", clean_name)[:15]
                if not slug:
                    slug = external_id[-4:] if len(external_id) >= 4 else "client"
                ch_name = f"del-{slug}-{external_id[-4:]}"

            logging.info(f"Auto-provisioning new delivery channel for {client_name}: {ch_name} (Private)")
            try:
                response = wc.conversations_create(name=ch_name, is_private=True)
                new_channel_id = response["channel"]["id"]
            except Exception as e:
                logging.error(f"Failed to create new Slack channel '{ch_name}': {e}")
                return jsonify({"success": False, "error": f"Failed to create new Slack channel: {e}"}), 500

            # Auto-invite Master Bot and Project Managers
            master_bot = os.getenv("MASTER_SLACK_BOT_ID")
            users_to_invite = []
            if master_bot: users_to_invite.append(master_bot)
            try:
                for rec in db_store.get_project_managers():
                    if rec.get("active") and rec.get("slack_user_id"):
                        users_to_invite.append(str(rec["slack_user_id"]).strip())
            except Exception as e:
                logging.warning(f"Failed to fetch project managers: {e}")

            if len(users_to_invite) == (1 if master_bot else 0):
                pm_env = os.getenv("PROJECT_MANAGER_SLACK_ID")
                if pm_env: users_to_invite.append(pm_env)

            users_to_invite = list(set(users_to_invite))
            if users_to_invite:
                try:
                    wc.conversations_invite(channel=new_channel_id, users=",".join(users_to_invite))
                except Exception as e:
                    logging.warning(f"Failed to auto-invite users to new channel: {e}")

            # Notify old channel and keep old channel registered as paused
            if old_channel_id and old_channel_id != new_channel_id:
                try:
                    wc.chat_postMessage(
                        channel=old_channel_id,
                        text=f"*Info:* Delivery communication for *{client_name}* has been moved to <#{new_channel_id}>. This channel is now paused and all communication will take place in the new channel.",
                        username="Bitlink Bridge Info"
                    )
                except Exception as e:
                    logging.warning(f"Failed to post migration notice to old channel {old_channel_id}: {e}")

                # Save old channel in client_mappings with paused=True so the bridges continue to block it
                try:
                    db_store.add_client_mapping(platform, client_name, external_id, old_channel_id, paused=True)
                except Exception as e:
                    logging.warning(f"Failed to record old channel as paused in client_mappings: {e}")

            fields["slack_channel_id"] = new_channel_id
            fields["paused"] = False

        ok = db_store.update_client_mapping(row_idx, fields)
        if not ok:
            return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404

        dispatch_bridge_refresh()
        logging.info(f"Updated delivery mapping {row_idx}: {fields.get('client_name')}")
        return jsonify({
            "success": True,
            "message": f"Mapping {row_idx} updated successfully." + (f" New channel <#{fields['slack_channel_id']}> created and old channel paused." if new_channel_name else ""),
            "slack_channel_id": fields["slack_channel_id"],
            "paused": fields.get("paused", False)
        })
    except Exception as e:
        logging.error(f"Error updating delivery mapping {row_idx}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/delivery/mappings/<int:row_idx>/pause', methods=['POST'])
def toggle_pause_delivery_mapping(row_idx: int):
    """Toggles or sets the paused state for a delivery mapping."""
    data = request.get_json(force=True) if request.data else {}
    try:
        current = db_store.get_client_mapping_by_id(row_idx)
        if not current:
            return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404

        # If explicit 'paused' bool provided in body, use it; otherwise toggle
        new_state = bool(data["paused"]) if "paused" in data else (not bool(current.get("paused")))

        if not db_store.update_client_mapping(row_idx, {"paused": new_state}):
            return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404

        dispatch_bridge_refresh()
        logging.info(f"Toggled pause on mapping {row_idx} to {new_state}")
        return jsonify({"success": True, "row_idx": row_idx, "paused": new_state})
    except Exception as e:
        logging.error(f"Error toggling pause on mapping {row_idx}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/delivery/mappings/<int:row_idx>', methods=['DELETE'])
def delete_delivery_mapping(row_idx: int):
    """Deletes a delivery client mapping."""
    if not db_store.delete_client_mapping(row_idx):
        return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404
    dispatch_bridge_refresh()
    logging.info(f"Deleted delivery mapping {row_idx}")
    return jsonify({"success": True, "message": f"Mapping {row_idx} deleted."})


# ==============================================================================
# 3. SALES CRM LEADS API (Sales Leads)
# ==============================================================================

@app.route('/api/sales/leads', methods=['GET'])
def list_sales_leads():
    """Lists all CRM sales leads with optional filter by stage or search."""
    stage_filter = request.args.get("stage", "").strip().lower()
    search = request.args.get("search", "").strip().lower()
    try:
        leads = []

        # Load delivery external IDs to filter out 'won' leads already delivered
        delivery_ext_ids = set()
        if stage_filter == "won":
            try:
                for dr in db_store.list_all_client_mappings():
                    if str(dr.get("external_id", "")):
                        delivery_ext_ids.add(str(dr.get("external_id", "")))
            except Exception as e:
                logging.error(f"Failed to fetch delivery records for filter: {e}")

        for row in db_store.get_sales_leads():
            lead_stage = str(row.get("stage", "new")).strip().lower()
            if stage_filter and lead_stage != stage_filter:
                continue

            cname = str(row.get("client_name", ""))
            ext_id = str(row.get("external_id", ""))
            if search and (search not in cname.lower() and search not in ext_id.lower()):
                continue

            if stage_filter == "won" and ext_id in delivery_ext_ids:
                continue

            leads.append({
                "row_idx": row["id"],
                "client_name": cname,
                "platform": row.get("platform", "whatsapp"),
                "external_id": ext_id,
                "slack_channel_id": str(row.get("slack_channel_id", "")),
                "tag": row.get("tag", "sales"),
                "owner_slack_id": str(row.get("owner_slack_id", "")),
                "stage": lead_stage,
                "lead_score": str(row.get("lead_score", "warm")),
                "source": str(row.get("source", "wa")),
                "deal_value": str(row.get("deal_value", "")),
                "currency": str(row.get("currency", "USD")),
                "created_at": str(row.get("created_at", "")),
                "last_client_msg_at": str(row.get("last_client_msg_at", "")),
                "geo": str(row.get("geo", "Unknown")),
                "language": str(row.get("language", "Unknown")),
                "paused": bool(row.get("paused"))
            })

        return jsonify({"success": True, "data": leads, "count": len(leads)})
    except Exception as e:
        logging.error(f"Error fetching sales leads: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/leads/<string:external_id>', methods=['PUT'])
def update_sales_lead(external_id: str):
    """Updates fields (stage, deal_value, owner, score, paused) of a sales lead."""
    data = request.get_json(force=True)
    try:
        clean_ext_id = external_id.strip()

        # A single external_id can appear on more than one sales lead, so the
        # dashboard addresses the row by its stable id. Fall back to the
        # external_id lookup only for older clients that don't send row_idx.
        row_idx = data.get("row_idx")
        if row_idx is not None:
            lead = db_store.get_sales_lead_by_id(int(row_idx))
        else:
            lead = db_store.find_sales_lead_by_external_id(clean_ext_id)
        if not lead:
            return jsonify({"success": False, "error": f"Lead with external_id {external_id} not found"}), 404
        # The row is addressed by id, so it must belong to this external_id.
        if str(lead.get("external_id", "")).strip() != clean_ext_id:
            return jsonify({
                "success": False,
                "error": f"Lead {row_idx} does not belong to external_id {external_id}"
            }), 400
        lead_id = lead["id"]

        # Safeguard: Prevent resuming sales channel if client was handed over to delivery
        if "paused" in data and not data["paused"]:
            is_handed_off = bool(lead.get("delivery_handed_off"))
            if not is_handed_off:
                is_handed_off = db_store.is_client_in_delivery(clean_ext_id)
            if is_handed_off:
                return jsonify({
                    "success": False,
                    "error": "Channel cannot be resumed: Client has been handed over to delivery."
                }), 400

        # If stage is changed to won or lost, automatically record closed_at timestamp
        if data.get("stage") in ["won", "lost"]:
            data["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # In-place Slack channel rename if requested
        channel_renamed = False
        channel_name_result = None
        rename_error = None
        target_channel_id = lead.get("slack_channel_id")

        if (data.get("rename_slack_channel") or data.get("new_channel_name")) and target_channel_id:
            try:
                from slack_sdk import WebClient
                from slack_sdk.errors import SlackApiError
                import re

                lead_platform = str(lead.get("platform", "whatsapp")).lower()
                bot_token = os.getenv("SLACK_BOT_TOKEN") if lead_platform == "whatsapp" \
                    else os.getenv("SLACK_BOT_TOKEN_TELEGRAM", os.getenv("SLACK_BOT_TOKEN"))
                wc = WebClient(token=bot_token)

                custom_name = (data.get("new_channel_name") or "").strip()
                if custom_name:
                    clean_slug = re.sub(r"[^a-z0-9-]", "", custom_name.lower())[:80]
                else:
                    new_name = (data.get("client_name") or lead.get("client_name") or "client").strip()
                    c_clean = re.sub(r"[^a-zA-Z0-9\s-]", "", new_name).strip().lower()
                    c_slug = re.sub(r"[\s_]+", "-", c_clean)[:25].strip("-") or "lead"
                    ext_digits = re.sub(r"\D", "", clean_ext_id.split("@")[0])
                    ext_suffix = ext_digits[-4:] if len(ext_digits) >= 4 else "client"
                    prefix = "sale-tg" if lead_platform == "telegram" else "sale-wa"
                    clean_slug = f"{prefix}-{c_slug}-{ext_suffix}"[:80]

                if clean_slug:
                    logging.info(f"Attempting to rename Slack channel {target_channel_id} to '{clean_slug}'...")
                    resp = wc.conversations_rename(channel=target_channel_id, name=clean_slug)
                    if resp.get("ok"):
                        channel_renamed = True
                        channel_name_result = clean_slug
                        logging.info(f"Successfully renamed Slack channel {target_channel_id} to '{clean_slug}'")
            except SlackApiError as sae:
                rename_error = sae.response.get("error", str(sae))
                logging.warning(f"Could not rename Slack channel {target_channel_id}: {rename_error}")
            except Exception as ex:
                rename_error = str(ex)
                logging.warning(f"Exception during Slack channel rename: {ex}")

        # Sync client_name to delivery client_mappings if mapped
        if "client_name" in data and data["client_name"]:
            try:
                deliv = db_store.find_delivery_mapping_by_external_id(clean_ext_id)
                if deliv and deliv.get("id"):
                    db_store.update_client_mapping(deliv["id"], {"client_name": str(data["client_name"]).strip()})
            except Exception as de:
                logging.warning(f"Could not sync client_name to delivery mapping: {de}")

        if not db_store.update_sales_lead(lead_id, data):
            return jsonify({"success": False, "error": f"Lead {clean_ext_id} not found"}), 404

        dispatch_bridge_refresh()
        logging.info(f"Updated sales lead {clean_ext_id} (id={lead_id}): {data}")

        res_msg = f"Lead {clean_ext_id} updated successfully."
        if channel_renamed:
            res_msg += f" Slack channel renamed to #{channel_name_result}."
        elif rename_error:
            res_msg += f" (Note: Slack channel rename skipped: {rename_error})"

        return jsonify({
            "success": True,
            "message": res_msg,
            "channel_renamed": channel_renamed,
            "channel_name": channel_name_result
        })
    except Exception as e:
        logging.error(f"Error updating sales lead {external_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# 4. SALES MEMBER POOL API (Sales Pool)
# ==============================================================================

@app.route('/api/sales/pool', methods=['GET'])
def list_sales_pool():
    """Lists all sales reps in the member pool with row index."""
    try:
        pool = [{
            "row_idx": r["id"],
            "slack_user_id": str(r.get("slack_user_id", "")),
            "name": str(r.get("name", "")),
            "active": bool(r.get("active"))
        } for r in db_store.get_sales_pool(active_only=False)]
        return jsonify({"success": True, "data": pool, "count": len(pool)})
    except Exception as e:
        logging.error(f"Error fetching sales pool: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/pool', methods=['POST'])
def add_sales_pool_member():
    """Adds a new sales rep to the member pool."""
    data = request.get_json(force=True)
    slack_user_id = str(data.get("slack_user_id", "")).strip()
    name = str(data.get("name", "")).strip() or slack_user_id
    active = bool(data.get("active", True))

    if not slack_user_id:
        return jsonify({"success": False, "error": "Missing slack_user_id"}), 400

    try:
        db_store.add_person("sales_pool", slack_user_id, name, active)
        logging.info(f"Added new sales pool rep: {name} ({slack_user_id})")
        return jsonify({"success": True, "message": f"Rep {name} added to sales pool."}), 201
    except Exception as e:
        logging.error(f"Error adding rep to sales pool: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/pool/<int:row_idx>', methods=['PUT'])
def update_sales_pool_member(row_idx: int):
    """Updates a rep's details or active/vacation status."""
    data = request.get_json(force=True)
    try:
        current = next((r for r in db_store.get_sales_pool(active_only=False)
                        if r["id"] == row_idx), None)
        if not current:
            return jsonify({"success": False, "error": f"Rep {row_idx} not found"}), 404

        slack_user_id = data.get("slack_user_id", current.get("slack_user_id", ""))
        name = data.get("name", current.get("name", ""))
        active = bool(data["active"]) if "active" in data else bool(current.get("active"))

        if not db_store.update_person("sales_pool", row_idx, slack_user_id, name, active):
            return jsonify({"success": False, "error": f"Rep {row_idx} not found"}), 404

        logging.info(f"Updated sales pool rep {row_idx}: {name} (active={active})")
        return jsonify({"success": True, "message": f"Rep {row_idx} updated."})
    except Exception as e:
        logging.error(f"Error updating rep {row_idx}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/pool/<int:row_idx>', methods=['DELETE'])
def delete_sales_pool_member(row_idx: int):
    """Deletes a sales rep from the member pool."""
    if not db_store.delete_person("sales_pool", row_idx):
        return jsonify({"success": False, "error": f"Rep {row_idx} not found"}), 404
    logging.info(f"Deleted rep {row_idx} from sales pool")
    return jsonify({"success": True, "message": f"Rep {row_idx} removed."})


# ==============================================================================
# 5. SETTINGS & SAFETY CONTROLS API (Settings)
# ==============================================================================

@app.route('/api/sales/settings', methods=['GET'])
def get_settings():
    """Retrieves all global and sales settings."""
    try:
        settings = db_store.get_sales_settings()
        return jsonify({"success": True, "data": settings})
    except Exception as e:
        logging.error(f"Error fetching settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/settings', methods=['PUT'])
def update_settings():
    """Updates key-value settings.

    The dashboard is the only supported way to change these values, so a
    silent partial write would leave the UI showing something the system is
    not actually using. Two protections:

    * Every key is verified and failures are reported.
    * A key is only written if the value the client last read is still current.
      A browser tab left open across a change would otherwise replay its stale
      copy of every field and silently undo the real value.
    """
    data = request.get_json(force=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"success": False, "error": "No settings supplied"}), 400

    expected = data.pop("_expected", None)
    if not isinstance(expected, dict):
        expected = {}

    try:
        current = db_store.get_sales_settings()
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not read current settings: {e}"}), 500

    # A full-object save with no expectations comes from an old/stale page.
    if len(data) > 1 and not expected:
        return jsonify({
            "success": False,
            "error": "This page is out of date and was not allowed to overwrite settings. "
                     "Reload the page, then make your change."
        }), 409

    try:
        note = f"Configured via Dashboard on {time.strftime('%Y-%m-%d')}"
        failed = []
        conflicts = []
        written = {}
        for key, val in data.items():
            if key in expected:
                if str(expected[key]) != str(current.get(key)):
                    conflicts.append({"key": key, "yours": val,
                                      "current": current.get(key)})
                    continue
            str_val = "TRUE" if val is True else ("FALSE" if val is False else str(val))
            # Validate numeric settings before writing
            if key == "archive_after_days":
                try:
                    n = int(float(str(val)))
                    if n < 1:
                        return jsonify({"success": False,
                                        "error": "Auto-archive threshold must be at least 1 day."}), 400
                    if n > 365:
                        return jsonify({"success": False,
                                        "error": "Auto-archive threshold cannot exceed 365 days."}), 400
                    str_val = str(n)
                except (ValueError, TypeError):
                    return jsonify({"success": False,
                                    "error": "Auto-archive threshold must be a whole number."}), 400
            if key == "sla_minutes":
                try:
                    n = int(float(str(val)))
                    if n < 1:
                        return jsonify({"success": False,
                                        "error": "SLA warning must be at least 1 minute."}), 400
                    str_val = str(n)
                except (ValueError, TypeError):
                    return jsonify({"success": False,
                                    "error": "SLA minutes must be a whole number."}), 400
            if not db_store.set_setting(key, str_val, note):
                failed.append(key)
            else:
                written[key] = str_val

        if conflicts:
            logging.warning(f"Settings write rejected as stale: {conflicts}")
            return jsonify({
                "success": False,
                "error": "Someone changed these settings while this page was open. "
                         "Reloaded to the current values - re-apply your edit.",
                "conflicts": conflicts,
            }), 409

        if failed:
            logging.error(f"Settings write failed for: {failed}")
            return jsonify({
                "success": False,
                "error": f"Failed to save: {', '.join(failed)}",
                "failed": failed,
                "saved": written,
            }), 500

        logging.info(f"Updated settings: {written}")
        return jsonify({"success": True, "message": "Settings updated successfully.", "data": written})
    except Exception as e:
        logging.error(f"Error updating settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/digest/trigger', methods=['POST'])
def trigger_sales_digest():
    """Triggers immediate calculation and posting of the Daily Sales Digest to Slack."""
    url = f"http://{WA_BRIDGE_HOST}:{WA_PORT}/sales-digest/trigger"
    try:
        try:
            r = requests.post(url, timeout=10.0)
        except requests.exceptions.RequestException:
            r = requests.post(f"http://127.0.0.1:{WA_PORT}/sales-digest/trigger", timeout=10.0)
        try:
            return r.json(), r.status_code
        except Exception:
            if r.status_code == 404:
                return jsonify({"success": False, "error": "Bridge is running an older process. Please restart main_whatsapp.py to activate the digest endpoint."}), 400
            return jsonify({"success": False, "error": f"Bridge returned status {r.status_code}: {r.text[:100]}"}), 500
    except Exception as e:
        logging.error(f"Error calling bridge digest trigger at {url}: {e}")
        return jsonify({"success": False, "error": "WhatsApp bridge is offline. Please start main_whatsapp.py"}), 503


@app.route('/api/sales/insights/generate', methods=['POST'])
def generate_sales_insights():
    """Triggers Gemini AI Win/Loss intelligence report for closed deals."""
    url = f"http://{WA_BRIDGE_HOST}:{WA_PORT}/sales-insights/generate"
    try:
        body = request.get_json(force=True) if request.data else {}
        try:
            r = requests.post(url, json=body, timeout=30.0)
        except requests.exceptions.RequestException:
            r = requests.post(f"http://127.0.0.1:{WA_PORT}/sales-insights/generate", json=body, timeout=30.0)
        try:
            return r.json(), r.status_code
        except Exception:
            return jsonify({"success": False, "error": f"Bridge error: {r.text[:100]}"}), 500
    except Exception as e:
        logging.error(f"Error calling bridge insights generation at {url}: {e}")
        return jsonify({"success": False, "error": "WhatsApp bridge is offline. Please start main_whatsapp.py"}), 503


# ==============================================================================
# 6. UNIVERSAL BLOCKLIST API (Blocklist)
# ==============================================================================

@app.route('/api/sales/blocklist', methods=['GET'])
def list_blocklist():
    """Lists all blocked numbers and JIDs with metadata."""
    try:
        blocklist = db_store.list_blocklist()
        return jsonify({"success": True, "data": blocklist, "count": len(blocklist)})
    except Exception as e:
        logging.error(f"Error fetching blocklist: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/blocklist', methods=['POST'])
def add_to_blocklist():
    """Adds a phone number or JID to the blocklist and pauses active client channels."""
    data = request.get_json(force=True) if request.data else {}
    external_id = str(data.get("external_id", "")).strip()
    reason = str(data.get("reason", "Added via Dashboard")).strip()
    added_at = time.strftime("%Y-%m-%d")

    if not external_id:
        return jsonify({"success": False, "error": "Missing external_id"}), 400

    try:
        # Idempotency check: verify if already blocked
        if db_store.is_external_id_blocked(external_id):
            return jsonify({"success": False, "error": f"{external_id} is already in the blocklist."}), 409

        db_store.add_to_blocklist(external_id, reason, added_at)
        # Pause existing channels for this contact (without deleting the contact)
        db_store.set_lead_paused_by_external_id(external_id, True)
        logging.info(f"Added {external_id} to blocklist (reason: {reason}) & paused channels")
        return jsonify({"success": True, "message": f"{external_id} added to blocklist."}), 201
    except Exception as e:
        logging.error(f"Error adding to blocklist: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/blocklist/<int:row_idx>', methods=['DELETE'])
def delete_from_blocklist(row_idx: int):
    """Deletes a blocklist entry by id and resumes channel communication."""
    try:
        entry = db_store.get_blocklist_entry_by_id(row_idx)
        ext_id = entry.get("external_id") if entry else None

        if not db_store.delete_from_blocklist(row_idx):
            return jsonify({"success": False, "error": f"Blocklist entry {row_idx} not found"}), 404

        # Resume channel communication if contact exists
        if ext_id:
            db_store.set_lead_paused_by_external_id(ext_id, False)

        logging.info(f"Removed blocklist entry {row_idx} ({ext_id}) & resumed channels")
        return jsonify({"success": True, "message": f"Blocklist entry {row_idx} removed & connection resumed."})
    except Exception as e:
        logging.error(f"Error removing from blocklist: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ==============================================================================
# SALES: SNIPPETS & LEADERBOARD & AI
# ==============================================================================

@app.route('/api/sales/snippets', methods=['GET'])
def get_snippets_api():
    """Returns the list of snippets."""
    snippets = db_store.get_snippets()
    return jsonify({"success": True, "snippets": snippets})

@app.route('/api/sales/snippets', methods=['POST'])
def create_snippet():
    """Creates a new snippet."""
    data = request.json
    title = data.get("title", "")
    content = data.get("content", "")
    if not title or not content:
        return jsonify({"success": False, "error": "Title and content are required"}), 400
    
    success = db_store.save_snippet(title, content) is not None
    return jsonify({"success": success})

@app.route('/api/sales/snippets/<int:row_idx>', methods=['PUT'])
def update_snippet_api(row_idx: int):
    """Updates an existing snippet."""
    data = request.json
    title = data.get("title", "")
    content = data.get("content", "")
    if not db_store.update_snippet(row_idx, title, content):
        return jsonify({"success": False, "error": f"Snippet {row_idx} not found"}), 404
    return jsonify({"success": True})

@app.route('/api/sales/snippets/<int:row_idx>', methods=['DELETE'])
def delete_snippet_api(row_idx: int):
    """Deletes a snippet."""
    if not db_store.delete_snippet(row_idx):
        return jsonify({"success": False, "error": f"Snippet {row_idx} not found"}), 404
    return jsonify({"success": True})

@app.route('/api/sales/leaderboard', methods=['GET'])
def get_leaderboard_api():
    """Returns leaderboard metrics."""
    metrics = db_store.get_leaderboard_metrics()
    return jsonify({"success": True, "leaderboard": metrics})

@app.route('/api/sales/ai/weekly-stage', methods=['POST'])
def trigger_weekly_stage_api():
    """Triggers the weekly stage AI script manually."""
    try:
        import subprocess
        import os
        script_path = os.path.join(os.path.dirname(__file__), "sales_ai_jobs.py")
        if os.path.exists(script_path):
            subprocess.Popen(["python3", script_path, "--run-now"])
            return jsonify({"success": True, "message": "Triggered Weekly Stage AI job in background."})
        else:
            return jsonify({"success": False, "error": "Script not found."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# 5. DEVELOPERS API (admin only)
#
# Two rosters are managed separately on purpose. project_managers are the
# oversight people auto-invited into client channels; developers are the people
# who receive technical failures. An operator may appear in one, the other, or
# both, so neither table is derived from the other.
# ==============================================================================

def _roster_payload(rows) -> Dict[str, Any]:
    return {"success": True, "data": [{
        "row_idx": r["id"],
        "slack_user_id": str(r.get("slack_user_id", "")),
        "name": str(r.get("name", "")),
        "active": bool(r.get("active")),
        "channel_ids": r.get("channel_ids", [])
    } for r in rows]}


def _read_roster(table: str):
    if table == "project_managers":
        return db_store.list_project_managers()
    return db_store.list_developers()


def _sync_error_channel_if_developers(channel_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Push roster changes into the error channel(s), best effort."""
    if channel_id:
        try:
            return alerts.sync_channel_membership(channel_id=channel_id)
        except Exception as e:
            logging.error(f"Error channel membership sync failed for {channel_id}: {e}")
            return {"posted": False, "reason": str(e)}

    channel_ids = alerts.error_channel_ids()
    if not channel_ids:
        return {"posted": False, "reason": "no_channel"}
    results = []
    for cid in channel_ids:
        try:
            results.append(alerts.sync_channel_membership(channel_id=cid))
        except Exception as e:
            logging.error(f"Error channel membership sync failed for {cid}: {e}")
            results.append({"ok": False, "channel_id": cid, "error": str(e)})
    return {"ok": True, "results": results}


@app.route('/api/developers/project-managers', methods=['GET'])
def list_project_managers():
    denied = require_admin()
    if denied:
        return denied
    try:
        payload = _roster_payload(db_store.list_project_managers())
        payload["count"] = len(payload["data"])
        return jsonify(payload)
    except Exception as e:
        logging.error(f"Error fetching project managers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/roster', methods=['GET'])
def list_error_channel_developers():
    denied = require_admin()
    if denied:
        return denied
    try:
        payload = _roster_payload(db_store.list_developers())
        payload["count"] = len(payload["data"])
        return jsonify(payload)
    except Exception as e:
        logging.error(f"Error fetching developer roster: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _validate_slack_id_or_return(slack_user_id: str):
    """Verify a Slack ID resolves to a real user before storing it.

    Returns (None, (response, status)) on failure and (record, None) on success.
    A stale ID left in a roster is the failure mode this prevents: it silently
    degrades channel membership with no feedback at the time of the mistake.
    """
    check = alerts.validate_slack_user(None, slack_user_id)
    if check.get("ok"):
        return check, None
    if check.get("skipped"):
        return {"slack_user_id": slack_user_id}, None
    return None, (jsonify({"success": False,
                           "error": f"Slack does not recognise that user ID: {check.get('error')}"}), 400)


def _add_roster_member(table: str, label: str):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(force=True)
    slack_user_id = str(data.get("slack_user_id", "")).strip()
    name = str(data.get("name", "")).strip()
    active = bool(data.get("active", True))
    selected_channel_ids = data.get("channel_ids")
    if not slack_user_id:
        return jsonify({"success": False, "error": "Missing slack_user_id"}), 400

    check, failure = _validate_slack_id_or_return(slack_user_id)
    if failure:
        return failure
    if not name:
        name = check.get("name") or slack_user_id

    try:
        db_store.add_person(table, slack_user_id, name, active)
    except Exception as e:
        detail = str(e)
        lowered = detail.lower()
        if "unique" in lowered or "duplicate" in lowered:
            return jsonify({"success": False,
                            "error": "That Slack ID is already on this list."}), 409
        logging.error(f"Error adding to {table}: {e}")
        return jsonify({"success": False, "error": detail}), 500

    sync = None
    warning = None
    if table == "developers":
        if selected_channel_ids is None or len(selected_channel_ids) == 0:
            active_channels = db_store.list_alert_channels(active_only=True)
            selected_channel_ids = [c["channel_id"] for c in active_channels]
            if not selected_channel_ids:
                single = alerts.error_channel_id()
                if single:
                    selected_channel_ids = [single]
        db_store.set_developer_channels(slack_user_id, selected_channel_ids)

        sync_results = []
        for cid in selected_channel_ids:
            res = alerts.sync_channel_membership(channel_id=cid)
            sync_results.append(res)
            if res.get("skipped"):
                for sk in res.get("skipped", []):
                    if sk.get("user_id") == slack_user_id:
                        warning = f"Added to list, but Slack invite to channel {cid} failed: {sk.get('error')}"
                        break
        sync = {"ok": True, "results": sync_results}

    logging.info(f"Added {label}: {name} ({slack_user_id}) active={active}")
    return jsonify({"success": True, "message": warning or f"{name} added.", "warning": warning, "sync": sync}), 201


def _update_roster_member(table: str, row_idx: int, label: str):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(force=True)
    try:
        current = next((r for r in _read_roster(table) if r["id"] == row_idx), None)
        if not current:
            return jsonify({"success": False, "error": f"Row {row_idx} not found"}), 404

        old_slack_id = str(current.get("slack_user_id", "")).strip()
        slack_user_id = str(data.get("slack_user_id", old_slack_id)).strip()
        name = str(data.get("name", current.get("name", "")))
        active = bool(data["active"]) if "active" in data else bool(current.get("active"))

        if slack_user_id != old_slack_id:
            check, failure = _validate_slack_id_or_return(slack_user_id)
            if failure:
                return failure
            if not name:
                name = check.get("name") or slack_user_id
            if table == "developers" and old_slack_id:
                for cid in alerts.error_channel_ids():
                    try:
                        alerts.kick_from_error_channel(old_slack_id, channel_id=cid)
                    except Exception as e:
                        logging.warning(f"Failed to kick previous Slack user {old_slack_id} from {cid}: {e}")

        if not db_store.update_person(table, row_idx, slack_user_id, name, active):
            return jsonify({"success": False, "error": f"Row {row_idx} not found"}), 404

        if table == "developers":
            if "channel_ids" in data:
                db_store.set_developer_channels(slack_user_id, data["channel_ids"])

        kicks = []
        if table == "developers" and not active and slack_user_id:
            for cid in alerts.error_channel_ids():
                try:
                    k = alerts.kick_from_error_channel(slack_user_id, channel_id=cid)
                    kicks.append(k)
                except Exception as e:
                    logging.warning(f"Failed to kick deactivated developer {slack_user_id} from {cid}: {e}")
        elif table == "developers" and active and slack_user_id:
            assigned = db_store.get_developer_channel_ids(slack_user_id)
            for cid in assigned:
                alerts.sync_channel_membership(channel_id=cid)

        sync = _sync_error_channel_if_developers() if table == "developers" else None
        logging.info(f"Updated {label} {row_idx}: {name} active={active}")

        msg = "Saved."
        if table == "developers":
            if not active:
                msg = f"{name} deactivated and excluded from alert channels."
            else:
                msg = f"{name} updated and synced to alert channels."

        return jsonify({"success": True, "message": msg, "kicks": kicks, "sync": sync})
    except Exception as e:
        logging.error(f"Error updating {table} {row_idx}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _delete_roster_member(table: str, row_idx: int, label: str):
    denied = require_admin()
    if denied:
        return denied
    try:
        current = next((r for r in _read_roster(table) if r["id"] == row_idx), None)
        if not current:
            return jsonify({"success": False, "error": f"Row {row_idx} not found"}), 404
        slack_user_id = str(current.get("slack_user_id", "")).strip()
        name = str(current.get("name", "Member"))

        if not db_store.delete_person(table, row_idx):
            return jsonify({"success": False, "error": f"Row {row_idx} not found"}), 404

        kicks = []
        if table == "developers" and slack_user_id:
            # Exclude developer from ALL currently present alert channels!
            all_channels = db_store.list_alert_channels()
            channel_ids = [c["channel_id"] for c in all_channels]
            single = alerts.error_channel_id()
            if single and single not in channel_ids:
                channel_ids.append(single)
            for cid in channel_ids:
                try:
                    k = alerts.kick_from_error_channel(slack_user_id, channel_id=cid)
                    kicks.append(k)
                except Exception as e:
                    logging.warning(f"Failed to kick deleted developer {slack_user_id} from {cid}: {e}")
            db_store.set_developer_channels(slack_user_id, [])

        logging.info(f"Deleted {label} {row_idx} ({name}, {slack_user_id})")

        msg = "Removed."
        if table == "developers":
            msg = f"{name} removed from roster and excluded from all alert channels."

        return jsonify({"success": True, "message": msg, "kicks": kicks})
    except Exception as e:
        logging.error(f"Error deleting from {table} {row_idx}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/project-managers', methods=['POST'])
def add_project_manager():
    return _add_roster_member("project_managers", "project manager")


@app.route('/api/developers/project-managers/<int:row_idx>', methods=['PUT'])
def update_project_manager(row_idx: int):
    return _update_roster_member("project_managers", row_idx, "project manager")


@app.route('/api/developers/project-managers/<int:row_idx>', methods=['DELETE'])
def delete_project_manager(row_idx: int):
    return _delete_roster_member("project_managers", row_idx, "project manager")


@app.route('/api/developers/roster', methods=['POST'])
def add_error_channel_developer():
    return _add_roster_member("developers", "developer")


@app.route('/api/developers/roster/<int:row_idx>', methods=['PUT'])
def update_error_channel_developer(row_idx: int):
    return _update_roster_member("developers", row_idx, "developer")


@app.route('/api/developers/roster/<int:row_idx>', methods=['DELETE'])
def delete_error_channel_developer(row_idx: int):
    return _delete_roster_member("developers", row_idx, "developer")


@app.route('/api/developers/admins', methods=['GET'])
def list_admins():
    denied = require_admin()
    if denied:
        return denied
    try:
        user_ids = db_store.get_admin_user_ids()
        return jsonify({"success": True, "data": user_ids, "count": len(user_ids)})
    except Exception as e:
        logging.error(f"Error fetching admins: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/error-channel', methods=['GET'])
def get_error_channel():
    denied = require_admin()
    if denied:
        return denied
    channel_id = alerts.error_channel_id()
    out: Dict[str, Any] = {"success": True, "channel_id": channel_id, "configured": bool(channel_id),
                           "slack_configured": alerts.get_client() is not None}
    if channel_id:
        client = alerts.get_client()
        if client is not None:
            try:
                info = client.conversations_info(channel=channel_id)
                ch = info.get("channel", {}) or {}
                out["channel_name"] = ch.get("name", "")
                out["is_archived"] = bool(ch.get("is_archived", False))
                members = client.conversations_members(channel=channel_id)
                out["member_count"] = len(members.get("members", []) or [])
            except Exception as e:
                # A token without conversations:read cannot resolve the channel.
                # That is a scope gap, not a broken configuration, so say which.
                out["channel_resolve_error"] = str(e)
    return jsonify(out)


@app.route('/api/developers/error-channel', methods=['POST'])
def set_error_channel():
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(force=True)
    channel_id = str(data.get("channel_id", "")).strip()
    if not channel_id:
        return jsonify({"success": False, "error": "A channel ID is required."}), 400
    if not (channel_id.startswith("C") or channel_id.startswith("G")):
        return jsonify({"success": False,
                        "error": "A Slack channel ID starts with C (channel) or G (group)."}), 400

    client = alerts.get_client()
    if client is not None:
        try:
            info = client.conversations_info(channel=channel_id)
            resolved = (info.get("channel", {}) or {}).get("name", channel_id)
        except Exception as e:
            logging.error(f"Channel {channel_id} could not be resolved: {e}")
            return jsonify({"success": False,
                            "error": f"Slack cannot open that channel with this bot: {e}"}), 400
    else:
        resolved = channel_id

    if not db_store.set_setting("critical_alerts_channel_id", channel_id,
                               note="Set from the Developers page"):
        return jsonify({"success": False, "error": "Could not save the channel ID."}), 500

    sync = alerts.sync_channel_membership(channel_id=channel_id)
    alerts.reset_state()
    logging.info(f"Error channel set to {channel_id} (#{resolved}) by {session.get('user')}")
    return jsonify({"success": True, "message": f"Alerts will now go to #{resolved}.",
                    "channel_name": resolved, "sync": sync})


@app.route('/api/developers/error-channel/create', methods=['POST'])
def create_error_channel():
    denied = require_admin()
    if denied:
        return denied
    result = alerts.create_error_channel()
    if not result.get("ok"):
        logging.warning(f"Error channel creation failed: {result}")
        return jsonify({"success": False, "error": result.get("error", "unknown"),
                        "detail": result.get("detail", "")}), 400
    alerts.reset_state()
    logging.info(f"Error channel {result['channel_name']} created by {session.get('user')}")
    return jsonify({"success": True, "message": f"Created #{result['channel_name']}.",
                    "channel_id": result["channel_id"], "sync": result.get("membership")})


@app.route('/api/developers/error-channel/sync', methods=['POST'])
def sync_error_channel():
    denied = require_admin()
    if denied:
        return denied
    channel_id = alerts.error_channel_id()
    if not channel_id:
        return jsonify({"success": False,
                        "error": "No error channel is set. Choose one first."}), 400
    result = alerts.sync_channel_membership(channel_id=channel_id)
    if not result.get("ok"):
        return jsonify({"success": False, "error": result.get("reason", "sync failed")}), 400
    if result.get("skipped"):
        logging.warning(f"Channel sync skipped IDs: {result['skipped']}")
    return jsonify({"success": True, "message": "Channel membership refreshed.", "sync": result})


# ---------------------------------------------------------------------------
# Multi Alert Channels Management
# ---------------------------------------------------------------------------

@app.route('/api/developers/alert-channels', methods=['GET'])
def list_alert_channels_route():
    denied = require_admin()
    if denied:
        return denied
    channels = db_store.list_alert_channels()
    client = alerts.get_client()
    for ch in channels:
        cid = ch["channel_id"]
        ch["is_archived"] = False
        ch["is_private"] = False
        ch["member_count"] = 0
        if client is not None:
            try:
                info = client.conversations_info(channel=cid)
                c_data = info.get("channel", {}) or {}
                ch["is_archived"] = bool(c_data.get("is_archived", False))
                ch["is_private"] = bool(c_data.get("is_private", False))
                name = c_data.get("name")
                if name:
                    ch["channel_name"] = name
                members = client.conversations_members(channel=cid)
                ch["member_count"] = len(members.get("members", []) or [])
            except Exception as e:
                ch["error"] = str(e)
    return jsonify({"success": True, "channels": channels})


@app.route('/api/developers/alert-channels', methods=['POST'])
def add_alert_channel_route():
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(force=True)
    raw_name = str(data.get("channel_name", "")).strip().lstrip("#")
    channel_id = str(data.get("channel_id", "")).strip()

    client = alerts.get_client()
    channel_name = raw_name

    # If channel_id is not provided, create a new channel in Slack
    if not channel_id:
        if not channel_name:
            return jsonify({"success": False, "error": "A channel name or channel ID is required."}), 400
        if client is None:
            return jsonify({"success": False, "error": "Slack is not configured on this server."}), 400
        create_res = alerts.create_public_channel(name=channel_name, client=client)
        if not create_res.get("ok"):
            err_msg = create_res.get("detail") or create_res.get("error") or "Failed to create channel in Slack."
            return jsonify({"success": False, "error": err_msg}), 400
        channel_id = create_res.get("channel_id")
        channel_name = create_res.get("channel_name", channel_name)

    if not channel_id:
        return jsonify({"success": False, "error": "Could not determine channel ID."}), 400

    if client is not None:
        try:
            info = client.conversations_info(channel=channel_id)
            ch = info.get("channel", {}) or {}
            channel_name = ch.get("name", channel_name or channel_id)
            if ch.get("is_archived"):
                try:
                    client.conversations_unarchive(channel=channel_id)
                except Exception:
                    pass
        except Exception as e:
            logging.warning(f"Could not verify channel {channel_id}: {e}")

    try:
        db_store.add_alert_channel(channel_id, channel_name, active=True)
        db_store.set_setting("critical_alerts_channel_id", channel_id, note="Set from Alert Channels")
    except Exception as e:
        logging.error(f"Failed to save alert channel {channel_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    sync = alerts.sync_channel_membership(channel_id=channel_id)
    alerts.reset_state()
    logging.info(f"Alert channel #{channel_name} ({channel_id}) added by {session.get('user')}")
    return jsonify({"success": True, "message": f"Added #{channel_name} ({channel_id}).",
                    "channel_id": channel_id, "channel_name": channel_name, "sync": sync}), 201


@app.route('/api/developers/alert-channels/<channel_id>/sync', methods=['POST'])
def sync_specific_alert_channel_route(channel_id: str):
    denied = require_admin()
    if denied:
        return denied
    result = alerts.sync_channel_membership(channel_id=channel_id)
    return jsonify({"success": True, "message": f"Synced channel {channel_id}", "sync": result})


@app.route('/api/developers/alert-channels/<channel_id>', methods=['DELETE'])
def delete_alert_channel_route(channel_id: str):
    denied = require_admin()
    if denied:
        return denied
    try:
        db_store.delete_alert_channel(channel_id)
        alerts.reset_state()
        logging.info(f"Alert channel {channel_id} removed by {session.get('user')}")
        return jsonify({"success": True, "message": f"Alert channel {channel_id} removed."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/alerts', methods=['GET'])
def list_technical_alerts():
    """Full technical alerts, joined with the detail table. Admin only.

    Sales reads /api/alerts, which selects no technical column at all.
    """
    denied = require_admin()
    if denied:
        return denied
    try:
        rows = db_store.get_alerts_with_detail(
            severity=request.args.get("severity") or None,
            component=request.args.get("component") or None,
            status=request.args.get("status") or None,
            category=request.args.get("category") or None,
            limit=int(request.args.get("limit", 100)))
        return jsonify({
            "success": True,
            "data": rows,
            "alerts": rows,
            "count": len(rows),
            "open_count": db_store.open_alert_count()
        })
    except Exception as e:
        logging.error(f"Error fetching technical alerts: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/alerts/<int:alert_id>', methods=['GET'])
def get_technical_alert(alert_id: int):
    denied = require_admin()
    if denied:
        return denied
    try:
        row = db_store.get_alert_detail(alert_id)
        if not row:
            return jsonify({"success": False, "error": "Alert not found"}), 404
        return jsonify({"success": True, "data": row})
    except Exception as e:
        logging.error(f"Error fetching alert {alert_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/alerts/<int:alert_id>/resolve', methods=['POST'])
def resolve_technical_alert(alert_id: int):
    denied = require_admin()
    if denied:
        return denied
    try:
        if not db_store.resolve_alert(alert_id):
            return jsonify({"success": False, "error": "Alert not found"}), 404
        logging.info(f"Alert {alert_id} resolved by {session.get('user')}")
        return jsonify({"success": True, "message": "Alert resolved."})
    except Exception as e:
        logging.error(f"Error resolving alert {alert_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# DASHBOARD USERS API (Admin Only)
# ==============================================================================

@app.route('/api/users', methods=['GET'])
def list_dashboard_users_route():
    denied = require_admin()
    if denied:
        return denied
    users = db_store.list_dashboard_users()
    return jsonify({"success": True, "users": users, "count": len(users)})


@app.route('/api/users', methods=['POST'])
def add_dashboard_user_route():
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(force=True) if request.data else {}
    username = str(data.get("username") or data.get("id") or "").strip()
    password = str(data.get("password") or "").strip()
    role = str(data.get("role") or "sales").strip().lower()

    if not username:
        return jsonify({"success": False, "error": "Username is required."}), 400
    if not password:
        return jsonify({"success": False, "error": "Password is required."}), 400
    if role not in ("admin", "sales"):
        return jsonify({"success": False, "error": "Role must be 'admin' or 'sales'."}), 400

    res = db_store.create_dashboard_user(username, password, role)
    if not res.get("ok"):
        return jsonify({"success": False, "error": res.get("error") or "Failed to create user."}), 400

    logging.info(f"Dashboard user '{username}' created with role '{role}' by '{session.get('user')}'")
    return jsonify({"success": True, "message": f"User '{username}' created with role '{role}'.", "user": res}), 201


@app.route('/api/users/<user_id>', methods=['PUT'])
def update_dashboard_user_route(user_id: str):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(force=True) if request.data else {}
    target_user = str(user_id or "").strip()

    # Check if role update requested
    if "role" in data:
        role = str(data.get("role") or "").strip().lower()
        if role not in ("admin", "sales"):
            return jsonify({"success": False, "error": "Role must be 'admin' or 'sales'."}), 400
        if role != "admin":
            admin_ids = db_store.get_admin_user_ids()
            if target_user in admin_ids and len(admin_ids) <= 1:
                return jsonify({"success": False, "error": "Cannot demote the only remaining admin."}), 400

        res = db_store.update_dashboard_user_role(target_user, role)
        if not res.get("ok"):
            return jsonify({"success": False, "error": res.get("error")}), 400

        if target_user == session.get("user"):
            session["role"] = role

        logging.info(f"Dashboard user '{target_user}' role updated to '{role}' by '{session.get('user')}'")
        return jsonify({"success": True, "message": f"User '{target_user}' role updated to '{role}'."})

    # Check if password update requested
    if "password" in data:
        password = str(data.get("password") or "").strip()
        if not password:
            return jsonify({"success": False, "error": "Password cannot be empty."}), 400
        res = db_store.update_dashboard_user_password(target_user, password)
        if not res.get("ok"):
            return jsonify({"success": False, "error": res.get("error")}), 400

        logging.info(f"Dashboard user '{target_user}' password updated by '{session.get('user')}'")
        return jsonify({"success": True, "message": f"Password updated for '{target_user}'."})

    return jsonify({"success": False, "error": "No updates specified."}), 400


@app.route('/api/users/<user_id>', methods=['DELETE'])
def delete_dashboard_user_route(user_id: str):
    denied = require_admin()
    if denied:
        return denied
    target_user = str(user_id or "").strip()

    if target_user == session.get("user"):
        return jsonify({"success": False, "error": "You cannot delete your own logged-in account."}), 400

    admin_ids = db_store.get_admin_user_ids()
    if target_user in admin_ids and len(admin_ids) <= 1:
        return jsonify({"success": False, "error": "Cannot delete the only remaining admin."}), 400

    res = db_store.delete_dashboard_user(target_user)
    if not res.get("ok"):
        return jsonify({"success": False, "error": res.get("error")}), 400

    logging.info(f"Dashboard user '{target_user}' deleted by '{session.get('user')}'")
    return jsonify({"success": True, "message": f"User '{target_user}' deleted."})


# ==============================================================================
# 6. ALERTS API (sales-facing, critical only)
# ==============================================================================

@app.route('/api/alerts', methods=['GET'])
def list_sales_alerts():
    """Plain-English critical alerts for every signed-in user.

    Reads db_store.get_sales_alerts, whose column list excludes context,
    fingerprint and slack_permalink and which never touches alert_details, so a
    traceback cannot reach this response.
    """
    if not session.get("authenticated", False):
        return jsonify({"success": False, "error": "Not signed in."}), 401
    try:
        severity = request.args.get("severity") or None
        include_ackd = request.args.get("include_acknowledged", "true").lower() != "false"
        limit = int(request.args.get("limit", 100))
        rows = db_store.get_sales_alerts(
            severity=severity,
            include_ackd=include_ackd,
            limit=limit)
        return jsonify({
            "success": True,
            "data": rows,
            "alerts": rows,
            "count": len(rows),
            "open_count": db_store.open_alert_count(severity=severity)
        })
    except Exception as e:
        logging.error(f"Error fetching sales alerts: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/alerts/<int:alert_id>/ack', methods=['POST'])
def ack_sales_alert(alert_id: int):
    if not session.get("authenticated", False):
        return jsonify({"success": False, "error": "Not signed in."}), 401
    try:
        if not db_store.ack_alert(alert_id, session.get("user", "")):
            return jsonify({"success": False, "error": "Alert not found"}), 404
        logging.info(f"Alert {alert_id} acknowledged by {session.get('user')}")
        return jsonify({"success": True, "message": "Acknowledged."})
    except Exception as e:
        logging.error(f"Error acknowledging alert {alert_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/alerts/<int:alert_id>', methods=['DELETE'])
def delete_sales_alert(alert_id: int):
    """Permanently delete a sales alert.  Only permitted once the alert has
    been acknowledged or resolved — the DB enforces this too, but we return
    a clear 400 so the UI can explain the rule rather than showing a silent
    failure.  Any authenticated user may delete their own acknowledged alerts.
    """
    if not session.get("authenticated", False):
        return jsonify({"success": False, "error": "Not signed in."}), 401
    try:
        row = db_store.get_alert_detail(alert_id)
        if not row:
            return jsonify({"success": False, "error": "Alert not found."}), 404
        if row.get("status") not in ("acknowledged", "resolved"):
            return jsonify({
                "success": False,
                "error": "Alert must be acknowledged before it can be deleted."
            }), 400
        if not db_store.delete_alert(alert_id):
            return jsonify({"success": False, "error": "Could not delete alert."}), 500
        logging.info(f"Alert {alert_id} deleted by {session.get('user')}")
        return jsonify({"success": True, "message": "Alert deleted."})
    except Exception as e:
        logging.error(f"Error deleting alert {alert_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/developers/alerts/<int:alert_id>', methods=['DELETE'])
def delete_dev_alert(alert_id: int):
    """Permanently delete a developer alert.  Admin only.  Same acknowledged-
    or-resolved guard as the sales endpoint.
    """
    denied = require_admin()
    if denied:
        return denied
    try:
        row = db_store.get_alert_detail(alert_id)
        if not row:
            return jsonify({"success": False, "error": "Alert not found."}), 404
        if row.get("status") not in ("acknowledged", "resolved"):
            return jsonify({
                "success": False,
                "error": "Alert must be acknowledged or resolved before it can be deleted."
            }), 400
        if not db_store.delete_alert(alert_id):
            return jsonify({"success": False, "error": "Could not delete alert."}), 500
        logging.info(f"Developer alert {alert_id} deleted by {session.get('user')}")
        return jsonify({"success": True, "message": "Alert deleted."})
    except Exception as e:
        logging.error(f"Error deleting developer alert {alert_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# 7. SERVICE WATCHER
#
# This lives in the dashboard process, so a control_server outage means no
# service-level alerts at all. The bridges still push their own failures via
# critical_alerts.report, which is why alerting is not dependent on this thread.
# ==============================================================================

WATCH_INTERVAL_SECONDS = 30
WATCH_TIMEOUT_SECONDS = 5
# Consecutive failed watcher passes before alerting that monitoring is broken.
WATCH_FAILURE_ALERT_AFTER = 3

# Only services the supervisor actually keeps alive belong here. Every entry is
# a permanent alerting obligation: a component listed here that nothing starts
# will report SERVICE_DOWN forever and train the team to ignore the channel.
# This list must stay in sync with SERVICES in start_services.py.
COMPONENT_ENDPOINTS = {
    "whatsapp_gateway": f"http://{GATEWAY_HOST}:{GATEWAY_PORT}",
    "whatsapp_bridge": f"http://{WA_BRIDGE_HOST}:{WA_PORT}",
    "telegram_bridge": f"http://{TG_BRIDGE_HOST}:{TG_PORT}",
}
COMPONENT_LABELS = {
    "whatsapp_gateway": "The WhatsApp session (Node gateway)",
    "whatsapp_bridge": "The WhatsApp connection",
    "telegram_bridge": "The Telegram connection",
    "postgres": "The system database",
}
_watch_state: Dict[str, bool] = {}


def _check_service(component: str, base_url: str) -> Dict[str, Any]:
    """Inspects a service health endpoint and returns detailed status."""
    try:
        resp = requests.get(f"{base_url}/health", timeout=WATCH_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return {"healthy": False, "status": f"http_{resp.status_code}", "raw": resp.text[:100]}
        try:
            body = resp.json()
        except ValueError:
            return {"healthy": False, "status": "invalid_json"}

        if not isinstance(body, dict):
            return {"healthy": False, "status": "invalid_format"}

        status_str = str(body.get("status", "")).lower()
        if status_str == "qr_required":
            return {"healthy": False, "status": "qr_required", "details": body}
        if status_str == "needs_login" or body.get("authorized") is False:
            return {"healthy": False, "status": "needs_login", "details": body}
        if status_str == "ready" or bool(body.get("ok")):
            return {"healthy": True, "status": "ready", "details": body}
        return {"healthy": False, "status": status_str or "not_ready", "details": body}
    except requests.RequestException as e:
        return {"healthy": False, "status": "unreachable", "error": str(e)}


def _watch_once() -> Dict[str, bool]:
    checks: Dict[str, Dict[str, Any]] = {}

    for component, url in COMPONENT_ENDPOINTS.items():
        checks[component] = _check_service(component, url)

    try:
        pg_ok = db.is_available()
        checks["postgres"] = {"healthy": pg_ok, "status": "ready" if pg_ok else "unreachable"}
    except Exception as e:
        checks["postgres"] = {"healthy": False, "status": "unreachable", "error": str(e)}

    state: Dict[str, bool] = {}

    # Only transitions are reported, so a service that stays down does not post
    # on every tick, and a flapping service does not flood the channel.
    for component, check in checks.items():
        healthy = check["healthy"]
        state[component] = healthy
        previous = _watch_state.get(component)
        _watch_state[component] = healthy
        if previous is None and healthy:
            continue
        if previous is not None and previous == healthy:
            continue
        label = COMPONENT_LABELS.get(component, component)
        if healthy:
            alerts.report(code="SERVICE_RECOVERED", severity="info", component=component,
                          summary_plain=f"{label} is working again.",
                          technical_detail=f"{component} recovered and is answering health checks.",
                          key=component)
        else:
            status_val = check.get("status")
            if status_val == "qr_required":
                alerts.report(code="WHATSAPP_QR_REQUIRED", severity="critical", component=component,
                              summary_plain="WhatsApp requires a QR code scan. Open server logs (docker compose logs -f gateway) to link phone.",
                              technical_detail=f"{component} returned status 'qr_required' at {COMPONENT_ENDPOINTS.get(component)}.",
                              key=component)
            elif status_val == "needs_login":
                alerts.report(code="TELEGRAM_LOGIN_REQUIRED", severity="critical", component=component,
                              summary_plain="Telegram requires phone/OTP login. Run 'docker compose run --rm tg-bridge python main_telegram.py' on server.",
                              technical_detail=f"{component} reported not authorized at {COMPONENT_ENDPOINTS.get(component)}.",
                              key=component)
            else:
                alerts.report(code="SERVICE_DOWN", severity="critical", component=component,
                              summary_plain=f"{label} is not responding. Client messages may not be reaching the team.",
                              technical_detail=f"{component} failed its health check (status: {status_val}) at "
                                                f"{COMPONENT_ENDPOINTS.get(component, 'the PostgreSQL pool')}.",
                              key=component)
    return state


def _watch_loop() -> None:
    consecutive_failures = 0
    while True:
        try:
            _watch_once()
            consecutive_failures = 0
        except Exception as e:
            # The watcher must outlive any single failure inside it.
            logging.error(f"Service watcher pass failed: {e}")
            consecutive_failures += 1
            # A pass that keeps failing is worse than a logged line: it means
            # nothing is being monitored at all while everything looks fine.
            # Alert once the failure is clearly persistent rather than a blip.
            if consecutive_failures == WATCH_FAILURE_ALERT_AFTER:
                alerts.report(
                    code="SERVICE_WATCHER_BROKEN", severity="critical",
                    component="dashboard",
                    summary_plain="The system stopped checking whether services are "
                                  "alive. Outages may go unnoticed.",
                    technical_detail=f"The service watcher has failed "
                                      f"{consecutive_failures} times in a row and is no longer "
                                      f"reliable. Last error: {e}",
                    key="service_watcher")
        time.sleep(WATCH_INTERVAL_SECONDS)


def start_watcher() -> threading.Thread:
    thread = threading.Thread(target=_watch_loop, name="service-watcher", daemon=True)
    thread.start()
    logging.info(f"Service watcher started (every {WATCH_INTERVAL_SECONDS}s).")
    return thread


# ==============================================================================
# MAIN RUNNER
# ==============================================================================

if __name__ == "__main__":
    alerts.install_excepthook("dashboard")
    start_watcher()
    logging.info(f"🚀 Bitlink Control Dashboard REST API running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
