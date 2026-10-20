#!/bin/bash
# Simplified VPS keepalive for the stockbot.
# No tunnel/URL logic here — on a real server the dashboard binds directly
# and there is nothing ephemeral to babysit. Just make sure the bot runs.
# Prints exactly one log line per invocation.
set -u

NOW=$(date -Is)
STATE=$(systemctl is-active stockbot.service 2>/dev/null || echo unknown)

if [ "$STATE" = "active" ]; then
  echo "$NOW keepalive: stockbot active"
  exit 0
fi

echo "$NOW keepalive: stockbot was $STATE -> restarting"
sudo -n systemctl restart stockbot.service
sleep 3
STATE2=$(systemctl is-active stockbot.service 2>/dev/null || echo unknown)
echo "$NOW keepalive: stockbot now $STATE2"
