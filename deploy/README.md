# Deploying the stock alert bot to a standalone server (VPS)

This guide installs the bot on a stock Ubuntu/Debian server so it runs fully
automatically — bot + dashboard + all four schedules — with zero dependence on
the ephemeral dev VM (no egress proxy, no SSH tunnel, no URL juggling).

## Prerequisites

- Ubuntu 22.04+ (or Debian 11+) with internet access
- A user with sudo (the bot runs as this user)
- Copy this whole project directory to the server (e.g. `/home/you/stockbot`)

## Install (one command)

```bash
cd /home/you/stockbot
bash deploy/install.sh
```

The script is **idempotent** — re-run it after copying updated files to
redeploy. It will:

1. Install `python3-venv` if missing, create `.venv`, install `requirements.txt`
2. Create `.env` from `.env.example` if missing (and add an empty
   `DASHBOARD_TOKEN=` line if the example predates it)
3. Install `stockbot.service` into `/etc/systemd/system` (with your username
   and project path baked in), enable and start it
4. Add a sudoers rule so the 15-minute keepalive cron can restart the bot
   without a password prompt
5. Merge the four schedules into your crontab (inside a `# BEGIN/END stockbot`
   marker block, so re-runs replace the block instead of duplicating it)
6. Print a status summary

## Fill in `.env` keys

If the installer printed missing keys, edit `.env` and fill them in:

| Key | Where | Notes |
|---|---|---|
| `ALPACA_KEY` / `ALPACA_SECRET` | https://alpaca.markets → Your API keys (free) | Real-time data + websocket feed |
| `FINNHUB_KEY` | https://finnhub.io (free) | Fundamentals, news, analyst ratings |
| `NTFY_TOPIC` | Free ntfy app on your phone | Hard-to-guess topic name, e.g. `stockbot-<random>-alerts` |
| `DASHBOARD_TOKEN` | You invent it | `openssl rand -hex 24` — the dashboard password |
| `NTFY_SERVER` | (leave unset) | Defaults to `https://ntfy.sh`, which works natively on a real server. The `envs.net` workaround was specific to the proxied dev VM. |

After editing: `sudo systemctl restart stockbot.service`.

## What runs when

| What | When | How |
|---|---|---|
| Alert bot + dashboard | always | `systemd` `stockbot.service`, `Restart=always` |
| Morning deep scan | Mon–Fri 08:00 ET | cron → `python -m scanner` — ranked BUY/SELL lists, one ntfy push |
| Real-time watch | every 15 min | cron → `python -m scanner --watch` — script exits quietly outside 09:30–16:00 ET; instant ntfy on new setups |
| Market-open bell | daily 09:30 ET | cron → `python -m market_open` — script exits quietly on weekends/holidays |
| Keepalive | every 15 min | cron → `deploy/keepalive_vps.sh` — restarts the service only if it went down |

The server's own timezone does not matter: every cron line pins
`CRON_TZ=America/Toronto`, and the Python code uses America/Toronto internally.

## Dashboard

With no tunnel needed, the dashboard binds directly:

```
http://<server-ip>:8000/?token=<your DASHBOARD_TOKEN>
```

The `?token=` is required (wrong token → 401). Honest notes:

- It's plain HTTP with a token gate. On your home LAN that's fine. If you
  expose it to the open internet, put a reverse proxy in front (nginx/Caddy)
  with HTTPS, and/or restrict port 8000 to your IP in the firewall
  (`sudo ufw allow from <your-ip> to any port 8000`).
- Nimesh's own setup uses this for personal viewing only; never share the link.

## Status and logs

```bash
systemctl status stockbot          # service state
journalctl -u stockbot -f          # live bot log
journalctl -u stockbot -n 100      # last 100 lines
tail -f logs/cron.log              # output of the cron jobs
crontab -l                         # verify the four schedules
```

## Updating

1. Copy the new/changed project files over (`.env` and `stockbot.db` are never
   touched by the installer — your keys and alert state survive).
2. Re-run `bash deploy/install.sh`.

## Uninstall

```bash
sudo systemctl stop stockbot.service
sudo systemctl disable stockbot.service
sudo rm /etc/systemd/system/stockbot.service /etc/sudoers.d/stockbot
sudo systemctl daemon-reload
crontab -l | sed '/^# BEGIN stockbot$/,/^# END stockbot$/d' | crontab -
```

## What a fresh server still needs (nothing is automated here by design)

- The `.env` keys (you fill them in once).
- Firewall/reverse proxy if you expose the dashboard beyond your LAN.
- Backups of `.env` and `stockbot.db` if you care about them — the bot
  recreates the DB from scratch, but you lose alert history/dedupe state.
