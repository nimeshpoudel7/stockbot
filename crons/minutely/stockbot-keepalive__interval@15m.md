---
id: stockbot-keepalive
title: Stockbot keep-alive watchdog
enabled: false
owner: goal:real-time-stock-alert-bot
mode: task
schedule:
  kind: interval
  timezone: America/Toronto
  at: 2026-10-19T17:37:06
  every: 15m
timeout_secs: 300
delivery:
  - chat_id: aabee22a-210e-4a59-80cd-eaefb0be60d2
metadata:
  tags: [cron:automatic-interval-anchor]
  originating_chat_context_json: '{"chat_id":"aabee22a-210e-4a59-80cd-eaefb0be60d2","origin_provider":"main","chat_kind":"direct","event_kind":"message","require_mention":false,"device_id":"010d139c-1538-4511-b638-3094835def1b"}'
  presentation_locale: en-US
---
Stockbot keep-alive watchdog (Nimesh's real-time stock alert bot).

Do this every run:
1. Run `/home/hatch/workspace/goals/real-time-stock-alert-bot/systemd/keepalive.sh` and capture its single output line, e.g. `BOT=active TUNNEL=active URL=https://xxxx.lhr.life`.
2. Read the watermark file `~/workspace/goals/real-time-stock-alert-bot/hidden_files/tunnel_url.txt` if it exists.
3. If the URL in the output differs from the watermark (or the watermark is missing and a URL is present): write the new URL into the watermark file.
4. Decide what the user needs to hear:
   - URL changed (or first run with a URL): do BOTH of these:
     a. Push the new link DIRECTLY to his phone via ntfy so he is never dependent on chat relay: read NTFY_TOPIC, NTFY_SERVER and DASHBOARD_TOKEN from `/home/hatch/workspace/goals/real-time-stock-alert-bot/.env`, then `curl -s -m 30 -H "Title: Dashboard moved" -d "New dashboard link (old one stopped working): <URL>?token=<DASHBOARD_TOKEN>" <NTFY_SERVER>/<NTFY_TOPIC>`. Confirm the push returned success.
     b. Report to chat with the full URL plus `?token=` (token value from the same .env as DASHBOARD_TOKEN). Keep the message short: the new link and a note that the old one stopped working.
   - BOT or TUNNEL was not `active` before this run healed it: mention briefly that the bot/tunnel was restarted.
   - Everything already active and URL unchanged: say nothing (no user message).
5. Append one line to `~/memory/2026-10-19.md` only when you healed something or the URL changed; otherwise leave memory alone.
