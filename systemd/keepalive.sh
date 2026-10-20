#!/bin/bash
# Keep-alive for the stockbot: reinstalls systemd units after a VM
# replacement (system files are ephemeral) and ensures bot + tunnel run.
# Prints: BOT=<active|inactive> TUNNEL=<active|inactive> URL=<tunnel url or none>
set -u
P=/home/hatch/workspace/goals/real-time-stock-alert-bot

for u in stockbot.service stockbot-tunnel.service; do
  if [ ! -f "/etc/systemd/system/$u" ]; then
    sudo cp "$P/systemd/$u" /etc/systemd/system/
    sudo systemctl daemon-reload
  fi
done

sudo systemctl start stockbot.service >/dev/null 2>&1
sudo systemctl start stockbot-tunnel.service >/dev/null 2>&1
sleep 2

bot=$(sudo systemctl is-active stockbot.service 2>/dev/null)
tun=$(sudo systemctl is-active stockbot-tunnel.service 2>/dev/null)
url=$(sudo journalctl -u stockbot-tunnel.service --no-pager 2>/dev/null \
      | grep -oE "https://[a-z0-9]+\.lhr\.life" | tail -1)
echo "BOT=$bot TUNNEL=$tun URL=${url:-none}"
