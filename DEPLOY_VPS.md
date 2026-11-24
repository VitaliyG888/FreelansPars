# VPS Deployment Guide — Freelance Bot

Production deployment guide for Ubuntu/Debian VPS with systemd service, automatic restart, log rotation, and security hardening.

---

## Prerequisites

- **VPS:** Ubuntu 22.04+ or Debian 11+ with root access
- **Python:** 3.11+ (3.13 recommended)
- **RAM:** 2 GB minimum (4 GB recommended for browser instances)
- **Disk:** 10 GB minimum
- **Network:** Stable internet, outbound HTTPS allowed
- **Optional:** Russian residential proxy for Avito scraping
- **Optional:** LLM API key (OpenAI/OpenRouter) for quality messages

---

## Architecture

```
/opt/freelance-bot/          # application directory (non-root user)
├── venv/                    # isolated Python environment
├── src/                     # code
├── config.yaml              # runtime config
├── .env                     # secrets (chmod 600)
├── var/
│   ├── freelance.db         # SQLite database
│   ├── outbox/              # dry-run messages
│   └── logs/                # application logs
└── systemd/
    └── freelance-bot.service

User: freelance-bot (no login, no password)
Service: systemd unit with auto-restart
Logs: journalctl + file rotation via logrotate
Firewall: ufw blocks inbound except SSH
```

---

## Step 1: Create Non-Root User

```bash
# As root or sudo user
sudo adduser --system --group --no-create-home --disabled-login freelance-bot
sudo usermod -s /usr/sbin/nologin freelance-bot
```

---

## Step 2: Install System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+ and build tools
sudo apt install -y python3 python3-pip python3-venv python3-dev \
    build-essential libssl-dev libffi-dev \
    git curl wget sqlite3

# Install Playwright dependencies for Chromium (headless browser)
sudo apt install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0

# Verify Python version (must be 3.11+)
python3 --version
```

---

## Step 3: Deploy Application

```bash
# Create application directory
sudo mkdir -p /opt/freelance-bot
sudo chown freelance-bot:freelance-bot /opt/freelance-bot

# Clone or upload the project
# Option A: Clone from Git (if private, set up deploy key first)
sudo -u freelance-bot git clone https://github.com/YOUR_ORG/freelance-bot.git /opt/freelance-bot

# Option B: Upload via rsync/scp (from local machine)
# rsync -avz --exclude='venv' --exclude='var' --exclude='.git' \
#   ./ user@YOUR_VPS_IP:/tmp/freelance-bot/
# sudo mv /tmp/freelance-bot/* /opt/freelance-bot/
# sudo chown -R freelance-bot:freelance-bot /opt/freelance-bot

# Switch to application directory
cd /opt/freelance-bot
```

---

## Step 4: Set Up Python Environment

```bash
# Create virtual environment as freelance-bot user
sudo -u freelance-bot python3 -m venv /opt/freelance-bot/venv

# Activate venv and install dependencies
sudo -u freelance-bot /opt/freelance-bot/venv/bin/pip install --upgrade pip
sudo -u freelance-bot /opt/freelance-bot/venv/bin/pip install -e ".[browser,dev]"

# Install Playwright Chromium
sudo -u freelance-bot /opt/freelance-bot/venv/bin/playwright install chromium

# Verify installation
sudo -u freelance-bot /opt/freelance-bot/venv/bin/python -m freelance_bot.cli version
# Expected output: freelance-bot 0.4.0
```

---

## Step 5: Create .env File

```bash
# Create .env from template
sudo -u freelance-bot cp /opt/freelance-bot/.env.example /opt/freelance-bot/.env

# Edit secrets (use nano or vim)
sudo -u freelance-bot nano /opt/freelance-bot/.env
```

**Paste the following and replace placeholders:**

```bash
# --- Secrets (never commit .env) ---
FREELANCE_BOT_ENV=production
FREELANCE_BOT_CONFIG=config.yaml

# LLM (OpenAI-compatible endpoint; works with OpenAI, OpenRouter, local vLLM)
# Leave empty to use deterministic Russian fallback generator
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-YOUR-OPENAI-KEY-HERE
LLM_MODEL=gpt-4o-mini

# CAPTCHA solving (optional; only used when a solver is reached)
CAPSOLVER_API_KEY=
TWOCAPTCHA_API_KEY=

# Russian residential proxy pool (required for Avito scraping)
# Format: http://user:pass@host:port,http://user:pass@host2:port
# Leave empty to run without Avito (datacenter IPs are blocked by Qrator)
RU_PROXY_URLS=

# Storage / output
FREELANCE_BOT_DB=/opt/freelance-bot/var/freelance.db
FREELANCE_BOT_OUTBOX=/opt/freelance-bot/var/outbox
```

**Secure the .env file:**

```bash
sudo chmod 600 /opt/freelance-bot/.env
sudo chown freelance-bot:freelance-bot /opt/freelance-bot/.env
```

---

## Step 6: Configure Runtime

```bash
# Create var/ directories
sudo -u freelance-bot mkdir -p /opt/freelance-bot/var/outbox
sudo -u freelance-bot mkdir -p /opt/freelance-bot/var/logs

# Edit config.yaml if needed (scoring weights, categories, etc.)
sudo -u freelance-bot nano /opt/freelance-bot/config.yaml

# Run preflight check
sudo -u freelance-bot /opt/freelance-bot/venv/bin/python -m freelance_bot.cli preflight
# Expected: all checks 'ok' or 'warn' (LLM/proxy/CAPTCHA are optional)
```

---

## Step 7: Create systemd Service

```bash
# Create systemd unit file
sudo nano /etc/systemd/system/freelance-bot.service
```

**Paste the following:**

```ini
[Unit]
Description=Freelance Bot — Russian marketplace aggregator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=freelance-bot
Group=freelance-bot
WorkingDirectory=/opt/freelance-bot
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONIOENCODING=utf-8"
EnvironmentFile=/opt/freelance-bot/.env

# Run the bot in continuous mode: cycles every 5 minutes
# Change --interval to adjust cycle frequency (in seconds)
ExecStart=/opt/freelance-bot/venv/bin/python -m freelance_bot.cli run \
    --top 10 \
    --interval 300

# Automatic restart on failure
Restart=on-failure
RestartSec=10s

# Resource limits (adjust for your VPS capacity)
MemoryMax=1.5G
CPUQuota=150%

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/freelance-bot/var

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=freelance-bot

[Install]
WantedBy=multi-user.target
```

**Reload systemd and enable service:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable freelance-bot.service
```

---

## Step 8: Start and Monitor Service

```bash
# Start the bot
sudo systemctl start freelance-bot.service

# Check status
sudo systemctl status freelance-bot.service

# View live logs (follow mode)
sudo journalctl -u freelance-bot.service -f

# View last 100 lines
sudo journalctl -u freelance-bot.service -n 100 --no-pager

# Check for errors
sudo journalctl -u freelance-bot.service -p err -n 50
```

---

## Step 9: Log Rotation

systemd journal rotates logs automatically, but you can configure file-based rotation:

```bash
# Create log directory
sudo -u freelance-bot mkdir -p /opt/freelance-bot/var/logs

# Create logrotate config
sudo nano /etc/logrotate.d/freelance-bot
```

**Paste the following:**

```
/opt/freelance-bot/var/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 freelance-bot freelance-bot
    sharedscripts
    postrotate
        systemctl reload freelance-bot.service > /dev/null 2>&1 || true
    endscript
}
```

---

## Step 10: Database Backup

```bash
# Create backup script
sudo nano /opt/freelance-bot/backup.sh
```

**Paste the following:**

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/opt/freelance-bot/var/backups"
DB_PATH="/opt/freelance-bot/var/freelance.db"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

# SQLite backup (safe for running database)
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/freelance_$TIMESTAMP.db'"

# Keep only last 7 days of backups
find "$BACKUP_DIR" -name "freelance_*.db" -mtime +7 -delete

echo "Backup completed: $BACKUP_DIR/freelance_$TIMESTAMP.db"
```

**Make executable and schedule daily backup:**

```bash
sudo chmod +x /opt/freelance-bot/backup.sh
sudo chown freelance-bot:freelance-bot /opt/freelance-bot/backup.sh

# Add to crontab (as freelance-bot user)
sudo -u freelance-bot crontab -e
```

**Add the following line (daily at 3 AM):**

```
0 3 * * * /opt/freelance-bot/backup.sh >> /opt/freelance-bot/var/logs/backup.log 2>&1
```

---

## Step 11: Firewall Configuration

```bash
# Install ufw if not present
sudo apt install -y ufw

# Default policies: deny incoming, allow outgoing
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH (adjust port if non-standard)
sudo ufw allow 22/tcp

# Enable firewall
sudo ufw --force enable

# Verify rules
sudo ufw status verbose
```

**Expected output:**

```
Status: active
To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
```

---

## Step 12: Health Check Script

```bash
# Create healthcheck script
sudo nano /opt/freelance-bot/healthcheck.sh
```

**Paste the following:**

```bash
#!/bin/bash
set -euo pipefail

SERVICE="freelance-bot.service"

# Check if service is running
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "ERROR: $SERVICE is not running"
    exit 1
fi

# Check if database is accessible
DB_PATH="/opt/freelance-bot/var/freelance.db"
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found at $DB_PATH"
    exit 1
fi

# Check if recent jobs were collected (last 24 hours)
RECENT_JOBS=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM jobs WHERE seen_at > datetime('now', '-24 hours');")
if [ "$RECENT_JOBS" -eq 0 ]; then
    echo "WARNING: No jobs collected in the last 24 hours"
    exit 2
fi

echo "OK: $SERVICE is healthy, $RECENT_JOBS jobs collected in last 24h"
exit 0
```

**Make executable and schedule hourly check:**

```bash
sudo chmod +x /opt/freelance-bot/healthcheck.sh
sudo chown freelance-bot:freelance-bot /opt/freelance-bot/healthcheck.sh

# Add to crontab (as freelance-bot user)
sudo -u freelance-bot crontab -e
```

**Add the following line (hourly check):**

```
0 * * * * /opt/freelance-bot/healthcheck.sh >> /opt/freelance-bot/var/logs/health.log 2>&1
```

---

## Step 13: Rollback Procedure

In case of issues after deployment:

```bash
# Stop the service
sudo systemctl stop freelance-bot.service

# Restore database from backup (replace TIMESTAMP)
sudo -u freelance-bot cp /opt/freelance-bot/var/backups/freelance_TIMESTAMP.db \
    /opt/freelance-bot/var/freelance.db

# Rollback code (if using Git)
cd /opt/freelance-bot
sudo -u freelance-bot git reset --hard HEAD~1

# Or restore from previous rsync snapshot
# sudo rsync -avz --delete /backup/freelance-bot-snapshot/ /opt/freelance-bot/

# Restart service
sudo systemctl start freelance-bot.service

# Verify status
sudo systemctl status freelance-bot.service
sudo journalctl -u freelance-bot.service -n 50
```

---

## Maintenance Commands

```bash
# View service status
sudo systemctl status freelance-bot.service

# Stop/start/restart service
sudo systemctl stop freelance-bot.service
sudo systemctl start freelance-bot.service
sudo systemctl restart freelance-bot.service

# View live logs
sudo journalctl -u freelance-bot.service -f

# Check database size
du -h /opt/freelance-bot/var/freelance.db

# Check outbox messages
sudo -u freelance-bot /opt/freelance-bot/venv/bin/python -m freelance_bot.cli outbox

# Manually trigger a cycle (dry-run)
sudo -u freelance-bot /opt/freelance-bot/venv/bin/python -m freelance_bot.cli run --top 10

# Update code (if using Git)
cd /opt/freelance-bot
sudo -u freelance-bot git pull
sudo systemctl restart freelance-bot.service

# Update dependencies
sudo -u freelance-bot /opt/freelance-bot/venv/bin/pip install --upgrade -e ".[browser,dev]"
sudo systemctl restart freelance-bot.service
```

---

## Monitoring & Alerts

For production monitoring, integrate with:

- **Logs:** `journalctl -u freelance-bot.service --since "1 hour ago"`
- **Disk:** `df -h /opt/freelance-bot/var`
- **Memory:** `systemctl status freelance-bot.service` (check MemoryCurrent)
- **Alerts:** Set up email/Telegram notifications on service failures:

```bash
# Install mailutils for email alerts
sudo apt install -y mailutils

# Create alert script
sudo nano /opt/freelance-bot/alert.sh
```

```bash
#!/bin/bash
SERVICE="freelance-bot.service"
EMAIL="your-email@example.com"

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "Service $SERVICE is DOWN on $(hostname)" | mail -s "Alert: $SERVICE Failed" "$EMAIL"
fi
```

```bash
sudo chmod +x /opt/freelance-bot/alert.sh
# Add to crontab: */5 * * * * /opt/freelance-bot/alert.sh
```

---

## Security Checklist

- ✅ Non-root user (`freelance-bot`) with no login shell
- ✅ `.env` file with `chmod 600` (secrets not world-readable)
- ✅ systemd security hardening (`ProtectSystem=strict`, `NoNewPrivileges`)
- ✅ Firewall enabled (ufw), only SSH port open
- ✅ No inbound HTTP/HTTPS ports (bot is polling-based, not webhook)
- ✅ Database backups automated and tested
- ✅ Log rotation configured
- ✅ Health checks scheduled
- ✅ Auto-restart on failure

---

## Dry-Run vs Live Mode

**By default, the bot runs in dry-run mode:**
- Messages are written to `var/outbox/` as Markdown files
- No messages are sent to marketplaces
- Safe for testing and validation

**To enable live sending:**
1. Implement a `DeliveryTransport` subclass in `src/freelance_bot/sender.py`
2. Wire it up in `OutboxSender.__init__`
3. Set `messaging.require_human_approval: false` in `config.yaml` (⚠️ not recommended)
4. The bot will call your transport for each message

**Current design is intentionally conservative:** no authenticated transport is bundled to prevent accidental mass-messaging.

---

## Troubleshooting

**Service won't start:**
```bash
sudo journalctl -u freelance-bot.service -n 100 --no-pager
# Check for Python import errors, missing dependencies, or invalid config
```

**No jobs collected:**
```bash
# Check network connectivity
curl -I https://www.fl.ru/projects/
# Check if platforms are enabled in config.yaml
sudo -u freelance-bot /opt/freelance-bot/venv/bin/python -m freelance_bot.cli platforms
# Check logs for anti-bot blocks
sudo journalctl -u freelance-bot.service -p warning -n 50
```

**High memory usage:**
```bash
# Playwright Chromium instances can use 200-300 MB each
# Adjust MemoryMax in systemd unit or reduce browser-based platforms
# Check current usage:
systemctl status freelance-bot.service | grep Memory
```

**Database locked errors:**
```bash
# Ensure only one bot instance is running
pgrep -fa freelance_bot
# Kill orphan processes if found
sudo pkill -f freelance_bot
sudo systemctl restart freelance-bot.service
```

---

## Production Checklist

Before going live:

- [ ] VPS provisioned (Ubuntu 22.04+, 2+ GB RAM, 10+ GB disk)
- [ ] Non-root user created and configured
- [ ] Python 3.11+ installed and verified
- [ ] Application deployed to `/opt/freelance-bot`
- [ ] Dependencies installed in venv
- [ ] `.env` file created with secrets (chmod 600)
- [ ] `config.yaml` reviewed and customized
- [ ] Preflight checks passed
- [ ] systemd service created and enabled
- [ ] Service started and running (`systemctl status`)
- [ ] Logs streaming without errors (`journalctl -f`)
- [ ] Database backups scheduled (cron)
- [ ] Log rotation configured (logrotate)
- [ ] Firewall enabled (ufw)
- [ ] Health checks scheduled (cron)
- [ ] Rollback procedure tested
- [ ] Monitoring/alerts configured (optional)

---

## Support & Updates

- **Logs:** `sudo journalctl -u freelance-bot.service -f`
- **Config:** `/opt/freelance-bot/config.yaml`
- **Database:** `/opt/freelance-bot/var/freelance.db`
- **Outbox:** `/opt/freelance-bot/var/outbox/`
- **README:** `/opt/freelance-bot/README.md`
- **Report:** `/opt/freelance-bot/REPORT.md`

For updates, pull the latest code and restart:

```bash
cd /opt/freelance-bot
sudo -u freelance-bot git pull
sudo systemctl restart freelance-bot.service
```

---

**Production deployment complete.** The bot will run continuously, collecting jobs every 5 minutes (configurable via `--interval`) and writing dry-run messages to `var/outbox/`. Review the outbox periodically and implement a delivery transport when ready to send live messages.
