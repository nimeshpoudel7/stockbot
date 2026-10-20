# Real-time stock alert bot

Goal ID: goal_847428247a63
Goal slug: real-time-stock-alert-bot

## Description
Nimesh's personal real-time stock alert system for his own use: instant phone
push alerts via ntfy.sh (no Telegram — pivoted Oct 19 per his call) plus a web
dashboard that is the single control panel (watchlist, live prices, full alert
CRUD, charts). Free real-time US data via Alpaca's IEX websocket (30 symbols);
engine supports up to 39 total with Finnhub as the wired-in overflow provider.
TSX stays delayed on free tiers.

## Build status (Oct 19, 2026)
MVP complete and verified in `~/workspace/goals/real-time-stock-alert-bot/`:
`main.py`, `feed.py` (Alpaca + Finnhub feeds, FeedManager), `alerts.py`
(state machine ARMED→TRIGGERED→COOLDOWN, 8 alert types, hysteresis, dedupe),
`indicators.py`, `storage.py` (SQLite), `notify.py` (ntfy), `dashboard.py`
(FastAPI), `config.py`, `requirements.txt` (pinned), `stockbot.service`,
`.env.example`, `README.md`. 31 unit tests pass (indicators + engine);
dashboard smoke-tested live via uvicorn (watchlist/alert CRUD, engine fire →
push → COOLDOWN reflected in API). Awaiting his 3 setup values before go-live:
ALPACA_KEY, ALPACA_SECRET, NTFY_TOPIC.
