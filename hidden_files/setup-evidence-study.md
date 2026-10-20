# Setup Evidence Study — stockbot scanner

Generated 2026-10-20 by `study_setups.py` (analysis only; production scoring unchanged).

## Method

- **Universe:** scanner.SP500_FALLBACK — 465 symbols requested; 460 had >= 300 daily bars and were studied (5 skipped).
- **History:** Alpaca daily bars fetched with days=800 (IEX feed, split-adjusted). First 250 bars per symbol are warmup; evaluation days run 2025-07-15 → 2026-09-21 (136,159 symbol-days).
- **Setup detection:** precomputed causal indicator series replicating scanner.technical_screen line-by-line (RSI/Bollinger/SMA/volume-median/52-week extremes/ATR).
- **Verification:** 250 random (symbol, day) pairs checked against scanner.technical_screen(bars[:i+1]) — **250/250 exact matches, 0 mismatches** across all 13 setups.
- **Setups:** technical_screen defines 13 named setups — 7 bullish (incl. two +5 trend bonuses) and 6 bearish; all 13 are studied.
- **Events:** a setup firing on consecutive days counts once; a new event requires >= 5 no-fire sessions. Total deduped events: **17,869**.
- **Outcomes (from the fire day's close):** forward returns at +5/+10/+20 sessions (direction-adjusted for bearish); target-first = +5% reached before −3% (direction-adjusted) within 20 sessions on daily highs/lows (a day hitting both counts as stop-first).
- **Baseline:** 20,000 random (symbol, day) pairs on which NO setup fired, scored long (for bullish comparisons) and short (for bearish). Baseline long: win%10 50.1, avg10 +0.22%; baseline short: win%10 49.8, avg10 -0.22%.

## Bullish setups

| Setup | Wt now | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| oversold_bounce | 25 | 524 | 51.9 | -0.05% | -0.08% | +0.71% | +0.28% | 35.7 | -62.8% | -0.30% avg10 / +1.8 pts win |
| bollinger_squeeze | 20 | 1009 | 46.5 | +0.00% | +0.08% | +0.46% | -0.40% | 32.9 | -44.3% | -0.13% avg10 / -3.6 pts win |
| volume_spike | 15 | 3034 | 51.4 | +0.36% | +0.45% | +0.92% | +0.19% | 37.6 | -52.4% | +0.23% avg10 / +1.3 pts win |
| breakout_watch | 15 | 1657 | 52.6 | +0.25% | +0.49% | +0.96% | +0.37% | 36.7 | -86.0% | +0.27% avg10 / +2.5 pts win |
| golden_cross | 15 | 456 | 51.3 | +0.37% | +0.55% | +0.56% | +0.23% | 34.2 | -24.1% | +0.33% avg10 / +1.2 pts win |
| trend_above_sma200 | 5 | 1311 | 53.9 | +0.38% | +0.58% | +1.34% | +0.68% | 38.8 | -34.6% | +0.37% avg10 / +3.8 pts win |
| trend_sma50_above_sma200 | 5 | 659 | 56.6 | +0.71% | +0.95% | +1.04% | +1.13% | 40.7 | -31.3% | +0.73% avg10 / +6.5 pts win |

## Bearish setups

| Setup | Wt now | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline |
|---|---|---|---|---|---|---|---|---|---|---|
| overbought_breakdown | 25 | 885 | 48.7 | -0.28% | -0.61% | -0.82% | -0.14% | 33.4 | -89.0% | -0.39% avg10 / -1.1 pts win |
| bollinger_breakdown | 20 | 3034 | 47.0 | -0.52% | -0.63% | -1.17% | -0.42% | 31.1 | -105.2% | -0.41% avg10 / -2.9 pts win |
| volume_spike_down | 15 | 3200 | 47.3 | -0.43% | -0.56% | -0.97% | -0.39% | 34.1 | -123.0% | -0.34% avg10 / -2.5 pts win |
| low52_breakdown | 15 | 806 | 45.9 | -0.31% | -0.33% | -0.58% | -0.52% | 34.5 | -40.2% | -0.11% avg10 / -3.9 pts win |
| death_cross | 15 | 364 | 44.8 | -0.62% | -0.77% | -1.38% | -0.73% | 32.7 | -36.3% | -0.55% avg10 / -5.0 pts win |
| atr_selloff | 10 | 930 | 46.9 | -0.45% | -0.78% | -0.95% | -0.53% | 33.5 | -61.8% | -0.56% avg10 / -2.9 pts win |

## Verdicts

- **oversold_bounce** (bullish, weight 25): **WEAKEN** — edge vs baseline is ~zero/mixed (-0.30% avg 10d, +1.8 pts win rate).
- **bollinger_squeeze** (bullish, weight 20): **WEAKEN** — edge vs baseline is ~zero/mixed (-0.13% avg 10d, -3.6 pts win rate).
- **volume_spike** (bullish, weight 15): **WEAKEN** — edge vs baseline is ~zero/mixed (+0.23% avg 10d, +1.3 pts win rate).
- **breakout_watch** (bullish, weight 15): **KEEP** — positive edge vs baseline (+0.27% avg 10d, +2.5 pts win rate).
- **golden_cross** (bullish, weight 15): **KEEP** — positive edge vs baseline (+0.33% avg 10d, +1.2 pts win rate).
- **trend_above_sma200** (bullish, weight 5): **KEEP** — positive edge vs baseline (+0.37% avg 10d, +3.8 pts win rate).
- **trend_sma50_above_sma200** (bullish, weight 5): **KEEP** — positive edge vs baseline (+0.73% avg 10d, +6.5 pts win rate).
- **overbought_breakdown** (bearish, weight 25): **KILL** — negative edge vs baseline (-0.39% avg 10d, -1.1 pts win rate) — worse than doing nothing.
- **bollinger_breakdown** (bearish, weight 20): **KILL** — negative edge vs baseline (-0.41% avg 10d, -2.9 pts win rate) — worse than doing nothing.
- **volume_spike_down** (bearish, weight 15): **KILL** — negative edge vs baseline (-0.34% avg 10d, -2.5 pts win rate) — worse than doing nothing.
- **low52_breakdown** (bearish, weight 15): **WEAKEN** — edge vs baseline is ~zero/mixed (-0.11% avg 10d, -3.9 pts win rate).
- **death_cross** (bearish, weight 15): **KILL** — negative edge vs baseline (-0.55% avg 10d, -5.0 pts win rate) — worse than doing nothing.
- **atr_selloff** (bearish, weight 10): **KILL** — negative edge vs baseline (-0.56% avg 10d, -2.9 pts win rate) — worse than doing nothing.

Verdict rule: STRENGTHEN = avg-10d edge >= +0.75% and win-rate edge >= +3 pts; KEEP = edge >= +0.25% with win edge >= 0; KILL = edge <= −0.25% with win edge <= 0; WEAKEN otherwise; < 30 events = KEEP (insufficient sample).

## Proposed weights (0–100 scale, same as today)

| Setup | Current | Proposed |
|---|---|---|
| oversold_bounce | 25 | 0 |
| bollinger_squeeze | 20 | 0 |
| volume_spike | 15 | 8 |
| breakout_watch | 15 | 9 |
| golden_cross | 15 | 11 |
| trend_above_sma200 | 5 | 10 |
| trend_sma50_above_sma200 | 5 | 10 |
| overbought_breakdown | 25 | 0 |
| bollinger_breakdown | 20 | 0 |
| volume_spike_down | 15 | 0 |
| low52_breakdown | 15 | 0 |
| death_cross | 15 | 0 |
| atr_selloff | 10 | 0 |

Derivation: within each side, proposed weight ∝ max(0, avg-10d edge vs baseline), scaled so the strongest setup gets 25; KILL verdicts get 0; trend bonuses capped at 10; each side normalised to sum <= 100. These are proposals from this study only — not applied to scanner.py.

## Limitations

- **IEX-only volume:** Alpaca's free IEX feed carries only IEX-exchange volume (~a few % of consolidated tape), so the volume-median spike conditions — and any volume-based result — are measured on a partial tape; on the consolidated tape the same setups may fire differently.
- **No transaction costs:** returns are gross — no commissions, spreads, slippage, borrow costs (for the bearish side) or taxes; real-world results would be lower, especially for the 5-day horizon.
- **Survivorship bias:** the universe is the built-in SP500_FALLBACK snapshot (compiled 2026-10-19) applied retroactively — companies that left the index (or failed) during the window are missing, which flatters results.
- **Window/regime:** the ~2-year window (fetch-bounded by calendar days, ≈575 trading days of bars, ~1.2 years of post-warmup evaluation) covers a mostly-rising market; setup edges — especially trend bonuses and bounce setups — may not transfer to a sustained bear regime. Edges are also measured independently per setup: real scanner scores stack setups, and stacked behaviour is not validated here.
- Split-adjusted prices only (no dividend adjustment), and no liquidity gate was applied in the study (the production scanner additionally requires 20-day avg dollar volume >= $250k — immaterial for this universe).
