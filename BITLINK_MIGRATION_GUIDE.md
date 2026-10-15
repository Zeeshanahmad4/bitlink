# BitLink — Complete Production Migration & Cut-Over Guide

This guide details the complete, end-to-end process for migrating your running BitLink instance from Google Sheets to the new PostgreSQL-backed BitLink Sales CRM on your production server (`173.212.215.140`), while:
1. **Preserving existing Slack channels** for all delivery and sales clients.
2. **Preserving your dashboard admin user** and credentials.
3. **Migrating all active leads, client mappings, reps, and settings** from Google Sheets.
4. **Resuming bidirectional message forwarding** between WhatsApp/Telegram and Slack immediately.

---

## Table of Contents

1. [Architecture & Safety Guarantees](#1-architecture--safety-guarantees)
2. [Prerequisites Checklist](#2-prerequisites-checklist)
3. [Slack Apps & Bot Permissions (Full Reference)](#3-slack-apps--bot-permissions-full-reference)
4. [Production Environment Configuration (`.env`)](#4-production-environment-configuration-env)
5. [Database Clean-Up & Data Migration](#5-database-clean-up--data-migration)
6. [Gateway Authorization (WhatsApp & Telegram)](#6-gateway-authorization-whatsapp--telegram)
7. [Resuming Communication in Existing Channels](#7-resuming-communication-in-existing-channels)
8. [Post-Migration Verification & Health Checks](#8-post-migration-verification--health-checks)

---

## 1. Architecture & Safety Guarantees

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Google Spreadsheet                                │
│                   (BitLink Client Mappings - Read Only)                     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                      One-time import  ▼  migrate_sheets_to_pg.py
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PostgreSQL 15 (Docker)                             │
│                                                                             │
│  ✅ PRESERVED: dashboard_users (Admin login safe, never truncated)          │
│  🔄 REFRESHED: client_mappings, sales_leads, sales_pool, project_managers,  │
│               blocklist, settings, snippets, critical_alerts               │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼ Real-Time In-Memory Maps
┌─────────────────────────────────────────────────────────────────────────────┐
│                       BitLink Routing Bridges (Docker)                      │
│                                                                             │
│  [WhatsApp Bridge] ◄──────────────► [Existing Slack Delivery Channels]     │
│  [Telegram Bridge] ◄──────────────► [Existing Slack Sales Channels]        │
└─────────────────────────────────────────────────────────────────────────────┘
```

* **Server Isolation:** All new files and containers run inside `/opt/bitlink-sales`. The legacy instance at `/opt/bbitlink` is completely isolated and untouched.
* **Port Mapping:** Dashboard runs on port `5051` (proxied by Nginx at `https://bitlink-sales.duckdns.org`), WhatsApp refresh server on `8102`, Telegram refresh on `8004`, and Gateway on `3101`.

---

## 2. Prerequisites Checklist

Before executing the migration, ensure the following are ready:

- [ ] **Google Spreadsheet Access:**
  * Open your Google Spreadsheet (default name: `BitLink Client Mappings`).
  * Click **Share** (top right) and add the BitLink service account email as **Viewer** or **Editor**:
    ```text
    bitlink-sa@bitlink-509011.iam.gserviceaccount.com
    ```
- [ ] **Slack Workspace Admin Access:**
  * Access to [api.slack.com/apps](https://api.slack.com/apps) for your 3 Slack apps.
- [ ] **Production WhatsApp Phone:**
  * Device with WhatsApp installed, ready to scan a QR code from the terminal.
- [ ] **Production Telegram Account:**
  * Phone number with Telegram installed, ready to receive an OTP login code.
  * `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
- [ ] **Google Gemini API Key:**
  * Production key from [Google AI Studio](https://aistudio.google.com/).

---

## 3. Slack Apps & Bot Permissions (Full Reference)

BitLink uses **three Slack Apps**. When channels are already created, the bots must have the following exact scopes configured under **OAuth & Permissions → Scopes → Bot Token Scopes**:

### App 1: BitLink WhatsApp Bridge (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`)

| Scope | Required For |
| :--- | :--- |
| `chat:write` | Post forwarded WhatsApp messages and notices into Slack channels |
| `chat:write.customize` | Post using the client's custom display name and avatar |
| `channels:history` | Read messages sent by your team in public channels to forward to WhatsApp |
| `channels:read` | Look up public channels and channel metadata |
| `channels:manage` | In-place channel renaming (`conversations.rename`) and archiving |
| `channels:join` | Automatically join public channels |
| `groups:history` | Read messages sent by your team in **private** channels |
| `groups:read` | Look up private channel metadata |
| `groups:write` | In-place channel renaming and archiving for private channels |
| `files:write` | Upload media files (images, audio, PDFs) received from WhatsApp |
| `files:read` | Download media files uploaded by your team in Slack to relay to WhatsApp |
| `reactions:write` | Add `:white_check_mark:` on delivery success and `:x:` on failure |
| `reactions:read` | Verify Slack reactions |
| `im:history` | Read direct messages |
| `im:write` | Send direct alerts/notifications to assigned sales reps |
| `im:read` | Look up DM channels |
| `users:read` | Resolve Slack User IDs to human-readable names |

* **Socket Mode:** Enable in **Socket Mode** tab.
* **App-Level Token:** Under **Basic Information → App-Level Tokens**, create a token named `socket-mode` with scope `connections:write` (`xapp-...`).
* **Event Subscriptions:** Enable Events and subscribe to: `message.channels`, `message.groups`, `message.im`, `app_mention`.

---

### App 2: BitLink Telegram Bridge (`SLACK_BOT_TOKEN_TELEGRAM`, `SLACK_APP_TOKEN_TELEGRAM`)

| Scope | Required For |
| :--- | :--- |
| `chat:write` | Post forwarded Telegram messages and files into Slack channels |
| `chat:write.customize` | Post using the client's custom name and Telegram avatar |
| `channels:history` | Read outbound messages sent in public channels to relay to Telegram |
| `channels:read` | Read public channel details |
| `channels:manage` | In-place channel renaming and archiving |
| `channels:join` | Allow bot to join public channels |
| `groups:history` | Read outbound messages sent in **private** channels |
| `groups:read` | Read private channel details |
| `groups:write` | In-place channel renaming and archiving for private channels |
| `files:write` | Upload media files (photos, audio, documents) received from Telegram |
| `files:read` | Read files uploaded in Slack to forward to Telegram clients |
| `reactions:write` | Add `:white_check_mark:` on delivery success and `:x:` on failure |
| `reactions:read` | Read Slack reactions |
| `im:history` | Read direct message history |
| `im:write` | Send direct alerts/notifications to sales reps |
| `im:read` | Read direct message channel data |
| `users:read` | Resolve Slack User IDs to display names |

* **Socket Mode:** Enable in **Socket Mode** tab.
* **App-Level Token:** Generate `connections:write` token (`xapp-...`).
* **Event Subscriptions:** Subscribe to: `message.channels`, `message.groups`, `message.im`, `app_mention`.

---

### App 3: BitLink Manager (`MANAGER_SLACK_BOT_TOKEN`, `MANAGER_SLACK_APP_TOKEN`)

| Scope | Required For |
| :--- | :--- |
| `commands` | Trigger slash commands (`/handoff`, `/sales-snippet`, `/enhance`) |
| `chat:write` | Post interactive modals, confirmations, and announcements |
| `chat:write.public` | Post in public channels without prior invite |
| `channels:read` | Read channel lists and membership |
| `groups:read` | Read private channel lists and membership |
| `users:read` | List sales pool reps and project managers in drop-down menus |
| `im:write` | Send direct notifications and modal responses |

* **Socket Mode:** Enable in **Socket Mode** tab with `connections:write` token (`xapp-...`).
* **Slash Commands:** Register:
  * `/handoff` — Transfer lead ownership to another rep.
  * `/sales-snippet` — Insert quick sales message templates.
  * `/enhance` — AI-draft an intelligent response from conversation history.
* **Master Bot User ID:** In Slack, view the Manager bot profile → click `...` → **Copy member ID** (format: `U0...`). Save as `MASTER_SLACK_BOT_ID`.

---

### Critical Step for Existing Channels

For existing **private delivery channels** and **private sales channels**, Slack bots cannot read or post messages unless they are invited members.

In each private channel where clients are mapped, run:
```text
/invite @BitLink WhatsApp Bridge
/invite @BitLink Telegram Bridge
/invite @BitLink Manager
```
*(If you are reusing the exact same bot tokens from your legacy instance, the bots are already in those channels).*

---

## 4. Production Environment Configuration (`.env`)

Update `/Users/apple/Desktop/BitLink-Sales/bitlink/.env` with your production values:

```bash
# --- Slack: WhatsApp Bridge ---
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# --- Slack: Telegram Bridge ---
SLACK_BOT_TOKEN_TELEGRAM=xoxb-...
SLACK_APP_TOKEN_TELEGRAM=xapp-...

# --- Slack: BitLink Manager ---
MANAGER_SLACK_BOT_TOKEN=xoxb-...
MANAGER_SLACK_APP_TOKEN=xapp-...
MASTER_SLACK_BOT_ID=U0...

# --- Telegram API (my.telegram.org) ---
API_ID=...
API_HASH=...

# --- AI Insights & Digestion ---
GEMINI_API_KEY=AIzaSy...

# --- Microservices Inter-Container Networking ---
NODE_API_URL=http://gateway:3101
WHATSAPP_REFRESH_PORT=8102
TELEGRAM_REFRESH_PORT=8004
CONTROL_DASHBOARD_PORT=5051
MAX_FILE_SIZE_MB=500
SESSIONS_DIR=sessions

# --- PostgreSQL Database ---
DATABASE_URL=postgresql://bitlink:YourProductionPassword@db:5432/bitlink
DB_PASSWORD=YourProductionPassword
```

Deploy the updated configuration to the server:
```bash
cd /Users/apple/Desktop/BitLink-Sales/bitlink
./deploy.sh 173.212.215.140
```

---

## 5. Database Clean-Up & Data Migration

This step clears all test data from PostgreSQL while **strictly preserving your dashboard admin user**, and imports all active data from your Google Sheet.

### Step 5.1: Run the Migration Script
SSH into the server and run the migration inside the `dashboard` container:

```bash
ssh root@173.212.215.140
cd /opt/bitlink-sales

docker compose exec dashboard python3 migrate_sheets_to_pg.py --allow-truncate
```

### What Happens:
1. `TRUNCATE` runs against:
   * `client_mappings`
   * `sales_leads`
   * `sales_pool`
   * `blocklist`
   * `project_managers`
   * `snippets`
   * `settings`
   * `critical_alerts`
   * `alert_details`
2. **`dashboard_users` is preserved:** Your existing admin user login remains completely functional and untouched.
3. All rows from Google Sheets are imported with type safety and indexed natural keys.
4. An automatic `/refresh` signal is dispatched to the running bridges.

### Step 5.2: Verify Data Parity
Check that row counts in PostgreSQL match your Google Spreadsheet:

```bash
docker compose exec dashboard python3 migrate_sheets_to_pg.py --verify
```

Expected output:
```text
table                    sheets   postgres  status
-------------------------------------------------------
client_mappings              42         42  OK
sales_leads                 118        118  OK
sales_pool                    6          6  OK
blocklist                     3          3  OK
project_managers              4          4  OK
snippets                     12         12  OK
settings                      -         21  (deduped from sheet)
dashboard_users               -          2  (hashed, existing preserved)

value-level check: OK (compared client_mappings, sales_leads, settings)
```

---

## 6. Gateway Authorization (WhatsApp & Telegram)

### 6.1 Link Production WhatsApp (Baileys)
1. Reset old test session data:
   ```bash
   docker compose stop gateway
   rm -rf whatsapp-bot/baileys_auth_info/*
   docker compose start gateway
   ```
2. Display the QR code:
   ```bash
   docker compose logs -f gateway
   ```
3. Open WhatsApp on your production phone → **Linked Devices** → **Link a device** → Scan the QR code in the terminal.
4. Confirm in logs:
   ```text
   ✅ [BAILEYS] Connection opened successfully!
   ```

### 6.2 Link Production Telegram (Telethon)
1. Stop the bridge and clear stale test sessions:
   ```bash
   docker compose stop tg-bridge
   rm -f sessions/*.session
   ```
2. Run one-time interactive authorization:
   ```bash
   docker compose run --rm -it tg-bridge python3 -c "
   from telethon.sync import TelegramClient
   import os
   client = TelegramClient('sessions/telethon', int(os.getenv('API_ID')), os.getenv('API_HASH'))
   client.start()
   print('✅ Telegram logged in successfully!')
   "
   ```
3. Enter:
   * **Phone Number** (with country code, e.g. `+1...` or `+92...`)
   * **OTP Login Code** (received in your Telegram app)
   * **2FA Password** (if enabled)
4. Restart the service:
   ```bash
   docker compose restart tg-bridge
   ```

---

## 7. Resuming Communication in Existing Channels

Once data is migrated, the bridges need to load the fresh database rows into memory:

```bash
docker compose restart wa-bridge tg-bridge
```

### How Message Resumption Works:
* **Delivery Clients:**
  * In Google Sheets, each delivery client row contains `platform`, `client_name`, `external_id`, and `slack_channel_id`.
  * The bridge maps `external_id` (phone number or Telegram chat ID) directly to `slack_channel_id`.
  * **Inbound:** When the client sends a message, it is instantly routed into their existing Slack channel.
  * **Outbound:** When team members reply in that Slack channel, the bridge captures the message and sends it to the client.
  * **Status Tick:** Successful deliveries will be reacted with `:white_check_mark:` in Slack.
* **Sales Leads:**
  * Active sales leads mapped to `#sale-wa-...` or `#sale-tg-...` resume normal forwarding.
  * Any unmapped incoming messages from new contacts will auto-provision a new channel and assign a sales rep from the active sales pool via round-robin.

---

## 8. Post-Migration Verification & Health Checks

### 8.1 Verify Bot Connectivity
Run this test command on the server to verify all 3 Slack apps are authenticating:

```bash
docker compose exec dashboard python3 -c "
import os
from dotenv import load_dotenv; load_dotenv()
from slack_sdk import WebClient

bots = [
    ('WhatsApp Bridge', os.getenv('SLACK_BOT_TOKEN')),
    ('Telegram Bridge', os.getenv('SLACK_BOT_TOKEN_TELEGRAM')),
    ('BitLink Manager', os.getenv('MANAGER_SLACK_BOT_TOKEN'))
]

for name, token in bots:
    try:
        res = WebClient(token=token).auth_test()
        print(f'✅ {name}: Connected as @{res[\"user\"]} (ID: {res[\"user_id\"]})')
    except Exception as e:
        print(f'❌ {name}: Failed -> {e}')
"
```

### 8.2 Verify Dashboard Health
1. Open **`https://bitlink-sales.duckdns.org`** in your browser.
2. Log in with your admin credentials.
3. Check the status indicators in the header:
   * **WhatsApp Bridge:** `Connected`
   * **Telegram Bridge:** `Connected`
   * **PostgreSQL Database:** `Connected`
4. Verify the **Delivery Clients** view shows all your imported delivery rows.
5. Verify the **Sales CRM Pipeline** view shows all your leads and stages.

### 8.3 Live End-to-End Test
1. Send a test message from a personal WhatsApp or Telegram account to the production number.
2. Verify the message arrives in the corresponding Slack channel.
3. Reply in Slack to that message.
4. Verify the reply arrives on WhatsApp/Telegram and Slack adds a `:white_check_mark:` reaction to your message.
