"""'What usually happens next' for each chart-reading finding.

Two layers, kept apart on purpose:

1. TEXTBOOK — what the technique is generally expected to lead to, in
   plain words (the trader's playbook).
2. HISTORY — what actually followed the same finding on this bot's own
   data: data/pattern_stats.json, written by study_patterns.py (entry next
   open, exit 5 / 10 sessions later, compared with ANY day in the same
   sample). Shown with the sample size so thin evidence is visible.

Neither is a promise; history is one mostly-bull window.
"""
from __future__ import annotations

import json
import os

STATUS_KEYS = {"Confirmed", "Forming", "Failed", "Breaking out", "Consolidating",
               "Handle forming", "Inside the pattern", "Broke out up", "Broke down",
               "Retest held", "Bull trap", "Bear trap", "Selling climax",
               "Buying climax", "Bullish divergence", "Bearish divergence",
               "Possible reversal", "Pullback", "Bounce in a downtrend",
               "Weak volume", "Uptrend", "Downtrend", "Sideways",
               "Bullish stack", "Bearish stack", "Mixed", "Discount", "Premium"}

STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                          "pattern_stats.json")
MIN_N = 60          # fewer samples than this: don't show a history line


def finding_key(f: dict) -> str:
    """Same key study_patterns.py records under."""
    k = f"{f['name']}|{f['bias']}"
    if f.get("status") in STATUS_KEYS:
        k += f"|{f['status']}"
    return k


# ---- 1. textbook expectations ---------------------------------------------
# name -> {bias: text} ("*" = any bias)
_BOOK: dict[str, dict[str, str]] = {
    "Market structure": {
        "bull": "Uptrends tend to persist. Base case: more higher highs and higher lows until a swing low breaks.",
        "bear": "Downtrends tend to persist. Base case: more lower highs and lower lows until a swing high breaks.",
        "*": "No clear trend. Expect back-and-forth until price breaks out of the range."},
    "Break of structure (BOS)": {
        "*": "A break of structure usually leads to another leg in the same direction, often after a pullback to the broken level."},
    "Change of character (CHoCH)": {
        "*": "An early hint the trend may turn. It needs a follow-through break to confirm, and many fail."},
    "Breakout and retest": {
        "bull": "A held retest is one of the cleaner continuation setups. The move often extends toward the next resistance.",
        "bear": "A failed retest from below often sends price lower toward the next support.",
        "*": "Watch whether the retest holds. That decides the next leg."},
    "False breakout": {
        "*": "Failed breakouts often move fast the other way as trapped traders exit."},
    "Liquidity sweep": {
        "*": "After stops are run, price often snaps back into the prior range."},
    "Fair value gap (FVG)": {
        "*": "Price often returns to fill part of the gap before continuing."},
    "Breaker block": {"*": "Old supply or demand that flipped. Price often reacts there on a retest."},
    "Bullish order block": {"*": "A zone where buyers stepped in before. Price often bounces on a first revisit."},
    "Bearish order block": {"*": "A zone where sellers stepped in before. Price often stalls on a first revisit."},
    "Pullback vs reversal": {
        "Pullback": "Most pullbacks in an uptrend resume the trend. A close below the last swing low would say otherwise.",
        "Bounce in a downtrend": "Bounces in a downtrend usually fade. A close above the last swing high would say otherwise.",
        "Possible reversal": "The drop has broken the pattern of higher lows, so this may be more than a pullback. Wait for a higher low, or a reclaim of the broken level, before treating it as a dip to buy.",
        "*": "Could still go either way. Wait for a swing level to break."},
    "Cup and handle": {"*": "A breakout from the handle on volume often runs about the cup's depth. Failures drop back into the handle."},
    "Rounded bottom": {"*": "A slow base. A break above the rim often starts a new uptrend."},
    "Double top": {"*": "If the neckline breaks, the textbook target is the pattern's height below it. Until then it can still fail."},
    "Double bottom": {"*": "If the neckline breaks, the textbook target is the pattern's height above it. Until then it can still fail."},
    "Head and shoulders": {"*": "A neckline break targets the head-to-neckline distance lower. Unconfirmed patterns fail often."},
    "Inverse head and shoulders": {"*": "A neckline break targets the head-to-neckline distance higher. Unconfirmed patterns fail often."},
    "Ascending triangle": {"*": "Usually breaks up through the flat top, but confirm with a close and volume."},
    "Descending triangle": {"*": "Usually breaks down through the flat bottom, but confirm with a close and volume."},
    "Symmetrical triangle": {"*": "Tends to break in the direction of the prior trend. The break itself is the signal."},
    "Rising wedge": {"*": "Often resolves lower, especially after a long rally."},
    "Falling wedge": {"*": "Often resolves higher, especially after a long decline."},
    "Rising channel": {"*": "Trade the channel until it breaks. A close below the lower line ends it."},
    "Falling channel": {"*": "Trade the channel until it breaks. A close above the upper line ends it."},
    "Rectangle (range)": {"*": "Range trading until a close outside the box. The breakout often runs the range's height."},
    "Bull flag": {"*": "A tight pause after a sharp rise. A break above the flag often runs about the length of the pole."},
    "Bear flag": {"*": "A tight pause after a sharp drop. A break below the flag often runs about the length of the pole."},
    "Pennant": {"*": "A small symmetrical pause after a sharp move. It usually breaks in the direction of that move."},
    "Bullish pennant": {"*": "A small pause after a sharp rise. Textbook says it breaks up; confirm with a close above it on volume."},
    "Bearish pennant": {"*": "A small pause after a sharp drop. Textbook says it breaks down; confirm with a close below it on volume."},
    "Breakdown and retest": {"*": "If the retest from below fails, price often heads to the next support. Reclaiming the level cancels it."},
    "Anchored VWAP (from 52-week high)": {"*": "The average price paid since the high. Holding above it means those buyers are back in profit; below it, rallies often stall there."},
    "Anchored VWAP (from 52-week low)": {"*": "The average price paid since the low. Losing it means buyers since the bottom are under water, which often brings selling."},
    "Previous day high / low": {"*": "Yesterday's high and low are the first levels day traders watch. A break often runs; a rejection often reverses."},
    "Relative volume": {"*": "Unusual volume means something is happening. Moves on heavy volume follow through more often."},
    "Volume profile": {"*": "Price tends to gravitate to the high-volume price (POC) and move fast through low-volume areas."},
    "Breakaway gap": {"*": "Breakaway gaps often don't fill soon and can start a new trend."},
    "Runaway gap": {"*": "Shows strong momentum. The trend often continues; the gap can act as support or resistance."},
    "Exhaustion gap": {"*": "Can mark the end of the move. A reversal or pause is common."},
    "Common gap": {"*": "Common gaps usually fill and mean little alone."},
    "Unfilled gap": {"*": "Open gaps often act as magnets or as support and resistance."},
    "Volume climax": {"*": "Climax volume often marks exhaustion. A short-term reversal or pause is common."},
    "Volume confirmation": {"*": "Moves on heavy volume are more likely to continue than moves on light volume."},
    "Price–volume divergence": {"*": "Price moving without volume often stalls."},
    "Support": {"*": "Expect buyers to show up here. A close below on volume often turns it into resistance."},
    "Resistance": {"*": "Expect sellers here. A close above on volume often turns it into support."},
    "Fibonacci retracement": {"*": "Pullbacks often pause or turn around 38 to 62% of the prior move."},
    "RSI (14)": {
        "bull": "Oversold readings often bounce within days in an uptrend, but can stay oversold in a downtrend.",
        "bear": "Overbought readings often cool off, but strong trends can stay overbought for weeks.",
        "*": "Neutral momentum. RSI adds little here on its own."},
    "MACD (12, 26, 9)": {"*": "A MACD cross is a lagging shift in momentum. It works best in the direction of the bigger trend."},
    "Stochastic (14, 3)": {"*": "Fast oscillator. Best for timing entries in the direction of the trend, not for calling tops or bottoms."},
    "CCI (20)": {"*": "Extremes often mean-revert in ranges but can keep running in strong trends."},
    "Bollinger Bands (20, 2)": {"*": "Closes outside the bands often snap back toward the middle band unless a strong trend is starting."},
    "ADX (14)": {"*": "Strong ADX says the trend is likely to continue. Low ADX favours range trading."},
    "Ichimoku cloud": {"*": "Above the cloud favours longs, below favours shorts. Inside it is a no-trade zone for many."},
    "Parabolic SAR": {"*": "Trend-following trailing stop. Flips are frequent in choppy markets."},
    "Donchian channel (20)": {"*": "New 20-day highs or lows are the classic trend-following entry. Many fail in ranges."},
    "On-balance volume (OBV)": {"*": "OBV moving with price confirms the move. Divergence warns of a turn."},
    "Relative strength vs SPY": {"*": "Leaders tend to keep leading in the short run, and laggards keep lagging."},
    "52-week high / low": {"*": "Near 52-week highs stocks keep making highs more often than people expect. Near lows, the opposite."},
    "Moving-average trend": {"*": "Above rising averages, dips to them tend to be bought. Below falling ones, rallies tend to be sold."},
    "SMA (20 / 50 / 200)": {"*": "Above rising averages, dips to them tend to be bought. Below falling ones, rallies tend to be sold."},
    "EMA (9 / 21)": {"*": "Short-term trend gauge. Pullbacks to the 21 EMA are a common entry in strong trends."},
    "VWAP (today)": {"*": "Intraday bias. Above VWAP, dips toward it tend to hold. Below it, rallies toward it tend to fail."},
    "Opening range (first 30 min)": {"*": "A break of the first 30-minute range often sets the day's direction."},
    "Multi-timeframe": {"*": "Trades with the weekly trend tend to work better than trades against it."},
    "Premium / discount": {"*": "Buyers prefer the discount half of the range, sellers the premium half."},
    "Equal highs": {"*": "Stops collect above equal highs. Price is often drawn there before reversing."},
    "Equal lows": {"*": "Stops collect below equal lows. Price is often drawn there before reversing."},
}
_CANDLE = {
    "bull": "A bullish candle signal needs a follow-through day (a close above its high) to confirm. On its own it is weak evidence, stronger at support.",
    "bear": "A bearish candle signal needs a follow-through day (a close below its low) to confirm. On its own it is weak evidence, stronger at resistance.",
    "neutral": "Indecision. The next candle's direction usually matters more than this one.",
}
CANDLES = {"Hammer", "Shooting star", "Bullish engulfing", "Bearish engulfing", "Doji",
           "Morning star", "Evening star", "Three white soldiers", "Three black crows",
           "Piercing line", "Dark cloud cover", "Inside bar", "Outside bar"}


def textbook(f: dict) -> str | None:
    if f["name"] in CANDLES:
        return _CANDLE.get(f["bias"])
    book = _BOOK.get(f["name"])
    if not book:
        return None
    return book.get(f.get("status")) or book.get(f["bias"]) or book.get("*")


# ---- 2. history -------------------------------------------------------------
_STATS: dict | None = None
_STATS_MTIME = 0.0


def _load() -> dict:
    global _STATS, _STATS_MTIME
    try:
        m = os.path.getmtime(STATS_PATH)
    except OSError:
        return {}
    if _STATS is None or m != _STATS_MTIME:
        try:
            with open(STATS_PATH) as fh:
                _STATS = json.load(fh)
            _STATS_MTIME = m
        except (OSError, ValueError):
            _STATS = {}
    return _STATS or {}


def history(f: dict) -> dict | None:
    """{n, up10, avg10, base_up10, base_avg10, edge} or None if thin/missing."""
    data = _load()
    st = (data.get("stats") or {}).get(finding_key(f))
    base = data.get("base") or {}
    if not st or st.get("n", 0) < MIN_N or not base:
        return None
    return {"n": st["n"], "up5": st["up5"], "up10": st["up10"],
            "avg5": st["avg5"], "avg10": st["avg10"], "med10": st.get("med10"),
            "base_up10": base.get("up10"), "base_avg10": base.get("avg10"),
            "edge10": round(st["avg10"] - (base.get("avg10") or 0), 2)}


def history_text(h: dict, bias: str = "neutral") -> str:
    edge = h["edge10"]
    base = (f"In {h['n']:,} past cases the stock was higher 10 trading days later "
            f"{h['up10']:.0f}% of the time (any day: {h['base_up10']:.0f}%), average move "
            f"{h['avg10']:+.1f}% (any day: {h['base_avg10']:+.1f}%).")
    if abs(edge) < 0.3:
        verdict = " About the same as a random day, so this alone gave no real edge."
    elif bias == "bull":
        verdict = (" Better than a random day, so it helped a little." if edge > 0 else
                   " Worse than a random day, so on its own it did not help here.")
    elif bias == "bear":
        verdict = (" Weaker than a random day, so the warning helped a little." if edge < 0 else
                   " Stronger than a random day: this warning usually failed and the stock "
                   "bounced, which is common in a rising market.")
    else:
        verdict = (" Better than a random day." if edge > 0 else " Worse than a random day.")
    return base + verdict


def annotate(sections) -> None:
    """Attach 'next' (textbook) and 'odds' (history) to active findings."""
    for sec in sections:
        items = sec["items"] if isinstance(sec, dict) else sec
        for f in items:
            if not f.get("active"):
                continue
            t = textbook(f)
            if t:
                f["next"] = t
            h = history(f)
            if h:
                f["odds"] = h
                f["odds_text"] = history_text(h, f.get("bias", "neutral"))
