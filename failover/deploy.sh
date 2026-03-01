#!/usr/bin/env bash
set -euo pipefail

# Deploy Nexus failover runner to fox-n1
# Usage: ./deploy.sh [fox-n1-host]

FOX_N1="${1:-root@fox-n0}"
REMOTE_DIR="/opt/nexus-failover"
NEXUS_SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Nexus Failover Deployment ==="
echo "Source:  $NEXUS_SRC"
echo "Target:  $FOX_N1:$REMOTE_DIR"
echo ""

# 1. Create remote directory structure
echo "[1/7] Creating remote directories..."
ssh "$FOX_N1" "mkdir -p $REMOTE_DIR/{nexus,state}"

# 2. Rsync nexus code (observers + failover + config + base modules)
echo "[2/7] Syncing nexus code..."
rsync -az --delete \
    --include='observers/' \
    --include='observers/*.py' \
    --include='observers/.state/' \
    --include='failover/' \
    --include='failover/*.py' \
    --include='config.py' \
    --include='engine.py' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.git/' \
    --exclude='node_modules/' \
    --exclude='.env' \
    "$NEXUS_SRC/" "$FOX_N1:$REMOTE_DIR/nexus/"

# 3. Copy .env (use failover template if main doesn't exist yet)
echo "[3/7] Deploying environment config..."
if ssh "$FOX_N1" "test -f $REMOTE_DIR/.env"; then
    echo "  .env already exists on fox-n1, preserving"
else
    echo "  Deploying .env from failover template"
    scp "$NEXUS_SRC/failover/.env.failover" "$FOX_N1:$REMOTE_DIR/.env"
fi

# 4. Copy RSS feeds config
echo "[4/7] Copying RSS feeds config..."
if [ -f "$HOME/.config/puretensor/rss_feeds.conf" ]; then
    scp "$HOME/.config/puretensor/rss_feeds.conf" "$FOX_N1:$REMOTE_DIR/rss_feeds.conf"
else
    echo "  WARNING: rss_feeds.conf not found locally"
fi

# 5. Seed state files from TC (if they exist and remote state is empty)
echo "[5/7] Seeding state files..."
TC_STATE_DIR="$NEXUS_SRC/observers/.state"
if [ -d "$TC_STATE_DIR" ]; then
    REMOTE_STATE_COUNT=$(ssh "$FOX_N1" "ls $REMOTE_DIR/state/*.json 2>/dev/null | wc -l" || echo "0")
    if [ "$REMOTE_STATE_COUNT" = "0" ]; then
        echo "  Seeding from TC state..."
        rsync -az "$TC_STATE_DIR/" "$FOX_N1:$REMOTE_DIR/state/"
    else
        echo "  Remote state already has $REMOTE_STATE_COUNT files, preserving"
    fi
fi

# 6. Install systemd units
echo "[6/7] Installing systemd units..."
scp "$NEXUS_SRC/failover/nexus-failover.service" "$FOX_N1:/etc/systemd/system/"
scp "$NEXUS_SRC/failover/nexus-failover.timer" "$FOX_N1:/etc/systemd/system/"
ssh "$FOX_N1" "systemctl daemon-reload && systemctl enable nexus-failover.timer && systemctl start nexus-failover.timer"

# 7. Set up daily state sync cron on TC (sync at 18:00 before typical shutdown)
echo "[7/7] Setting up daily state sync cron..."
CRON_LINE="0 18 * * * rsync -az $NEXUS_SRC/observers/.state/ $FOX_N1:$REMOTE_DIR/state/ 2>/dev/null"
if crontab -l 2>/dev/null | grep -qF "nexus-failover"; then
    echo "  State sync cron already exists"
else
    (crontab -l 2>/dev/null; echo "# nexus-failover: daily state sync to fox-n1"; echo "$CRON_LINE") | crontab -
    echo "  Added state sync cron (daily at 18:00)"
fi

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Verify:"
echo "  ssh $FOX_N1 'systemctl status nexus-failover.timer'"
echo "  ssh $FOX_N1 'systemctl list-timers nexus-failover.timer'"
echo ""
echo "Manual test:"
echo "  ssh $FOX_N1 'systemctl start nexus-failover.service'"
echo "  ssh $FOX_N1 'journalctl -u nexus-failover -f'"
