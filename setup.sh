#!/bin/bash
# ============================================================
# 4H Sweep Algo BTCUSD — PRODUCTION Setup
# AWS Lightsail Ubuntu 22.04+ | Run as root
# Installs to /opt/sweep-algo-production
# Uses .venv as virtual environment name
# ============================================================

set -e

APP_DIR="/opt/sweep-algo-production"
APP_USER="algo"
PYTHON_VERSION="python3"
VENV_DIR="$APP_DIR/.venv"
REPO_URL="${REPO_URL:-}"   # Optional: REPO_URL=https://token@github.com/... sudo bash setup.sh

echo "================================================"
echo "  4H Sweep Algo BTCUSD — PRODUCTION Setup"
echo "  Target : $APP_DIR"
echo "  Venv   : $VENV_DIR"
echo "================================================"

# ── 1. System packages ────────────────────────────────────
echo "[1/8] Installing system packages..."
apt-get update -y
apt-get install -y python3 python3-pip python3-venv cron logrotate git curl

# ── 2. Create app user ────────────────────────────────────
echo "[2/8] Creating app user '$APP_USER'..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$APP_USER"
    echo "  User '$APP_USER' created"
else
    echo "  User '$APP_USER' already exists"
fi

# ── 3. Clone repo OR copy local files ─────────────────────
echo "[3/8] Setting up application directory..."
mkdir -p "$APP_DIR/logs"

if [ -n "$REPO_URL" ]; then
    echo "  Cloning from $REPO_URL ..."
    TMP=$(mktemp -d)
    git clone "$REPO_URL" "$TMP/repo"
    SRC="$TMP/repo/4hr Sweep Algo BTCUSD/production"
    [ -d "$SRC" ] && cp "$SRC"/*.py "$APP_DIR/" && cp "$SRC/requirements.txt" "$APP_DIR/" && cp "$SRC/.env.example" "$APP_DIR/"
    rm -rf "$TMP"
    echo "  Files cloned from GitHub"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cp "$SCRIPT_DIR"/*.py "$APP_DIR/"
    cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"
    cp "$SCRIPT_DIR/.env.example" "$APP_DIR/"
    echo "  Files copied from $SCRIPT_DIR"
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ── 4. Python virtual environment (.venv) ─────────────────
echo "[4/8] Creating Python .venv at $VENV_DIR ..."
sudo -u "$APP_USER" $PYTHON_VERSION -m venv "$VENV_DIR"
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  Dependencies installed in .venv"

# ── 5. Setup .env ──────────────────────────────────────────
echo "[5/8] Setting up .env file..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
    echo ""
    echo "  *** ACTION REQUIRED: Add your PRODUCTION API keys ***"
    echo "  Run: nano $APP_DIR/.env"
    echo "  Get keys: https://india.delta.exchange -> Account -> API Keys"
    echo ""
else
    echo "  .env already exists — not overwriting"
fi
chmod 600 "$APP_DIR/.env"
chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"

# ── 6. Cron jobs ───────────────────────────────────────────
echo "[6/8] Installing cron jobs..."
CRON_FILE="/etc/cron.d/sweep-algo-production"
cat > "$CRON_FILE" << 'CRONEOF'
# 4H Sweep Algo BTCUSD PRODUCTION (all times UTC, IST = UTC+5:30)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Start algo at 01:20 IST = 19:50 UTC daily
50 19 * * * algo /opt/sweep-algo-production/.venv/bin/python /opt/sweep-algo-production/sweep_algo.py >> /opt/sweep-algo-production/logs/cron.log 2>&1

# Health check every 5 min from 01:30-21:35 IST (20:00-16:05 UTC)
*/5 20-23 * * * algo /opt/sweep-algo-production/.venv/bin/python /opt/sweep-algo-production/health_check.py >> /opt/sweep-algo-production/logs/health.log 2>&1
*/5 0-16  * * * algo /opt/sweep-algo-production/.venv/bin/python /opt/sweep-algo-production/health_check.py >> /opt/sweep-algo-production/logs/health.log 2>&1

# Clean logs older than 30 days
0 2 * * * algo find /opt/sweep-algo-production/logs -name "*.log" -mtime +30 -delete 2>/dev/null
CRONEOF
chmod 644 "$CRON_FILE"
echo "  Cron installed: $CRON_FILE"

# ── 7. Log rotation ────────────────────────────────────────
echo "[7/8] Configuring logrotate..."
cat > "/etc/logrotate.d/sweep-algo-production" << 'LOGEOF'
/opt/sweep-algo-production/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 640 algo algo
}
LOGEOF
echo "  Logrotate configured"

# ── 8. Connectivity test ───────────────────────────────────
echo "[8/8] Testing connectivity to Delta Exchange India Production..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://api.india.delta.exchange/v2/products?contract_types=call_options&page_size=1" \
    --max-time 10 || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "  Delta Exchange Production reachable (HTTP $HTTP_CODE)"
else
    echo "  WARNING: Got HTTP $HTTP_CODE — check network/firewall"
fi

echo ""
echo "================================================"
echo "  PRODUCTION SETUP COMPLETE!"
echo "================================================"
echo ""
echo "  NEXT STEPS:"
echo "  1. Add API keys  : nano $APP_DIR/.env"
echo "  2. Test run      : sudo -u $APP_USER $VENV_DIR/bin/python $APP_DIR/sweep_algo.py"
echo "  3. Live log      : tail -f $APP_DIR/logs/sweep_algo_\$(date +%Y-%m-%d).log"
echo "  4. Trade history : cat $APP_DIR/logs/trade_history.csv"
echo "  5. Events log    : tail -50 $APP_DIR/logs/events.csv"
echo ""
echo "  Activate .venv : source $VENV_DIR/bin/activate"
echo "  Cron auto-starts at 01:20 IST daily."
echo "================================================"
