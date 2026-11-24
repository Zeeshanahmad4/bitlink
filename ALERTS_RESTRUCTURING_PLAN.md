# BitLink Alert System Restructuring Plan: Pure Service Status (Running / Off)

**Document Version:** 1.0  
**Status:** Planned / Architecture Review  
**Target:** Restrict alerts to strictly **OFFLINE (Down)** and **ONLINE (Back)** notifications for the 5 core services.  

---

## 1. Audit: All Alerts Currently Generated Across the System

Below is the complete list of alerts and notifications currently firing in the BitLink codebase:

| Source File | Alert Code / Trigger | Current Destination | Description / Cause |
| :--- | :--- | :--- | :--- |
| **`control_server.py`** | `SERVICE_DOWN` | Developer Channel | Fired by watchdog when a service fails `/health` check or is unreachable. |
| **`control_server.py`** | `SERVICE_RECOVERED` | Developer Channel | Fired by watchdog when a service comes back online. |
| **`control_server.py`** | `WHATSAPP_QR_REQUIRED` | Developer Channel | Fired when Baileys gateway drops session and requests QR code scan. |
| **`control_server.py`** | `TELEGRAM_LOGIN_REQUIRED` | Developer Channel | Fired when Telethon drops session and requests phone/OTP login. |
| **`control_server.py`** | `WATCHER_FAILED` | Developer Channel | Fired if the health watchdog loop itself crashes 3 times consecutively. |
| **`main_whatsapp.py`** | `SLACK_FORWARD_FAILED` | Developer Channel | Inbound client message failed to post into Slack channel. |
| **`main_whatsapp.py`** | `MEDIA_UPLOAD_FAILED` | Developer Channel | Media attachment failed to upload to Slack. |
| **`main_whatsapp.py`** | `SLACK_POST_EXHAUSTED` | Developer Channel | Slack API rate limit retries exhausted. |
| **`main_whatsapp.py`** | `POLLER_LOOP_ERROR` | Developer Channel | Exception in WhatsApp polling/relay loop. |
| **`main_whatsapp.py`** | `EMERGENCY_STOP_ON/OFF` | Developer Channel | Fired when emergency kill switch is toggled. |
| **`main_whatsapp.py`** | `EMERGENCY_INBOUND_DROPPED` | Developer Channel | Client message dropped because kill switch is engaged. |
| **`main_whatsapp.py`** | `AI_REPORT_FAILED` | Developer Channel | Daily/Weekly AI summary report generation failed. |
| **`main_whatsapp.py`** | In-thread retry warnings | Client Slack Channel | `🔴 Failed to send ... to WhatsApp. Queued for retry.` |
| **`main_whatsapp.py`** | In-thread handoff info | Client Slack Channel | Notice when a channel is paused or handed over to delivery. |
| **`main_telegram.py`** | `MEDIA_DOWNLOAD_FAILED` | Developer Channel | Downloading media from Telegram failed. |
| **`main_telegram.py`** | `MEDIA_UPLOAD_FAILED` | Developer Channel | Uploading Telegram media to Slack failed. |
| **`main_telegram.py`** | `FORWARD_HANDLER_ERROR` | Developer Channel | Generic exception in Telegram event handler. |
| **`main_telegram.py`** | `New Telegram Chat Alert` | Admin Channel | Alert whenever bot creates a group or joins a new chat. |
| **`service-baileys.js`**| `New WhatsApp Group` | Admin Channel | Alert when WhatsApp joins a new group. |
| **`slack_log_handler.py`**| Raw `ERROR` / `INFO` Logs | `SLACK_ERROR_CHANNEL` | Streams every Python `logging.error()` call directly into Slack. |
| **All Python processes**| `UNHANDLED_EXCEPTION` | Developer Channel | Installed via `sys.excepthook` for any unhandled crash. |

---

## 2. Target Requirements

1. **Only 5 Monitored Components**:
   - `1. WhatsApp Gateway` (`whatsapp-bot/service-baileys.js`)
   - `2. WhatsApp Bridge` (`main_whatsapp.py`)
   - `3. Telegram Bridge` (`main_telegram.py`)
   - `4. AI Jobs` (`sales_ai_jobs.py`)
   - `5. BitLink Manager` (`bitlink_manager.py`)
2. **Alert Cadence & Allowed Alert Types**:
   - 🔴 **SERVICE_DOWN (Capped at 5 alerts, 5 minutes apart)**:
     - Sent immediately when a service crashes or becomes unreachable. Up to 4 reminders every 5m, then silenced.
   - ⚠️ **LOGIN_REQUIRED (WhatsApp QR Scan / Telegram Login - Capped at 5 alerts, 5 minutes apart)**:
     - **WhatsApp (`WHATSAPP_QR_REQUIRED`)**: Fired if session drops while running and Baileys requests a QR scan.
     - **Telegram (`TELEGRAM_LOGIN_REQUIRED`)**: Fired if Telethon session is invalidated or requires phone/OTP login.
     - Sent immediately (Alert 1), then up to 4 reminders every 5m, then silenced until re-authenticated.
   - 🟢 **SERVICE_RECOVERED (RUNNING / BACK)**:
     - Sent once immediately when the service recovers or login is completed.
     - Resets the down/login alert counter back to `0`.
3. **No other alerts**:
   - Suppress all operational failure alerts (media upload fails, message forward fails, rate-limits, unhandled exceptions).
   - Disable `slack_log_handler` log streaming to Slack.
   - Disable group creation notifications.

---

## 3. Architecture & Implementation Plan

### Step 1: Health / Heartbeat Endpoints for all 5 Services

Currently, only 3 services have `/health` HTTP endpoints. We need all 5 to report their live pulse:

| Monitored Component | Monitoring Mechanism | Endpoint / Pulse |
| :--- | :--- | :--- |
| **1. WhatsApp Gateway** | HTTP `GET /health` | `http://gateway:3101/health` (Port 3101) |
| **2. WhatsApp Bridge** | HTTP `GET /health` | `http://wa-bridge:8102/health` (Port 8102) |
| **3. Telegram Bridge** | HTTP `GET /health` | `http://tg-bridge:8004/health` (Port 8004) |
| **4. AI Jobs** | PostgreSQL Heartbeat | Updates `service_heartbeats` table every 30s in `sales_ai_jobs.py` |
| **5. BitLink Manager** | HTTP `GET /health` or PostgreSQL Heartbeat | Dedicated port or updates `service_heartbeats` table every 30s |

#### PostgreSQL Heartbeat Table (`schema.sql`):
```sql
CREATE TABLE IF NOT EXISTS service_heartbeats (
    service_name VARCHAR(64) PRIMARY KEY,
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    status VARCHAR(16) DEFAULT 'running'
);
```
- `sales_ai_jobs.py`: Touches `service_heartbeats` every 30 seconds.
- `bitlink_manager.py`: Touches `service_heartbeats` every 30 seconds.

---

### Step 2: Unified Watchdog Engine with Down Retry Limiter (`control_server.py`)

Update the service watcher in `control_server.py`:
1. Check HTTP `/health` for Gateway, WhatsApp Bridge, and Telegram Bridge.
2. Check `NOW() - last_seen < 60 seconds` in `service_heartbeats` for AI Jobs and BitLink Manager.
3. Track per-service status and alert counters in memory/state:
   ```python
   service_alert_state[service] = {
       "status": "running",         # "running" or "off"
       "down_alert_count": 0,       # 0 up to 5
       "last_down_alert_at": None   # timestamp of last down alert
   }
   ```
4. **State & Alert Logic**:
   - **Transition from Running ➔ Off**:
     - Mark `status = "off"`
     - Send **Alert 1 of 5 immediately**: `🔴 [SERVICE DOWN] The <Service> is OFF / Not Responding (Alert 1/5)`
     - Set `down_alert_count = 1` and `last_down_alert_at = now()`
   - **Service Remains Off**:
     - If `down_alert_count < 5` and `(now() - last_down_alert_at) >= 5 minutes`:
       - Send reminder alert: `🔴 [SERVICE STILL DOWN] The <Service> is STILL OFF (Alert X/5)`
       - Increment `down_alert_count += 1` and update `last_down_alert_at = now()`
     - If `down_alert_count >= 5`:
       - **Silence**: No further alerts sent while the service remains off.
   - **Transition from Off ➔ Running**:
     - Mark `status = "running"`
     - Send recovery alert: `🟢 [SERVICE RECOVERED] The <Service> is BACK / Running`
     - Reset `down_alert_count = 0` and `last_down_alert_at = None`

---

### Step 3: Global Suppression of Non-Status Alerts

1. **`critical_alerts.py`**:
   - Restrict `_post_to_channel()` to ONLY post alerts if `code` is in `("SERVICE_DOWN", "SERVICE_RECOVERED", "WHATSAPP_QR_REQUIRED", "TELEGRAM_LOGIN_REQUIRED")` and `component` is one of the allowed services.
   - All other codes (`MEDIA_UPLOAD_FAILED`, `SLACK_FORWARD_FAILED`, `UNHANDLED_EXCEPTION`, etc.) will still be written to the database for debugging, but **will NOT be posted to Slack**.
2. **`slack_log_handler.py`**:
   - Disable automatic log handler attachment in `main_telegram.py`, `main_whatsapp.py`, and `bitlink_manager.py`.
3. **`main_telegram.py` & `service-baileys.js`**:
   - Disable `send_new_chat_alert_to_slack` and `notifyNewGroup` Slack postings.

---

## 4. Exact Alert Message Format

### When a Service Goes Down (Alert 1 of 5 - Immediate):
> 🔴 **ALERT: SERVICE DOWN (1/5)**  
> **Component:** `WhatsApp Bridge`  
> **Status:** `OFF / Unreachable`  
> **Time:** `2026-10-20 15:45:00 PKT`  
> _Service down. Follow-up reminders scheduled every 5m (up to 5 alerts max)._

### When WhatsApp Drops Session & Requires QR Scan (Alert 1 of 5):
> 📱 **ALERT: WHATSAPP QR SCAN REQUIRED (1/5)**  
> **Component:** `WhatsApp Gateway`  
> **Status:** `SESSION_DISCONNECTED / QR_WAITING`  
> **Action Required:** `Open server logs (docker compose logs -f gateway) to scan the QR code with WhatsApp.`  
> **Time:** `2026-10-20 15:45:00 PKT`  
> _Reminders scheduled every 5m (up to 5 alerts max)._

### When Telegram Drops Session & Requires Login (Alert 1 of 5):
> 🔑 **ALERT: TELEGRAM LOGIN REQUIRED (1/5)**  
> **Component:** `Telegram Bridge`  
> **Status:** `SESSION_EXPIRED / NOT_AUTHORIZED`  
> **Action Required:** `Run 'docker compose run --rm tg-bridge python main_telegram.py' on the server to enter phone OTP.`  
> **Time:** `2026-10-20 15:45:00 PKT`  
> _Reminders scheduled every 5m (up to 5 alerts max)._

### When a Service Recovers / Login Completed:
> 🟢 **RESOLVED: SERVICE BACK**  
> **Component:** `WhatsApp Gateway`  
> **Status:** `RUNNING / Operational`  
> **Time:** `2026-10-20 16:02:10 PKT`  
> _Connection restored and answering health checks. Alert counters reset._

---

## 5. Verification Checklist (For Future Implementation)

- [ ] Stop `gateway` (`docker compose stop gateway`):
  - [ ] Verify **Alert 1/5** fires **immediately**.
  - [ ] Wait 5 minutes $\rightarrow$ Verify **Alert 2/5** fires.
  - [ ] Continue to Alert 5/5 $\rightarrow$ Verify **no more alerts** fire after the 5th alert.
- [ ] Start `gateway` (`docker compose start gateway`):
  - [ ] Verify **🟢 WhatsApp Gateway is BACK** fires once immediately.
  - [ ] Verify internal alert counter resets to 0.
- [ ] Stop `gateway` again $\rightarrow$ Verify the 5-alert sequence begins anew from Alert 1/5.
- [ ] Repeat test for `wa-bridge`, `tg-bridge`, `ai-jobs`, and `slack-manager`.
- [ ] Simulate media upload error or invalid message $\rightarrow$ Verify **no Slack alert** is sent.
- [ ] Check Slack alert channel is 100% clean of raw logs and spam.
