# BitLink Database Backup & Reset Plan (Workspace Migration)

This document provides the exact, production-ready sequence of commands to:
1. **Take a full database backup** on your production server.
2. **Transfer the backup safely** to your local machine.
3. **Wipe out all workspace-specific data and mappings** while **preserving your `dashboard_users`** (login credentials remain intact).
4. **Clear file-based state & caches** so old channels and messages never cross over.
5. **Restart services** so the new Slack workspace pairs cleanly.

---

## Architecture & Safety Overview

* **Tables Preserved**: 
  - `dashboard_users`: User logins and password hashes remain untouched.
* **Tables Wiped (`RESTART IDENTITY CASCADE`)**:
  - `client_mappings`: Old JID ↔ old Slack channel pairings.
  - `sales_leads`: Old client leads mapped to obsolete Slack channels & rep IDs.
  - `sales_pool`, `project_managers`, `developers`: Obsolete Slack User IDs (`U0...`) from the previous workspace.
  - `developer_channels`, `alert_channels`: Obsolete Slack channel IDs.
  - `critical_alerts`, `alert_details`: Alert histories referencing obsolete Slack channels.
  - `blocklist`, `snippets`, `settings`: Reset to default state so no old workspace references linger.
* **Local State Cleaned**: Message mapping JSONs and outbox queues.

---

## Phase 1: Server-Side Backup

Log in to your server via SSH:
```bash
ssh root@<YOUR_SERVER_IP>
```

Navigate to your BitLink project directory (default: `/opt/bitlink-sales`):
```bash
cd /opt/bitlink-sales
```

### 1. Create a timestamped SQL backup
Run `pg_dump` via Docker Compose (using `-T` to prevent TTY character corruption):

```bash
# Ensure backups folder exists
mkdir -p backups

# Export full database dump
BACKUP_FILE="backups/bitlink_full_backup_$(date +%Y%m%d_%H%M%S).sql"
docker compose exec -T db pg_dump -U bitlink bitlink > "$BACKUP_FILE"

# Verify the backup was created and has non-zero size
ls -lh "$BACKUP_FILE"
```

*(Optional) Create a binary custom-format dump for extra safety:*
```bash
BACKUP_DUMP="backups/bitlink_full_backup_$(date +%Y%m%d_%H%M%S).dump"
docker compose exec -T db pg_dump -U bitlink -F c bitlink > "$BACKUP_DUMP"
ls -lh "$BACKUP_DUMP"
```

---

## Phase 2: Transfer Backup to Local Machine

Open a **new terminal tab on your local Mac** (do not run this on the server).

Navigate to your local repository directory:
```bash
cd /Users/apple/Desktop/BitLink-Sales/bitlink
mkdir -p backups
```

### Download the backup files via SCP or Rsync:
Replace `<YOUR_SERVER_IP>` with your actual server IP.

```bash
# Option A: Using SCP
scp root@<YOUR_SERVER_IP>:/opt/bitlink-sales/backups/bitlink_full_backup_*.sql ./backups/

# Option B: Using Rsync (shows progress)
rsync -avzP root@<YOUR_SERVER_IP>:/opt/bitlink-sales/backups/ ./backups/
```

Verify locally:
```bash
ls -lh backups/
```

---

## Phase 3: Wipe Database (Keeping Dashboard Users)

Switch back to your **server SSH session**:
```bash
cd /opt/bitlink-sales
```

### 1. Truncate tables and reset ID sequences
Execute the SQL command to wipe all lead, mapping, and workspace tables while keeping `dashboard_users`:

```bash
docker compose exec -T db psql -U bitlink -d bitlink -c "
TRUNCATE TABLE 
    client_mappings,
    sales_leads,
    sales_pool,
    project_managers,
    developers,
    developer_channels,
    alert_channels,
    critical_alerts,
    alert_details,
    blocklist,
    snippets,
    settings
RESTART IDENTITY CASCADE;
"
```

### 2. Verify `dashboard_users` is preserved and other tables are empty:
```bash
docker compose exec -T db psql -U bitlink -d bitlink -c "
SELECT 'dashboard_users' AS table_name, count(*) FROM dashboard_users
UNION ALL SELECT 'client_mappings', count(*) FROM client_mappings
UNION ALL SELECT 'sales_leads', count(*) FROM sales_leads
UNION ALL SELECT 'sales_pool', count(*) FROM sales_pool;
"
```
*Expected result*: `dashboard_users` shows `> 0`, while all other tables show `0`.

---

## Phase 4: Clear File-Based State & Caches on Server

BitLink stores temporary mapping tables and message queues on disk. Clear them so old message IDs and threads do not conflict:

```bash
cd /opt/bitlink-sales

# Reset JSON message maps
echo "{}" > telegram_message_map.json
echo "{}" > telegram_reverse_map.json
echo "[]" > sent_telegram_alerts.json

# Reset message outbox queues
> whatsapp_outbox.jsonl
> telegram_outbox.jsonl
> critical_alerts_fallback.log
```

---

## Phase 5: Ensure `.env` is Updated & Restart Containers

1. Check that `/opt/bitlink-sales/.env` has the new tokens for the new workspace:
   - `SLACK_BOT_TOKEN`
   - `SLACK_APP_TOKEN`
   - `SLACK_BOT_TOKEN_TELEGRAM`
   - `SLACK_APP_TOKEN_TELEGRAM`
   - `MANAGER_SLACK_BOT_TOKEN`
   - `MANAGER_SLACK_APP_TOKEN`
   - `MASTER_SLACK_BOT_ID`

2. Restart all containers so memory caches are purged and new configurations are loaded:
```bash
docker compose down
docker compose up -d
```

3. Check container logs to verify clean boot:
```bash
docker compose logs -f --tail=50
```

---

## Phase 6: Post-Reset Setup (In the New Workspace)

Once the containers are running:
1. **Log in to Dashboard**: Navigate to your dashboard URL and log in using your existing dashboard credentials.
2. **Add Sales Reps**: Go to **Member Pool** and add the new Slack user IDs for your sales team.
3. **Verify Health**:
   ```bash
   curl -s http://127.0.0.1:5050/api/health | python3 -m json.tool
   ```
4. **Send a Test Message**: Inbound messages from WhatsApp/Telegram will now auto-provision new channels cleanly in the new Slack workspace!
