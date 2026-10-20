---
id: stockbot-market-open
title: Stockbot market-open bell
enabled: false
owner: goal:real-time-stock-alert-bot
mode: task
schedule:
  kind: daily
  timezone: America/Toronto
  time: 09:30:00
delivery:
  - chat_id: aabee22a-210e-4a59-80cd-eaefb0be60d2
metadata:
  originating_chat_context_json: '{"chat_id":"aabee22a-210e-4a59-80cd-eaefb0be60d2","origin_provider":"main","chat_kind":"direct","event_kind":"message","require_mention":false,"device_id":"010d139c-1538-4511-b638-3094835def1b"}'
  presentation_locale: en-US
---
Market-open bell for Nimesh's stock alert bot.

Run: `cd /home/hatch/workspace/goals/real-time-stock-alert-bot && .venv/bin/python -m market_open` (loads .env from that dir for NTFY_TOPIC/NTFY_SERVER).

The script checks the sessions.py market calendar itself and exits quietly on weekends/holidays — no chat message then. On trading days it pushes one "Market open" ntfy to his phone (regime + scanner scope). Report to chat only if the push failed or something errored; stay quiet on success.
