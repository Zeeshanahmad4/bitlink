# BitLink — Production Deployment Guide (Docker + DuckDNS + HTTPS)

Complete instructions for deploying BitLink on a production Linux server using **Docker Compose**,
with the dashboard exposed publicly over HTTPS via **DuckDNS** (free domain) and **Let's Encrypt** (free SSL).

> **Your server IP is static**, so there is no need for a dynamic DNS updater cron job.

---

## Architecture

```
 [Browser / Team]
       │ HTTPS :443
       ▼
 [DuckDNS Domain]  ──resolves-to──▶  [Linux Server Public IP]
                                              │
                               ┌──────────────▼──────────────┐
                               │   Nginx  (host, port 80/443) │
                               │   + Let's Encrypt SSL cert   │
                               └──────────────┬──────────────┘
                                              │ reverse-proxy
                                              ▼ 127.0.0.1:5050
                   ╔══════════════════════════════════════════════╗
                   ║           Docker bridge: bitlink-net          ║
                   ║                                              ║
                   ║  ┌─────────────────┐  ┌──────────────────┐  ║
                   ║  │  dashboard:5050  │  │  gateway:3101    │  ║
                   ║  │  (Flask/Python)  │  │  (Node Baileys)  │  ║
                   ║  └────────┬─────────┘  └────────┬─────────┘  ║
                   ║           │                     │            ║
                   ║  ┌────────▼─────────────────────▼─────────┐  ║
                   ║  │              db:5432 (PostgreSQL 15)    │  ║
                   ║  └─────────────────────────────────────── ┘  ║
                   ║                                              ║
                   ║  wa-bridge  │  tg-bridge  │  slack-manager   ║
                   ║  (Python)   │  (Python)   │  (Python)        ║
                   ╚══════════════════════════════════════════════╝
                         All containers on the same bridge.
                         Only port 5050 is published to host
                         (bound to 127.0.0.1 — Nginx only).
```

---

## Table of Contents

| § | Section |
|---|---------|
| 1 | [What BitLink runs](#1-what-bitlink-runs) |
| 2 | [Prerequisites](#2-prerequisites) |
| 3 | [Set up DuckDNS free domain](#3-set-up-duckdns-free-domain) |
| 4 | [Install Docker, Nginx, and Certbot on the server](#4-install-docker-nginx-and-certbot-on-the-server) |
| 5 | [Deploy code to server (rsync or Git)](#5-deploy-code-to-server-rsync-or-git) |
| 6 | [Configure environment variables](#6-configure-environment-variables) |
| 7 | [Build and start all containers](#7-build-and-start-all-containers) |
| 8 | [First-time logins (WhatsApp QR + Telegram OTP)](#8-first-time-logins-whatsapp-qr--telegram-otp) |
| 9 | [Create the dashboard admin user](#9-create-the-dashboard-admin-user) |
| 10 | [Configure Nginx and get free HTTPS](#10-configure-nginx-and-get-free-https) |
| 11 | [Progressive Web App (PWA) deployment & caching](#11-progressive-web-app-pwa-deployment--caching) |
| 12 | [Seed the Slack apps](#12-seed-the-slack-apps) |
| 13 | [First-run verification](#13-first-run-verification) |
| 14 | [Operational commands](#14-operational-commands) |
| 15 | [Upgrading a running server](#15-upgrading-a-running-server) |
| 16 | [Optional — migrating existing Google Sheets data](#16-optional--migrating-existing-google-sheets-data) |
| 17 | [Troubleshooting](#17-troubleshooting) |
| 18 | [Security notes](#18-security-notes) |

---

## 1. What BitLink Runs

Six containers managed by Docker Compose. PostgreSQL is also a container — it is not installed on the host.

| Container | Role | Internal Port |
|-----------|------|---------------|
| `db` | PostgreSQL 15 — sole CRM data store | `5432` |
| `gateway` | Node.js WhatsApp Baileys gateway | `3101` |
| `dashboard` | Flask web UI + REST API | `5050` → published to `127.0.0.1:5050` |
| `wa-bridge` | Python WhatsApp ↔ Slack relay + **daily digest + Sunday auto-reply** | — |
| `tg-bridge` | Python Telegram ↔ Slack relay | — |
| `slack-manager` | Python Slack slash-commands + modals | — |
| `ai-jobs` | Python scheduled jobs — **Sunday AI stage inference + daily auto-archive** | — |

Two services are **not** run as containers and are run on-demand:

- **`main_upwork.py`** — optional Upwork mail poller, only if you use it.

---

## 2. Prerequisites

| Item | Notes |
|------|-------|
| Linux server (Ubuntu 20.04+ / Debian 11+) | Static public IP assumed |
| WhatsApp number | For the Baileys gateway |
| Telegram account | For the Telethon bridge |
| Three Slack apps | See [§12](#12-seed-the-slack-apps) |
| DuckDNS account | Free at [duckdns.org](https://www.duckdns.org) |
| SSH access to the server | With `sudo` privileges |

---

## 3. Set Up DuckDNS Free Domain

> **🙋 Manual step — requires your browser.**

### 3.1 Create a subdomain

1. Go to [duckdns.org](https://www.duckdns.org) and sign in (Google/GitHub/etc.).
2. In the **domains** box, type a name (e.g. `mycompany-crm`) and click **add domain**.
3. In the **current ip** column, enter your server's **public IP address** and click **update ip**.
4. Your full domain is now `mycompany-crm.duckdns.org`.
5. Note your **token** shown at the top of the page — you will need it for Certbot.

> Because your server IP is static, there is no need to run a cron job to update DuckDNS. Just set the IP once.

### 3.2 Verify DNS propagation

From any machine (or use [dnschecker.org](https://dnschecker.org)):

```bash
nslookup mycompany-crm.duckdns.org
```

It should resolve to your server's public IP. This must work before you can get an SSL certificate in [§10](#10-configure-nginx-and-get-free-https).

### 3.3 Open firewall ports

**Cloud VPS (AWS / DigitalOcean / Hetzner / etc.):** Open ports **80** and **443** in your cloud security group / firewall rules panel.

**Home server / bare metal:** Forward external ports **80** and **443** to your server's LAN IP in your router's port-forwarding settings.

Then on the server itself:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow OpenSSH    # keep SSH open!
sudo ufw enable
sudo ufw status
```

---

## 4. Install Docker, Nginx, and Certbot on the Server

> **🙋 Manual step — run these commands on your Linux server over SSH.**

```bash
sudo apt update && sudo apt upgrade -y

# Docker Engine
sudo apt install -y ca-certificates curl gnupg lsb-release
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Allow your user to run Docker without sudo
sudo usermod -aG docker $USER
newgrp docker            # activate group in current shell without logging out

# Nginx and Certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# Verify
docker --version          # e.g. Docker version 26.x.x
docker compose version    # e.g. Docker Compose version v2.x.x
nginx -v
```

> [!NOTE]
> **PostgreSQL does NOT need to be installed on the host.**
> PostgreSQL runs in an isolated Docker container (`db: postgres:15-alpine`). Docker automatically creates the database, manages persistence, and runs `schema.sql` on first boot. You do not need to install `postgresql` via `apt`. (If you explicitly prefer running PostgreSQL on the host OS instead of Docker, see [§17.7](#177-running-postgresql-directly-on-the-host-instead-of-docker)).

---

## 5. Deploy Code to Server (rsync or Git)

> **⚠️ CRITICAL: Folder Isolation from Existing BitLink (`bbitlink`)**
> The server already has an existing instance running in a folder named `bbitlink` (or `bitlink`).
> To guarantee that the existing instance is **completely untouched and uninterrupted**, this new Sales CRM project is deployed into an isolated folder:
> **`/opt/bitlink-sales`**
>
> All containers, environment variables, databases, and volumes for this deployment live strictly inside `/opt/bitlink-sales`.

Choose whichever method fits your current access permissions:
- **Method A: Direct Deploy via `rsync` (Recommended)** — Best when you cannot push to the main GitHub repository (e.g. CEO is away or waiting on review). Transfers code directly from your Mac to `/opt/bitlink-sales` in seconds.
- **Method B: Git Clone from GitHub** — Standard deployment if your branch is already pushed to GitHub.

---

### Method A: Direct Deploy via `rsync` (Fastest & No GitHub Push Required)

#### Step 1 — Create the isolated directory on the Linux server:
Run once over SSH on the server:
```bash
sudo mkdir -p /opt/bitlink-sales
sudo chown -R $USER:$USER /opt/bitlink-sales
```

#### Step 2 — Run `rsync` from your local Mac terminal:
From inside your local project folder (`/Users/apple/Desktop/BitLink-Sales/bitlink`), run:
```bash
rsync -avz --progress \
  --exclude='.git' \
  --exclude='venv' \
  --exclude='.venv' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='whatsapp-bot/node_modules' \
  --exclude='whatsapp-bot/auth_info' \
  --exclude='sessions' \
  --exclude='.env' \
  --exclude='*.session' \
  --exclude='whatsapp_outbox.jsonl' \
  --exclude='telegram_outbox.jsonl' \
  ./ root@<YOUR_SERVER_IP>:/opt/bitlink-sales/
```

#### Step 3 — Automated 1-Click Updates (`deploy.sh`):
To easily deploy future code updates without typing commands repeatedly, create a script named `deploy.sh` on your local Mac:

```bash
#!/bin/bash
SERVER="root@<YOUR_SERVER_IP>"
TARGET_DIR="/opt/bitlink-sales"

echo "🚀 Syncing code changes to $SERVER:$TARGET_DIR..."
rsync -avz \
  --exclude='.git' \
  --exclude='venv' \
  --exclude='.venv' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='whatsapp-bot/node_modules' \
  --exclude='whatsapp-bot/auth_info' \
  --exclude='sessions' \
  --exclude='.env' \
  --exclude='*.session' \
  ./ "$SERVER:$TARGET_DIR/"

echo "🔄 Rebuilding & restarting Docker containers on server..."
ssh "$SERVER" "cd $TARGET_DIR && docker compose up -d --build"

echo "✅ Deployed successfully!"
```

Make it executable:
```bash
chmod +x deploy.sh
```
Now anytime you make changes locally on your Mac, simply run `./deploy.sh`.

---

### Method B: Clone from GitHub (Standard Git Flow)

If you have already pushed your branch to GitHub:

```bash
# Create directory and assign ownership
sudo mkdir -p /opt/bitlink-sales
sudo chown -R $USER:$USER /opt/bitlink-sales

# Clone the repository into the isolated folder
git clone -b feature/sales-crm-dashboard https://github.com/YOUR_USERNAME/YOUR_REPO.git /opt/bitlink-sales

cd /opt/bitlink-sales
```

> **Tip for Private Repositories:** If your GitHub repo is private, either:
> 1. Use a **Personal Access Token (PAT)** when prompted for the password, or
> 2. Generate an SSH key on the server (`ssh-keygen -t ed25519`) and add the public key as a **Deploy Key** in your GitHub repo settings (**Settings -> Deploy keys**).

### 5.3 Essential `.gitignore` Checklist (What Stays Local/Server-Only)

When cloning BitLink onto your server, the following files and directories are automatically ignored by Git. They contain local secrets, active logins, database backups, logs, or platform dependencies. They are generated dynamically on the server and **MUST NEVER** be committed to GitHub:

| Category | Ignored Paths in `.gitignore` | Purpose & Server Handling |
|----------|--------------------------------|---------------------------|
| **Secrets & Env Variables** | `.env`, `.env.local`, `.env.*.local`, `config.py` | Contains database passwords, Slack tokens, Gemini API keys, Telegram API Hash. `.env.example` is committed as a template. On server: `cp .env.example .env` and populate. |
| **WhatsApp Auth Session** | `whatsapp-bot/auth_info/`, `whatsapp-bot/.wwebjs_auth/`, `whatsapp-bot/sessions/` | Stores active WhatsApp QR pairing keys & auth state. Automatically created on server when scanning QR. |
| **Telegram Auth Session** | `sessions/`, `*.session`, `telegram_message_map.json` | Stores Telethon session keys and phone authentication. Automatically generated when running interactive login. |
| **Google Cloud Credentials** | `credentials/`, `service_account.json` | Google Cloud service account JSON private keys (if using Google Sheets migration). Copy manually if needed. |
| **Database Backups & Dumps** | `backups/`, `*.sql` (except `!schema.sql`) | Contains live SQL data dumps, client records, and password hashes. `schema.sql` (table structure) IS committed. |
| **Log Files & Queue Outboxes** | `logs/`, `*.log`, `slack_events.log`, `whatsapp_outbox.jsonl`, `telegram_outbox.jsonl`, `*.jsonl` | Real-time message logs, webhook events, and retry queues. Created automatically by running services. |
| **Dependencies & Virtual Envs** | `node_modules/`, `whatsapp-bot/node_modules`, `__pycache__/`, `venv/`, `.venv/` | Node/Python packages and bytecode. Installed inside Docker containers (`Dockerfile.node` & `Dockerfile.python`). |
| **OS / Editor Files** | `.DS_Store`, `Thumbs.db`, `temp_downloads_telegram/` | Temporary operating system files and downloaded Telegram media buffers. |

---

## 6. Configure Environment Variables

> **🙋 Manual step — you must fill in your own API tokens and passwords.**

On the server, copy the template `.env.example` to `.env` and fill in your actual credentials:

```bash
cd /opt/bitlink-sales
cp .env.example .env
nano .env
```

### 6.1 Required — app will not start without these

```ini
# ---- Database password (must match DB_PASSWORD below) ----
DB_PASSWORD=use_a_strong_password_here

# ---- Database URL (uses the Docker service name "db") ----
DATABASE_URL=postgresql://bitlink:use_a_strong_password_here@db:5432/bitlink
DB_POOL_MAX=30
DB_POOL_TIMEOUT=10

# ---- Slack app 1: WhatsApp bridge ----
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# ---- Slack app 2: Telegram bridge ----
SLACK_BOT_TOKEN_TELEGRAM=xoxb-...
SLACK_APP_TOKEN_TELEGRAM=xapp-...

# ---- Slack app 3: manager (slash commands) ----
MANAGER_SLACK_BOT_TOKEN=xoxb-...
MANAGER_SLACK_APP_TOKEN=xapp-...

# ---- Telegram ----
API_ID=...
API_HASH=...

# ---- WhatsApp: Slack user ID of the bot account ----
# Find it in Slack: click your profile -> ID at the bottom
MASTER_SLACK_BOT_ID=U0XXXXXXXX
```

### 6.2 Required for the lead pipeline

```ini
SLACK_ADMIN_CHANNEL_ID=C0XXXXXXXX
```

This is the Slack channel that receives kill-switch alerts. If unset, blocked leads are only logged and no alert is sent.

### 6.3 Required for AI features

```ini
GEMINI_API_KEY=...
```

Without this, AI reply drafting and win/loss insights raise `GEMINI_API_KEY is not configured`. Everything else keeps working.

### 6.4 Recommended service configuration

These override Docker Compose defaults and should match the values in `docker-compose.yml`:

```ini
NODE_API_URL=http://gateway:3101
WHATSAPP_REFRESH_PORT=8101
TELEGRAM_REFRESH_PORT=8003
DISCORD_REFRESH_PORT=8102
CONTROL_DASHBOARD_PORT=5050
SESSIONS_DIR=sessions
MAX_FILE_SIZE_MB=50
```

> **Important:** `NODE_API_URL` must use the Docker service hostname `gateway`, not `127.0.0.1`. Inside the Docker network, containers reach each other by service name.

### 6.5 Needed only for delivery handover

```ini
PROJECT_MANAGER_SLACK_ID=U0XXXXXXXX
```

Fallback only. The handover flow uses the `project_managers` table; this variable is used when that table is empty.

### 6.6 Optional integrations

```ini
# Upwork mail bridge (main_upwork.py)
UPWORK_EMAIL_ADDRESS=...
UPWORK_EMAIL_PASSWORD=...
UPWORK_IMAP_SERVER=imap.gmail.com
UPWORK_SMTP_SERVER=smtp.gmail.com
```

### 6.7 Lock down the file

```bash
chmod 600 /opt/bitlink-sales/.env
```

### 6.8 Runtime file paths

All paths are relative to the container's `/app` working directory. The Docker Compose file mounts `sessions/` as a volume so they persist across rebuilds.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUTBOX_PATH` | `whatsapp_outbox.jsonl` | Undelivered WA messages, retried |
| `OUTBOX_FLUSH_INTERVAL` | `60` | Seconds between outbox flushes |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Give up after this many failures |
| `SESSION_NAME` | `bitlink_telegram` | Telethon session basename |
| `SESSIONS_DIR` | `sessions` | Telegram session directory (mounted volume) |

### 6.9 Dual Credential Workflow: Personal Credentials → Company Credentials

If you want to deploy initially using **your own personal credentials** (for staging/testing) and later switch to **official company credentials** (for production deployment), follow this 2-phase process:

#### Phase A — Staging / Testing Deployment (Personal Credentials)
1. **Configure `.env`**: Fill `.env` with your personal/test Slack app bot tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`), personal Gemini API key, and test Slack channel IDs (`SLACK_ADMIN_CHANNEL_ID`).
2. **Scan Personal WhatsApp**: Run `docker compose logs -f gateway` and scan the QR code with your personal phone. Session files save to `./whatsapp-bot/auth_info/`.
3. **Log in Personal Telegram**: Run `docker compose run --rm tg-bridge python main_telegram.py` and log in with your personal phone number and OTP code. Session files save to `./sessions/bitlink_telegram.session`.
4. **Test Roster**: In the dashboard (**Member Pool**), add your personal Slack user ID to test lead distribution.

#### Phase B — Migration to Official Company Production Credentials

When testing is complete and you are ready to switch to company credentials:

##### Step 1 — Update `.env` with Company Credentials
```bash
nano /opt/bitlink-sales/.env
```
Replace personal credentials with official company values:
- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` (Company WhatsApp bridge app)
- `SLACK_BOT_TOKEN_TELEGRAM`, `SLACK_APP_TOKEN_TELEGRAM` (Company Telegram bridge app)
- `MANAGER_SLACK_BOT_TOKEN`, `MANAGER_SLACK_APP_TOKEN` (Company Manager app)
- `SLACK_ADMIN_CHANNEL_ID` (Company Slack admin channel)
- `MASTER_SLACK_BOT_ID` (Company Slack bot ID)
- `GEMINI_API_KEY` (Company Gemini API key)
- `DB_PASSWORD` & `DATABASE_URL` (Company database password)

##### Step 2 — Switch WhatsApp Session to Company Phone
Unlink personal WhatsApp and link the official company WhatsApp phone:
```bash
# Delete personal session state
rm -rf /opt/bitlink-sales/whatsapp-bot/auth_info/*

# Restart gateway to generate new QR code
docker compose restart gateway

# View logs and scan QR code with the official Company WhatsApp phone
docker compose logs -f gateway
```

##### Step 3 — Switch Telegram Session to Company Account
Unlink personal Telegram and link the official company Telegram phone:
```bash
# Stop Telegram bridge container
docker compose stop tg-bridge

# Remove personal session file
rm -f /opt/bitlink-sales/sessions/bitlink_telegram.session

# Interactive login using Company Telegram phone number & OTP
docker compose run --rm tg-bridge python main_telegram.py

# Start Telegram bridge in background
docker compose up -d tg-bridge
```

##### Step 4 — Update Roster & Clear Staging Test Data
```bash
# Optional: Clear staging test leads and mappings
docker compose exec db psql -U bitlink -d bitlink -c \
  "TRUNCATE sales_leads, client_mappings, critical_alerts, alert_details;"

# Clear test sales pool and reset project managers table
docker compose exec db psql -U bitlink -d bitlink -c "TRUNCATE sales_pool; TRUNCATE project_managers;"
```
Then add official company sales reps in the dashboard (**Member Pool → Add Sales Rep**).

##### Step 5 — Force Restart All Services
```bash
docker compose down
docker compose up -d
```

---

## 7. Build and Start All Containers

> **🙋 Manual step — run on your server.**

```bash
cd /opt/bitlink-sales

# Build all images (takes a few minutes on first run)
docker compose build

# Start everything in the background
docker compose up -d

# Confirm all 7 containers are running
docker compose ps
```

Expected output from `docker compose ps` — all containers should show `Up`:

```
NAME                    STATUS          PORTS
bitlink-db-1            Up (healthy)    5432/tcp
bitlink-gateway-1       Up              3101/tcp
bitlink-dashboard-1     Up              127.0.0.1:5050->5050/tcp
bitlink-wa-bridge-1     Up
bitlink-tg-bridge-1     Up
bitlink-slack-manager-1 Up
bitlink-ai-jobs-1       Up
```

### 7.1 Verify Database & Apply Schema / Migrations

The `db` container automatically creates the `bitlink` database on its very first start and runs `/docker-entrypoint-initdb.d/01_schema.sql` (which is mapped to `./schema.sql`).

Follow these steps to confirm everything initialized properly:

#### Step 1 — Check database connectivity
```bash
docker compose exec db pg_isready -U bitlink -d bitlink
```
Expected output:
```
bitlink:5432 - accepting connections
```

#### Step 2 — Verify all 13 CRM tables exist
```bash
docker compose exec db psql -U bitlink -d bitlink -c "\dt"
```
You should see all 13 tables listed in the database:
- `client_mappings`
- `sales_leads`
- `sales_pool`
- `blocklist`
- `project_managers`
- `snippets`
- `settings`
- `dashboard_users`
- `developers`
- `critical_alerts`
- `alert_details`
- `alert_channels`
- `developer_channels`

#### Step 3 — Manually apply or re-run schema / migrations
If any table is missing, or if you update `schema.sql` in the future with new columns or tables, apply the migration by piping `schema.sql` directly into `psql`:

```bash
docker compose exec -T db psql -U bitlink -d bitlink < schema.sql
```

> **Why this is completely safe:**
> Every query in `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. You can run this command at any time — it will apply new tables/indexes without touching or deleting your existing records.

---

## 8. First-Time Logins (WhatsApp QR + Telegram OTP)

> **🙋 Manual step — requires your phone.**

Both platform bridges require an interactive one-time login. After the first login, credentials are saved to mounted volumes and no re-authentication is needed on restart.

### 8.1 WhatsApp QR Scan

```bash
docker compose logs -f gateway
```

You will see output like:

```
📱 QR Code received — scan with WhatsApp on your phone:
[  QR code printed in terminal  ]
```

On your phone: open **WhatsApp → Linked Devices → Link a Device** → scan the QR code.

Once linked, the log shows `✅ Connection opened` and the QR disappears. The session is saved to `./whatsapp-bot/auth_info/` and persists across container restarts.

> If the QR expires before you scan it, restart the gateway container to get a fresh one:
> ```bash
> docker compose restart gateway
> ```

### 8.2 Telegram Phone Login

```bash
docker compose logs -f tg-bridge
```

On the **very first run**, Telethon prompts for your phone number and a one-time code:

```
Please enter your phone number (or bot token): +1234567890
Please enter the code you received: 12345
```

Because the container is in the background, you must send these inputs interactively:

```bash
docker compose stop tg-bridge
docker compose run --rm tg-bridge python main_telegram.py
```

Complete the login (phone number → code → optional 2FA password). The session is written to `./sessions/bitlink_telegram.session`.

Then start it normally again:

```bash
docker compose up -d tg-bridge
```

From this point forward, the session is reused automatically — no re-login needed on restart.

---

## 9. Create the Dashboard Admin User

> **🙋 Manual step — choose your own password.**

The dashboard has no default account. The `dashboard_users` table starts empty; nobody can log in until you insert a row. Passwords are stored as bcrypt hashes — plaintext is never accepted.

### Step 1 — Generate a bcrypt hash

```bash
docker compose run --rm dashboard python -c \
  "import bcrypt; print(bcrypt.hashpw(b'YOUR_CHOSEN_PASSWORD', bcrypt.gensalt(12)).decode())"
```

Copy the output. It looks like: `$2b$12$abc123...`

### Step 2 — Insert the user into the database

```bash
docker compose exec db psql -U bitlink -d bitlink -c \
  "INSERT INTO dashboard_users (id, password_hash)
   VALUES ('admin', '\$2b\$12\$PASTE_YOUR_HASH_HERE')
   ON CONFLICT (id) DO UPDATE SET password_hash = EXCLUDED.password_hash, updated_at = NOW();"
```

> **Note:** Escape the `$` characters as `\$` when running inside a shell command like above. If the hash contains many `$` signs, paste it into a `.sql` file and run `psql -f file.sql` instead.

### Step 3 — Verify

```bash
docker compose exec db psql -U bitlink -d bitlink -c \
  "SELECT id, updated_at FROM dashboard_users;"
```

Should return one row with `id = admin`.

- To add more users: repeat Step 1–2 with a different `id`.
- To reset a forgotten password: re-run Step 1–2 with the same `id`.

---

## 10. Configure Nginx and Get Free HTTPS

> **🙋 Partially manual — requires your DuckDNS domain to already resolve (§3.2).**

### 10.1 Create the Nginx site configuration

```bash
sudo nano /etc/nginx/sites-available/bitlink.conf
```

Paste the following, replacing `mycompany-crm.duckdns.org` with your actual DuckDNS domain:

```nginx
server {
    listen 80;
    server_name mycompany-crm.duckdns.org;

    # Redirect all HTTP traffic to HTTPS (Required for PWA & Service Workers)
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name mycompany-crm.duckdns.org;

    # SSL Certificate Paths (Managed automatically by Certbot / Let's Encrypt)
    # ssl_certificate /etc/letsencrypt/live/mycompany-crm.duckdns.org/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/mycompany-crm.duckdns.org/privkey.pem;

    # PWA Service Worker (Never cache sw.js, set Service-Worker-Allowed header)
    location /sw.js {
        proxy_pass http://127.0.0.1:5051/sw.js;
        proxy_set_header Host $host;
        add_header Cache-Control "no-cache";
        add_header Service-Worker-Allowed "/";
    }

    # PWA Web Application Manifest
    location /manifest.json {
        proxy_pass http://127.0.0.1:5051/manifest.json;
        proxy_set_header Host $host;
        add_header Cache-Control "public, max-age=3600";
    }

    # Main Dashboard Proxy
    location / {
        proxy_pass          http://127.0.0.1:5051;
        proxy_http_version  1.1;

        proxy_set_header    Host              $host;
        proxy_set_header    X-Real-IP         $remote_addr;
        proxy_set_header    X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;
        proxy_set_header    Upgrade           $http_upgrade;
        proxy_set_header    Connection        "upgrade";

        proxy_read_timeout    86400s;
        proxy_connect_timeout  10s;
        client_max_body_size  700M;
    }
}
```

Enable the site and reload Nginx:

```bash
# Enable site and remove the default placeholder
sudo ln -sf /etc/nginx/sites-available/bitlink.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

Verify basic HTTP is working before getting SSL:

```bash
curl -I http://mycompany-crm.duckdns.org
# Should return HTTP/1.1 200 OK (proxied from the dashboard container)
```

### 10.2 Get a free SSL certificate (Let's Encrypt)

```bash
sudo certbot --nginx -d mycompany-crm.duckdns.org
```

Certbot will:
1. Ask for an email address (for renewal reminders).
2. Ask you to agree to the terms.
3. Automatically verify your domain via HTTP challenge.
4. Issue the certificate and **automatically update** your Nginx config to redirect HTTP → HTTPS.

Certificates are **valid for 90 days** and auto-renewed by a system timer:

```bash
# Verify auto-renewal is configured
sudo systemctl status certbot.timer
sudo certbot renew --dry-run    # test renewal without actually renewing
```

### 10.3 Block direct port access

Now that Nginx handles HTTPS, block direct access to the dashboard port:

```bash
sudo ufw deny 5050/tcp
sudo ufw status
```

### 10.4 Access the dashboard

Open your browser and navigate to:

```
https://mycompany-crm.duckdns.org
```

Log in with `admin` and the password you set in [§9](#9-create-the-dashboard-admin-user).

---

## 11. Progressive Web App (PWA) Deployment & Caching

BitLink is built as a **Progressive Web App (PWA)**, allowing users to install the Control Dashboard as a native-like application on Desktop (Windows, macOS, Linux) and Mobile (iOS, Android). It features shell pre-caching, offline page fallbacks, and API error resilience.

### 11.1 PWA Architecture & Assets

| Component | Endpoint | Source File | Purpose |
|-----------|----------|-------------|---------|
| **Web App Manifest** | `/manifest.json` | `static/manifest.json` | Defines app name ("BitLink Control Dashboard"), standalone display mode, orientation, app icons, theme colors, and quick launcher shortcuts (`/?view=leads`, `/?view=dev_alerts`). |
| **Service Worker** | `/sw.js` | `static/sw.js` | Intercepts network requests to serve pre-cached shell assets, stale-while-revalidate static assets, and offline fallback responses. |
| **Offline Fallback UI** | `/offline.html` | `templates/offline.html` | Rendered automatically when a network connection is unavailable during page navigation. |
| **App Icons & Touch Assets** | `/static/icons/` | `static/icons/` | High-resolution iconography including 192x192, 512x512, maskable 512x512 PNGs, and 180x180 `apple-touch-icon.png`. |

### 11.2 HTTPS Requirement for Service Workers

> [!IMPORTANT]
> **Service Workers strictly require HTTPS.**
> Web browsers block Service Worker registration and PWA installation prompts on unencrypted HTTP connections (exempt only for `localhost`). Ensure HTTPS is active via Let's Encrypt (§10) before testing PWA features in production.

### 11.3 Service Worker (`sw.js`) Caching Strategy

BitLink uses a tailored multi-strategy caching engine in `sw.js`:

1. **Pre-caching Shell (`install` event)**:
   Pre-loads `/`, `/offline.html`, `/manifest.json`, and core branding icons on initial service worker installation (`skipWaiting()` activated).
2. **API Requests (`/api/*`) — Network-First**:
   Always attempts to fetch live data from the backend server first. If the device is offline, returns a clean JSON error response:
   ```json
   {
     "success": false,
     "offline": true,
     "error": "You are currently offline. Please check your internet connection."
   }
   ```
3. **HTML Navigation — Network-First with Offline Fallback**:
   Attempts network navigation and updates cached page copies. If offline and uncached, falls back to `/offline.html`.
4. **Static Assets (CSS, JS, Fonts, Images) — Stale-While-Revalidate**:
   Serves the cached copy immediately for sub-millisecond loading, while silently fetching updated assets in the background for future sessions.

### 11.4 Nginx Configuration & Header Rules

To ensure browsers check for Service Worker updates on every page load rather than caching old worker scripts, Nginx must apply `Cache-Control: "no-cache"` and `Service-Worker-Allowed: "/"`:

```nginx
# Service Worker endpoint (served from control_server.py)
location /sw.js {
    proxy_pass http://127.0.0.1:5050/sw.js;
    proxy_set_header Host $host;
    add_header Cache-Control "no-cache";
    add_header Service-Worker-Allowed "/";
}

# Web App Manifest
location /manifest.json {
    proxy_pass http://127.0.0.1:5050/manifest.json;
    proxy_set_header Host $host;
    add_header Cache-Control "public, max-age=3600";
}
```

### 11.5 Installing BitLink as a Desktop or Mobile App

#### Desktop (Chrome / Edge / Brave / macOS / Windows / Linux)
1. Open `https://mycompany-crm.duckdns.org` in Chrome or Edge.
2. Click the **Install BitLink** icon located on the right side of the address bar (or select **Browser Menu → Install BitLink...**).
3. BitLink runs in a standalone window with native taskbar/dock integration.

#### iOS / iPadOS (Safari)
1. Open `https://mycompany-crm.duckdns.org` in Safari.
2. Tap the **Share** button in the navigation bar.
3. Scroll down and select **Add to Home Screen**.
4. Tap **Add** in the top right.

#### Android (Chrome)
1. Open `https://mycompany-crm.duckdns.org` in Chrome.
2. Tap the three-dot menu icon in the upper right corner.
3. Select **Add to Home screen** or **Install app**.

### 11.6 Updating PWA Code & Cache Busting

When releasing new versions of the dashboard UI:

1. **Increment `CACHE_NAME` in `static/sw.js`**:
   ```javascript
   const CACHE_NAME = 'bitlink-v2'; // Bump version when updating static shell assets
   ```
2. **Automatic Legacy Cache Cleanup**:
   On the next page load, the browser detects the updated `sw.js` (because of `Cache-Control: "no-cache"`). The `activate` hook automatically purges old caches (`bitlink-v1`) and claims active clients (`clients.claim()`).

### 11.7 PWA Verification & Testing

#### Command Line Verification
```bash
# Verify Service Worker endpoint and headers
curl -I https://mycompany-crm.duckdns.org/sw.js
# Expected: HTTP/2 200, cache-control: no-cache, service-worker-allowed: /

# Verify Manifest endpoint
curl -I https://mycompany-crm.duckdns.org/manifest.json
# Expected: HTTP/2 200, content-type: application/manifest+json
```

#### Chrome DevTools Audit
1. Open `https://mycompany-crm.duckdns.org` in Chrome.
2. Open DevTools (`F12` or `Cmd + Option + I`).
3. Navigate to **Application → Manifest** to inspect branding, icons, and display mode.
4. Navigate to **Application → Service Workers** to inspect registration state (`Activated and running`).
5. Run a **Lighthouse** audit under the **Progressive Web App** category to confirm compliance.

---

## 12. Seed the Slack Apps

> **🙋 Manual step — requires your Slack workspace admin access.**

BitLink uses **three separate Slack apps**, each with its own bot token and app-level token.

| App | Bot Token Env Var | App Token Env Var | Runs In | Purpose |
|-----|-------------------|-------------------|---------|---------|
| 1 | `SLACK_BOT_TOKEN` | `SLACK_APP_TOKEN` | `wa-bridge` | WhatsApp ↔ Slack relay |
| 2 | `SLACK_BOT_TOKEN_TELEGRAM` | `SLACK_APP_TOKEN_TELEGRAM` | `tg-bridge` | Telegram ↔ Slack relay |
| 3 | `MANAGER_SLACK_BOT_TOKEN` | `MANAGER_SLACK_APP_TOKEN` | `slack-manager` | Slash commands and modals |

### Bot token scopes

**Apps 1 & 2 — Bot Token Scopes**

| Scope | Needed for |
|-------|------------|
| `channels:manage` | `conversations.create` for private lead channels |
| `channels:write` | `conversations.setTopic`, `invite`, `archive` |
| `channels:join` | Joining a channel to post |
| `channels:history` | Reading history for the AI transcript |
| `groups:history`, `groups:write`, `groups:read` | Private channel equivalents |
| `im:write`, `im:read` | DMs to sales reps about new leads |
| `chat:write` | Posting and `chat_postEphemeral` for interactive responses |
| `reactions:write`, `reactions:read` | ✅ / ❌ delivery receipts |
| `files:write` | Uploading media relayed to clients |
| `users:read` | Resolving user IDs to names |

**App 3 — Bot Token Scopes**

`commands`, `chat:write`, `chat:write.public`

### Socket Mode

Enable **Socket Mode** on all three apps. Under **Basic Information → App-Level Tokens**, generate an `xapp-…` token with the `connections:write` scope for each app.

### Event subscriptions

Subscribe to: `message.channels`, `message.groups`, `message.im`, `message.app_mention`, `app_mention`.

### Slash commands (App 3)

| Command | Purpose |
|---------|---------|
| `/handoff` | Transfer lead ownership to another rep |
| `/sales-snippet` | Post a saved snippet |
| `/enhance` | AI-draft a reply from the channel transcript |

### Verify tokens

```bash
docker compose exec dashboard python -c "
import os
from dotenv import load_dotenv; load_dotenv()
from slack_sdk import WebClient
for v in ['SLACK_BOT_TOKEN', 'SLACK_BOT_TOKEN_TELEGRAM', 'MANAGER_SLACK_BOT_TOKEN']:
    t = os.getenv(v)
    r = WebClient(token=t).auth_test()
    print(v, '->', r.get('user') or r.get('error'))
"
```

Each line should print a bot username.

---

## 13. First-Run Verification

### 13.1 All containers healthy

```bash
docker compose ps
```

All six containers should show `Up` or `Up (healthy)`.

### 13.2 Dashboard API health

```bash
curl -s http://127.0.0.1:5050/api/health | python3 -m json.tool
```

Expect `"status": "ok"` and `"online": true` for `whatsapp_gateway`, `whatsapp_bridge`, and `telegram_bridge`.

### 13.3 Database reachable

```bash
docker compose exec db psql -U bitlink -d bitlink -c "\dt"
```

You should see 8 tables: `client_mappings`, `sales_leads`, `sales_pool`, `blocklist`, `project_managers`, `snippets`, `settings`, `dashboard_users`.

### 13.4 Log in to the dashboard

Open `https://mycompany-crm.duckdns.org` and log in. If you get `Invalid username or password`, re-run [§9](#9-create-the-dashboard-admin-user).

### 13.5 Send a test lead

Message the linked WhatsApp number from a different phone. You should see in order:

1. A private Slack channel named `sale-wa-<name>-<YYYYMMDD>` appears
2. Topic: `WhatsApp Lead: <number> | Auto-provisioned by Bitlink Sales`
3. All active sales-pool members are added to the channel
4. Each rep gets a DM about the new lead
5. A lead row appears in **Sales CRM** assigned round-robin
6. Replying in that Slack channel delivers to WhatsApp with a ✅ reaction

### 13.6 Seed the sales pool and project managers

Both tables start empty.

**Sales reps** — add in the dashboard under **Member Pool → Add Sales Rep**.

**Project managers** — no dashboard UI yet. Insert directly:

```bash
docker compose exec db psql -U bitlink -d bitlink -c "
INSERT INTO project_managers (slack_user_id, name, active)
VALUES
  ('U0PROJECTMGR1', 'Priya',  TRUE),
  ('U0PROJECTMGR2', 'Daniel', TRUE)
ON CONFLICT DO NOTHING;
"
```

### 13.7 Scheduled Features: Daily Digest, Sunday Auto-Reply, and AI Insights

All three features are **always-on** in the Docker setup — no cron jobs or manual triggers needed.

#### How they run

| Feature | Runs in | When | Timezone | Controlled by |
|---------|---------|------|----------|--------------|
| **Daily Sales Digest** | `wa-bridge` | Daily at `digest_time` (default `09:00`) | **PKT always** (uses `ZoneInfo("Asia/Karachi")`) | Dashboard → Settings → Digest |
| **Sunday Auto-Reply** | `wa-bridge` | On every inbound WhatsApp message received on Sunday | **PKT** (`TZ=Asia/Karachi` set in Docker Compose) | Dashboard → Settings → Sunday Auto-Reply |
| **AI Win/Loss Insights** | `wa-bridge` | On-demand — dashboard button triggers it | N/A (stateless API call) | Dashboard → AI Insights button |
| **Weekly Stage Inference** | `ai-jobs` | Every Sunday at `02:00` PKT | **PKT** (`TZ=Asia/Karachi` set in Docker Compose) | Runs automatically — no toggle |
| **Daily Auto-Archive** | `ai-jobs` | Every day at `03:00` PKT | **PKT** (`TZ=Asia/Karachi` set in Docker Compose) | Dashboard → Settings → Archive After Days |

#### Enable Daily Digest

1. In the dashboard go to **Settings**.
2. Enable **Digest** and set the **Digest Time** (e.g. `09:00`).
3. Set or confirm the **Digest Channel** — the bot will auto-discover `#sales-digest`, `#sales-general`, or `#sales` if not explicitly set.

To trigger it immediately without waiting for the scheduled time:

```bash
docker compose exec wa-bridge curl -s -X POST http://localhost:8101/sales-digest/trigger
```

Verify it ran:

```bash
docker compose exec db psql -U bitlink -d bitlink -c \
  "SELECT value FROM settings WHERE key = 'digest_last_posted_day';"
```

#### Enable Sunday Auto-Reply

1. In the dashboard go to **Settings**.
2. Enable **Sunday Auto-Reply** and optionally customise the reply text.

The reply fires once per client per Sunday. If the WhatsApp emergency stop is active, the auto-reply is suppressed. The "already replied" state is stored in memory per-day and resets at midnight PKT — so one acknowledgment per client per Sunday is guaranteed.

#### AI Win/Loss Insights

Requires `GEMINI_API_KEY` in `.env`. Click the **AI Insights** button in the dashboard. The `dashboard` container proxies the request to `wa-bridge` which calls Gemini and returns the report. The report can optionally be posted to Slack directly from the dashboard.

Verify the Gemini key works:

```bash
docker compose exec dashboard python -c "
import gemini_client
print(gemini_client.call_gemini_api('Reply with the single word: OK'))
"
```

#### Weekly Stage AI + Daily Auto-Archive (`ai-jobs`)

The `ai-jobs` container runs `sales_ai_jobs.py` indefinitely and wakes up on the `schedule` library's timer:

```bash
# View the scheduler logs
docker compose logs -f ai-jobs

# Run all jobs right now (stage inference + auto-archive)
docker compose run --rm ai-jobs python sales_ai_jobs.py --run-now

# Run only auto-archive right now
docker compose run --rm ai-jobs python sales_ai_jobs.py --archive-now
```

> **Note on weekly stage inference:** `run_weekly_stage_inference` currently scans all open leads and logs them but the AI stage-update step is stubbed (no actual stage is changed in the database yet). Auto-archive works fully.

### 13.8 AI features (optional)

```bash
docker compose exec dashboard python -c "
import gemini_client
print(gemini_client.call_gemini_api('Reply with the single word: OK'))
"
```

---

## 14. Operational Commands

### View logs

```bash
docker compose logs -f                  # all containers
docker compose logs -f dashboard        # Flask dashboard only
docker compose logs -f gateway          # WhatsApp gateway only
docker compose logs -f wa-bridge        # WhatsApp bridge only
docker compose logs -f tg-bridge        # Telegram bridge only
docker compose logs -f slack-manager    # Slack manager only
docker compose logs -f db               # PostgreSQL only
```

### Restart a single container

```bash
docker compose restart dashboard
docker compose restart gateway
docker compose restart wa-bridge
```

### Stop and start everything

```bash
docker compose down      # stop and remove containers (volumes preserved)
docker compose up -d     # start everything again
```

### Database backup

```bash
mkdir -p /opt/bitlink-sales/backups
docker compose exec db pg_dump -U bitlink bitlink \
  > /opt/bitlink-sales/backups/bitlink_$(date +%Y%m%d_%H%M%S).sql
```

### Database restore

```bash
docker compose exec -T db psql -U bitlink -d bitlink \
  < /opt/bitlink-sales/backups/bitlink_TIMESTAMP.sql
```

### Health and refresh

```bash
curl http://127.0.0.1:5050/api/health
docker compose exec dashboard curl -X POST http://wa-bridge:8101/refresh
docker compose exec dashboard curl -X POST http://tg-bridge:8003/refresh
```

### Check outbox queue

```bash
docker compose exec dashboard wc -l whatsapp_outbox.jsonl
```

---

## 15. Upgrading a Running Server

### Step 1 — Back up the database first

```bash
mkdir -p /opt/bitlink-sales/backups
docker compose exec db pg_dump -U bitlink bitlink \
  > /opt/bitlink-sales/backups/bitlink_$(date +%Y%m%d_%H%M%S).sql
```

### Step 2 — Pull or rsync latest code updates

**If using rsync (from your local Mac):**
```bash
./deploy.sh
```

**If using GitHub (on the server):**
```bash
cd /opt/bitlink-sales
git pull origin <your-branch-or-main>
```

> Your `/opt/bitlink-sales/.env`, session files, and Docker volumes (`pgdata`, `baileys_auth`, `tg_sessions`) will remain completely untouched.

### Step 3 — Rebuild and restart containers

On the server:

```bash
cd /opt/bitlink-sales
docker compose build      # rebuild images with new code
docker compose up -d      # rolling restart of changed services
```

Docker Compose only restarts containers whose image changed. Sessions and database volumes are unaffected.

### Step 4 — Check for schema changes

This project has an idempotent schema. If a release updates `schema.sql`, apply the migration manually with:

```bash
docker compose exec -T db psql -U bitlink -d bitlink < schema.sql
```

Because all statements use `CREATE TABLE IF NOT EXISTS`, re-running is safe — it adds new tables but does not alter or truncate existing ones.

---

## 16. Optional — Migrating Existing Google Sheets Data

**Skip this section for a fresh install.** Only needed if moving from an older BitLink version that stored CRM data in Google Sheets.

Requirements: the spreadsheet named `BitLink Client Mappings`, and a service-account JSON at `credentials/service_account.json`.

```bash
# Verify only — compares counts, writes nothing
docker compose run --rm dashboard python migrate_sheets_to_pg.py --verify

# DESTRUCTIVE — truncates then imports
docker compose run --rm dashboard python migrate_sheets_to_pg.py --allow-truncate
```

> `--allow-truncate` destroys all eight tables. Only use this against an empty database.

---

## 17. Troubleshooting

**Dashboard returns 500 on every page.**
PostgreSQL is unreachable. Check `docker compose ps` — is the `db` container `Up (healthy)`? Check `docker compose logs db` for errors.

**Cannot log in to the dashboard.**
The `dashboard_users` table is empty. Re-run [§9](#9-create-the-dashboard-admin-user). Verify with:
```bash
docker compose exec db psql -U bitlink -d bitlink -c "SELECT id FROM dashboard_users;"
```

**WhatsApp gateway asks for QR scan on every restart.**
`whatsapp-bot/auth_info/` is empty or not mounted. Check the volume mount in `docker-compose.yml` and that the directory exists on the host:
```bash
ls /opt/bitlink-sales/whatsapp-bot/auth_info/
```

**Telegram re-asks for phone login.**
`sessions/bitlink_telegram.session` was deleted. Re-run the interactive login from [§8.2](#82-telegram-phone-login).

**Containers exit immediately / crash-loop.**
Read the logs:
```bash
docker compose logs --tail=50 <service-name>
```
Most common cause: missing or wrong `.env` values (bad token, wrong `DATABASE_URL`).

**`DATABASE_URL` connection refused.**
You probably left `127.0.0.1` in the URL. Inside Docker, use the service hostname:
`postgresql://bitlink:PASSWORD@db:5432/bitlink`

**Nobody is in the new lead channel.**
The sales pool is empty. Add reps under **Member Pool** in the dashboard.

**Handover creates a channel but no PMs are in it.**
The `project_managers` table is empty and `PROJECT_MANAGER_SLACK_ID` is not set. See [§13.6](#136-seed-the-sales-pool-and-project-managers).

**Every message processed twice, Slack rate-limit warnings.**
A legacy script (`backup_mainwhatsapp.py`, `tele_backup.py`, `management_server.py`) is running and sharing an app token with a live bridge. Do not run those files.

**`PORT` collision — gateway or dashboard will not bind.**
`PORT` configures the WhatsApp Baileys gateway (default `3101`). The dashboard reads `CONTROL_DASHBOARD_PORT` (default `5050`). Never set `PORT=5050`.

**Certbot says domain not reachable.**
Port 80 is not open. Check `sudo ufw status` and your cloud firewall rules. Ensure the domain resolves to your server IP:
```bash
nslookup mycompany-crm.duckdns.org
```

**SSL certificate expiry.**
Certbot auto-renews via `certbot.timer`. Verify: `sudo systemctl status certbot.timer`. Force a renewal test: `sudo certbot renew --dry-run`.

**PWA install prompt does not appear or Service Worker fails to register.**
Service Workers strictly require HTTPS. Ensure Certbot SSL is active (§10) and your server is accessed over `https://`. Verify `/sw.js` returns HTTP 200 with header `Service-Worker-Allowed: /`.

**Dashboard updates or CSS changes not appearing in browser.**
The Service Worker caches static assets. Increment `CACHE_NAME` in `static/sw.js` (e.g. `bitlink-v1` to `bitlink-v2`) and push changes, or perform a hard refresh (`Ctrl+Shift+R` / `Cmd+Shift+R`).

### 17.7 Running PostgreSQL directly on the Host (Alternative Option)

By default, Docker manages PostgreSQL in the `db` container with zero manual setup. If you instead prefer to install and run PostgreSQL natively on Ubuntu/Debian:

1. **Install PostgreSQL on the host:**
   ```bash
   sudo apt update && sudo apt install -y postgresql postgresql-contrib
   sudo systemctl enable postgresql
   sudo systemctl start postgresql
   ```

2. **Create the user, database, and apply schema:**
   ```bash
   sudo -u postgres psql -c "CREATE USER bitlink WITH PASSWORD 'your_strong_password';"
   sudo -u postgres psql -c "CREATE DATABASE bitlink OWNER bitlink;"
   sudo -u postgres psql -d bitlink -f /opt/bitlink-sales/schema.sql
   ```

3. **Allow Docker containers to connect to the host PostgreSQL:**
   Edit `/etc/postgresql/15/main/postgresql.conf` (adjust version number if needed):
   ```
   listen_addresses = 'localhost, 172.17.0.1'
   ```
   Edit `/etc/postgresql/15/main/pg_hba.conf`:
   ```
   host    bitlink    bitlink    172.17.0.0/16    md5
   ```
   Restart PostgreSQL:
   ```bash
   sudo systemctl restart postgresql
   ```

4. **Update `/opt/bitlink-sales/.env`:**
   ```ini
   DATABASE_URL=postgresql://bitlink:your_strong_password@172.17.0.1:5432/bitlink
   ```

5. **In `docker-compose.yml`:**
   Comment out the `db:` container service and remove the `depends_on: - db` lines from Python services.

---

## 18. Security Notes

### 18.1 Pre-launch checklist

- [ ] `chmod 600 /opt/bitlink-sales/.env`
- [ ] Strong unique `DB_PASSWORD` in `.env`
- [ ] Dashboard user created with a real password (no default account)
- [ ] Ports 80 and 443 open; port 5050 blocked (`sudo ufw deny 5050/tcp`)
- [ ] `sessions/` and `whatsapp-bot/auth_info/` are `chmod 700`
- [ ] `.env`, `sessions/`, `auth_info/` are in `.gitignore` — never commit them
- [ ] SSL certificate issued and HTTPS working

### 18.2 The `PORT` variable is a trap

`PORT` configures the **WhatsApp Baileys gateway** (`service-baileys.js`), default `3101`. The Flask dashboard reads `CONTROL_DASHBOARD_PORT`, default `5050`. Setting `PORT=5050` makes the gateway fight the dashboard for the same port — one will fail. Always use `CONTROL_DASHBOARD_PORT` for the dashboard.

### 18.3 Dashboard sessions

The Flask secret key is regenerated on every container restart, so all active sessions are invalidated on redeploy. This is intentional. Sessions expire after 24 hours regardless.

### 18.4 `DASHBOARD_PASSCODE` is inert

`DASHBOARD_PASSCODE` is defined in `control_server.py` but never used as an authentication check. Login is bcrypt-hash-only against `dashboard_users`. Do not rely on it as a second factor.

### 18.5 Database is not exposed to the host

The `db` container does not publish port 5432 to the host. It is reachable only on the internal `bitlink-net` Docker bridge. You cannot connect to it directly from your laptop — use `docker compose exec db psql ...` instead.

### 18.6 Auto-restart on crash and server reboot

All six containers have `restart: always` in `docker-compose.yml`. If a container crashes, Docker restarts it automatically. If the server reboots, Docker (enabled via `systemctl enable docker`) restarts all containers automatically. **No manual intervention needed for crashes or reboots.**

### 18.7 PWA Service Worker Scope & HTTPS Security

The Service Worker is served with `Service-Worker-Allowed: /` to allow controlling the entire domain scope. Because Service Workers can intercept network calls, HTTPS enforcement (§10) and `Cache-Control: "no-cache"` on `/sw.js` are mandatory security controls to prevent MITM code injection or stale authentication state caching.

---

*Companion documents: [CHANGES_SALES_CRM.md](CHANGES_SALES_CRM.md) (feature change log) · [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) (architecture) · [README.md](README.md) (product overview)*
