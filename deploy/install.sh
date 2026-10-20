#!/bin/bash
# One-command installer for the stock alert bot on a stock Ubuntu/Debian server.
# Idempotent: safe to re-run after copying updated files (it does NOT touch .env
# contents you already filled in, and it re-installs systemd units + cron).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
USER_NAME="$(id -un)"
SYSTEMCTL="$(command -v systemctl)"

echo "==> stockbot installer (project: $PROJECT_DIR, user: $USER_NAME)"

# ---- 1. python3 + venv -------------------------------------------------------
if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "==> installing python3-venv"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3-venv
  else
    echo "ERROR: no apt-get and python3 venv module missing. Install python3-venv manually."
    exit 1
  fi
fi

# ---- 2. virtualenv + deps ----------------------------------------------------
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "==> creating .venv"
  python3 -m venv "$PROJECT_DIR/.venv"
fi
echo "==> installing requirements"
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# ---- 3. .env -----------------------------------------------------------------
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "==> creating .env from .env.example"
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
fi
if ! grep -q '^DASHBOARD_TOKEN=' "$PROJECT_DIR/.env"; then
  # The dashboard is token-gated; the example file predates this key.
  printf '\n# Password for the web dashboard (http://<server>:8000/?token=...).\n# Pick a long random string, e.g. `openssl rand -hex 24`.\nDASHBOARD_TOKEN=\n' >> "$PROJECT_DIR/.env"
fi
chmod 600 "$PROJECT_DIR/.env"
mkdir -p "$PROJECT_DIR/logs"

MISSING_KEYS=""
for k in ALPACA_KEY ALPACA_SECRET NTFY_TOPIC DASHBOARD_TOKEN; do
  v=$(grep -E "^${k}=" "$PROJECT_DIR/.env" | cut -d= -f2- | head -1 || true)
  if [ -z "${v:-}" ]; then MISSING_KEYS="$MISSING_KEYS $k"; fi
done
if [ -n "$MISSING_KEYS" ]; then
  echo ""
  echo "!!! Fill in your keys in $PROJECT_DIR/.env :$MISSING_KEYS"
  echo "    Alpaca: free keys at https://alpaca.markets -> Your API keys"
  echo "    Finnhub: optional, https://finnhub.io (overflow + fundamentals)"
  echo "    NTFY_TOPIC: install the free ntfy app, subscribe to a hard-to-guess topic"
  echo "    NTFY_SERVER: leave unset on this server (defaults to https://ntfy.sh, which"
  echo "                works natively here — the envs.net workaround was VM-specific)"
  echo "    DASHBOARD_TOKEN: long random string, e.g. \$(openssl rand -hex 24)"
fi

# ---- 4. systemd unit ----------------------------------------------------------
echo "==> installing systemd unit"
sed -e "s#__USER__#$USER_NAME#g" -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
  "$PROJECT_DIR/deploy/systemd/stockbot.service" \
  | sudo tee /etc/systemd/system/stockbot.service >/dev/null
sudo "$SYSTEMCTL" daemon-reload
sudo "$SYSTEMCTL" enable --quiet stockbot.service

# ---- 5. passwordless systemctl for the keepalive cron ------------------------
# Lets the 15-min keepalive cron restart the bot without a password prompt.
echo "==> installing sudoers rule for keepalive (systemctl is-active/restart)"
printf '%s ALL=(root) NOPASSWD: %s is-active stockbot.service, %s restart stockbot.service\n' \
  "$USER_NAME" "$SYSTEMCTL" "$SYSTEMCTL" \
  | sudo tee /etc/sudoers.d/stockbot >/dev/null
sudo chmod 440 /etc/sudoers.d/stockbot

# ---- 6. crontab (merged via marker block, idempotent) ------------------------
echo "==> installing crontab"
RENDERED="$(mktemp)"
sed -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" "$PROJECT_DIR/deploy/crontab" > "$RENDERED"
EXISTING="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf '%s\n' "$EXISTING" | sed '/^# BEGIN stockbot$/,/^# END stockbot$/d')"
printf '%s\n# BEGIN stockbot\n%s\n# END stockbot\n' "$FILTERED" "$(cat "$RENDERED")" | crontab -
rm -f "$RENDERED"

# ---- 7. start the bot --------------------------------------------------------
echo "==> starting stockbot.service"
sudo "$SYSTEMCTL" restart stockbot.service
sleep 2
STATE="$("$SYSTEMCTL" is-active stockbot.service 2>/dev/null || echo unknown)"

# ---- 8. summary ----------------------------------------------------------------
echo ""
echo "================ STATUS ================"
echo "bot service:        $STATE"
echo "crons installed:    morning scan (Mon-Fri 08:00 ET), watch (15 min),"
echo "                    market-open bell (09:30 ET), keepalive (15 min)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "dashboard (local):  http://localhost:8000/?token=<DASHBOARD_TOKEN from .env>"
if [ -n "${IP:-}" ]; then
  echo "dashboard (LAN):    http://${IP}:8000/?token=<DASHBOARD_TOKEN from .env>"
fi
echo "logs:               journalctl -u stockbot -f   |  tail -f $PROJECT_DIR/logs/cron.log"
echo "=================================================="
if [ "$STATE" != "active" ]; then
  echo "WARNING: service is not active — run: journalctl -u stockbot -n 50"
  exit 1
fi
if [ -n "$MISSING_KEYS" ]; then
  echo "NOTE: fill the missing keys above, then: sudo systemctl restart stockbot.service"
fi
echo "Done."
