# control_server.py — Bitlink Control Dashboard REST API Server
import os
import logging
import threading
import time
import requests
from flask import Flask, request, jsonify, render_template, session
from dotenv import load_dotenv
load_dotenv()

import db_store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

from datetime import timedelta
import uuid

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

# --- Authentication Endpoints ---

def verify_credentials(username: str, password: str) -> bool:
    """Verifies dashboard credentials against the bcrypt hashes in PostgreSQL.

    db_store falls back to the legacy 'auth' worksheet and re-hashes on first
    successful login, so existing accounts keep working through the migration.

    The old hardcoded admin/admin bypass is gone. If credential lookup fails
    outright we deny the login rather than granting a default account.
    """
    try:
        return db_store.verify_login(username.strip(), password.strip()) is not None
    except Exception as e:
        logging.error(f"Error checking credentials: {e}")
        return False


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(force=True) if request.data else {}
    username = str(data.get("username") or data.get("id") or "").strip()
    password = str(data.get("password") or data.get("passcode") or "").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required."}), 400

    if verify_credentials(username, password):
        session.permanent = True
        session["authenticated"] = True
        session["user"] = username
        session["login_time"] = time.time()
        logging.info(f"User '{username}' logged in successfully (valid for 24 hours).")
        return jsonify({"success": True, "message": "Authentication successful.", "user": username})
    
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
        is_auth = False
        user = ""
    else:
        user = session.get("user", "")
    return jsonify({"authenticated": is_auth, "user": user})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    user = session.pop("user", "User")
    session.pop("authenticated", None)
    logging.info(f"User '{user}' logged out.")
    return jsonify({"success": True, "message": "Logged out successfully."})

PORT = int(os.getenv("CONTROL_DASHBOARD_PORT", 5050))


# --- Bridge Auto-Refresh Dispatcher ---

def dispatch_bridge_refresh():
    """Dispatches background refresh requests to running bridge services."""
    def _worker():
        ports = [
            ("WhatsApp", os.getenv("WHATSAPP_REFRESH_PORT", 8101)),
            ("Telegram", os.getenv("TELEGRAM_REFRESH_PORT", 8003)),
            ("Discord", os.getenv("DISCORD_REFRESH_PORT", 8102))
        ]
        headers = {'Content-Type': 'application/json'}
        for name, port in ports:
            url = f"http://localhost:{port}/refresh"
            try:
                requests.post(url, headers=headers, timeout=1.5)
                logging.info(f"⚡ [REFRESH] Signal sent successfully to {name} bridge on port {port}")
            except requests.exceptions.RequestException:
                logging.debug(f"ℹ️ [REFRESH] Could not reach {name} bridge on port {port} (may be offline)")

    threading.Thread(target=_worker, daemon=True).start()


# ==============================================================================
# 1. SYSTEM HEALTH API
# ==============================================================================

@app.route('/api/health', methods=['GET'])
def get_system_health():
    """Returns real-time health of all running bridges and gateway services."""
    bridges = {}

    # WhatsApp Node Gateway (3101)
    try:
        r = requests.get("http://127.0.0.1:3101/health", timeout=1.0)
        bridges["whatsapp_gateway"] = {"online": r.status_code == 200, "details": r.json() if r.status_code == 200 else {}}
    except Exception:
        bridges["whatsapp_gateway"] = {"online": False}

    # WhatsApp Python Bridge (8101)
    try:
        r = requests.get("http://127.0.0.1:8101/health", timeout=1.0)
        details = r.json() if r.status_code == 200 else {}
        # The bridge is reachable while emergency-stopped; surface that state
        # explicitly so the dashboard can say STOPPED rather than Offline.
        bridges["whatsapp_bridge"] = {
            "online": r.status_code == 200,
            "emergency_stop": details.get("emergency_stop", {"active": False}),
            "details": details,
        }
    except Exception:
        bridges["whatsapp_bridge"] = {"online": False, "emergency_stop": {"active": False}}

    # Telegram Python Bridge (8003)
    try:
        r = requests.get("http://127.0.0.1:8003/health", timeout=1.0)
        bridges["telegram_bridge"] = {"online": r.status_code == 200, "details": r.json() if r.status_code == 200 else {}}
    except Exception:
        bridges["telegram_bridge"] = {"online": False}

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
    """Adds a new delivery client mapping."""
    data = request.get_json(force=True)
    client_name = data.get("client_name", "").strip()
    platform = data.get("platform", "WhatsApp").strip()
    external_id = data.get("external_id", "").strip()
    slack_channel_id = data.get("slack_channel_id", "").strip()
    slack_channel_name = data.get("slack_channel_name", "").strip()
    paused = bool(data.get("paused"))

    if not client_name or not external_id:
        return jsonify({"success": False, "error": "Missing required fields: client_name, external_id"}), 400
        
    if not slack_channel_id and not slack_channel_name:
        return jsonify({"success": False, "error": "Must provide either a New Channel Name or an Existing Channel ID."}), 400

    if not slack_channel_id:
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
            
            # Remove duplicates
            users_to_invite = list(set(users_to_invite))
            
            if users_to_invite:
                try:
                    wc.conversations_invite(channel=slack_channel_id, users=",".join(users_to_invite))
                except Exception as e:
                    logging.warning(f"Failed to auto-invite users {users_to_invite} to delivery channel: {e}")
        except Exception as e:
            return jsonify({"success": False, "error": f"Failed to auto-provision channel: {e}"}), 500

    # Set when this request provisioned a fresh Slack channel, so a failed
    # database write does not leave an orphaned channel behind.
    provisioned_channel_id = None

    try:
        # 1. Idempotency check
        for rec in db_store.list_all_client_mappings():
            if str(rec.get("external_id", "")) == external_id and \
               str(rec.get("slack_channel_id", "")) == slack_channel_id:
                return jsonify({"success": False, "error": "Client is already mapped to Delivery."}), 409

        db_store.add_client_mapping(platform, client_name, external_id,
                                    slack_channel_id, paused)

        # 2. Auto-pause the matching sales lead
        try:
            db_store.update_sales_lead_by_external_id(external_id, {"paused": True})
        except Exception as e:
            logging.error(f"Failed to auto-pause sales lead: {e}")

        dispatch_bridge_refresh()
        logging.info(f"Created delivery mapping: {client_name} ({external_id}) -> {slack_channel_id}")
        return jsonify({"success": True, "message": "Delivery mapping created successfully."}), 201
    except Exception as e:
        logging.error(f"Error adding delivery mapping: {e}")
        if provisioned_channel_id:
            # Best effort: don't leave a channel that maps to no client.
            try:
                from slack_sdk import WebClient as _WC
                bot_token = os.getenv("SLACK_BOT_TOKEN") if platform.lower() == "whatsapp" \
                    else os.getenv("SLACK_BOT_TOKEN_TELEGRAM", os.getenv("SLACK_BOT_TOKEN"))
                _WC(token=bot_token).conversations_archive(channel=provisioned_channel_id)
                logging.warning(f"Archived orphaned Slack channel {provisioned_channel_id} "
                                f"after the mapping failed to save.")
            except Exception as e2:
                logging.error(f"Could not archive orphaned channel "
                              f"{provisioned_channel_id}: {e2}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/delivery/mappings/<int:row_idx>', methods=['PUT'])
def update_delivery_mapping(row_idx: int):
    """Updates an existing delivery client mapping. row_idx is the stable id."""
    data = request.get_json(force=True)
    try:
        current = db_store.get_client_mapping_by_id(row_idx)
        if not current:
            return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404

        fields = {
            "client_name": data.get("client_name", current.get("client_name", "")),
            "platform": data.get("platform", current.get("platform", "WhatsApp")),
            "external_id": data.get("external_id", current.get("external_id", "")),
            "slack_channel_id": data.get("slack_channel_id", current.get("slack_channel_id", "")),
        }
        if "paused" in data:
            fields["paused"] = bool(data["paused"])

        ok = db_store.update_client_mapping(row_idx, fields)
        if not ok:
            return jsonify({"success": False, "error": f"Mapping {row_idx} not found"}), 404

        dispatch_bridge_refresh()
        logging.info(f"Updated delivery mapping {row_idx}: {fields.get('client_name')}")
        return jsonify({"success": True, "message": f"Mapping {row_idx} updated successfully."})
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

        # Safeguard: Prevent resuming sales channel if delivery channel is still active
        if "paused" in data and not data["paused"]:
            for d_rec in db_store.list_all_client_mappings():
                if str(d_rec.get("external_id", "")).strip() == clean_ext_id:
                    if not bool(d_rec.get("paused")):
                        return jsonify({
                            "success": False,
                            "error": "Cannot resume Sales Channel while Delivery Channel is still active. Please pause the client in the Delivery team first."
                        }), 403

        # If stage is changed to won or lost, automatically record closed_at timestamp
        if data.get("stage") in ["won", "lost"]:
            data["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if not db_store.update_sales_lead(lead_id, data):
            return jsonify({"success": False, "error": f"Lead {clean_ext_id} not found"}), 404

        dispatch_bridge_refresh()
        logging.info(f"Updated sales lead {clean_ext_id} (id={lead_id}): {data}")
        return jsonify({"success": True, "message": f"Lead {clean_ext_id} updated successfully."})
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
    try:
        url = f"http://127.0.0.1:{os.getenv('WHATSAPP_REFRESH_PORT', 8101)}/sales-digest/trigger"
        r = requests.post(url, timeout=10.0)
        try:
            return r.json(), r.status_code
        except Exception:
            if r.status_code == 404:
                return jsonify({"success": False, "error": "Bridge is running an older process. Please restart main_whatsapp.py to activate the digest endpoint."}), 400
            return jsonify({"success": False, "error": f"Bridge returned status {r.status_code}: {r.text[:100]}"}), 500
    except Exception as e:
        logging.error(f"Error calling bridge digest trigger: {e}")
        return jsonify({"success": False, "error": "WhatsApp bridge is offline. Please start main_whatsapp.py"}), 503


@app.route('/api/sales/insights/generate', methods=['POST'])
def generate_sales_insights():
    """Triggers Gemini AI Win/Loss intelligence report for closed deals."""
    try:
        url = f"http://127.0.0.1:{os.getenv('WHATSAPP_REFRESH_PORT', 8101)}/sales-insights/generate"
        body = request.get_json(force=True) if request.data else {}
        r = requests.post(url, json=body, timeout=30.0)
        try:
            return r.json(), r.status_code
        except Exception:
            return jsonify({"success": False, "error": f"Bridge error: {r.text[:100]}"}), 500
    except Exception as e:
        logging.error(f"Error calling bridge insights generation: {e}")
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
    """Adds a phone number or JID to the blocklist."""
    data = request.get_json(force=True)
    external_id = str(data.get("external_id", "")).strip()
    reason = str(data.get("reason", "Added via Dashboard")).strip()
    added_at = time.strftime("%Y-%m-%d")

    if not external_id:
        return jsonify({"success": False, "error": "Missing external_id"}), 400

    try:
        # Idempotency check
        if external_id in db_store.get_blocklist():
            return jsonify({"success": False, "error": f"{external_id} is already in the blocklist."}), 409

        db_store.add_to_blocklist(external_id, reason, added_at)
        logging.info(f"Added {external_id} to blocklist (reason: {reason})")
        return jsonify({"success": True, "message": f"{external_id} added to blocklist."}), 201
    except Exception as e:
        logging.error(f"Error adding to blocklist: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/sales/blocklist/<int:row_idx>', methods=['DELETE'])
def delete_from_blocklist(row_idx: int):
    """Deletes a blocklist entry by id."""
    if not db_store.delete_from_blocklist(row_idx):
        return jsonify({"success": False, "error": f"Blocklist entry {row_idx} not found"}), 404
    logging.info(f"Removed blocklist entry {row_idx}")
    return jsonify({"success": True, "message": f"Blocklist entry {row_idx} removed."})

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
# MAIN RUNNER
# ==============================================================================

if __name__ == "__main__":
    logging.info(f"🚀 Bitlink Control Dashboard REST API running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
