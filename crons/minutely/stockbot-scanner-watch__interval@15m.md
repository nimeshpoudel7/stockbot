---
id: stockbot-scanner-watch
title: Real-time scanner watch
enabled: false
owner: goal:real-time-stock-alert-bot
mode: task
schedule:
  kind: interval
  timezone: America/Toronto
  at: 2026-10-19T19:15:31
  every: 15m
timeout_secs: 900
metadata:
  tags: [cron:automatic-interval-anchor]
  originating_chat_context_json: '{"chat_id":"aabee22a-210e-4a59-80cd-eaefb0be60d2","origin_provider":"main","chat_kind":"direct","event_kind":"message","require_mention":false,"device_id":"010d139c-1538-4511-b638-3094835def1b"}'
  presentation_locale: en-US
---
Run Nimesh's real-time scanner watch (stock alert bot, goal real-time-stock-alert-bot).

1. Execute exactly this, capturing output:
   cd /home/hatch/workspace/goals/real-time-stock-alert-bot && export https_proxy=http://hatch-egress-proxy:3128 http_proxy=http://hatch-egress-proxy:3128 HTTPS_PROXY=http://hatch-egress-proxy:3128 HTTP_PROXY=http://hatch-egress-proxy:3128 SSL_CERT_FILE=/run/hatch/egress-tls/ca-bundle.pem && .venv/bin/python -m scanner --watch
   (Do NOT set REQUESTS_CA_BUNDLE. The scanner loads API keys from the .env in that directory itself.)
2. The script guards itself: it exits quietly outside market hours (weekdays 9:30 AM–4:00 PM ET), on holidays/weekends, and when there are no NEW setup matches. It pushes to ntfy on its own the moment a symbol newly matches.
3. This run needs no chat message in the normal case. Surface a message only if the command itself failed (non-zero exit or traceback) — then report the error briefly so it can be fixed.
4. Do not edit MEMORY.md; on failure only, append one line to ~/memory/YYYY-MM-DD.md.
