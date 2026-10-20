# Sequence Evidence Study — confirmed vs raw breakouts

Generated 2026-10-20 by `study_sequences.py` (analysis only; production scoring unchanged). Companion to `setup-evidence-study.md`.

## Method

- **Universe:** scanner.SP500_FALLBACK — 465 symbols requested; 460 had >= 300 daily bars and were studied (5 skipped).
- **History:** Alpaca daily bars fetched with days=800 (IEX feed, split-adjusted) — identical fetch to study 1. First 250 bars per symbol are warmup; evaluation days run 2025-07-15 → 2026-09-22 (136,413 symbol-days).
- **Events (all bullish):** A = close crosses above the Bollinger(20, 2σ) upper band (yesterday's close <= yesterday's band). B = A + volume >= 1.5x median of the prior 20 sessions. C = A + the scanner's bollinger_squeeze condition (bandwidth at its 126-day low, within 2%) held on >= 1 of the prior 10 sessions (breakout day excluded). D = A + B + C. E = second consecutive close above the band (event day = second close), plus an E+vol split with the volume condition. F = close above the band on >= 3 of the last 5 sessions (event day = first day the count reaches 3) — a momentum-continuation event, not an entry-from-flat.
- **Dedupe:** identical to study 1 — an event counts once per variant per symbol; a new event requires >= 5 no-fire sessions. Total deduped events: **10,486**.
- **Outcomes (from the event day's close):** forward returns at +5/+10/+20 sessions; target-first = +5% reached before −3% within 20 sessions on daily highs/lows (a day hitting both counts as stop-first) — identical machinery to study 1.
- **Baseline:** 20,000 random (symbol, day) pairs on which NO scanner setup fired (identical construction to study 1), scored long: win%10 49.9, avg10 +0.20%.
- **Verification:** 250 random (symbol, day) pairs — Bollinger mid/upper/lower and bandwidth from the study's series vs indicators.bollinger_bands / bollinger_bandwidth on the prefix, the per-day squeeze condition vs scanner.technical_screen's bollinger_squeeze flag, and the prior-20 volume median vs a direct computation — **250/250 exact matches, 0 mismatches**. (The raw cross itself is a new definition; its components — band level and close vs band — are what is verified.)
- **Low-confidence flag:** variants with < 100 events are flagged ⚠ and no verdict is drawn from them.

## Results

| Variant | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d | Med 10d | Target-first % | Worst 20d | Edge vs baseline | Edge vs A |
|---|---|---|---|---|---|---|---|---|---|---|
| A — raw breakout | 3512 | 51.0 | +0.21% | +0.34% | +0.82% | +0.13% | 36.4 | -49.1% | +0.14% avg10 / +1.1 pts win | — (control) |
| B — breakout + volume | 1622 | 51.2 | +0.36% | +0.47% | +0.97% | +0.17% | 37.3 | -49.1% | +0.27% avg10 / +1.2 pts win | +0.13% avg10 / +0.2 pts win |
| C — squeeze → breakout | 550 | 51.3 | +0.15% | +0.34% | +0.84% | +0.11% | 35.3 | -41.4% | +0.14% avg10 / +1.3 pts win | +0.00% avg10 / +0.3 pts win |
| D — squeeze → breakout + volume | 228 | 50.9 | +0.05% | +0.38% | +0.78% | +0.10% | 38.2 | -28.9% | +0.18% avg10 / +0.9 pts win | +0.04% avg10 / -0.1 pts win |
| E — confirmed (2nd close) | 2145 | 51.2 | +0.09% | +0.30% | +0.73% | +0.17% | 34.0 | -86.0% | +0.10% avg10 / +1.3 pts win | -0.04% avg10 / +0.2 pts win |
| E+vol — confirmed + volume | 957 | 52.8 | +0.48% | +0.61% | +0.99% | +0.33% | 35.4 | -86.0% | +0.41% avg10 / +2.8 pts win | +0.27% avg10 / +1.8 pts win |
| F — walking the band | 1472 | 51.3 | +0.02% | +0.23% | +0.83% | +0.20% | 33.8 | -51.4% | +0.03% avg10 / +1.4 pts win | -0.11% avg10 / +0.3 pts win |

Baseline (no setup, long): 20,000 days, win%10 49.9, avg10 +0.20%, target-first 36.0%.

### Detail (remaining horizons)

| Variant | Win% 5d | Win% 20d | Med 5d | Med 20d |
|---|---|---|---|---|
| A — raw breakout | 50.5 | 52.6 | +0.04% | +0.50% |
| B — breakout + volume | 51.5 | 53.6 | +0.12% | +0.66% |
| C — squeeze → breakout | 48.5 | 52.0 | -0.11% | +0.40% |
| D — squeeze → breakout + volume | 51.3 | 53.5 | +0.11% | +0.59% |
| E — confirmed (2nd close) | 49.2 | 52.9 | -0.08% | +0.61% |
| E+vol — confirmed + volume | 53.0 | 53.0 | +0.31% | +0.64% |
| F — walking the band | 49.9 | 53.1 | -0.02% | +0.61% |

## Verdicts

Criterion: a confirmation 'adds real edge' if, vs variant A, it gains >= +0.25% avg 10d return AND >= +2 pts 10d win rate on >= 100 events (study 1's KEEP-threshold spirit); symmetric thresholds for 'hurts'.

- **A — raw breakout**: CONTROL. Raw upper-band crosses: 3512 events, win%10 51.0, avg10 +0.34% vs baseline +0.20% — the yardstick every confirmation below is measured against.
- **B — breakout + volume**: NO CLEAR ADDED EDGE — vs raw breakouts +0.13% avg 10d / +0.2 pts win rate on 1622 events; inside the noise band.
- **C — squeeze → breakout**: NO CLEAR ADDED EDGE — vs raw breakouts +0.00% avg 10d / +0.3 pts win rate on 550 events; inside the noise band.
- **D — squeeze → breakout + volume**: NO CLEAR ADDED EDGE — vs raw breakouts +0.04% avg 10d / -0.1 pts win rate on 228 events; inside the noise band.
- **E — confirmed (2nd close)**: NO CLEAR ADDED EDGE — vs raw breakouts -0.04% avg 10d / +0.2 pts win rate on 2145 events; inside the noise band.
- **E+vol — confirmed + volume**: NO CLEAR ADDED EDGE — vs raw breakouts +0.27% avg 10d / +1.8 pts win rate on 957 events; inside the noise band.
- **F — walking the band**: NO CLEAR ADDED EDGE — vs raw breakouts -0.11% avg 10d / +0.3 pts win rate on 1472 events; inside the noise band.

## Recommendation

Ranked by avg-10d edge vs the no-setup baseline, the strongest variant is **E+vol — confirmed + volume** (+0.41% avg 10d, +2.8 pts win rate, 957 events).
No confirmation variant cleared the 'adds real edge' bar against raw breakouts. On this evidence the honest move is to add NO new breakout signal: variant A's own edge vs baseline is +0.14% avg 10d, and layering volume/squeeze/second-close conditions on top does not improve it beyond noise. The existing breakout_watch (proximity to the 52-week high, weight 9) remains the scanner's breakout exposure.
These are proposals from this study only — not applied to scanner.py.

## Limitations

- **IEX-only volume:** Alpaca's free IEX feed carries only IEX-exchange volume (~a few % of consolidated tape), so the volume-median conditions in variants B, D and E+vol — and any volume-based result — are measured on a partial tape; on the consolidated tape the same variants may fire differently (relatively more often on high-attention names).
- **No transaction costs:** returns are gross — no commissions, spreads, slippage or taxes; real-world results would be lower, especially for the 5-day horizon.
- **Survivorship bias:** the universe is the built-in SP500_FALLBACK snapshot (compiled 2026-10-19) applied retroactively — companies that left the index (or failed) during the window are missing, which flatters results.
- **Window/regime:** the ~2-year window (fetch-bounded by calendar days, ≈575 trading days of bars, ~1.2 years of post-warmup evaluation) covers a mostly-rising market; breakout behaviour — especially momentum continuation (variant F) — may not transfer to a sustained bear or choppy regime. Variants are also evaluated independently: B–F overlap heavily with A and with each other, event counts are not independent samples, and stacked-signal behaviour with the rest of the scanner's setups is not validated here.
- Split-adjusted prices only (no dividend adjustment), and no liquidity gate was applied in the study (the production scanner additionally requires 20-day avg dollar volume >= $250k — immaterial for this universe). Bands use split-adjusted closes on the IEX feed; a close exactly at the band edge is a measure-zero case decided by floating point.
