---
id: stockbot-morning-scanner
title: Morning market scanner
enabled: false
owner: goal:real-time-stock-alert-bot
mode: task
schedule:
  kind: weekly
  timezone: America/Toronto
  time: 08:00:00
  dow: [Mon, Tue, Wed, Thu, Fri]
timeout_secs: 1800
metadata:
  originating_chat_context_json: '{"chat_id":"aabee22a-210e-4a59-80cd-eaefb0be60d2","origin_provider":"main","chat_kind":"direct","event_kind":"message","require_mention":false,"device_id":"010d139c-1538-4511-b638-3094835def1b"}'
  presentation_locale: en-US
---
Run Nimesh's morning market scanner (stock alert bot, goal real-time-stock-alert-bot).

1. Execute exactly this, capturing output:
   cd /home/hatch/workspace/goals/real-time-stock-alert-bot && export https_proxy=http://hatch-egress-proxy:3128 http_proxy=http://hatch-egress-proxy:3128 HTTPS_PROXY=http://hatch-egress-proxy:3128 HTTP_PROXY=http://hatch-egress-proxy:3128 SSL_CERT_FILE=/run/hatch/egress-tls/ca-bundle.pem && .venv/bin/python -m scanner
   (Do NOT set REQUESTS_CA_BUNDLE. The scanner loads API keys from the .env in that directory itself.)
2. The scanner already pushes the ranked candidate list to ntfy on its own, and exits quietly on non-trading days or when nothing clears the bar.
3. This run needs no chat message in the normal case: if the scanner pushed, the user got the ntfy alert; if it exited quietly, there is nothing to report. Surface a message only if the command itself failed (non-zero exit or traceback) — then report the error briefly so it can be fixed.
4. Do not edit MEMORY.md; on failure only, append one line to ~/memory/YYYY-MM-DD.md.
