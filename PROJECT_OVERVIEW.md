# Bitlink — Complete Project Understanding

## 🎯 Project Purpose
**Bitlink** is a multi-platform client communication hub that centralizes conversations from **WhatsApp, Telegram, and Discord** directly into **Slack**, enabling teams to manage all client communications in one place without forcing clients to switch apps.

### Core Problem Solved
- Teams work with clients across multiple messaging platforms (WhatsApp, Telegram, Discord, Email)
- These conversations are scattered and lack visibility
- Clients prefer staying in their native apps
- **Bitlink bridges all platforms into Slack**, creating a single source of truth while respecting client preferences

---

## 📊 Architecture Overview

### Data Flow
```
Client Platforms  →  Bridge Services  →  Google Sheets (Config)  →  Slack Channels
                                      ↓
                           Management Server (Updates)
```

### Key Components

#### 1. **Bridge Services** (Python - Main Connectors)
These services listen to incoming messages from each platform and relay them to Slack:

| Service | File | Purpose |
|---------|------|---------|
| **Telegram Bridge** | `main_telegram.py` (881 lines) | Listens to Telegram messages via TelegramClient, posts to Slack |
| **WhatsApp Bridge** | `main_whatsapp.py` (847 lines) | Listens to WhatsApp messages, handles two-way sync with Slack |
| **Discord Bridge** | `discum_ai_http.py` (387 lines) | Listens to Discord DMs/channels, syncs with Slack |
| **Upwork/Email Bridge** | `main_upwork.py` (202 lines) | Polls Upwork email, posts to Slack, enables email replies |

**Common Bridge Features:**
- Two-way message sync (client platform ↔ Slack)
- Message deduplication to prevent loops
- File/media attachment handling
- Per-client channel mapping (e.g., `#client-acme-corp-dm`)
- Graceful error handling with Slack logging
- Hot-reload support via `/refresh` endpoint

#### 2. **Gateway Services** (Node.js - WhatsApp Only)
WhatsApp requires special handling due to the WhatsApp Cloud API:

| Service | File | Purpose |
|---------|------|---------|
| **WhatsApp Gateway** | `wa_gateway/whatsapp_gateway.js` | Node.js service using `whatsapp-web.js` to authenticate & relay messages |
| **WhatsApp Bot** | `whatsapp-bot/service.js` (560 lines) | Production-ready WhatsApp client with group discovery & notification tracking |

**Why separate?** WhatsApp web automation requires Node.js + Puppeteer; bridges communicate via HTTP webhooks.

#### 3. **Configuration Layer** (Google Sheets)
**File:** `g_sheets_client.py`

Stores a centralized mapping table:
- **Columns:** platform, client_name, external_id, slack_channel_id, paused
- **Accessed by:** All bridges at startup and when config changes
- **Smart matching:** Supports exact platform match OR prefix matching (e.g., "Discord" vs "Discord-Channel")

#### 4. **Management Server**
**File:** `management_server.py` (193 lines)

Flask-based admin server for managing client configurations:
- **Slack Command:** `/add-client [platform] "[Client Name]" [external_id] [slack_channel_id]`
- **Workflow:**
  1. Parses command using `shlex`
  2. Writes new mapping to Google Sheets
  3. Sends `/refresh` signal to all running bridges
  4. Bridges reload config without restarting

#### 5. **Helper Services**

| Service | File | Purpose |
|---------|------|---------|
| **Slack Logger** | `slack_log_handler.py` | Sends INFO/ERROR logs to designated Slack channels for visibility |
| **Message Enhancer** | `gemini_enhance.py` | Uses Google Gemini API to polish client-facing messages (via Slack `/enhance` command) |
| **Slack Webhook** | `enhance_slack_webhook.py` | HTTP endpoint for Slack's `/enhance` slash command |

---

## 🔧 Technology Stack

### Backend
- **Python 3.11+** - Main bridge logic
- **Telethon** - Telegram Bot API client
- **discum** - Discord user account client
- **Flask** - HTTP servers for webhooks & management
- **aiohttp** - Async HTTP for Slack integration
- **slack_sdk** - Official Slack Python SDK with socket mode for real-time events
- **gspread** - Google Sheets API client
- **python-dotenv** - Environment configuration

### Frontend (Minimal)
- None - Slack is the primary UI

### External Services
- **Google Sheets** - Client mapping storage
- **Slack Workspace** - Primary UI & command center
- **Telegram** - Incoming messages
- **WhatsApp** - Incoming messages (web automation)
- **Discord** - Incoming messages
- **Google Gemini API** - Message enhancement

### DevOps
- **Node.js** - WhatsApp gateway (separate process)
- **Puppeteer** - WhatsApp web automation

---

## 📁 Project Structure

```
bitlink-github/
├── Core Bridges (Python)
│   ├── main_telegram.py          # Telegram ↔ Slack bridge
│   ├── main_whatsapp.py          # WhatsApp ↔ Slack bridge (orchestrator)
│   ├── main_upwork.py            # Upwork Email ↔ Slack bridge
│   ├── discum_ai_http.py         # Discord ↔ Slack bridge
│   └── management_server.py      # Admin CLI interface
│
├── Gateway Services (Node.js)
│   ├── wa_gateway/
│   │   ├── whatsapp_gateway.js   # WhatsApp web automation gateway
│   │   └── package.json
│   │
│   └── whatsapp-bot/
│       ├── service.js             # WhatsApp bot service
│       ├── notified_groups.json   # Group notification tracking
│       ├── package.json
│       └── node_modules/
│
├── Configuration & Utilities
│   ├── g_sheets_client.py         # Google Sheets integration
│   ├── slack_log_handler.py       # Slack logging handler
│   ├── gemini_enhance.py          # Message enhancement with Gemini
│   ├── enhance_slack_webhook.py   # Slack /enhance command handler
│   │
│   └── credentials/
│       └── service_account.json   # Google Service Account (git-ignored)
│
├── Configuration Files
│   ├── .env                       # Environment variables
│   ├── pyproject.toml             # Python project metadata
│   ├── requirements.txt           # Python dependencies
│   ├── setup.sh                   # Setup script
│   │
│   └── bitlink_telegram.session   # Telethon session file
│
├── Logs & Temp Data
│   ├── temp_downloads_telegram/   # Temp downloads from Telegram
│   └── attached_assets/           # Uploaded/attached media
│
└── Documentation
    ├── README.md                  # Project overview
    └── Architect.png              # Architecture diagram
```

---

## 🔄 Message Flow (Example: WhatsApp Message)

### Inbound (Client → Slack)
1. **Client sends message on WhatsApp** (WhatsApp-web.js detects)
2. **WhatsApp Gateway (`whatsapp_gateway.js`)** receives message
3. **Gateway posts to management_server** webhook with: sender, text, media
4. **main_whatsapp.py** processes:
   - Looks up `external_id` in Google Sheets
   - Finds mapped Slack channel (e.g., `#client-acme-dm`)
   - Deduplicates using message hash
   - Posts formatted message to Slack
   - Stores message mapping (WA ID ↔ Slack timestamp)

### Outbound (Slack → Client)
1. **Team member posts in `#client-acme-dm`** (or replies in thread)
2. **Slack Socket Mode** alerts `main_whatsapp.py`
3. **main_whatsapp.py** processes:
   - Validates message (not from bot, not thread reply)
   - Looks up original WA sender via message mapping
   - Calls WhatsApp Gateway `/wa/sendText` endpoint
   - Gateway sends message via WhatsApp
   - Confirms to Slack

---

## 🔐 Security & Configuration

### Environment Variables (.env)
```
# Telegram
API_ID=<telegram_app_id>
API_HASH=<telegram_app_hash>
SESSION_NAME=bitlink_telegram
SLACK_BOT_TOKEN_TELEGRAM=xoxb-...
SLACK_APP_TOKEN_TELEGRAM=xapp-...

# WhatsApp
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
NODE_API_URL=http://127.0.0.1:3101
GATEWAY_SECRET=<shared_secret_for_gateway>

# Discord
DISCORD_TOKEN=<user_token>

# Upwork Email
UPWORK_EMAIL_ADDRESS=...
UPWORK_EMAIL_PASSWORD=...
UPWORK_IMAP_SERVER=imap.gmail.com
UPWORK_SMTP_SERVER=smtp.gmail.com

# Google Sheets & Gemini
CREDENTIALS_FILE=credentials/service_account.json
GEMINI_API_KEY=<gemini_api_key>

# Logging
SLACK_INFO_CHANNEL=<channel_id>
SLACK_ERROR_CHANNEL=<channel_id>

# Ports
TELEGRAM_REFRESH_PORT=8003
WHATSAPP_REFRESH_PORT=8101
DISCORD_REFRESH_PORT=8102
```

### Authentication Flow
- **Telegram:** TelegramClient session (API_ID + API_HASH)
- **WhatsApp:** Puppeteer + WhatsApp-web.js with localStorage persistence
- **Discord:** User token (token-based, not OAuth)
- **Slack:** Bot token (xoxb-) + App token (xapp-) for Socket Mode
- **Google Sheets:** Service account JSON with OAuth 2.0

### Security Best Practices
✅ Webhook signature verification (Slack)  
✅ Shared secrets for inter-service communication  
✅ Per-tenant isolation via Slack channel mapping  
✅ Role-based access (each user sees only their client channels)  
✅ Message deduplication to prevent replay attacks  
✅ Error handling without exposing sensitive data  

---

## 🚀 Running the Project

### Prerequisites
- Python 3.11+
- Node.js 16+
- Slack workspace with bot permissions
- Google Service Account (for Sheets access)
- Telegram account + API credentials
- Discord user account + token
- WhatsApp account (for web automation)

### Startup Sequence
```bash
# 1. Configure environment (.env file)
# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install Node.js dependencies
cd wa_gateway && npm install
cd ../whatsapp-bot && npm install
cd ..

# 4. Start WhatsApp gateway (Node.js)
node wa_gateway/whatsapp_gateway.js &
node whatsapp-bot/service.js &

# 5. Start Python bridges in parallel
python main_whatsapp.py &
python main_telegram.py &
python discum_ai_http.py &
python main_upwork.py &

# 6. Start management server
python management_server.py &
```

### Health Checks
- **Telegram Bridge:** Listens on port 8003 `/refresh` endpoint
- **WhatsApp Bridge:** Listens on port 8101 `/refresh` endpoint
- **Discord Bridge:** Listens on port 8102 `/refresh` endpoint
- **Management Server:** Flask server (port configured in code)
- **WhatsApp Gateway:** Express server on port 3001

---

## 🎮 Key Features

### ✅ Implemented
- [x] Two-way message sync (all platforms)
- [x] File & media attachment handling
- [x] Message deduplication
- [x] Per-client channel mapping
- [x] Hot-reload configuration (no service restart needed)
- [x] Slack logging for errors & info
- [x] Message enhancement with Gemini
- [x] WhatsApp group discovery & notifications
- [x] Email bridge (Upwork)

### 🔄 Roadmap
- [ ] Sentiment analysis & auto-tagging
- [ ] SLA timers & escalations
- [ ] Command palette (`/bitlink assign @alice`)
- [ ] Insights dashboard (response times, volume)
- [ ] Airtable/HubSpot CRM sync

---

## 📊 Database Layer
**No traditional database!** Uses:
- **Google Sheets** for client mappings (source of truth)
- **JSON files** for state (e.g., `notified_groups.json`, message maps)
- **Slack** as the message archive (searchable, with metadata)

### Why Google Sheets?
- Non-technical users can edit mappings
- Changes auto-sync to all bridges (no manual DB migration)
- Accessible from anywhere (no VPN needed)
- Free tier covers typical usage

---

## 🧪 Testing & Debugging

### Logging
- **File logs:** `main_whatsapp3.log`, `enhance_slack_webhook.log`
- **Slack logs:** Sent to `SLACK_INFO_CHANNEL` (INFO level) and `SLACK_ERROR_CHANNEL` (ERROR level)
- **Console logs:** Standard Python logging output

### Message Tracing
- Telegram: Message ID stored in map
- WhatsApp: Message ID + timestamp
- Discord: Message ID
- Slack: Message timestamp (ts)
- Cross-references enable full audit trail

### Common Issues
1. **Missing environment variables:** Check `.env` file completeness
2. **Google Sheets access denied:** Ensure service_account.json has correct permissions
3. **Slack token invalid:** Verify token has required scopes (chat:write, channels:read, etc.)
4. **WhatsApp not connecting:** Scan QR code in `whatsapp-bot/service.js` output
5. **Discord not receiving:** Check user token isn't revoked, user is in correct guild

---

## 📈 Metrics & Analytics (Future)
Currently tracked:
- Message count per client/platform
- Response times (Slack message → delivery to client)
- Platform uptime/errors
- Configuration change history (via Google Sheets)

---

## 🎓 Key Learnings for Developers

1. **Multi-Platform Complexity:** Each platform has unique authentication, rate limits, and API quirks
2. **Async/Await Pattern:** Telegram & Discord use async extensively (Telethon, discum)
3. **Slack Socket Mode:** Required for real-time inbound events (vs polling)
4. **State Management:** Message maps (ID deduplication) are critical to prevent loops
5. **Service Isolation:** Bridges are independent; one failure doesn't affect others
6. **Configuration as Code:** Google Sheets as config enables non-technical management

---

## 🔗 Dependencies Summary

### Python Packages
| Package | Version | Purpose |
|---------|---------|---------|
| slack_sdk | Latest | Slack Bot API, Socket Mode |
| telethon | Latest | Telegram Bot API |
| discum | Latest | Discord user client |
| aiohttp | Latest | Async HTTP client |
| flask | Latest | HTTP server |
| gspread | 5.7.2 | Google Sheets API |
| requests | 2.28.2 | HTTP client |
| google-auth | 2.3.3 | Google OAuth |
| python-dotenv | 0.21.1 | .env file support |

### Node.js Packages
| Package | Purpose |
|---------|---------|
| whatsapp-web.js | WhatsApp web automation |
| express | HTTP server |
| puppeteer | Browser automation |
| axios | HTTP client |
| qrcode-terminal | QR code display |
| dotenv | .env support |

---

## 📞 Support & Contacts
- **Discord:** https://discord.gg/vBu9huKBvy
- **Telegram:** https://t.me/devpilot1

---

**Last Updated:** January 17, 2026  
**Project Status:** Production-Ready  
**Maintainer:** Bitbash Team
