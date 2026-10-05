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
| 5 | [Clone and deploy from GitHub](#5-clone-and-deploy-from-github) |
| 6 | [Configure environment variables](#6-configure-environment-variables) |
| 7 | [Build and start all containers](#7-build-and-start-all-containers) |
| 8 | [First-time logins (WhatsApp QR + Telegram OTP)](#8-first-time-logins-whatsapp-qr--telegram-otp) |
| 9 | [Create the dashboard admin user](#9-create-the-dashboard-admin-user) |
| 10 | [Configure Nginx and get free HTTPS](#10-configure-nginx-and-get-free-https) |
| 11 | [Seed the Slack apps](#11-seed-the-slack-apps) |
| 12 | [First-run verification](#12-first-run-verification) |
| 13 | [Operational commands](#13-operational-commands) |
| 14 | [Upgrading a running server](#14-upgrading-a-running-server) |
| 15 | [Optional — migrating existing Google Sheets data](#15-optional--migrating-existing-google-sheets-data) |
| 16 | [Troubleshooting](#16-troubleshooting) |
| 17 | [Security notes](#17-security-notes) |

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
| Three Slack apps | See [§11](#11-seed-the-slack-apps) |
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

---

## 5. Clone and Deploy from GitHub

> **🙋 Run on your Linux server via SSH.**

BitLink is deployed directly from your GitHub repository. Sensitive files, sessions, logs, and database files are excluded via `.gitignore` so they are never committed to GitHub.

### 5.1 Push latest code from local machine to GitHub

On your **local machine (macOS)**, commit and push your changes:

```bash
git status
# Verify that .env, sessions, and credentials are NOT staged
git add .
git commit -m "chore: prepare docker deployment and gitignore"
git push origin <your-branch-or-main>
```

### 5.2 Clone the repository on the Linux server

SSH into your Linux server and clone the project into `/opt/bitlink`:

```bash
# Create directory and assign ownership
sudo mkdir -p /opt/bitlink
sudo chown -R $USER:$USER /opt/bitlink

# Clone the repository
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git /opt/bitlink

# Or if you are using SSH deploy keys:
# git clone git@github.com:YOUR_USERNAME/YOUR_REPO.git /opt/bitlink

cd /opt/bitlink
```

> **Tip for Private Repositories:** If your GitHub repo is private, either:
> 1. Use a **Personal Access Token (PAT)** when prompted for the password, or
> 2. Generate an SSH key on the server (`ssh-keygen -t ed25519`) and add the public key as a **Deploy Key** in your GitHub repo settings (**Settings -> Deploy keys**).

---

## 6. Configure Environment Variables

> **🙋 Manual step — you must fill in your own API tokens and passwords.**

On the server, copy the template `.env.example` to `.env` and fill in your actual credentials:

```bash
cd /opt/bitlink
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
chmod 600 /opt/bitlink/.env
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

---

## 7. Build and Start All Containers

> **🙋 Manual step — run on your server.**

```bash
cd /opt/bitlink

# Build all images (takes a few minutes on first run)
docker compose build

# Start everything in the background
docker compose up -d

# Confirm all 6 containers are running
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
```

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

    client_max_body_size 100M;

    location / {
        proxy_pass          http://127.0.0.1:5050;
        proxy_http_version  1.1;

        proxy_set_header    Host              $host;
        proxy_set_header    X-Real-IP         $remote_addr;
        proxy_set_header    X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;
        proxy_set_header    Upgrade           $http_upgrade;
        proxy_set_header    Connection        "upgrade";

        proxy_read_timeout    300s;
        proxy_connect_timeout  10s;
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

## 11. Seed the Slack Apps

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

## 12. First-Run Verification

### 12.1 All containers healthy

```bash
docker compose ps
```

All six containers should show `Up` or `Up (healthy)`.

### 12.2 Dashboard API health

```bash
curl -s http://127.0.0.1:5050/api/health | python3 -m json.tool
```

Expect `"status": "ok"` and `"online": true` for `whatsapp_gateway`, `whatsapp_bridge`, and `telegram_bridge`.

### 12.3 Database reachable

```bash
docker compose exec db psql -U bitlink -d bitlink -c "\dt"
```

You should see 8 tables: `client_mappings`, `sales_leads`, `sales_pool`, `blocklist`, `project_managers`, `snippets`, `settings`, `dashboard_users`.

### 12.4 Log in to the dashboard

Open `https://mycompany-crm.duckdns.org` and log in. If you get `Invalid username or password`, re-run [§9](#9-create-the-dashboard-admin-user).

### 12.5 Send a test lead

Message the linked WhatsApp number from a different phone. You should see in order:

1. A private Slack channel named `sale-wa-<name>-<YYYYMMDD>` appears
2. Topic: `WhatsApp Lead: <number> | Auto-provisioned by Bitlink Sales`
3. All active sales-pool members are added to the channel
4. Each rep gets a DM about the new lead
5. A lead row appears in **Sales CRM** assigned round-robin
6. Replying in that Slack channel delivers to WhatsApp with a ✅ reaction

### 12.6 Seed the sales pool and project managers

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

### 12.7 Scheduled Features: Daily Digest, Sunday Auto-Reply, and AI Insights

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

### 12.8 AI features (optional)

```bash
docker compose exec dashboard python -c "
import gemini_client
print(gemini_client.call_gemini_api('Reply with the single word: OK'))
"
```

---

## 13. Operational Commands

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
mkdir -p /opt/bitlink/backups
docker compose exec db pg_dump -U bitlink bitlink \
  > /opt/bitlink/backups/bitlink_$(date +%Y%m%d_%H%M%S).sql
```

### Database restore

```bash
docker compose exec -T db psql -U bitlink -d bitlink \
  < /opt/bitlink/backups/bitlink_TIMESTAMP.sql
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

## 14. Upgrading a Running Server

### Step 1 — Back up the database first

```bash
mkdir -p /opt/bitlink/backups
docker compose exec db pg_dump -U bitlink bitlink \
  > /opt/bitlink/backups/bitlink_$(date +%Y%m%d_%H%M%S).sql
```

### Step 2 — Pull latest changes from GitHub

On the **server**:

```bash
cd /opt/bitlink
git pull origin <your-branch-or-main>
```

> Your `/opt/bitlink/.env`, session files, and Docker volumes (`pgdata`, `baileys_auth`, `tg_sessions`) will remain completely untouched because they are ignored by Git.

### Step 3 — Rebuild and restart containers

On the server:

```bash
cd /opt/bitlink
docker compose build      # rebuild images with new code
docker compose up -d      # rolling restart of changed services
```

Docker Compose only restarts containers whose image changed. Sessions and database volumes are unaffected.

### Step 4 — Check for schema changes

This project has no migration framework. If a release changes `schema.sql`, apply it manually:

```bash
docker compose exec db psql -U bitlink -d bitlink \
  -f /app/schema.sql
```

Because all statements use `CREATE TABLE IF NOT EXISTS`, re-running is safe — it adds new tables but does not alter or truncate existing ones.

---

## 15. Optional — Migrating Existing Google Sheets Data

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

## 16. Troubleshooting

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
ls /opt/bitlink/whatsapp-bot/auth_info/
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
The `project_managers` table is empty and `PROJECT_MANAGER_SLACK_ID` is not set. See [§12.6](#126-seed-the-sales-pool-and-project-managers).

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

---

## 17. Security Notes

### 17.1 Pre-launch checklist

- [ ] `chmod 600 /opt/bitlink/.env`
- [ ] Strong unique `DB_PASSWORD` in `.env`
- [ ] Dashboard user created with a real password (no default account)
- [ ] Ports 80 and 443 open; port 5050 blocked (`sudo ufw deny 5050/tcp`)
- [ ] `sessions/` and `whatsapp-bot/auth_info/` are `chmod 700`
- [ ] `.env`, `sessions/`, `auth_info/` are in `.gitignore` — never commit them
- [ ] SSL certificate issued and HTTPS working

### 17.2 The `PORT` variable is a trap

`PORT` configures the **WhatsApp Baileys gateway** (`service-baileys.js`), default `3101`. The Flask dashboard reads `CONTROL_DASHBOARD_PORT`, default `5050`. Setting `PORT=5050` makes the gateway fight the dashboard for the same port — one will fail. Always use `CONTROL_DASHBOARD_PORT` for the dashboard.

### 17.3 Dashboard sessions

The Flask secret key is regenerated on every container restart, so all active sessions are invalidated on redeploy. This is intentional. Sessions expire after 24 hours regardless.

### 17.4 `DASHBOARD_PASSCODE` is inert

`DASHBOARD_PASSCODE` is defined in `control_server.py` but never used as an authentication check. Login is bcrypt-hash-only against `dashboard_users`. Do not rely on it as a second factor.

### 17.5 Database is not exposed to the host

The `db` container does not publish port 5432 to the host. It is reachable only on the internal `bitlink-net` Docker bridge. You cannot connect to it directly from your laptop — use `docker compose exec db psql ...` instead.

### 17.6 Auto-restart on crash and server reboot

All six containers have `restart: always` in `docker-compose.yml`. If a container crashes, Docker restarts it automatically. If the server reboots, Docker (enabled via `systemctl enable docker`) restarts all containers automatically. **No manual intervention needed for crashes or reboots.**

---

*Companion documents: [CHANGES_SALES_CRM.md](CHANGES_SALES_CRM.md) (feature change log) · [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) (architecture) · [README.md](README.md) (product overview)*
