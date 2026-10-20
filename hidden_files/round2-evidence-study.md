# Round 2 Evidence Study — trend overlap, regime split, out-of-sample weights, squeeze-as-filter

Generated 2026-10-20 by `study_round2.py` (analysis only; production scoring unchanged). Companion to `setup-evidence-study.md` (round 1) and `sequence-evidence-study.md`.

## Method

- **Universe:** scanner.SP500_FALLBACK — 465 symbols requested; 460 had >= 300 daily bars (5 skipped). SPY fetched separately for the regime map (570 bars).
- **History:** Alpaca daily bars, days=800 (IEX feed, split-adjusted). First 250 bars per symbol are warmup; evaluation days run 2025-07-15 → 2026-09-22 (136,592 symbol-days). Fit half = days < 2026-02-17; test half = days >= 2026-02-17 (fit 68,276 / test 68,316 symbol-days).
- **Setup detection:** the round-1 precomputed causal series replicating scanner.technical_screen exactly (see study_setups.py).
- **Verification:** round-1 check 250 random (symbol, day) pairs vs scanner.technical_screen — **250/250 exact, 0 mismatches**. Round-2 check 250 pairs: merged-uptrend condition vs a direct SMA computation on the raw prefix, breakout& squeeze and volume& squeeze conjunctions vs the screen, and the SPY regime tag vs a direct prefix-SMA computation — **250/250 exact, 0 mismatches**.
- **Events:** deduped as in round 1 (>= 5 no-fire sessions before a new event). Total deduped setup events: **17,936**, plus the Q1/Q3 conjunction streams (own dedupe). Outcomes identical to round 1 (forward 5/10/20d from the fire day's close, direction-adjusted; target-first +5% before −3% within 20 sessions).
- **Regime tag (as specified for this study):** a day is **bull** if SPY's close that day > SPY's SMA200 that day, else **bear** (binary; production regime.py additionally requires the 20-day SMA slope sign and can say sideways). A stock's day takes the regime of the most recent SPY session on or before it. Eval-window calendar days: 415 bull / 20 bear; symbol-days: 130,677 bull / 5,915 bear / 0 untagged.
- **Baseline:** 20,000 random no-setup (symbol, day) pairs (round-1 reservoir method), tagged by regime and half. Full-window baseline long: win%10 50.0, avg10 +0.15%; short: win%10 50.0, avg10 -0.15%. Regime baselines (long): bull avg10 +0.04% (n=19,092), bear avg10 +2.32% (n=908); short: bull avg10 -0.04%, bear avg10 -2.32%.
- **Low-confidence flag:** any cell with < 100 events is marked ⚠ and no verdict rests on it.

## Q1 — Do the trend components double-count?

Components: A = `trend_above_sma200` (+10), B = `trend_sma50_above_sma200` (+10), C = `golden_cross` (+11). Day-level prevalence over firing days (pre-dedupe): A on 80,738 days, B on 81,143 days, both on 70,572 — **87% of B-days also have A**. Golden cross fires on 2,676 days, of which **89% already have both trend bonuses true**.

### (a) Standalone (full window, round-1 method)

| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| trend_above_sma200 | 10 | 1,312 | 53.9 | +0.38% | +0.58% | +1.34% | +0.67% | 38.8 | -34.6% | +0.43% / +3.9 pts |
| trend_sma50_above_sma200 | 10 | 659 | 56.6 | +0.71% | +0.95% | +1.04% | +1.13% | 40.7 | -31.3% | +0.80% / +6.7 pts |
| golden_cross | 11 | 457 | 51.2 | +0.35% | +0.53% | +0.53% | +0.22% | 34.1 | -24.1% | +0.38% / +1.3 pts |
| merged uptrend (A and B, own stream) | — | 927 | 53.8 | +0.54% | +0.78% | +1.00% | +0.64% | 38.7 | -31.3% | +0.64% / +3.9 pts |

### (b) Conditional edges

B's own deduped events, split by whether A also fired that day:

| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| B events with A |  | 603 | 56.4 | +0.66% | +0.94% | +1.09% | +1.14% | 40.6 | -31.3% | +0.79% / +6.4 pts |
| B events without A |  | 56 ⚠ | 58.9 | +1.19% | +1.12% | +0.58% | +0.94% | 41.1 | -19.6% | +0.97% / +9.0 pts |

C's own deduped events, split by whether both trend bonuses were already true that day:

| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| C events with A and B |  | 423 | 51.1 | +0.34% | +0.47% | +0.47% | +0.22% | 34.0 | -24.1% | +0.32% / +1.1 pts |
| C events without A and B |  | 34 ⚠ | 52.9 | +0.48% | +1.30% | +1.27% | +0.21% | 35.3 | -19.6% | +1.15% / +3.0 pts |

### Q1 verdict

- Merged uptrend vs A alone: **NO CLEAR ADDED EDGE — +0.20% avg 10d / -0.1 pts win rate vs the unconditioned signal; inside the noise band**.
- B-events-with-A vs A alone: **REAL IMPROVEMENT — +0.36% avg 10d and +2.5 pts win rate over the unconditioned signal, clearing the pre-set bar**.
- C-events-with-A-and-B vs the merged uptrend state: **HURTS — -0.31% avg 10d and -2.8 pts win rate vs the unconditioned signal**.
- B without A (SMA50 > SMA200 but price under the 200-day) carries an avg-10d edge of +0.97% vs baseline on 56 events — the direct read on whether B means anything once A is absent.
- Reading: the three components are state descriptions of one fact (an uptrend) far more than three independent signals; the numbers above are the basis for merge/cap vs keep-as-is. (Verdict wording in the console summary follows these same comparisons.)

## Q2 — Regime split and out-of-sample validation of the current weights

Current weights (Oct 20, from scanner.py): bullish — oversold_bounce 0, bollinger_squeeze 0, volume_spike 8, breakout_watch 9, golden_cross 11, trend_above_sma200 10, trend_sma50_above_sma200 10; bearish — overbought_breakdown 25, bollinger_breakdown 20, volume_spike_down 15, low52_breakdown 15, death_cross 15, atr_selloff 10.

### (a) Regime split — 10d performance by SPY regime day

Each cell is measured against the same-regime, same-direction baseline. ⚠ = < 100 events (low confidence). Bear-regime baseline sample sizes: long n=908, short n=908.

**Bullish setups**

| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| oversold_bounce — bull days | 0 | 499 | 50.1 | -0.13% | -0.32% | +0.55% | +0.01% | 33.7 | -62.8% | -0.36% / +0.9 pts |
| oversold_bounce — bear days | 0 | 30 ⚠ | 80.0 | +0.97% | +3.52% | +3.80% | +2.94% | 63.3 | -23.8% | +1.20% / +13.8 pts |
| bollinger_squeeze — bull days | 0 | 985 | 45.8 | -0.09% | -0.10% | +0.28% | -0.48% | 32.1 | -44.3% | -0.14% / -3.4 pts |
| bollinger_squeeze — bear days | 0 | 27 ⚠ | 77.8 | +2.96% | +6.79% | +7.65% | +8.10% | 63.0 | -10.2% | +4.47% / +11.6 pts |
| volume_spike — bull days | 8 | 2,961 | 51.0 | +0.34% | +0.37% | +0.78% | +0.13% | 37.3 | -52.4% | +0.33% / +1.9 pts |
| volume_spike — bear days | 8 | 79 ⚠ | 65.8 | +1.16% | +3.51% | +5.82% | +2.24% | 48.1 | -16.0% | +1.19% / -0.4 pts |
| breakout_watch — bull days | 9 | 1,623 | 52.7 | +0.24% | +0.50% | +0.96% | +0.37% | 36.8 | -86.0% | +0.45% / +3.5 pts |
| breakout_watch — bear days | 9 | 36 ⚠ | 47.2 | +0.30% | +0.05% | +0.87% | -0.30% | 27.8 | -14.3% | -2.27% / -19.0 pts |
| golden_cross — bull days | 11 | 454 | 51.3 | +0.36% | +0.55% | +0.51% | +0.23% | 34.4 | -24.1% | +0.50% / +2.1 pts |
| golden_cross — bear days | 11 | 3 ⚠ | 33.3 | -0.90% | -2.05% | +2.62% | -0.33% | 0.0 | -7.7% | -4.37% / -32.9 pts |
| trend_above_sma200 — bull days | 10 | 1,289 | 53.5 | +0.35% | +0.50% | +1.22% | +0.61% | 38.3 | -34.6% | +0.46% / +4.3 pts |
| trend_above_sma200 — bear days | 10 | 23 ⚠ | 78.3 | +1.88% | +4.80% | +8.17% | +3.64% | 65.2 | -15.8% | +2.48% / +12.1 pts |
| trend_sma50_above_sma200 — bull days | 10 | 656 | 56.7 | +0.72% | +0.96% | +1.04% | +1.14% | 40.9 | -31.3% | +0.92% / +7.5 pts |
| trend_sma50_above_sma200 — bear days | 10 | 3 ⚠ | 33.3 | -0.90% | -2.05% | +2.62% | -0.33% | 0.0 | -7.7% | -4.37% / -32.9 pts |

**Bearish setups**

| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| overbought_breakdown — bull days | 25 | 872 | 48.6 | -0.28% | -0.66% | -0.92% | -0.14% | 33.5 | -89.0% | -0.62% / -2.1 pts |
| overbought_breakdown — bear days | 25 | 14 ⚠ | 50.0 | -0.70% | +2.72% | +6.14% | -0.49% | 28.6 | -1.6% | +5.04% / +16.4 pts |
| bollinger_breakdown — bull days | 20 | 2,902 | 48.2 | -0.42% | -0.41% | -0.86% | -0.25% | 32.2 | -61.8% | -0.37% / -2.5 pts |
| bollinger_breakdown — bear days | 20 | 144 | 21.5 | -2.24% | -4.97% | -7.42% | -4.03% | 11.8 | -105.2% | -2.65% / -12.1 pts |
| volume_spike_down — bull days | 15 | 3,140 | 48.1 | -0.38% | -0.45% | -0.87% | -0.28% | 34.4 | -123.0% | -0.40% / -2.7 pts |
| volume_spike_down — bear days | 15 | 86 ⚠ | 20.9 | -1.62% | -4.66% | -5.68% | -3.72% | 27.9 | -56.6% | -2.34% / -12.7 pts |
| low52_breakdown — bull days | 15 | 768 | 46.1 | -0.33% | -0.32% | -0.56% | -0.51% | 34.6 | -38.7% | -0.28% / -4.7 pts |
| low52_breakdown — bear days | 15 | 41 ⚠ | 39.0 | -0.12% | -1.10% | -1.80% | -1.60% | 31.7 | -40.2% | +1.22% / +5.4 pts |
| death_cross — bull days | 15 | 332 | 48.2 | -0.44% | -0.40% | -1.00% | -0.30% | 35.2 | -25.0% | -0.35% / -2.6 pts |
| death_cross — bear days | 15 | 37 ⚠ | 16.2 | -1.99% | -3.88% | -4.92% | -3.82% | 10.8 | -36.3% | -1.56% / -17.4 pts |
| atr_selloff — bull days | 10 | 913 | 47.5 | -0.39% | -0.70% | -0.89% | -0.37% | 34.0 | -61.8% | -0.66% / -3.2 pts |
| atr_selloff — bear days | 10 | 19 ⚠ | 21.1 | -2.87% | -3.95% | -4.05% | -3.63% | 15.8 | -40.2% | -1.63% / -12.5 pts |

Bear-regime cells with < 100 events: **12 of 13** — the window is mostly bull, so most bear-regime reads are indicative only.

### (b) Out-of-sample: fit half vs test half

Fit = evaluation days < 2026-02-17; test = days >= 2026-02-17. Edges are avg-10d vs the same-half, same-direction baseline.

| Setup | Wt now | Fit edge avg10 | Test edge avg10 | Fit events | Test events |
|---|---|---|---|---|---|
| oversold_bounce | 0 | -0.07% | -0.42% | 271 | 258 |
| bollinger_squeeze | 0 | +0.30% | -0.56% | 622 | 390 |
| volume_spike | 8 | +0.47% | +0.14% | 1,734 | 1,306 |
| breakout_watch | 9 | +1.01% | -0.49% | 942 | 717 |
| golden_cross | 11 | +1.25% | -0.51% | 234 | 223 |
| trend_above_sma200 | 10 | +0.49% | +0.43% | 819 | 493 |
| trend_sma50_above_sma200 | 10 | +1.51% | -0.47% | 437 | 222 |
| overbought_breakdown | 25 | -0.43% | -0.57% | 531 | 355 |
| bollinger_breakdown | 20 | -0.97% | +0.01% | 1,548 | 1,498 |
| volume_spike_down | 15 | -0.57% | -0.25% | 1,811 | 1,415 |
| low52_breakdown | 15 | -0.17% | -0.25% | 397 | 412 |
| death_cross | 15 | -0.66% | -0.54% | 174 | 195 |
| atr_selloff | 10 | -0.51% | -0.94% | 604 | 328 |

**Rank correlations (Spearman):**
- current weight vs test-half edge (bullish): **+0.05**
- current weight vs test-half edge (bearish): **+0.33**
- current weight vs test-half edge (all 13): **-0.04**
- fit-half edge vs test-half edge (bullish): **-0.14** (how rank-stable edges are at all in this window)
- fit-half edge vs test-half edge (bearish): **-0.49** (how rank-stable edges are at all in this window)
- fit-half edge vs test-half edge (all 13): **+0.08** (how rank-stable edges are at all in this window)

**Portfolio rules — mean test-half avg10 edge:**

| Side | Fit-top-half, equal weight | Current weights (weight-proportional, all setups) | All setups, equal weight |
|---|---|---|---|
| bullish | -0.26% (setups: trend_sma50_above_sma200, golden_cross, breakout_watch, trend_above_sma200) | -0.19% | -0.27% |
| bearish | -0.59% (setups: low52_breakdown, overbought_breakdown, atr_selloff) | -0.39% | -0.42% |

Current-weight top halves for reference — bullish: golden_cross, trend_above_sma200, trend_sma50_above_sma200, breakout_watch; bearish: overbought_breakdown, bollinger_breakdown, volume_spike_down (ties at the cutoff broken by rank order).

### Q2 verdict

- Bullish weights vs test-half performance: Spearman +0.05; the naive fit-top-half rule scores -0.26% mean test edge vs -0.19% for the shipped weights (weight-proportional) and -0.27% for all setups equally.
- Bearish weights vs test-half performance: Spearman +0.33; fit-top-half rule -0.59% vs shipped -0.39% vs all-equal -0.42%.
- Whether that reads as 'generalizes' or 'period-fitted' is stated in the console summary from exactly these numbers; the regime table above is the caveat either way — most of both halves are bull-regime days.

## Q3 — Squeeze as a multiplier, not a signal

Pre-set improvement bar (as in the sequence study): >= +0.25% avg 10d AND >= +2 pts win rate vs the unconditioned signal, on >= 100 events.

| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| breakout_watch (all events) | 9 | 1,659 | 52.6 | +0.24% | +0.49% | +0.95% | +0.37% | 36.6 | -86.0% | +0.34% / +2.6 pts |
| breakout_watch WHILE squeeze (own stream) |  | 101 | 47.5 | -0.06% | +0.00% | +0.49% | -0.15% | 29.7 | -25.2% | -0.14% / -2.4 pts |
| breakout_watch events, squeeze that day |  | 37 ⚠ | 59.5 | +0.43% | +2.10% | +3.44% | +1.22% | 43.2 | -15.8% | +1.95% / +9.5 pts |
| breakout_watch events, no squeeze |  | 1,622 | 52.4 | +0.24% | +0.45% | +0.90% | +0.36% | 36.5 | -86.0% | +0.30% / +2.5 pts |
| volume_spike (all events) | 8 | 3,040 | 51.4 | +0.36% | +0.45% | +0.91% | +0.20% | 37.5 | -52.4% | +0.31% / +1.5 pts |
| volume_spike WHILE squeeze (own stream) |  | 45 ⚠ | 48.9 | -1.09% | -0.41% | +0.03% | -0.59% | 44.4 | -26.4% | -0.56% / -1.1 pts |
| volume_spike events, squeeze that day |  | 41 ⚠ | 48.8 | -1.39% | -0.55% | +0.07% | -0.59% | 41.5 | -26.4% | -0.69% / -1.2 pts |
| volume_spike events, no squeeze |  | 2,999 | 51.5 | +0.38% | +0.47% | +0.92% | +0.20% | 37.5 | -52.4% | +0.32% / +1.5 pts |

### Q3 verdict

- breakout_watch while coiled (conjunction stream): **HURTS — -0.49% avg 10d and -5.0 pts win rate vs the unconditioned signal**.
- breakout_watch events with squeeze that day (split view): **LOW-CONFIDENCE — only 37 events (< 100); no verdict drawn**.
- volume_spike while coiled (conjunction stream): **LOW-CONFIDENCE — only 45 events (< 100); no verdict drawn**.
- volume_spike events with squeeze that day (split view): **LOW-CONFIDENCE — only 41 events (< 100); no verdict drawn**.

## Limitations

- **Bear-regime scarcity:** the evaluation window is mostly a rising market — SPY spent most days above its SMA200, so bear-regime cells (and the bear baseline) rest on far fewer events; every such cell is flagged ⚠ in the tables and no strong claim should rest on it. Regime-conditional edges for bearish setups in particular remain close to untested.
- **Binary regime tag:** per this study's spec the tag is SPY close vs SMA200 only; production regime.py also requires the SMA slope sign (bull/bear/sideways), so regime-cell membership differs slightly from what the live gate sees.
- **IEX-only volume:** volume conditions (volume_spike and its squeeze conjunctions) are measured on Alpaca's free IEX tape, a few % of consolidated volume; on the full tape the same conditions may fire differently.
- **No transaction costs:** gross returns only — no commissions, spreads, slippage, borrow costs or taxes.
- **Survivorship bias:** SP500_FALLBACK is a 2026-10-19 snapshot applied retroactively; index leavers/failures in the window are missing, flattering all results.
- **Overlapping conditions:** Q1/Q3 buckets share events by construction (a conjunction event is also an event of each component), so bucket counts are not independent samples; conditional splits of the same event stream are paired comparisons, not fresh evidence.
- **One window, two halves:** the fit/test split is a single chronological cut of a mostly-bull ~1.2-year evaluation window; Spearman values on 6–7 points per side are coarse and a different cut could move them.
- Split-adjusted prices only (no dividend adjustment); no liquidity gate in-study (production additionally requires 20-day avg dollar volume >= $250k — immaterial for this universe).
