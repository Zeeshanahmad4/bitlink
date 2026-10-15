#!/bin/bash
# =============================================================
# BitLink Sales CRM — Fast 1-Click Deployment Script via rsync
# =============================================================
# Usage:
#   ./deploy.sh <SERVER_IP>
# Or set SERVER_IP as an environment variable or edit below.

SERVER_IP="${1:-$SERVER_IP}"

if [ -z "$SERVER_IP" ]; then
  echo "❌ Error: SERVER_IP not provided."
  echo "Usage: ./deploy.sh <SERVER_IP>"
  echo "Example: ./deploy.sh 123.45.67.89"
  exit 1
fi

SERVER="root@$SERVER_IP"
TARGET_DIR="/opt/bitlink-sales"

echo "============================================================"
echo "🚀 Deploying BitLink Sales CRM to $SERVER:$TARGET_DIR"
echo "============================================================"

# Step 1: Ensure target directory exists on server
ssh "$SERVER" "mkdir -p $TARGET_DIR"

# Step 2: Sync code files excluding local dependencies, secrets, & sessions
echo "📦 Syncing code files via rsync..."
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
  --exclude='sent_whatsapp_alerts.json' \
  --exclude='sent_telegram_alerts.json' \
  ./ "$SERVER:$TARGET_DIR/"

# Step 3: Sync .env to server
echo "📄 Syncing .env to $SERVER:$TARGET_DIR/.env..."
scp .env "$SERVER:$TARGET_DIR/.env"

# Step 4: Rebuild and start Docker containers
echo "🔄 Rebuilding & restarting Docker containers on server..."
ssh "$SERVER" "cd $TARGET_DIR && docker compose up -d --build"

echo "============================================================"
echo "✅ Deployment completed successfully!"
echo "============================================================"
