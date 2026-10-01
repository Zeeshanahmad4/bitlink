# BitLink Production Linux Server Deployment Guide

Complete step-by-step instructions for deploying BitLink, all platform bridges (WhatsApp & Telegram), the Slack App Manager, and the Web Control Dashboard on a production Linux server (Ubuntu/Debian).

---

## 📑 Table of Contents
1. [Architecture & Port Overview](#1-architecture--port-overview)
2. [Server Prerequisites Installation](#2-server-prerequisites-installation)
3. [Migrating Code & Active Logins](#3-migrating-code--active-logins)
4. [Setting Up Python Virtualenv & Node Dependencies](#4-setting-up-python-virtualenv--node-dependencies)
5. [Configuring Environment Variables](#5-configuring-environment-variables)
6. [Option A: Production Deployment via Systemd (Recommended)](#6-option-a-production-deployment-via-systemd-recommended)
7. [Option B: Production Deployment via PM2 (Alternative)](#7-option-b-production-deployment-via-pm2-alternative)
8. [Configuring Dashboard Remote Access & Nginx SSL](#8-configuring-dashboard-remote-access--nginx-ssl)
9. [Operational Commands & Monitoring](#9-operational-commands--monitoring)
10. [Troubleshooting & Common FAQs](#10-troubleshooting--common-faqs)

---

## 1. Architecture & Port Overview

BitLink operates 5 integrated processes that run 24/7 on the server:

| Service | Technology | Port / Connection | Purpose |
|---|---|---|---|
| **Node WhatsApp Gateway** | Node.js (Baileys) | `127.0.0.1:3101` | Multi-device WhatsApp socket connector |
| **WhatsApp Python Bridge** | Python (aiohttp/Flask) | `127.0.0.1:8101` + Slack Socket Mode | Bridges Slack channels ↔ WhatsApp chats |
| **Telegram Python Bridge** | Python (Telethon) | `127.0.0.1:8003` + Slack Socket Mode | Bridges Slack channels ↔ Telegram chats |
| **Control Dashboard Server** | Python (Flask REST API) | `0.0.0.0:5050` | Web UI & API for sales reps and managers |
| **Slack App Manager** | Python (Slack SDK) | Slack Socket Mode | Handles `/enhance`, lead claims, AI triage |

---

## 2. Server Prerequisites Installation

SSH into your Linux server and run the following updates and package installations:

```bash
# Update Ubuntu package index
sudo apt update && sudo apt upgrade -y

# Install Python 3, venv, curl, and build tools
sudo apt install -y python3 python3-pip python3-venv git curl build-essential

# Install Node.js v20 LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Verify installed versions
python3 --version   # Should be 3.10+
node -v             # Should be v20.x
npm -v              # Should be 10.x
```

---

## 3. Migrating Code & Active Logins

To avoid having to re-scan WhatsApp QR codes or re-authenticate Telegram with SMS codes on the server, you must preserve the session and credentials directories.

### Critical Directories to Migrate:
* `sessions/` — Contains Telethon's active MTProto session (`bitlink_telegram.session`).
* `whatsapp-bot/auth_info/` — Contains Baileys multi-device authentication credentials.
* `credentials/` — Contains Google Sheets API `service_account.json`.
* `.env` — Contains all Slack bot tokens, app tokens, and API credentials.

### Transfer Command (Run from your local Mac/PC):
```bash
rsync -avz --progress \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'whatsapp-bot/node_modules' \
  --exclude 'temp_downloads_telegram' \
  /Users/apple/Desktop/BitLink-Sales/ ubuntu@<YOUR_SERVER_IP>:/opt/BitLink-Sales/
```
*(Replace `ubuntu` and `<YOUR_SERVER_IP>` with your Linux username and IP address. Destination `/opt/BitLink-Sales` is the standard production path).*

---

## 4. Setting Up Python Virtualenv & Node Dependencies

SSH into the server:

```bash
cd /opt/BitLink-Sales/bitlink

# 1. Create a dedicated Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Upgrade pip and install all Python requirements
pip install --upgrade pip
pip install -r requirements.txt

# 3. Install Node.js gateway dependencies
cd whatsapp-bot
npm install
cd ..
```

---

## 5. Configuring Environment Variables

Verify that your `/opt/BitLink-Sales/bitlink/.env` file contains all necessary tokens:

```bash
nano /opt/BitLink-Sales/bitlink/.env
```

Ensure the following variables are defined:
```ini
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN_TELEGRAM=xoxb-...
SLACK_APP_TOKEN_TELEGRAM=xapp-...
MANAGER_SLACK_APP_TOKEN=xapp-...
MANAGER_SLACK_BOT_TOKEN=xoxb-...
MASTER_SLACK_BOT_ID=...
API_ID=...
API_HASH=...
NODE_API_URL=http://127.0.0.1:3101
WHATSAPP_REFRESH_PORT=8101
TELEGRAM_REFRESH_PORT=8003
PORT=5050
SESSIONS_DIR=sessions
```

---

## 6. Option A: Production Deployment via Systemd (Recommended)

Systemd is the native Linux service manager. It automatically starts BitLink on system boot, captures logs into `journalctl`, and restarts processes if they crash.

### Step 1: Verify the Unit File
Check the pre-configured [bitlink.service](file:///Users/apple/Desktop/BitLink-Sales/bitlink/bitlink.service):

```ini
[Unit]
Description=BitLink Unified Process Supervisor (WhatsApp, Telegram, Dashboard, Manager)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/BitLink-Sales/bitlink
ExecStart=/opt/BitLink-Sales/bitlink/.venv/bin/python start_services.py
Restart=always
RestartSec=5
KillMode=process
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bitlink

[Install]
WantedBy=multi-user.target
```
*(Make sure the `User` and `WorkingDirectory` match your actual Linux user and folder).*

### Step 2: Enable & Start the Service
```bash
# 1. Copy unit file to systemd directory
sudo cp /opt/BitLink-Sales/bitlink/bitlink.service /etc/systemd/system/bitlink.service

# 2. Reload systemd daemon
sudo systemctl daemon-reload

# 3. Enable auto-start on server boot
sudo systemctl enable bitlink

# 4. Start all services now
sudo systemctl start bitlink

# 5. Check status
sudo systemctl status bitlink
```

---

## 7. Option B: Production Deployment via PM2 (Alternative)

If your team prefers Node's PM2 process manager:

```bash
# 1. Install PM2 globally
sudo npm install -g pm2

# 2. Navigate to project directory
cd /opt/BitLink-Sales/bitlink

# 3. Start all services using the included ecosystem file
pm2 start ecosystem.config.js

# 4. Save state and configure startup script
pm2 save
pm2 startup
```

---

## 8. Configuring Dashboard Remote Access & Nginx SSL

The Control Dashboard binds to port `5050` (`0.0.0.0:5050`).

### Method 1: Direct Firewall Access (Quickest)
Open port 5050 in the UFW firewall (and in your AWS / DigitalOcean security group):
```bash
sudo ufw allow 5050/tcp
```
Access in your browser: `http://<YOUR_SERVER_IP>:5050`

### Method 2: Nginx Reverse Proxy with Free HTTPS/SSL (Recommended)
Use a custom domain or subdomain (e.g. `dashboard.yourcompany.com`):

1. **Install Nginx & Certbot**:
   ```bash
   sudo apt install -y nginx certbot python3-certbot-nginx
   ```

2. **Configure Nginx**:
   ```bash
   sudo cp /opt/BitLink-Sales/bitlink/nginx-bitlink.conf /etc/nginx/sites-available/bitlink.conf
   sudo nano /etc/nginx/sites-available/bitlink.conf
   ```
   *(Update `server_name yourdomain.com;` with your actual domain).*

3. **Enable Site & Obtain SSL Certificate**:
   ```bash
   sudo ln -s /etc/nginx/sites-available/bitlink.conf /etc/nginx/sites-enabled/
   sudo nginx -t
   sudo systemctl reload nginx

   # Obtain free HTTPS certificate via Let's Encrypt
   sudo certbot --nginx -d yourdomain.com
   ```

Now access securely at `https://yourdomain.com`.

---

## 9. Operational Commands & Monitoring

### Service Lifecycle
```bash
# Check service health status
sudo systemctl status bitlink

# Restart all services (loads code updates)
sudo systemctl restart bitlink

# Stop all services
sudo systemctl stop bitlink
```

### Viewing Real-Time Logs
```bash
# Follow live colored logs from all bridges and dashboard
journalctl -u bitlink -f

# View last 200 lines
journalctl -u bitlink -n 200 --no-pager
```

### Checking Health APIs
```bash
# Overall system health (checks gateway :3101, WA :8101, TG :8003)
curl http://127.0.0.1:5050/api/health

# Telegram refresh & sync
curl -X POST http://127.0.0.1:8003/refresh

# WhatsApp refresh & sync
curl -X POST http://127.0.0.1:8101/refresh
```

---

## 10. Troubleshooting & Common FAQs

### 1. What happens if the server reboots?
Systemd automatically launches `start_services.py` on boot. Upon startup, both WhatsApp and Telegram bridges run the **Downtime Catch-Up Sync**, automatically discovering any messages your sales team sent in Slack while the server was offline and delivering them to the clients.

### 2. How do sales reps know messages were delivered?
Every message forwarded to WhatsApp or Telegram receives a **`✅` (`:white_check_mark:`)** reaction in Slack upon confirmation. If a delivery temporarily fails, it receives an **`❌`** reaction and is queued in the disk-backed outbox (`outbox.jsonl` / `telegram_outbox.jsonl`) for automatic retry.

### 3. A child process crashed on Linux. Do I need to SSH in?
No. `start_services.py` has an automated keep-alive watchdog. If any bridge or gateway process terminates, the supervisor automatically restarts it in 3 seconds.

### 4. Updating Code in the Future
To push updates from development to production:
```bash
cd /opt/BitLink-Sales/bitlink
git pull  # Or rsync new files
sudo systemctl restart bitlink
```

---

## 11. Deploying to an ALREADY RUNNING Server (In-Place Upgrade)

If your Linux server is **already live and running** an older version of BitLink, follow this zero-downtime, zero-session-loss procedure:

### Step 1: Identify How Services Are Currently Running
SSH into your server and run:
```bash
# Check running processes and their directory path
ps aux | grep -E 'python|node'

# Check if managed by systemd
systemctl list-units | grep -i bitlink

# Check if managed by PM2
pm2 list 2>/dev/null || echo "PM2 not used"

# Check if running in tmux or screen
screen -ls 2>/dev/null; tmux ls 2>/dev/null
```
Note down the exact directory where BitLink is installed (e.g. `/opt/bitlink` or `/opt/BitLink-Sales/bitlink`).

### Step 2: Create a Safety Backup of the Server's Active Logins
Before replacing any code, preserve the server's active WhatsApp and Telegram login tokens:
```bash
# On the server (replace /opt/bitlink with your actual path):
sudo cp -r /opt/bitlink /opt/bitlink_backup_$(date +%Y%m%d_%H%M%S)
```

### Step 3: Sync Updated Code (PROTECTING Live Sessions)
Run this command from your **local machine**. Notice the `--exclude` flags that prevent overwriting the server's live WhatsApp and Telegram sessions:

```bash
rsync -avz --progress \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'whatsapp-bot/node_modules' \
  --exclude 'sessions/' \
  --exclude 'whatsapp-bot/auth_info/' \
  --exclude 'outbox.jsonl' \
  --exclude 'telegram_outbox.jsonl' \
  --exclude 'telegram_message_map.json' \
  --exclude 'sent_telegram_alerts.json' \
  /Users/apple/Desktop/BitLink-Sales/bitlink/ ubuntu@<YOUR_SERVER_IP>:/opt/bitlink/
```
*(Replace `/opt/bitlink` with your server's directory and `ubuntu@<YOUR_SERVER_IP>` with your login).*

### Step 4: Update Virtualenv Dependencies (On Server)
SSH into your server:
```bash
cd /opt/bitlink

# Activate the server's virtualenv and update packages
source .venv/bin/activate
pip install -r requirements.txt

# Ensure Node gateway dependencies are current
cd whatsapp-bot
npm install
cd ..
```

### Step 5: Restart the Services Cleanly
Depending on how the server runs:

* **If using Systemd (`bitlink.service`)**:
  ```bash
  sudo systemctl restart bitlink
  sudo systemctl status bitlink
  ```

* **If using PM2**:
  ```bash
  pm2 restart all
  pm2 status
  ```

* **If running manually in a terminal / screen / tmux**:
  ```bash
  # Find and kill the old processes:
  pkill -f "start_services.py|main_whatsapp.py|main_telegram.py|control_server.py|service-baileys.js"
  
  # Start the new supervisor:
  source .venv/bin/activate
  python3 start_services.py
  ```

### Step 6: Verify Catch-Up
Check the live log output:
```bash
journalctl -u bitlink -f  # (or tail -f logs / pm2 logs)
```
You will see:
1. `✅ Connected to Slack Socket Mode`
2. `🔄 [CATCH-UP] Scanning Slack for messages sent during downtime...`
3. Any messages sent by your sales reps during the ~5-second restart will be caught up automatically and marked with **`✅`** in Slack.

