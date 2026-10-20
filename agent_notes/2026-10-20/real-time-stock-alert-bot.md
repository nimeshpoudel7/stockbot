# Study Notes - 2026-10-20 - Real-time stock alert bot
<!-- studying-run-start: 2026-10-20T11:08:17Z -->

## Current state (as of Tue 2026-10-20 7:08 AM EDT)

Nimesh's stock alert bot went from a VM prototype to a fully self-running product on his own Oracle Cloud free-tier server (Ubuntu, VM.Standard.E2.1.Micro — Ampere AD-1 was out of capacity; public IP <server-ip>) between ~2:22 AM and ~3:08 AM EDT. The old VM bot, rotating tunnel links, and all four old schedules were shut down ~4:13 AM EDT at his order ("stop creating"). Oracle is now the only bot.

Feature set, all live on the server:
- **Scanner hunts ~13,520 tradeable US stocks** (penny/small/mid included; no price floor; dead tickers filtered by dollar-volume liquidity), BUY and SELL setups both, using the same indicator logic as the original 39-symbol engine (RSI, Bollinger, volume, SMA crosses, 52-week levels).
- **Two cadences:** weekday 8:00 AM ET deep scan with ranked buy+sell lists, and 15-min intraday watch during market hours pushing instantly on new matches (one alert per stock per day).
- **Every alert and morning-list line carries a /10 rating** in the title (display-only, thresholds unchanged).
- **Sentiment overlay (trusted sources only):** 7-day financial-press news tone (Finnhub headlines, finance lexicon with negation handling) 50/50 blended with analyst buy/hold/sell counts; swings each rating ±1.5 with the breakdown shown in alerts. He explicitly REJECTED Stocktwits/social sentiment as untrustworthy (~00:31–00:32 UTC); X sentiment was also declined ($100/mo).
- **Dashboard** at a permanent link (http://<server-ip>:8000, token-gated), with a **Scanner Picks** section showing all setups with ratings, **chart range buttons (1D/1M/6M/1Y)**, watchlist showing last close when market is closed, professional layout. **Stock detail page** live (~5:19 AM UTC): tap any symbol → price/day %, sector, market cap, chart, levels table (support, resistance, breakout/breakdown points, buy zone, SMA50/200, RSI, volume), scanner verdict with which setups fired, financial condition (Piotroski, Altman Z, DCF fair value; pre-revenue names show n/a honestly), sentiment section.
- **9:30 AM ET market-open bell** push each trading day with regime + scanner status; weekends/holidays quiet automatically.
- Phone pushes via ntfy (default ntfy.sh server), topic `stockbot-<random>-alerts`.
- He has SSH access from his PC and knows the three manual commands (full scan, `--watch` rescan, `--dry-run` rehearsal) — he explicitly wanted to run scans himself; assistant ran a demo then cleared the field.

### Critical quality failure found and fixed overnight
The scanner silently screened only ~800 of 13,520 stocks (Alpaca pagination: first page only, 5–6 stocks per 100-stock batch). Nimesh caught it via missing IREN. After the fix (98/100 per batch verified, chunked re-screening, 224 tests pass), the true full scan found **2,437 buy + 893 sell setups** (vs 102+43 before); phone push takes top 12 per side. The quality bar was also lowered 45→30 so more real setups surface; the dashboard shows all 145+ setups, phone gets top 12.

Specific per-ticker work in chat (evidence for future reads):
- IREN $27.73: was invisible; now in BUY list at 2.5/10 (RSI 25 oversold bounce only, no volume surge, near 52w low).
- MU $904.68: healthy uptrend above 200d, no trigger — correctly excluded.
- NFLX $69.82: no signal either way — correctly excluded.
- APLD $19.24: missed by a hair (RSI 32 > 30; volume exactly 2.0x, needs >2x).
- PLTR: +4.4% that day, +58% in 2 months, 9% below high — no buy setup; score 1.0/10 means "no trigger today," not "bad company."
- HOOD: -22% in a month, -31% from high; scanner fired a SELL (broke below lower Bollinger band) — on the sell side.
- ASTS: -69% from $133 to $42, one +3% green day — no confirmed reversal signal yet.
- RGTI: $11.66, -61% from high ($30.20), just above 52w low ($11.12), RSI 28.4 falling into oversold, no bounce yet.
- NIBS: no data anywhere; symbol likely wrong/delisted.

## What this changes about the goal
- The goal description in the goal record is now stale (says "Telegram bot," "30 symbols," "TSX") — a later turn that owns goal metadata may update it, but this study does not edit it.
- Today's real question is not more features but **delivery evidence**: the first real trading-day test (8:00 AM deep scan + 9:30 AM bell reaching his phone) is scheduled for today and the evidence is still owed as of this run (7:08 AM EDT). The alignment synthesis flags this as the current friction.
- Trust dynamic to respect: he audits hard after the pagination failure ("why still dip," score calibration). Lead with exact numbers, state what was checked, and keep the honest-gap convention (n/a rather than invented financials).

## What to watch / support implications
- Confirm today: 8:00 AM scan fired with a phone push, 9:30 AM bell arrived, and both dashboards' Scanner Picks rendered the real full-market list. If any leg fails, that is the single escalation.
- Cron inventory on the server per goal record: stockbot-market-open (daily 9:30), stockbot-keepalive (15m), stockbot-scanner-watch (15m), stockbot-morning-scanner (weekdays 8:00). The older VM-side crons were killed ~4:13 AM EDT.
- His standing rules touching this goal: every alert and morning list carries the /10 rating in the title; sentiment stays trusted-press + analysts only; he may name a free trusted news source later (Alpha Vantage was floated, undecided — do not wire it in without his word).
- Note the open money question: his API keys live only in the server-side .env; he once asked for them as a file and the assistant declined (correct — keys must never pass through chat).
- This goal is an **outcome** product build, not a learning goal: the assigned study plan's retrieval/practice/rubric/transfer shape was treated as scheduler metadata and not carried into this note.

## Open questions
- Will the 8:00 AM / 9:30 AM first real trading-day delivery reach his phone cleanly? (unresolved at run start)
- Whether he ever names the forgotten free trusted news source (Alpha Vantage candidate parked; his call).
- Whether his ephemeral Oracle IP stays stable and whether he ever wants reserved IP / resizing to 4 OCPU.

## Already Covered in Main Chat
- Nimesh ordered the bot from his 28-page PDF spec: ntfy alerts (Telegram declined), web dashboard, free data only (real market prices, not paper trading).
- Assistant built MVP (8 alert types, 31/31 tests), then full guide phase 2 (Piotroski/Altman/DCF, Bollinger/Fibonacci/ATR, news-spike/sell-the-news, split/buyback via headlines, multi-timeframe, regime filter, backtests, experimental ML predictor at 60.3% SPY hit rate), all disclosed with honest caveats.
- He ordered scanner expansion from S&P 500 to all ~13,520 US stocks with BUY/SELL labels, then /10 ratings on every alert, then trusted-source sentiment (press tone + analyst ratings, ±1.5 swing) after rejecting Stocktwits/social.
- Bot deployed to his Oracle Cloud free tier (he did the 15-min signup; Ampere was out of capacity so VM.Standard.E2.1.Micro won; he opened port 8000 himself); old VM bot/tunnel killed at his order ~4:13 AM EDT; Oracle is the only bot.
- He was told the pagination bug, the fix (98/100 per batch), and the true full scan results (2,437 buy + 893 sell; phone push top 12 per side); per-ticker reads for IREN, MU, NFLX, APLD, PLTR, HOOD, ASTS, RGTI delivered with numbers.
- Scanner Picks dashboard section, professional UI (1D/1M/6M/1Y charts, last-close watchlist at night), and the full stock detail page (levels, financials, sentiment, scanner verdict) were all announced in chat with a live ASTS link.
- He has SSH access and the three manual scan commands; he wanted to run scans himself.
- Morning 8:00 AM deep scan and 9:30 AM market-open bell are scheduled on the server; first real trading-day test is today.

## Research Trajectory
- *External sources viewed:* none — no web research was needed; the goal's material is fully in the user's own product and conversation.
- *Prior trajectories reviewed:* `workspace/goals/real-time-stock-alert-bot/agent_notes/` (empty — no prior study notes), `workspace/goals/real-time-stock-alert-bot/briefs/` (empty — no prior briefs); repetition search over both directories with variants ("stock alert", "scanner", "sentiment", "dashboard", "detail page", "breakout") returned zero hits. Goal record `user_goal.get` (goal_847428247a63) shows `briefings: []` — no Letter has ever gone out for this goal.
- *Scaffolds and support tactics:* none consulted; goal category `money` is a product-build outcome, and the plan's learning-science scaffolding was treated as scheduler metadata per the step instructions.
- *Connected user data:* none pulled — no relevant device/calendar/finance data needed; the bot's own server state and the conversation carried the evidence.
- *Image references checked:* none.
- *User context and memory:* `~/MEMORY.md` (standing preferences/commitments: /10 ratings, trusted sentiment only, Oracle free-tier choice, paused autopilots), `~/dreams/alignment/derived/ALIGNMENT_SYNTHESIS.md` (first real trading-day test today, delivery evidence owed; audit-hard-after-incident trust dynamic; kitchen shifts Mon/Tue off — today is his day off).
- *Searches skipped or unavailable:* none beyond the empty local repetition search noted above.
