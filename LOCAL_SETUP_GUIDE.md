# BitLink — Local Development Setup Guide

Get BitLink running on your own Mac for **local testing only**. Each developer uses their own Slack apps, their own `.env`, and their own local database — nothing here is for production.

> For deploying to a real server, use `DEPLOYMENT_GUIDE.md` instead. This document is only for running it on your laptop.

---

## Table of contents

1. [What you'll run](#1-what-youll-run)
2. [Requirements](#2-requirements)
3. [Get the code](#3-get-the-code)
4. [Install PostgreSQL locally](#4-install-postgresql-locally)
5. [Create the database and user](#5-create-the-database-and-user)
6. [Apply the schema](#6-apply-the-schema)
7. [Create your dashboard login](#7-create-your-dashboard-login)
8. [Install Python dependencies](#8-install-python-dependencies)
9. [Install the Node gateway dependencies](#9-install-the-node-gateway-dependencies)
10. [Create your three Slack apps](#10-create-your-three-slack-apps)
11. [Create your .env](#11-create-your-env)
12. [Log in to WhatsApp and Telegram](#12-log-in-to-whatsapp-and-telegram)
13. [Run the five processes](#13-run-the-five-processes)
14. [Verify everything works](#14-verify-everything-works)
15. [Seed test data](#15-seed-test-data)
16. [Everyday commands](#16-everyday-commands)
17. [Troubleshooting](#17-troubleshooting)
18. [Local-only safety notes](#18-local-only-safety-notes)

---

## 1. What you'll run

Five processes, each in its own terminal. Plus a local PostgreSQL.

| # | Process | Command | Port |
|---|---------|---------|------|
| 1 | WhatsApp Gateway | `node service-baileys.js` | `3101` |
| 2 | WhatsApp Bridge | `python main_whatsapp.py` | `8101` |
| 3 | Telegram Bridge | `python main_telegram.py` | `8003` |
| 4 | Control Dashboard | `python control_server.py` | `5050` |
| 5 | Slack App Manager | `python bitlink_manager.py` | — |
| — | PostgreSQL | launchd service | `5432` |

The dashboard is the only one you open in a browser: <http://localhost:5050>

---

## 2. Requirements

| Tool | Version | Notes |
|------|---------|-------|
| macOS | 12 or newer | |
| Python | 3.11+ | 3.12 is fine |
| Node.js | 20 or 22 | |
| PostgreSQL | 15+ | 17 recommended |
| Slack | a workspace where you can create apps | You must be able to create apps |

Check what you have:

```bash
python3 --version
node -v && npm -v
psql --version
```

If anything is missing:

```bash
# Node (via Homebrew)
brew install node

# PostgreSQL — the official installer works well on macOS
# https://www.postgresql.org/download/macosx/
```

> Python is usually already present on macOS. If you need a specific version, `brew install python@3.12`.

---

## 3. Get the code

```bash
git clone <your-repo-url> bitlink
cd bitlink
git checkout feature/sales-crm-dashboard   # or whichever branch you're testing
```

If you already have the folder, just `cd` into it.

---

## 4. Install PostgreSQL locally

**Skip this if PostgreSQL is already installed and running.**

Install from <https://www.postgresql.org/download/macosx/> using the `.dmg` installer, then let it register as a launchd service so it starts on login. Verify:

```bash
pg_isready -h localhost -p 5432
```

Expected: `localhost:5432 - accepting connections`

If it says `no response`, start it:

```bash
sudo launchctl load -w /Library/LaunchDaemons/postgresql-17.plist
pg_isready -h localhost -p 5432
```

Adjust the filename if you installed a different major version.

---

## 5. Create the database and user

BitLink stores everything in PostgreSQL. Create a role and a database:

```bash
psql -h localhost -U <your-mac-username> -d postgres
```

Enter the password you chose during installation. Then, inside the `psql` prompt:

```sql
CREATE ROLE bitlink WITH LOGIN PASSWORD 'pick-your-own-password';
CREATE DATABASE bitlink OWNER bitlink;
\q
```

Pick your own password — everyone develops with their own. It goes in your `.env` in [§11](#11-create-your-env).

Verify:

```bash
PGPASSWORD='your-password' psql -h localhost -U bitlink -d bitlink -c "SELECT 1;"
```

---

## 6. Apply the schema

The app never creates its own tables. Apply `schema.sql` yourself. It creates 8 tables:

`client_mappings`, `sales_leads`, `sales_pool`, `blocklist`, `project_managers`, `snippets`, `settings`, `dashboard_users`

```bash
cd /path/to/bitlink
PGPASSWORD='your-password' psql -h localhost -U bitlink -d bitlink -f schema.sql
```

Confirm:

```bash
PGPASSWORD='your-password' psql -h localhost -U bitlink -d bitlink -c "\dt"
```

You should see 8 tables. Every statement is `CREATE TABLE IF NOT EXISTS`, so re-running is safe.

---

## 7. Create your dashboard login

**Mandatory.** There is no default account — the old `admin`/`admin` bypass was removed, and `schema.sql` contains no seed data. Without this you cannot log in.

### Generate a bcrypt hash

Make sure your virtualenv exists first ([§8](#8-install-python-dependencies)), since `bcrypt` comes from there. If you're doing this in order, skip ahead and come back.

```bash
source venv/bin/activate
python -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"
```

Copy the output string.

### Insert it

```bash
PGPASSWORD='your-db-password' psql -h localhost -U bitlink -d bitlink <<'SQL'
INSERT INTO dashboard_users (id, password_hash)
VALUES ('yourname', '\$2b\$12\$PASTE_YOUR_HASH_HERE')
ON CONFLICT (id) DO UPDATE SET password_hash = EXCLUDED.password_hash, updated_at = NOW();
SQL
```

`'yourname'` is the username you type on the login screen. The `\$` escapes are required inside a heredoc.

Forgot your password later? Re-run the generator and the same statement.

---

## 8. Install Python dependencies

This project uses a virtualenv named **`venv`** (not `.venv` — the Linux guide uses that name, this one doesn't):

```bash
cd /path/to/bitlink
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Activate it in every terminal before running a Python script:

```bash
source venv/bin/activate
```

Your shell prompt shows `(venv)` when active. Deactivate with `deactivate`.

### Run the tests

```bash
source venv/bin/activate
python -m unittest discover -s tests -v
```

Expected: `Ran 142 tests ... OK`. These are pure and touch neither Slack nor the database, so you can run them before anything is configured. If `tests/` is missing, it was excluded by `.gitignore` — ask a teammate for it.

---

## 9. Install the Node gateway dependencies

```bash
cd /path/to/bitlink/whatsapp-bot
npm install
cd ..
```

This takes a few minutes and downloads a Chromium build for `puppeteer` (~300 MB). Don't skip it — the gateway needs Chromium to render the WhatsApp QR code.

Verify:

```bash
ls ~/.cache/puppeteer/chrome
```

---

## 10. Create your three Slack apps

**Every developer makes their own apps in their own Slack workspace.** Do not reuse someone else's tokens, and never commit yours.

BitLink needs **three separate apps**. They cannot be the same app, because sharing an app token between two bridges makes both consume every event and Slack rate-limits you immediately.

| App | Name it | Runs | What it does |
|-----|---------|------|--------------|
| 1 | `bitlink-wa-<yourname>` | WhatsApp bridge | Relays WhatsApp traffic, creates private lead channels |
| 2 | `bitlink-tg-<yourname>` | Telegram bridge | Relays Telegram traffic |
| 3 | `bitlink-mgr-<yourname>` | Manager | Slash commands and modals only |

Go to <https://api.slack.com/apps> → **Create New App** → **From scratch** → pick your workspace.

### For each app: enable Socket Mode

**Settings → Socket Mode → Enable.** Then under **Basic Information → App-Level Tokens → Generate an app-level token** with the `connections:write` scope. This gives you the `xapp-…` token.

Repeat for all three apps.

### For each app: add the bot token scopes

**OAuth & Permissions → Scopes → Bot Token Scopes → Add an OAuth Scope.**

**Apps 1 and 2** need all of these:

| Scope | Why |
|-------|-----|
| `channels:manage` | Creating private lead channels |
| `channels:write` | Setting topics, inviting, archiving |
| `channels:join` | Joining a channel to post |
| `channels:history` | Reading history for AI context |
| `groups:history` | Private channel history |
| `groups:write` | Private channel writes |
| `groups:read` | Private channel reads |
| `im:write` | DMing sales reps about new leads |
| `im:read` | Reading DMs |
| `chat:write` | Posting messages and ephemeral replies |
| `reactions:write` | ✅ / ❌ delivery receipts |
| `reactions:read` | Reading reactions |
| `files:write` | Uploading relayed media |
| `users:read` | Resolving user IDs to names |

**App 3** needs only:

| Scope | Why |
|-------|-----|
| `commands` | Slash commands |
| `chat:write` | Posting |
| `chat:write.public` | Responding in channels |

### For each app: install it and copy the tokens

Click **Install to Workspace** → **Allow**. Then under **OAuth & Permissions → Bot User OAuth Token** copy the `xoxb-…` token.

### App 1 and 2: subscribe to events

**Event Subscriptions → Subscribe to bot events** → add:

- `message.channels`
- `message.groups`
- `message.im`
- `message.app_mention`
- `app_mention`

### App 3: register slash commands

**Slash Commands → Create New Command:**

| Command | Short Description |
|---------|-------------------|
| `/handoff` | Transfer lead ownership |
| `/sales-snippet` | Post a saved snippet |
| `/enhance` | AI-draft a reply |

`/sales-snippet` also answers to `/snippet-sales`.

### Note your Slack user ID

You'll need this for `MASTER_SLACK_BOT_ID`. In Slack, click your profile picture → your ID (`U0XXXXXXXX`) appears at the bottom of the panel.

### Get a Telegram API ID and hash

From <https://my.telegram.org> → **API development tools** → create an app. You'll get `API_ID` (a number) and `API_HASH` (a string).

---

## 11. Create your .env

Create `/path/to/bitlink/.env`. Every value is **yours** — nothing here is shared.

```ini
# ---------- Database (yours, from §5) ----------
DATABASE_URL=postgresql://bitlink:your-db-password@localhost:5432/bitlink
DB_POOL_MAX=30
DB_POOL_TIMEOUT=10

# ---------- Slack app 1: WhatsApp bridge ----------
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# ---------- Slack app 2: Telegram bridge ----------
SLACK_BOT_TOKEN_TELEGRAM=xoxb-...
SLACK_APP_TOKEN_TELEGRAM=xapp-...

# ---------- Slack app 3: manager ----------
MANAGER_SLACK_BOT_TOKEN=xoxb-...
MANAGER_SLACK_APP_TOKEN=xapp-...

# ---------- Telegram (from my.telegram.org) ----------
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890

# ---------- Your Slack user ID ----------
MASTER_SLACK_BOT_ID=U0YOURID

# ---------- A channel you control, for kill-switch alerts ----------
SLACK_ADMIN_CHANNEL_ID=C0YOURID

# ---------- AI features ----------
GEMINI_API_KEY=...

# ---------- Local ports (defaults are fine) ----------
NODE_API_URL=http://127.0.0.1:3101
WHATSAPP_REFRESH_PORT=8101
TELEGRAM_REFRESH_PORT=8003
CONTROL_DASHBOARD_PORT=5050
SESSIONS_DIR=sessions
MAX_FILE_SIZE_MB=50

# ---------- Optional: fallback if project_managers is empty ----------
PROJECT_MANAGER_SLACK_ID=U0SOMEONE
```

Two entries deserve attention:

**`MASTER_SLACK_BOT_ID`** is your Slack **user** ID (starts with `U`), not an app ID. It gets invited into every auto-created channel so the bridge can read and reply there. Without it, lead channels are created with no bot in them.

**`SLACK_ADMIN_CHANNEL_ID`** is a channel you can post in. It receives alerts when the kill switch blocks a lead.

### Keep it private

```bash
chmod 600 .env
```

`.env` is gitignored. Confirm before your first commit:

```bash
git check-ignore -q .env && echo "safe: .env is ignored"
```

---

## 12. Log in to WhatsApp and Telegram

Both are one-time. Credentials persist locally and won't need repeating.

### WhatsApp

You'll start the gateway in [§13](#13-run-the-five-processes). On first run it prints a QR code to Terminal 1:

```
🌐 API server listening at http://localhost:3101
📱 QR Code received — scan with WhatsApp on your phone:
```

Scan it with WhatsApp → **Settings → Linked Devices → Link a Device**.

Prefer not to scan? Use a **test number you don't mind disconnecting**. Linking a real work phone kicks you off WhatsApp Web on your phone.

Credentials save to `whatsapp-bot/auth_info/`. You only rescan if you delete that folder.

### Telegram

Terminal 3 will prompt for your phone number and a login code the first time it runs. The session saves to `sessions/bitlink_telegram.session`.

Use a secondary Telegram account if you don't want the bridge on your main one.

---

## 13. Run the five processes

Open **five terminals** in `/path/to/bitlink`. One process each. Leave them running.

### Terminal 1 — WhatsApp gateway

```bash
cd /path/to/bitlink/whatsapp-bot
node service-baileys.js
```

**The directory matters.** The gateway reads `../.env`, so it only finds your config when run from inside `whatsapp-bot/`. Run it from the project root and it'll start with no tokens.

Scan the QR here on first run ([§12](#12-log-in-to-whatsapp-and-telegram)).

### Terminal 2 — WhatsApp bridge

```bash
cd /path/to/bitlink
source venv/bin/activate
python main_whatsapp.py
```

Expect `WhatsApp refresh server listening on port 8101`.

### Terminal 3 — Telegram bridge

```bash
cd /path/to/bitlink
source venv/bin/activate
python main_telegram.py
```

Expect `Telegram refresh server listening on port 8003`. First run will ask for your phone and login code.

### Terminal 4 — Dashboard

```bash
cd /path/to/bitlink
source venv/bin/activate
python control_server.py
```

Open <http://localhost:5050> and log in with your dashboard user from [§7](#7-create-your-dashboard-login).

### Terminal 5 — Slack App Manager

```bash
cd /path/to/bitlink
source venv/bin/activate
python bitlink_manager.py
```

Expect `🚀 BitLink Manager Connected to Slack Socket Mode!`

### Start order

Gateway first, then the rest. The bridges call the gateway on port 3101, so starting it first avoids connection errors during their first seconds. Any that appear anyway recover on their own.

---

## 14. Verify everything works

### Health check

```bash
curl -s http://localhost:5050/api/health | python -m json.tool
```

Expect `"status": "ok"` with `online: true` for `whatsapp_gateway`, `whatsapp_bridge`, `telegram_bridge`.

A bridge showing `online: true` with `emergency_stop.active: true` means someone hit stop — that's healthy-but-paused, not broken.

### Database

```bash
source venv/bin/activate
python -c "import db; print('DB OK' if db.is_available() else 'DB DOWN')"
```

### Slack tokens

```bash
source venv/bin/activate
for v in SLACK_BOT_TOKEN SLACK_BOT_TOKEN_TELEGRAM MANAGER_SLACK_BOT_TOKEN; do
  python -c "
import os
from dotenv import load_dotenv; load_dotenv()
from slack_sdk import WebClient
print('$v ->', WebClient(token=os.getenv('$v')).auth_test().get('user') or 'ERROR')"
done
```

Each should print a bot username. `missing_scope` means you didn't add a required scope — go back to [§10](#10-create-your-three-slack-apps).

### End-to-end lead test

Send a WhatsApp message to your test number from a different phone. You should see:

1. A **private** Slack channel named `sale-wa-<name>-<YYYYMMDD>`
2. Topic: `WhatsApp Lead: <number> | Auto-provisioned by Bitlink Sales`
3. Sales pool members **added to** the channel
4. A DM to each rep
5. A new row in **Sales CRM**, assigned round-robin
6. Replying in that Slack channel reaches WhatsApp with a ✅ reaction

---

## 15. Seed test data

Two tables start empty and both change behavior when empty.

### Sales reps — via the dashboard

**Member Pool → Add Sales Rep**, for each teammate you want in lead channels. Until you add someone, channels are created with nobody in them.

### Project managers — via SQL

There's no dashboard UI for this table yet. Insert directly:

```bash
PGPASSWORD='your-db-password' psql -h localhost -U bitlink -d bitlink <<'SQL'
INSERT INTO project_managers (slack_user_id, name, active)
VALUES ('U0YOURID', 'Your Name', TRUE)
ON CONFLICT DO NOTHING;
SQL
```

This is what makes **handover to delivery** work — it creates a private channel and invites these PMs. With the table empty, it falls back to `PROJECT_MANAGER_SLACK_ID`.

### Snippets and blocks

Create test snippets from **Snippets → Add Snippet**. Create test blocks from **Blocklist**. Both are easier through the UI than SQL.

---

## 16. Everyday commands

### Before running anything

```bash
cd /path/to/bitlink
source venv/bin/activate
```

### What's running

```bash
ps aux | grep -E 'service-baileys|main_whatsapp|main_telegram|control_server|bitlink_manager' \
  | grep -v grep
```

### Stopping everything

`Ctrl+C` in each terminal. To stop them all at once:

```bash
pkill -f service-baileys.js
pkill -f main_whatsapp.py
pkill -f main_telegram.py
pkill -f control_server.py
pkill -f bitlink_manager.py
```

**Nothing restarts automatically locally.** If something crashes, re-run its command.

### Saving logs to files

Useful when you want to grep without scrolling five terminals:

```bash
mkdir -p logs
python main_whatsapp.py 2>&1 | tee -a logs/whatsapp.log
python control_server.py 2>&1 | tee -a logs/dashboard.log
cd whatsapp-bot && node service-baileys.js 2>&1 | tee -a ../logs/gateway.log
```

### Reloading channel mappings after a dashboard change

Dashboard changes trigger this automatically. Run it by hand only when debugging:

```bash
curl -X POST http://localhost:3101/refresh
curl -X POST http://localhost:8101/refresh
curl -X POST http://localhost:8003/refresh
```

### Pulling new code

```bash
git pull
source venv/bin/activate
pip install -r requirements.txt        # in case requirements.txt changed
cd whatsapp-bot && npm install && cd ..
```

Then restart all five processes. The dashboard secret key is regenerated on restart, so **you're logged out** — log in again.

### Resetting your database

Wipes everything local. Only for when your schema is out of sync:

```bash
PGPASSWORD='your-db-password' psql -h localhost -U bitlink -d bitlink -c "DROP DATABASE bitlink;"
PGPASSWORD='your-db-password' psql -h localhost -U bitlink -d postgres -c "CREATE DATABASE bitlink OWNER bitlink;"
PGPASSWORD='your-db-password' psql -h localhost -U bitlink -d bitlink -f schema.sql
```

Then redo [§7](#7-create-your-dashboard-login) — `dashboard_users` is gone.

---

## 17. Troubleshooting

**`DB DOWN` from the health check.**
Postgres isn't running. `pg_isready -h localhost -p 5432`. Start it with `launchctl load -w /Library/LaunchDaemons/postgresql-17.plist`. Check the log line just above `DB DOWN` — it names the real failure.

**Can't log in to the dashboard.**
`dashboard_users` is empty or your hash is wrong. Check:

```bash
PGPASSWORD='your-db-password' psql -h localhost -U bitlink -d bitlink \
  -c "SELECT id FROM dashboard_users;"
```

**Gateway has no Slack token, or listens on 3101 when you set a port.**
You ran it from the project root. It must run from `whatsapp-bot/` — [§13](#13-run-the-five-processes).

**`missing_scope` from a Slack call.**
A required scope is missing on that app. Compare the error against the scope table in [§10](#10-create-your-three-slack-apps).

**Nothing happens when I get a WhatsApp message.**
Check in order: is Terminal 1 logged in (no QR prompt = connected)? Does `MASTER_SLACK_BOT_ID` match your real user ID? Is the kill switch or triage mode on in dashboard **Settings**? Both halt channel creation by design.

**Messages processed twice, Slack rate-limit warnings.**
A legacy file is running alongside the real bridges. `backup_mainwhatsapp.py`, `tele_backup.py`, and `management_server.py` each open their own Socket Mode connection with the same app token. Kill them.

**Tests fail with `ModuleNotFoundError`.**
Your venv isn't active. `source venv/bin/activate` first.

**WhatsApp asks for a QR scan every time.**
`whatsapp-bot/auth_info/` was deleted. Scan once more.

**Telegram asks for the login code every time.**
`sessions/bitlink_telegram.session` was deleted. Run Terminal 3 in the foreground to complete the login.

**Port already in use.**

```bash
lsof -nP -iTCP:5050 -sTCP:LISTEN
lsof -nP -iTCP:8101 -sTCP:LISTEN
```

Kill the old process, or change the port in `.env`. If you change the dashboard port, use `CONTROL_DASHBOARD_PORT` — **never `PORT`**, which belongs to the gateway.

---

## 18. Local-only safety notes

- **Your `.env` is yours.** Never commit it, never paste tokens into Slack or GitHub issues.
- **Never commit session files.** `sessions/` and `whatsapp-bot/auth_info/` contain live logins to your WhatsApp and Telegram accounts. They're gitignored — verify with `git status` before committing.
- **Use a test WhatsApp number.** Linking a real work phone will sign you out of WhatsApp Web on that device.
- **Your local database is yours alone.** It contains whatever leads you test with — including real client conversations. Don't point it at production data, and don't copy production dumps into it without thinking.
- **Don't run the legacy files.** `backup_mainwhatsapp.py`, `tele_backup.py`, `management_server.py` — duplicate Socket Mode connections, duplicate messages.
- **`GEMINI_API_KEY` costs money.** AI drafting and insights call the Gemini API on every message. Use a restricted key for local testing.

---

*Related: `DEPLOYMENT_GUIDE.md` (production server), `CHANGES_SALES_CRM.md` (feature change log), `PROJECT_OVERVIEW.md` (architecture).*
