# Bitlink — Technical Reference & System Architecture

_Last updated: March 30, 2026_

---

## 🚀 What Is Bitlink?
Bitlink is a production-grade, multi-platform chat bridge that synchronizes client conversations from **WhatsApp, Telegram, Discord (and optional email/Upwork)** directly into **Slack**. This allows teams to manage and reply to all client chats from Slack, while clients continue using their preferred platforms. Bitlink is designed for agencies, consultancies, and product teams needing inbox unification, traceability, and robust cross-platform communication.

---

## 🎯 Core Problem Solved
- Clients prefer to stay on their own messaging apps
- Critical conversations get fragmented and lost
- Teams need one searchable, transparent source of truth
- Bitlink bridges all major chat platforms into Slack, allowing full visibility and seamless two-way communication without requiring clients to migrate

---

## 📐 High-Level Architecture
Bitlink orchestrates a set of independently operating bridge services (mainly Python), a WhatsApp-specific gateway (Node.js), and a central configuration/map in Google Sheets.

```
Client platforms (WhatsApp, Telegram, Discord, Email/Upwork)
    ↓
Bridge Services (Python: main_telegram.py, main_whatsapp.py, discum_ai_http.py, main_upwork.py)
    ↓
Google Sheets mapping table (g_sheets_client.py)
    ↓
Slack channels (per-client mapping)
    ↑
Management Server (Flask: management_server.py) + Slack commands for config ops
```

### Message Flow Example (WhatsApp):
- **Inbound:** Client → WhatsApp Gateway (Node) → management_server → main_whatsapp.py → Google Sheets lookup → Slack channel
- **Outbound:** Slack message → main_whatsapp.py → WhatsApp Gateway (Node) → WhatsApp client

---

## 🗂 Key Components
### Python Services
- **main_telegram.py** — Telegram ↔ Slack bridge
- **main_whatsapp.py** — WhatsApp ↔ Slack bridge (communicates with Node.js gateway)
- **main_upwork.py** — IMAP/SMTP bridge for Upwork/Email → Slack
- **discum_ai_http.py** — Discord ↔ Slack bridge
- **management_server.py** — Central admin entrypoint (handles config via Slack commands, can hot-reload bridge configs)
- **g_sheets_client.py** — Client/account mapping via Google Sheets
- **slack_log_handler.py** — Sends logs to Slack info/error channels

### Node.js Services (WhatsApp-specific)
- **wa_gateway/whatsapp_gateway.js** — WhatsApp Web automation, receives API calls from Python bridge
- **whatsapp-bot/service.js** — Standalone WA bot (group tracking, notification)

### Utilities
- **gemini_enhance.py** — Uses Google Gemini API to rewrite/polish outgoing client messages (triggered via Slack `/enhance` command)
- **enhance_slack_webhook.py** — HTTP handler for message enhancement commands

---

## 📊 Configuration & Data Model
- **No traditional DB.**
- **Google Sheets:** Holds the canonical mapping of clients/accounts to Slack channels (platform, client_name, external_id, slack_channel_id, paused).
- **JSON files:** Store message state maps, group notification history (e.g., notified_groups.json).
- **.env:** Holds all API keys, secrets, and service configuration (see example below).

---

## 🔒 Security Model
- **.env:** Stores all secret keys, tokens, shared secrets, and API endpoints.
- **OAuth tokens:** Used for Slack, Telegram, Google APIs
- **Webhook signature validation:** Enforced for Slack
- **Per-tenant isolation** (mapped Slack channels)
- **Access control:** Only mapped team members see client channels
- **Error/log redaction:** Sensitive errors are handled gracefully and logged privately

**.env Example:**
```
API_ID=<telegram_app_id>
API_HASH=<telegram_app_hash>
SLACK_BOT_TOKEN_TELEGRAM=...
SLACK_BOT_TOKEN=...
NODE_API_URL=http://127.0.0.1:3101
DISCORD_TOKEN=...
CREDENTIALS_FILE=credentials/service_account.json
GEMINI_API_KEY=...
SLACK_INFO_CHANNEL=...
SLACK_ERROR_CHANNEL=...
TELEGRAM_REFRESH_PORT=8003
WHATSAPP_REFRESH_PORT=8101
...
```

---

## 🛠️ Running and Operating Bitlink
### Prerequisites
- Python 3.11+, Node.js 16+, Slack workspace & bot tokens, Google Service Account, Telegram+Discord accounts & tokens, WhatsApp QR login

### Start Sequence
1. Ensure `.env` is complete & all tokens set
2. Install Python deps: `pip install -r requirements.txt`
3. Install Node deps: `cd wa_gateway && npm install && cd ../whatsapp-bot && npm install`
4. Start WhatsApp gateways:
   - `node wa_gateway/whatsapp_gateway.js &`
   - `node whatsapp-bot/service.js &`
5. Start Python bridges (each in a separate terminal):
   - `python main_whatsapp.py &`
   - `python main_telegram.py &`
   - `python discum_ai_http.py &`
   - `python main_upwork.py &`
6. Start management server:
   - `python management_server.py &`

### Management
- Add clients: `/add-client [platform] "[Name]" [external_id] [slack_channel_id]` (in Slack)
- Configuration changes auto-refresh across bridges via `/refresh` endpoint
- Logs available in Slack channels (INFO & ERROR) and local files

---

## 📡 Features & Roadmap
**Implemented:**
- Two-way sync and deduplication (Slack ↔ WhatsApp, Telegram, Discord, Email/Upwork)
- File/media attachment support
- Per-client Slack channels
- Webhook-driven architecture for extensibility
- Hot-reload config changes (no restart needed)
- /enhance (Gemini AI-powered message rewrite)

**Roadmap:**
- Sentiment analysis, auto-assignment, SLAs and escalations
- Analytics/dashboard for volume & response times
- CRM sync (Airtable, HubSpot)

---

## 📦 Dependencies Summary
**Python:** slack_sdk · telethon · discum · aiohttp · flask · gspread · requests · google-auth · python-dotenv

**Node:** whatsapp-web.js · puppeteer · express · axios · qrcode-terminal · dotenv

---

## 🎓 Developer Insights
- Bridges are completely uncoupled: one platform’s failure won’t take down others
- All persistent state lives in Google Sheets and JSON, not a real DB
- Async code and real-time socket/event handling is used throughout (esp. Telegram and Discord bridges)
- Google Sheets config allows non-devs to update without deployment
- Strict mapping prevents message loops; all IDs cross-referenced for audits

---

## 👩‍💻 Onboarding & Troubleshooting
- Missing env var? Check `.env` is populated
- Bridge not syncing? Check logs in local files/Slack; verify tokens
- Google Sheets issues? Confirm service account access
- WhatsApp/Discord QR/token revoked? Re-authenticate or update .env

**Support:**
- Discord: https://discord.gg/vBu9huKBvy
- Telegram: https://t.me/devpilot1

---

## 📁 At-a-Glance Project Structure
```
bitlink-github/
├─ main_telegram.py        # Telegram bridge
├─ main_whatsapp.py        # WhatsApp bridge
├─ main_upwork.py          # (optional) Email/Upwork bridge
├─ discum_ai_http.py       # Discord bridge
├─ management_server.py    # Slack/admin entrypoint
├─ g_sheets_client.py      # Google Sheets integration
├─ slack_log_handler.py    # Logs → Slack
├─ gemini_enhance.py       # AI message enhancement
├─ wa_gateway/
│     └─ whatsapp_gateway.js  # WhatsApp Node gateway
├─ whatsapp-bot/
│     └─ service.js           # WhatsApp Node bot
├─ requirements.txt/pyproject.toml/.env/
├─ credentials/
│     └─ service_account.json # Google credentials (not in git)
└─ ...
```

---