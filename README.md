# 📈 Stockbot — personal real-time stock alert bot

Your own real-time stock alert system: a Python engine watches US stocks tick-by-tick,
fires alerts the instant your rules trigger, pushes them to your phone via **ntfy**,
and gives you a **web dashboard** to manage everything. No Telegram, no accounts,
no monthly fees.

> **Not financial advice.** Alerts are information, not buy/sell recommendations.

## How it works

```
 Alpaca IEX websocket (real-time US trades, free, 30 symbols)
        │  Finnhub websocket (free, overflow symbols 31–39)
        ▼
 ┌─────────────┐   ticks    ┌──────────────┐   fires   ┌──────────┐
 │ FeedManager │ ─────────▶ │ AlertEngine  │ ────────▶ │ ntfy.sh  │ ──▶ your phone
 └─────────────┘            │ ARMED→TRIG→   │           └──────────┘
        │                   │ COOLDOWN→ARMED│
        │ warm-up           └──────┬───────┘
        ▼ (1m + daily bars)        │ state persisted
 ┌─────────────┐                   ▼
 │  SQLite     │◀────────── alert state, watchlist,
 │ stockbot.db │             candle cache, fire history,
 └─────────────┘             fundamentals, calendar
        ▲
        │  http://localhost:8000
 ┌─────────────┐
 │ Dashboard   │  watchlist, live prices, alert CRUD, charts,
 │ (FastAPI)   │  fundamental scores, predictions, backtests
 └─────────────┘

Hourly (asyncio task): Finnhub event poller → split/buyback/news/earnings
alerts via `AlertEngine.fire_external`. Daily: SPY regime refresh.
```

## Setup — 4 steps, ~10 minutes

**1. Free Alpaca API keys** — sign up at https://alpaca.markets, go to
"Your API keys", generate a key + secret. (Paper-trading keys work fine;
the bot only reads market data, never trades.)

**2. ntfy push alerts** — install the free **ntfy** app (iOS / Android),
tap "subscribe", and enter a topic name. **Make it hard to guess**
(e.g. `stockbot-q8x2m-alerts`, NOT `myalerts`) — ntfy.sh is a public
server and the topic name is the only thing keeping your alerts private.

**3. Configure**
```bash
cd ~/workspace/goals/real-time-stock-alert-bot
cp .env.example .env
# edit .env and set exactly three values:
#   ALPACA_KEY=...  ALPACA_SECRET=...  NTFY_TOPIC=stockbot-q8x2m-alerts
# optional: SYMBOLS=AAPL,MSFT,NVDA  (seeds your watchlist on first run)
```

**4. Install & run**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
# dashboard: http://localhost:8000
```

**Keep it running** (systemd, restarts on failure and on reboot):
```bash
mkdir -p ~/.config/systemd/user
cp stockbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stockbot
systemctl --user status stockbot
```
Alert state lives in SQLite, so a restart never re-fires an old alert.

## Alert types (all in the dashboard — create / edit / pause / resume / delete)

| Type | Rule | Evaluated |
|---|---|---|
| Price above / below | `price ≥ $X` / `≤ $X` | every tick |
| Day % change | `±N%` vs prior close | every tick |
| 52-week high / low | breaks the 52w extreme | every tick |
| RSI(14) | crosses above 70 / below 30 (Wilder's) | each bar close |
| Volume spike | volume > 3× the 20-bar average | each bar close |
| SMA cross | SMA(20) × SMA(50) | each bar close |
| Bollinger break | close crosses outside SMA(20) ± 2σ | each bar close |
| Bollinger squeeze | bandwidth at its 126-day (6-month) low | each daily close |
| Fibonacci cross | price crosses a retracement level (23.6/38.2/50/61.8/78.6%) of the recent swing | each bar close |
| ATR stop | long: `entry − k×ATR(14)` / short: `entry + k×ATR(14)`; fires when touched | every tick |
| Split announced | Finnhub news headline match (free tier has no splits endpoint) | hourly poll |
| Buyback announced | Finnhub news headline match | hourly poll |
| News spike | today's article count ≥ 3× the 20-day median | daily poll |
| Sell-the-news | earnings beat (actual > estimate) then next session ≤ −3% | daily poll |

Every alert supports: **one-time or recurring**, **cooldown** (default 15 min),
**expiry date**, **hysteresis** (re-arms only after price moves 0.5% back past
the trigger — no spam when price hugs the line), **ntfy priority** 1–5,
**timeframe** (bar-close types: 1m / 5m / 15m / 1h — the engine aggregates 1m
bars internally and evaluates each type on its own timeframe's bar close), and
**regime gating** (fire only in a bull or bear market, see below).

Push format — title: `🔔 AAPL $232.14`, message: `Price above $230 — triggered 10:32 AM ET`.

## Market regime filter

`regime.py` — SPY vs its 200-day SMA (+ 20-day slope): **bull** (price > SMA200
and rising), **bear** (below and falling), **sideways** otherwise. Refreshed at
most once a day (1 Alpaca call) and shown as a badge on the dashboard. Any
alert can be set to "bull only" / "bear only"; unknown regime fails open (the
alert still fires).

## Fundamental scores

`fundamentals.py` — per watchlist symbol, from Finnhub annual 10-Ks:

- **Piotroski F-Score (0–9)**: the 9 tests (profitability ×4, leverage/liquidity
  ×3, operating efficiency ×2), each True/False/None with missing data.
- **Altman Z-Score**: 1.2A + 1.4B + 3.3C + 0.6D + 1.0E → distress / grey / safe.
- **Simplified DCF**: 5-year FCF projection (configurable WACC/growth) + Gordon
  terminal value → fair value per share, shown against the last price.

Dashboard "Scores" section shows all three plus the data year. **Missing fields
never crash — the score shows n/a.** Scores are SQLite-cached with a 24h TTL;
the dashboard auto-load costs **zero** Finnhub calls, and the ↻ button forces
one call per symbol.

## Event alerts (hourly Finnhub poller)

`event_alerts.py` — splits, buybacks, news spikes, and earnings are polled
hourly but **only for symbols with an armed event alert**, at most once per
day per symbol per data type (~1 news + 1 earnings call). Deduped by event key
so a restart never double-fires. Note: Finnhub's `/stock/split` returns 403 on
the free tier, so split/buyback detection uses `/company-news` headline keyword
matching instead — no extra calls.

## Session calendar

`sessions.py` — Alpaca's free `/v2/calendar` is cached in SQLite, so holidays
and half-days are explicit: "next session" logic (sell-the-news) always steps
to a real trading day, and the bot logs the session state at boot.

## Experimental predictions (NOT advice)

`predict.py` — sklearn `HistGradientBoostingClassifier` on RSI(14), MACD
histogram, Bollinger %B, volume z-score, ATR%, 5d/20d returns, distance from
SMA20. Label: up-move over the next 5 sessions. **Walk-forward validation**:
train on 252 sessions, test the next 21, roll forward — the dashboard shows
the probability **and the walk-forward hit rate**, so you see exactly how
(un)reliable it is. Predictions are never pushed and never presented as
advice; every surface says EXPERIMENTAL.

Retrain: `python -m predict SYMBOL` (needs ~2y of daily bars via Alpaca), or
the dashboard's train button. Models are saved as JSON in `models/`.

## Backtests

`backtest.py` + dashboard tab — for each rule-based alert type, 1 year of
daily bars: how often +5% was hit before −3% after the alert fired (bar
high/low, same-bar touch counts as stop-first = pessimistic), average 20-day
move, worst 20-day move. Run from the dashboard; results are per-type with a
per-symbol fire count.

## Market scanner (buy + sell setups, whole market)

`scanner.py` — screens **every tradable US stock** (Alpaca
`/v2/assets?status=active&asset_class=us_equity`, tradable only — penny
stocks, small/mid caps and large caps alike) every weekday at 8:00 AM ET
(before the 9:30 open) and pushes the top candidates to ntfy. It finds
setups from the guide's alert library in **both directions**; **you decide
what to buy or sell** — nothing here trades.

Pipeline: trading-day guard (skips holidays/weekends quietly) → universe
(cached in SQLite, refreshed weekly; S&P 500 fallback chain if the Alpaca
fetch fails) → **liquidity filter** → technical screen on 300 daily bars per
symbol (Alpaca REST, 100 symbols per call) → fundamental overlay
(Piotroski/Altman/DCF via the 24h cache) on the top 25 scorers per side →
ranked top 12 buys and top 12 sells pushed as one ntfy message with
**BUY CANDIDATES** and **SELL CANDIDATES** sections.

**Liquidity filter** (no price floor): a symbol must average **>$250k/day in
dollar volume over the last 20 days**. This drops dead tickers while
keeping penny stocks — a $0.40 stock trading 2M shares/day passes; a $50
stock nobody touches does not.

**How the scanner reads a chart** (rebuilt Oct 20, 2026 — see
`study_trader_signals.py` for the validation): trend context first, then
where price sits in that trend, then confirmation, then risk. Every
playbook scores 0–100, BUY and SELL on the same scale.

**BUY — two playbooks** (a stock's BUY score is the stronger one):

*Dip* — buy weakness (mean reversion). Worked best in pullbacks and
corrections:

| setup | condition | pts |
|---|---|---|
| oversold_rsi30 / _rsi35 | RSI(14) < 30 / < 35 | 40 / 25 |
| below_lower_band | close under the lower Bollinger band of the prior 20 days | 20 |
| stretched_below_sma20 | close more than 2× ATR below the 20-day average | 15 |
| capitulation_volume | RSI < 35 on volume > 2× the 20-day median | 25 |

*Momentum* — buy strength (leaders). Worked best in steady bull runs:

| setup | condition | pts |
|---|---|---|
| leader_rs20 / _rs10 | 3-month return beats SPY by > 20 / > 10 points | 30 / 15 |
| extended15 / _10 | close > 15% / > 10% above the 50-day | 20 / 10 |
| volume_up_day | volume > 2× median, close ≥ open | 20 |
| breakout20_volume | close above the prior 20-day high, top 30% of the day's range, volume ≥ 1.5× | 15 |
| uptrend | above a rising 200-day, 50-day above 200-day | 15 |

**SELL — sell strength inside the stock's own downtrend** (sell the rip):

| setup | condition | pts |
|---|---|---|
| rally_in_downtrend | downtrend and close > 2× ATR above the 20-day | 40 |
| overbought_below_200 | RSI(14) > 70 while below the 200-day | 30 |
| short_term_overbought_dn | downtrend and RSI(2) > 90 | 20 |
| downtrend | below a falling 200-day, 50 < 200 (only adds to a setup) | 10 |

**Push bars** (technical score; fundamentals and sentiment only rank and
rate): BUY dip **60**, BUY momentum **75**, SELL **40**. The SPY regime
picks the favoured BUY playbook — bull market: dip bar 75; sideways/bear:
momentum bar 90 — and in a bear regime the SELL bar is 90 (bear-market
rallies squeezed shorts). Every alert carries an **ATR stop and target**
(stop 2× ATR, target 3× ATR), e.g.
`BUY: XYZ @ $42.10 — 8.4/10 — dip buy; RSI 27 — deeply oversold; …; stop $39.30 · target $46.30`.

**What the data threw out** (and why some rules look "backwards"): on
~3.5 years of real daily bars (S&P 500 + a random liquid small/mid-cap
sample, entry at the next open), buying near the 52-week high lost
(−0.4% vs random days over 10 sessions), plain uptrend / MACD / RSI 60–80
did nothing, pullbacks to the 20/50-day did nothing, and **every
"sell weakness" rule** — breakdowns, lower-band closes, heavy-volume red
days, death crosses, oversold — **lost money as a short**: weak stocks
bounced. Selling into oversold was the single worst signal, so SELL only
ever fires on strength.

**Validated results** of the pushed signals (10 sessions, vs random days):
S&P 500 BUY +0.6% (58% winners), SELL +1.1% (55%); small/mid BUY +0.4%
(55%), SELL +0.6% (53%). Both halves of the window positive for every
group. (The old rules on the same data: BUY −0.3% / −0.4%, SELL +0.2% /
−0.9%.) Baseline
win rate is ~52%: these **tilt the odds, they are not certainties** —
use the stop.

Fundamental bonus (−25…+25): Piotroski ≥8 +10 / 6–7 +5 / ≤2 −10; Altman safe
+5 / distress −10; DCF ≥20% below fair value +10 / >20% above −5. The sell
side uses the mirror. It ranks candidates and moves the /10 rating by at
most ±2; it never decides whether a setup is pushed.

**Parity with the 39-symbol alert engine:** the scanner uses the *same
indicator math* (RSI, Bollinger, SMAs, ATR, volume) — but on **daily bars, every 15 minutes, across the whole
universe**, while the engine evaluates **per-tick on streaming data** for
your 39 watchlist symbols. Engine-only features the scanner does not
replicate: your personal price-level alerts, intraday %-move alerts, and
the news/corporate-action event feeds.

Free-tier discipline: ~65–70 Alpaca REST calls per full-universe pass
(100 symbols/call — comfortably under the 200/min limit, so the 15-minute
watch cadence holds); Finnhub at most ~25 calls/day per side for the
fundamental overlay (24h cache, 1.5s spacing).

Run manually: `python -m scanner` (scan + push) or
`python -m scanner --dry-run` (print the ranked list, no push).

**Ratings:** every setup shown to the user carries a rating out of 10 with
one decimal (e.g. `6.8/10`): the technical score as a fraction of **its
own side's scale** (BUY out of 28, SELL out of 100), so a full-strength
BUY and a full-strength SELL both read 10.0/10 — in the morning list and
in watch mode alike. In the morning scan the fundamental bonus then moves
it by up to ±2 (Piotroski/Altman/DCF; no data = neutral, no change).
Display-only; internal scores and push bars stay raw points.

**Sentiment overlay** (`sentiment.py`) — two free Finnhub signals, candidates
only (never the full universe), each leg cached 24h in SQLite:

- **Headline tone** — `/company-news` (7-day window), finance-tuned keyword
  lexicon with negation handling, recency-weighted into [-1, +1].
- **Analyst recommendations** — `/stock/recommendation`, latest monthly
  period: score = (2×strongBuy + buy − sell − 2×strongSell) / total
  analysts, clamped to [-1, +1]. This is the trusted signal — real analysts,
  not social noise. (Social chatter was deliberately left out: X/Twitter's
  API costs $100/mo and free social feeds skew meme/penny-heavy.)

Combined total = 0.65 × news + 0.35 × analyst (news-heavier — analyst
consensus skews bullish, since almost nobody rates "sell"). It shifts
the /10 rating by
total × 1.5 (clamped to [0, 10]), signed by side: good news raises a BUY
and lowers a SELL, bad news does the reverse — a buy setup on glowing news
and bullish analysts can show 7.8/10 instead of 6.8/10. Each alert shows the breakdown,
e.g. `news: +0.6 | analysts: 31 buy / 4 hold / 1 sell` (Strong Buy folds
into buy, Strong Sell into sell). No coverage on either leg → no sentiment
line, rating unchanged. Fetch failures degrade to neutral and are cached,
so a dead feed never hammers the API or crashes a scan.

**Watch mode** — `python -m scanner --watch` (or `--watch --dry-run`):
re-runs the technical screen over the universe and pushes an ntfy alert
**immediately** when a symbol *newly* matches the setup rules
(bull score ≥ 20, e.g. uptrend + near 52-week high → **"BUY SETUP: XYZ
@ $12.30 — 7.1/10 — within 1.2% of 52-week high; in uptrend"**; bear score
≥ 30 → **"SELL SETUP: …"**; single weak setups ignored as noise). Dedupe:
flagged symbols persist in SQLite (`scanner_watch_seen`) — one alert per
symbol per side per trading day, recorded only after the push succeeds
(a failed push retries next pass; `--dry-run` never touches the list).
The morning scan resets the list. No fundamentals in watch mode (keeps it fast and
free-tier-safe). Guards: weekdays + trading day + 09:30–16:00 ET from the
sessions.py calendar — exits quietly otherwise.

**Schedules** (runtime crons, goal `real-time-stock-alert-bot`):
- `stockbot-morning-scanner` — weekdays 8:00 AM ET, the deep scan above.
- `stockbot-scanner-watch` — every 15 min; the script's own market-hours
  guard decides whether to run (quiet outside 9:30–16:00 ET / holidays).
  Both export the egress proxy + CA bundle env (see AGENTS.md: plain
  `requests` calls fail without them) and load API keys from the project's
  `.env`.

## Free-tier limits (read this)

- **Alpaca free Basic**: real-time US stocks via the **IEX feed** (one exchange's
  view, not the full consolidated tape), **30 streaming symbols max**,
  200 REST calls/min. Full SIP feed = $99/mo (you don't need it for alerts).
- **Engine cap: 39 symbols total.** Symbols 1–30 go to Alpaca; 31–39 need a
  free **Finnhub** key (`FINNHUB_KEY` in `.env`, https://finnhub.io) — the
  Finnhub websocket provider is already wired in, just add the key.
- **Finnhub free: ~60 calls/min, but this bot stays under ~50/day** by design:
  fundamentals are SQLite-cached (24h TTL; dashboard auto-load = 0 calls, ↻ =
  1 call/symbol), event alerts poll at most 1 news + 1 earnings call per symbol
  per day *and only for symbols with an armed event alert*, the regime needs 1
  Alpaca call/day. Splits use the news poll (no `/stock/split` on free tier —
  it's 403), so they cost nothing extra.
- **TSX / Canadian stocks: real-time is NOT free anywhere.** Free tiers are
  ~15 min delayed. This bot does real-time for **US stocks only**; add a
  Canadian symbol and you'll get delayed data (the dashboard will show it,
  but treat the timing accordingly).

## API endpoints (all behind the `?token=` gate)

| Endpoint | What it does |
|---|---|
| `GET /api/regime` | current SPY regime (bull/bear/sideways) |
| `GET /api/scores` | cached fundamentals for the watchlist (0 Finnhub calls) |
| `POST /api/scores/{sym}/refresh` | force a fundamentals refresh (1 Finnhub call) |
| `GET /api/predict/{sym}` | trained experimental model: P(up 5d) + walk-forward hit rate |
| `POST /api/predict/{sym}/train` | train the model (~2y daily bars) |
| `GET /api/backtest` | last backtest results |
| `POST /api/backtest` | run the 1y backtest over the watchlist |

## Adding a second provider later

1. Subclass `DataFeed` in `feed.py` (`name`, `max_symbols`, `has_credentials()`,
   `run()`, `get_1m_bars()`, `get_daily_bars()`) — see `FinnhubFeed` (~60 lines).
2. Instantiate it in `main.py` and append to the `feeds` list.
3. `FeedManager` fills providers in order and enforces the 39-symbol cap.
No engine changes needed — it only sees merged ticks.

## Files

| File | What it does |
|---|---|
| `main.py` | starts feed + engine + dashboard; graceful shutdown |
| `feed.py` | `DataFeed` abstraction, `AlpacaFeed`, `FinnhubFeed`, `FeedManager` (multi-provider routing, reconnect w/ backoff, warm-up) |
| `alerts.py` | `AlertEngine`: ARMED→TRIGGERED→COOLDOWN state machine, 16 alert types, hysteresis, per-minute dedupe, multi-timeframe bars, regime gating, `fire_external` for event alerts |
| `indicators.py` | pure math: SMA, EMA, Wilder's RSI, ATR, crosses, volume spike, Bollinger, Fibonacci, MACD, volume z-score, 1m→N-minute aggregation |
| `storage.py` | SQLite: alerts (+timeframe/regime), alert state, watchlist, candle cache, fire history, fundamentals cache, poller state, market calendar |
| `fundamentals.py` | Piotroski / Altman / DCF from Finnhub annuals, 24h SQLite cache |
| `regime.py` | SPY vs SMA200 bull/bear/sideways |
| `sessions.py` | Alpaca market calendar (holidays/half-days) |
| `event_alerts.py` | hourly Finnhub poller: splits/buybacks (news keywords), news spikes, sell-the-news |
| `predict.py` | EXPERIMENTAL sklearn model + walk-forward validation; `python -m predict SYMBOL` |
| `backtest.py` | per-type 1y backtest stats (+5% before −3%, avg/worst move) |
| `notify.py` | ntfy.sh push sender |
| `dashboard.py` | FastAPI app + single-page dashboard (live prices, alert CRUD, charts, scores, predictions, backtests) |
| `config.py` | `.env` loading, limits, validation |
| `tests/` | 94 tests: indicators, engine state machine, MTF/timeframe, regime, fundamentals, events, walk-forward, backtest — all passing |

Run tests: `.venv/bin/python -m unittest discover -s tests -t .`

## Troubleshooting

- **"ALPACA_KEY / ALPACA_SECRET missing"** — `.env` not filled or not in the project dir.
- **No ticks / "waiting for data"** — market closed? IEX streams during US market
  hours (9:30–16:00 ET). Also check the key hasn't been revoked.
- **No push on phone** — topic name in `.env` must match the ntfy app subscription
  exactly; check the log for `ntfy: send failed`.
- **Port 8000 busy** — set `PORT=8001` in `.env`.
