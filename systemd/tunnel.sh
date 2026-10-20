#!/bin/bash
# SSH tunnel exposing the dashboard via localhost.run (free, no account).
exec /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ProxyCommand="/home/hatch/workspace/goals/real-time-stock-alert-bot/proxy_cmd.py %h %p" \
  -R 80:localhost:8000 nokey@localhost.run
