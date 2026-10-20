"""Market scanner: screens ALL tradable US stocks (not just the S&P 500) for
bullish (BUY) and bearish (SELL) setups from the guide's alert library, and
pushes labeled alerts to ntfy. The user decides what to buy or sell — the
scanner only hunts setups; nothing here trades.

Pipeline
--------
1. Trading-day guard (sessions.py calendar) — quiet exit on holidays/weekends.
2. Universe: every tradable US equity from Alpaca
   (GET /v2/assets?status=active&asset_class=us_equity, tradable only),
   cached in SQLite, refreshed weekly. Fallback chain if the fetch fails:
   Wikipedia S&P 500 -> datahub CSV -> hardcoded S&P 500 snapshot.
3. Liquidity filter: 20-day average dollar volume must exceed $250k/day.
   This drops dead tickers but keeps penny stocks — the SCREEN has no
   price floor (a $0.40 stock with real volume is screened and lands on
   the dashboard lists), but PUSHES require price >= $3.00
   (PUSH_MIN_PRICE): penny names are exactly where volume spikes and
   breakouts are noisiest, so they never reach the phone.
4. Technical screen on 300 daily bars per symbol (Alpaca REST, multi-symbol
   batches of 100). Bullish and bearish setups are scored separately
   (weights below; each side's raw sum is capped at 100, but note the
   scales differ — bull setups sum to at most 28, bear setups to 100):

   BULLISH (buy setups)
   | setup            | condition (daily bars, closed candles)      | pts |
   |------------------|---------------------------------------------|-----|
   | oversold_bounce  | RSI(14) < 30 and turning up                 |   0 |
   | bollinger_squeeze| bandwidth at 126-trading-day low            |   0 |
   | golden_cross     | SMA50 crossed above SMA200 in last 5 days   |   0 |
   | volume_spike     | last volume > 2x 20-day median, green day   |   8 |
   | breakout_watch   | close within 2% below 52-week high          |   9 |
   | uptrend          | close > SMA200 AND SMA50 > SMA200           |  11 |

   (Weights re-fit Oct 20, 2026 from the setup evidence studies: trend
   carried the edge; oversold_bounce and bollinger_squeeze scored 0.
   Round 2 showed the three trend signals are one fact — 87-89% co-fire,
   and the golden cross adds nothing once the uptrend is known — so they
   merged into a single `uptrend` worth 11 and the cross dropped to 0.
   All zero-weight setups are still detected and shown; they just add
   no points.)

   BEARISH (sell setups)
   | setup               | condition                                   | pts |
   |---------------------|---------------------------------------------|-----|
   | overbought_breakdown| RSI(14) > 70 and turning down               |  25 |
   | bollinger_breakdown | close below lower Bollinger band (20, 2σ)   |  20 |
   | volume_spike_down   | volume > 2x 20-day median, red day          |  15 |
   | low52_breakdown     | close within 2% above 52-week low           |  15 |
   | death_cross         | SMA50 crossed below SMA200 in last 5 days   |  15 |
   | atr_selloff         | single-day drop > 2x ATR(14)                |  10 |

   A symbol is classified BUY when its bull score AS A FRACTION OF THE
   BULL SCALE (score / 28) is >= the bear score's fraction of the bear
   scale (score / 100), else SELL (ties go bullish). Raw sums are never
   compared across scales — that tilted borderline names toward SELL.
5. Fundamental overlay for the top ~25 scorers per side
   (fundamentals.py, 24h cache; missing data scores 0, never fetched for
   the whole universe). Bullish: Piotroski/Altman/DCF as documented;
   bearish uses the mirror (weak fundamentals ADD to a sell score).

Watch mode (`python -m scanner --watch`, every 15 min during market hours):
technical screen only (no fundamentals), pushes immediately when a symbol
NEWLY matches (its side's score clears that side's bar — explicit raw
values: buy 20, sell 30; see the push-bars note in the regime section
below). Dedupe: flagged symbols persist in SQLite per
trading day — one alert per symbol PER SIDE per day ("SYM|SIDE" entries;
a BUY alert does not mute a later SELL flip, and legacy plain "SYM"
entries mute both sides); the morning scan resets the seen-list.
Today's bar is partial in watch mode, so the volume-spike threshold is
scaled by the elapsed session fraction (minutes since 09:30 ET / 390,
clamped [0.05, 1]) — a 10:00 run expects ~8% of a full day's volume, not
a full day's. RSI and close-vs-open keep as-of-now semantics. Pushes
require price >= $3.00, same as the morning scan.
Guards: weekdays + trading day + 09:30-16:00 ET via the sessions.py
calendar; exits quietly otherwise.

Display: every setup shown to the user carries a rating out of 10 with one
decimal (e.g. 6.8/10) derived from the internal 0-100 composite score via
_rating() — display-only; all internal scores and thresholds stay 0-100.

Sentiment (sentiment.py): ranked candidates (morning scan) and newly
matched symbols (watch mode) get a sentiment overlay from two free Finnhub
feeds (candidates only — never the full universe; each leg cached 24h):
headline tone from /company-news (7-day window, keyword lexicon) and
analyst recommendations from /stock/recommendation (latest period,
(2×strongBuy + buy − sell − 2×strongSell) / total). Combined total =
0.5 × news + 0.5 × analyst, in [-1, +1]; it shifts the /10 rating by
total × 1.5, clamped to [0, 10]. The breakdown shows in each alert, e.g.
"news: +0.6 | analysts: 14 buy / 4 hold / 1 sell". No coverage on either
leg → no sentiment line (neutral).

Earnings flag: push candidates (morning scan) and new watch matches whose
company reports earnings within the next 3 TRADING days get a
"⚠ earnings <Mon D>" note appended to their reasons — information only,
it never suppresses a candidate. (Trading days, not calendar days, so a
Friday scan flags Monday earnings.) Data: Finnhub /calendar/earnings,
cached per symbol for 24h in poller_state (key earnings_<SYM>); any
failure (no key, API error) means no flag, never a broken scan.

Push bars are EXPLICIT raw values, calibrated to setup combinations
(morning on the composite, watch on the tech score): morning buy 20 /
sell 50, watch buy 20 / sell 30. Buy bar 20 = uptrend (11) +
breakout_watch (9), or two setups plus fundamental help — a single
setup never pushes. The bars used to be fractions of each side's scale
(40% morning / 30% watch), but that quantized badly on the coarse bull
scale (only three scored setups, max 28): the morning buy bar landed
at 21 — secretly all three setups, since 11 + 9 = 20 < 21 — and the
watch buy bar landed at 8, BELOW the single uptrend weight, so the
first live watch pass on Oct 20 matched 2,002 buy names and hammered
Finnhub into 429s. Fractions were dropped for bars; classify_side
still compares score fractions across the two scales, which is where
normalization actually matters.

Regime gate (push only): when the market regime (regime.py, SPY vs
SMA200) fights a side — bear regime for buys, bull regime for sells,
sideways for BOTH — that side's push bar rises by raw points (buy +5,
sell +15, same for morning and watch). Effective bars: morning buy
20 -> 25, sell 50 -> 65; watch buy 20 -> 25, sell 30 -> 45. The full
dashboard lists are never gated: every setup persists with its true
score/rating. Unknown regime → no gating (fail open).

Parity with the 39-symbol alert engine: the scanner runs the SAME indicator
math (RSI, Bollinger, SMA crosses, 52-week extremes, volume spikes, ATR)
but on daily bars every 15 minutes across the whole universe, while the
engine evaluates per-tick on streaming data for 39 symbols. Engine-only
features (user-set price levels, intraday % moves, news/corporate-action
event feeds) are not part of the scan.

Free-tier discipline: Alpaca REST is batched (100 symbols/call); Finnhub at
most ~25 calls/day per side for the fundamental overlay (24h cache, 1.5s
spacing). Nothing here touches the live bot's watchlist, alerts, or
dashboard.

Run:  python -m scanner               (morning scan + push)
      python -m scanner --dry-run     (print ranked list, no push)
      python -m scanner --watch       (real-time watch: technical screen only,
                                       pushes immediately on NEW setup matches)
      python -m scanner --watch --dry-run  (watch once, print, no push)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import re
import statistics
import time
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional, mirrors config.py
    pass

import indicators
import sessions as sessions_mod
from config import Settings
from notify import send_ntfy
from storage import Store

import fundamentals
import regime as regime_mod
import sentiment

log = logging.getLogger("stockbot.scanner")

# ---------------------------------------------------------------------------
# Scoring weights (kept as data so tests can assert the documented values)
# ---------------------------------------------------------------------------
WEIGHTS = {  # bullish side (kept under the historic name for compatibility)
    # Re-weighted Oct 20, 2026 from the setup evidence studies
    # (17,869 events, Jul 2025-Sep 2026). Trend carried the edge;
    # oversold bounce and Bollinger squeeze showed no edge and carry 0.
    # Round 2 (round2-evidence-study.md) showed the three trend setups
    # co-fire 87-89% of the time and the golden cross adds nothing once
    # the uptrend is known — merged into one `uptrend` worth 11, and
    # golden_cross dropped to 0. Zero-weight setups are still detected
    # and shown; they just add no score.
    "oversold_bounce": 0,
    "bollinger_squeeze": 0,
    "golden_cross": 0,
    "volume_spike": 8,
    "breakout_watch": 9,
    "uptrend": 11,
}
BEAR_WEIGHTS = {  # bearish side
    "overbought_breakdown": 25,
    "bollinger_breakdown": 20,
    "volume_spike_down": 15,
    "low52_breakdown": 15,
    "death_cross": 15,
    "atr_selloff": 10,
}
TECH_CAP = 100
BEAR_CAP = 100
MIN_TECH_BARS = 60        # need at least this many daily bars to score
FUND_TOP_N = 25           # fundamental overlay applies to top-N per side
SCAN_TOP_N = 12           # candidates pushed per side
UNIVERSE_TTL_DAYS = 7
UNIVERSE_CACHE_KEY = "scanner_universe_v2"  # bumped: universe is now all US equities
WATCH_SEEN_KEY = "scanner_watch_seen"  # {"date": "YYYY-MM-DD", "symbols": [...]}
SCAN_MORNING_KEY = "scanner_morning"  # poller key: ranked lists for the dashboard
SCAN_WATCH_KEY = "scanner_watch"      # poller key: today's watch matches
WATCH_PUSH_MAX = 15        # cap symbols listed in one watch push

# --- Push bars: explicit raw values (calibrated to setup combinations) -----
# classify_side still compares each side's score AS A FRACTION of its own
# scale (bull max 28 vs bear max 100) — normalization matters for that
# cross-scale comparison, and it stays. The push BARS, however, are raw
# points. They used to be fractions of each side's scale (morning 40%,
# watch 30%, regime gate +15%), but fractions quantize badly on the
# coarse bull scale, which has only three scored setups (uptrend 11,
# breakout_watch 9, volume_spike 8; goldens/bounces/squeezes score 0):
#   * morning 40% x (28 tech + 25 bonus) = 21 secretly required ALL THREE
#     setups — uptrend + breakout alone is 11 + 9 = 20 < 21
#   * watch 30% x 28 = 8 sat BELOW the single uptrend weight, so the
#     first live watch pass (Oct 20) matched 2,002 buy names and drove
#     Finnhub sentiment calls into 429s
# Raw calibration (morning is on the composite, watch on tech alone):
#   morning buy 20 = uptrend + breakout, or two setups + fundamental help
#   morning sell 50 ~ three stacked bear setups
#   watch buy 20 / sell 30 — same idea without the fundamental overlay
# The regime gate adds RAW points to the fighting side's bar (sideways
# gates both): buy +5, sell +15, morning and watch alike — so the gated
# bars are morning 25/65 and watch 25/45.
BULL_TECH_MAX = sum(WEIGHTS.values())        # 28 — derived, never hardcoded
BEAR_TECH_MAX = sum(BEAR_WEIGHTS.values())   # 100 — derived, never hardcoded
# Informational ceilings only (the scale the old fraction bars used);
# bars no longer derive from these.
FUND_BONUS_MAX = 25    # fundamental_bonus ceiling: Piotroski +10, Altman +5, DCF +10
SHORT_BONUS_MAX = 30   # short_bonus ceiling: Piotroski +10, Altman +10, DCF +10
MORNING_BUY_BAR = 20    # composite: uptrend + breakout, or 2 setups + funds
MORNING_SELL_BAR = 50   # composite: ~three stacked bear setups
WATCH_BUY_BAR = 20      # tech only (watch runs no fundamental overlay)
WATCH_SELL_BAR = 30     # tech only
GATE_BUY_BONUS = 5      # raw points added to a regime-fighting buy bar
GATE_SELL_BONUS = 15    # raw points added to a regime-fighting sell bar
PUSH_MIN_PRICE = 3.0       # pushes require price >= $3.00; the persisted
                           # dashboard lists are NOT price-filtered
BARS_BATCH_SIZE = 100      # symbols per Alpaca multi-bars call
ALPACA_REQ_PER_MIN = 200   # free-tier REST limit (budget check below)
# Liquidity filter: 20-day average dollar volume must clear this. No price
# floor — penny stocks pass on volume alone; dead tickers don't.
LIQ_MIN_DOLLAR_VOL = 250_000.0
LIQ_LOOKBACK_DAYS = 20
ET = ZoneInfo("America/Toronto")
SP500_SNAPSHOT_DATE = "2026-10-19"  # when the fallback list was compiled

# S&P 500 constituent snapshot (fallback + seed for the weekly refresh).
# Membership drifts; refresh_universe() prunes delisted symbols via Alpaca.
# Full alphabetical constituent list (snapshot; see SP500_SNAPSHOT_DATE).
SP500_FALLBACK = tuple(sorted(set(
    "AAPL ABBV ABT ACGL ACN ADBE ADI ADM ADP ADSK AEE AEP AES AFL AIG AIZ "
    "AJG AKAM ALB ALGN ALL ALLE AMAT AMCR AMD AME AMGN AMP AMT AMZN ANET "
    "ANSS AON AOS APD APH APTV ARE ASML ATO AVB AVGO AVY AWK AXON AXP AZO "
    "BA BAC BALL BAX BBWI BBY BDX BEN BF.B BG BH BHF BIIB BK BKNG BKR BLK "
    "BLDR BMY BR BRK.B BRO BSX BWA BX BXP C CAG CAH CARR CAT CB CBOE CBRE "
    "CCI CDNS CDW CE CEG CF CFG CHD CHRW CHTR CI CINF CL CLX CMCSA CME CMG "
    "CMI CMS CNP COF COIN COP COR COST CPAY CPB CPRT CPT CRL CRWD CSCO CSGP "
    "CSX CTAS CTRA CTSH CTVA CVS CVX D DAL DASH DD DE DECK DELL DFS DG DHI "
    "DHR DIS DLR DLTR DOV DOW DPZ DRI DTE DUK DVA DVN EA EBAY ECL ED EFX "
    "EIX EL ELV EMR ENPH EOG EPAM EQIX EQR EQT ERIE ES ESS ETN ETR EVRG EW "
    "EXC EXPD EXPE EXR F FANG FAST FCX FDS FDX FE FI FICO FIS FITB FMC FOX "
    "FOXA FRT FSLR FTNT FTV GD GDDY GE GEHC GEN GIS GL GLW GM GNRC GOOG "
    "GOOGL GPC GPN GRMN GS GWW HAL HAS HBAN HCA HD HES HIG HII HLT HOLX "
    "HON HOOD HPE HPQ HST HUBB HUM HWM IBM IBKR ICE IDXX IEX IFF ILMN INCY "
    "INTC INTU INVH IP IPG IQV IR IRM ISRG IT ITW IVZ J JCI JNJ JPM K KDP "
    "KEY KEYS KHC KIM KLAC KMB KMI KR KVUE L LHX LKQ LLY LMT LNT LOW LRCX "
    "LULU LUV LW LYV MA MAN MANT MCHP MCD MCK MCO MDLZ MDT MET META MGM "
    "MHK MKC MKTX MLM MMC MMM MNST MO MOS MPC MRK MS MSCI MSI MTB MTCH MTD "
    "MU NDAQ NEE NEM NFLX NI NKE NOC NOW NRG NSC NTAP NTRS NUE NVDA NVR NWL "
    "NWS NWSA NXPI O ODFL OKE OMC ORCL ORLY OTIS OXY PANW PARA PAYC PAYX "
    "PCAR PCG PFG PG PGR PH PHM PKG PLD PLTR PM PNC PNR PNW PODD POOL PPG "
    "PPL PRU PSA PSX PTC PYPL QCOM QRVO RCL REG REGN RF RHI RJF RL RMD ROK "
    "ROL ROP ROST RSG RTX SBUX SCHW SHW SJM SLB SMCI SNA SNPS SO SOLV SPG "
    "SPGI SRE STE STLD STT STX STZ SW SWK SWKS SYF SYK SYY T TAP TDG TDY "
    "TECH TEL TER TFC TGT TJX TKO TMO TMUS TPR TRGP TRV TSCO TSLA TSN TT "
    "TTWO TXN TXT TYL UAL UBER UDR UHS ULTA UNH UNM UNP UPS URI USB V VICI "
    "VLO VLTO VMC VRSK VRSN VRTX VST VTR VZ WAB WAT WBD WDAY WDC WEC WELL "
    "WFC WHR WM WMB WMT WRB WRK WSM WST WTW WY WYNN XEL XOM XYL YUM ZBH "
    "ZBRA ZTS".split()
)))


# ---------------------------------------------------------------------------
# Alpaca REST (daily bars, multi-symbol batches)
# ---------------------------------------------------------------------------
class AlpacaREST:
    REST = "https://data.alpaca.markets/v2"
    ASSETS = "https://paper-api.alpaca.markets/v2/assets"

    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.key,
                "APCA-API-SECRET-KEY": self.secret}

    def active_us_equity_symbols(self, tradable_only: bool = True) -> set[str]:
        """Active US-equity symbols (one call). tradable_only drops anything
        Alpaca won't let the user trade (delisted shells, etc.)."""
        r = requests.get(self.ASSETS,
                         headers=self._headers(),
                         params={"asset_class": "us_equity", "status": "active"},
                         timeout=30)
        r.raise_for_status()
        out = set()
        for a in r.json():
            if not a.get("symbol"):
                continue
            if tradable_only and not a.get("tradable"):
                continue
            out.add(a["symbol"])
        return out

    def daily_bars_multi(self, symbols: list[str],
                         days: int = 300) -> dict[str, list[dict]]:
        """{symbol: [bars oldest-first]}. Missing symbols are skipped.

        Alpaca paginates the multi-symbol bars endpoint: `limit` is a
        page budget shared by ALL symbols in the request, so with the old
        limit=1000 only ~5 of 100 chunk symbols got any bars back (the
        rest were silently skipped downstream). Follow next_page_token
        until the chunk is exhausted.
        """
        out: dict[str, list[dict]] = {}
        end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)
        start = end - dt.timedelta(days=days + 30)
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "start": start.isoformat(), "end": end.isoformat(),
                      "limit": 10000, "feed": "iex", "sort": "asc",
                      "adjustment": "split"}
            acc: dict[str, list[dict]] = {}
            try:
                while True:
                    r = requests.get(f"{self.REST}/stocks/bars",
                                     headers=self._headers(), params=params,
                                     timeout=30)
                    r.raise_for_status()
                    j = r.json()
                    for sym, blist in (j.get("bars") or {}).items():
                        acc.setdefault(sym, []).extend(
                            {"ts": _ts(b["t"]), "o": b["o"], "h": b["h"],
                             "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
                            for b in blist)
                    tok = j.get("next_page_token")
                    if not tok:
                        break
                    params = {**params, "page_token": tok}
                    time.sleep(0.1)
            except Exception as e:  # noqa: BLE001 - one bad batch skips
                log.warning("bars batch failed (%d symbols): %s", len(chunk), e)
                continue
            for sym, bars in acc.items():
                if bars:
                    out[sym] = bars[-days:]
            time.sleep(0.2)
        return out


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def _parse_sp500_wikipedia(html: str) -> list[str] | None:
    """Tickers from Wikipedia's S&P 500 constituents table. None if unparsable."""
    m = re.search(r'<table[^>]*id="constituents".*?</table>', html, re.S)
    if not m:
        return None
    syms: list[str] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S)[1:]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if not tds:
            continue
        cell = re.sub(r"<[^>]+>", "", tds[0]).strip()
        if re.fullmatch(r"[A-Z0-9.\-]{1,8}", cell):
            syms.append(cell.replace(".", "-"))
    return syms or None


def _fetch_sp500_wikipedia() -> list[str] | None:
    """Current S&P 500 list from Wikipedia. None on any failure."""
    r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                     timeout=25,
                     headers={"User-Agent": "stockbot-scanner/1.0"})
    r.raise_for_status()
    return _parse_sp500_wikipedia(r.text)


def _fetch_sp500_datahub() -> list[str] | None:
    """S&P 500 list from datahub.io CSV (backup source). None on any failure."""
    r = requests.get("https://datahub.io/core/s-and-p-500-companies/r/0.csv",
                     timeout=20)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    syms = [row["Symbol"].strip().replace(".", "-") for row in rows
            if row.get("Symbol")]
    return syms or None


def api_calls_per_pass(n_symbols: int,
                       batch: int = BARS_BATCH_SIZE) -> int:
    """Alpaca REST calls needed for one full-universe bars fetch."""
    return (n_symbols + batch - 1) // batch


def refresh_universe(store: Store, rest: AlpacaREST | None = None) -> list[str]:
    """Rebuild the cached universe: every tradable US equity via Alpaca.

    Never raises. If the Alpaca fetch fails, falls back to the S&P 500
    chain (Wikipedia -> datahub -> hardcoded snapshot) so the scanner keeps
    working in degraded mode.
    """
    symbols: list[str] | None = None
    if rest is not None:
        try:
            symbols = sorted(rest.active_us_equity_symbols())
            log.info("universe: %d tradable US equities from Alpaca",
                     len(symbols))
        except Exception as e:  # noqa: BLE001 - network is best-effort
            log.warning("universe: alpaca fetch failed (%s)", e)
            symbols = None

    if not symbols:
        log.warning("universe: using S&P 500 fallback chain")
        for fetcher, name in ((_fetch_sp500_wikipedia, "wikipedia"),
                              (_fetch_sp500_datahub, "datahub")):
            try:
                symbols = fetcher()
                if symbols:
                    log.info("universe: fetched %d symbols from %s",
                             len(symbols), name)
                    break
            except Exception as e:  # noqa: BLE001 - network is best-effort
                log.warning("universe: %s fetch failed (%s)", name, e)
        if not symbols:
            log.warning("universe: all remote fetches failed; using snapshot")
            symbols = list(SP500_FALLBACK)

    if not symbols:  # never cache an empty universe
        symbols = list(SP500_FALLBACK)
    payload = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "snapshot_date": SP500_SNAPSHOT_DATE,
               "symbols": symbols}
    store.poller_set(UNIVERSE_CACHE_KEY, json.dumps(payload))
    return symbols


def get_universe(store: Store, rest: AlpacaREST | None = None) -> list[str]:
    """Cached universe; refreshes when older than UNIVERSE_TTL_DAYS."""
    raw = store.poller_get(UNIVERSE_CACHE_KEY)
    if raw:
        try:
            data = json.loads(raw)
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(data["fetched_at"])).days
            if age < UNIVERSE_TTL_DAYS and data.get("symbols"):
                return data["symbols"]
        except (ValueError, KeyError, TypeError):
            pass
    return refresh_universe(store, rest)


# ---------------------------------------------------------------------------
# Technical screen
# ---------------------------------------------------------------------------
def _sma_now(closes: list[float], n: int) -> float | None:
    return indicators.sma(closes, n)


def _crossed_up_recently(closes: list[float], fast_n: int, slow_n: int,
                         window: int = 5) -> bool:
    """True if SMA(fast) crossed above SMA(slow) within the last `window` bars."""
    n = len(closes)
    span = window + 2
    fast = [indicators.sma(closes[: n - span + 1 + k], fast_n)
            for k in range(span)]
    slow = [indicators.sma(closes[: n - span + 1 + k], slow_n)
            for k in range(span)]
    if any(v is None for v in fast + slow):
        return False
    for end in range(2, span + 1):
        if indicators.crossed_above(fast[:end], slow[:end]):  # type: ignore[arg-type]
            return True
    return False


def _crossed_down_recently(closes: list[float], fast_n: int, slow_n: int,
                           window: int = 5) -> bool:
    """True if SMA(fast) crossed below SMA(slow) within the last `window` bars."""
    n = len(closes)
    span = window + 2
    fast = [indicators.sma(closes[: n - span + 1 + k], fast_n)
            for k in range(span)]
    slow = [indicators.sma(closes[: n - span + 1 + k], slow_n)
            for k in range(span)]
    if any(v is None for v in fast + slow):
        return False
    for end in range(2, span + 1):
        if indicators.crossed_below(fast[:end], slow[:end]):  # type: ignore[arg-type]
            return True
    return False


def avg_dollar_volume_20d(bars: list[dict]) -> float | None:
    """Mean daily dollar volume over the last 20 bars. None if <20 bars."""
    if len(bars) < LIQ_LOOKBACK_DAYS:
        return None
    window = bars[-LIQ_LOOKBACK_DAYS:]
    return sum(b["c"] * b["v"] for b in window) / LIQ_LOOKBACK_DAYS


def passes_liquidity(bars: list[dict]) -> bool:
    """Liquidity gate: 20-day average dollar volume >= $250k.

    No price floor — a $0.40 penny stock with real volume passes; a
    $50 stock nobody trades does not.
    """
    adv = avg_dollar_volume_20d(bars)
    return adv is not None and adv >= LIQ_MIN_DOLLAR_VOL


def _fmt_price(p: float) -> str:
    """Human price with decimals suited to the magnitude (penny-friendly)."""
    if p >= 1:
        return f"${p:,.2f}"
    if p >= 0.01:
        return f"${p:,.4f}"
    return f"${p:,.6f}"


def _rating(score: float) -> str:
    """Display-only rating out of 10 (one decimal) for a 0–100 composite.

    The internal 0–100 score (and all thresholds) are unchanged; only the
    user-facing text uses this. Clamped so a fundamental-bonus composite
    above 100 never renders past 10.0/10.
    """
    return f"{max(0.0, min(score, 100.0)) / 10.0:.1f}/10"


def technical_screen(bars: list[dict],
                     volume_scale: float = 1.0) -> dict | None:
    """Score one symbol's daily bars, bullish and bearish. None when there
    isn't enough history.

    Returns {price, tech_score, setups:[(name, pts, reason)] (bullish, kept
    for compatibility), bear_score, bear_setups:[(name, pts, reason)],
    indicators:{...}}. A symbol's side is decided by classify_side,
    which compares each score as a fraction of its own scale.

    volume_scale (< 1 only in watch mode, where the last bar is today's
    PARTIAL session): the volume-spike conditions compare today's volume
    against 2 x median x volume_scale — the volume expected by this time
    of day. RSI and close-vs-open keep as-of-now semantics (they read the
    forming bar as it stands); the morning scan always passes 1.0.
    """
    if len(bars) < MIN_TECH_BARS:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [b["v"] for b in bars]
    opens = [b["o"] for b in bars]
    price = closes[-1]
    green_day = closes[-1] >= opens[-1]
    red_day = closes[-1] < opens[-1]
    setups: list[tuple[str, int, str]] = []       # bullish
    bear_setups: list[tuple[str, int, str]] = []  # bearish
    ind: dict = {}

    # --- RSI --------------------------------------------------------------
    rsi = indicators.rsi_wilder(closes)
    rsi_now, rsi_prev = rsi[-1], rsi[-2]
    ind["rsi"] = rsi_now
    if (rsi_now is not None and rsi_prev is not None
            and rsi_now < 30 and rsi_now > rsi_prev):
        setups.append(("oversold_bounce", WEIGHTS["oversold_bounce"],
                       f"RSI {rsi_now:.0f} turning up from oversold"))
    if (rsi_now is not None and rsi_prev is not None
            and rsi_now > 70 and rsi_now < rsi_prev):
        bear_setups.append(("overbought_breakdown",
                            BEAR_WEIGHTS["overbought_breakdown"],
                            f"RSI {rsi_now:.0f} rolling over from overbought"))

    # --- Bollinger ----------------------------------------------------------
    bw_now = indicators.bollinger_bandwidth(closes)
    ind["bb_bandwidth"] = bw_now
    if bw_now is not None and len(closes) >= 126 + 20:
        hist = [indicators.bollinger_bandwidth(closes[: len(closes) - 126 + k])
                for k in range(127)]
        hist = [h for h in hist if h is not None]
        if hist and bw_now <= min(hist) * 1.02:
            setups.append(("bollinger_squeeze", WEIGHTS["bollinger_squeeze"],
                           "Bollinger squeeze (bandwidth at 6-month low)"))
    _, _, bb_lower = indicators.bollinger_bands(closes)
    ind["bb_lower"] = bb_lower
    if bb_lower is not None and price < bb_lower:
        bear_setups.append(("bollinger_breakdown",
                            BEAR_WEIGHTS["bollinger_breakdown"],
                            f"closed below lower Bollinger band "
                            f"({_fmt_price(bb_lower)})"))

    # --- volume spike (direction splits the signal) -------------------------
    base = vols[-21:-1]
    if len(base) >= 20:
        med = statistics.median(base)
        ind["vol_ratio"] = (vols[-1] / med) if med > 0 else None
        # volume_scale < 1 (watch mode, partial day) lowers the bar to the
        # volume expected by now, instead of a full day's median x 2.
        if med > 0 and vols[-1] > 2 * med * volume_scale:
            if green_day:
                setups.append(("volume_spike", WEIGHTS["volume_spike"],
                               f"volume {vols[-1] / med:.1f}x 20-day median "
                               f"(up day)"))
            elif red_day:
                bear_setups.append(
                    ("volume_spike_down",
                     BEAR_WEIGHTS["volume_spike_down"],
                     f"volume {vols[-1] / med:.1f}x 20-day median "
                     f"(down day — distribution)"))

    # --- 52-week high / low ---------------------------------------------------
    lookback = min(252, len(highs))
    high52 = max(highs[-lookback:])
    low52 = min(lows[-lookback:])
    ind["high52"] = high52
    ind["low52"] = low52
    if high52 > 0:
        dist = (high52 - price) / high52
        ind["dist_to_high52_pct"] = dist * 100
        if 0 <= dist <= 0.02:
            setups.append(("breakout_watch", WEIGHTS["breakout_watch"],
                           f"within {dist * 100:.1f}% of 52-week high"))
    if low52 > 0:
        dist_lo = (price - low52) / low52
        ind["dist_to_low52_pct"] = dist_lo * 100
        if 0 <= dist_lo <= 0.02:
            bear_setups.append(("low52_breakdown",
                                BEAR_WEIGHTS["low52_breakdown"],
                                f"within {dist_lo * 100:.1f}% of 52-week low"))

    # --- SMA50 / SMA200 crosses -----------------------------------------------
    sma50 = _sma_now(closes, 50)
    sma200 = _sma_now(closes, 200)
    ind["sma50"] = sma50
    ind["sma200"] = sma200
    if sma50 is not None and sma200 is not None:
        if _crossed_up_recently(closes, 50, 200, window=5):
            setups.append(("golden_cross", WEIGHTS["golden_cross"],
                           "golden cross (SMA50 crossed above SMA200)"))
        if _crossed_down_recently(closes, 50, 200, window=5):
            bear_setups.append(("death_cross", BEAR_WEIGHTS["death_cross"],
                                "death cross (SMA50 crossed below SMA200)"))
        if price > sma200 and sma50 > sma200:
            setups.append(("uptrend", WEIGHTS["uptrend"],
                           "in uptrend (above 200-day, 50-day above 200-day)"))

    # --- ATR selloff: single-day drop wider than 2x ATR(14) ---------------------
    atr14 = indicators.atr(bars, 14)
    ind["atr14"] = atr14
    if atr14 and atr14 > 0 and price > 0 and len(closes) >= 2:
        drop_pct = (closes[-2] - price) / price
        atr_pct = atr14 / price
        if drop_pct > 2 * atr_pct:
            bear_setups.append(("atr_selloff", BEAR_WEIGHTS["atr_selloff"],
                                f"single-day drop {drop_pct * 100:.1f}% "
                                f"(>2x ATR {atr_pct * 100:.1f}%)"))

    tech_score = min(TECH_CAP, sum(p for _, p, _ in setups))
    bear_score = min(BEAR_CAP, sum(p for _, p, _ in bear_setups))
    return {"price": price, "tech_score": tech_score,
            "setups": setups, "bear_score": bear_score,
            "bear_setups": bear_setups, "indicators": ind}


def classify_side(res: dict) -> str:
    """'BUY' when the bull score's fraction of its scale >= the bear's.

    The sides live on different scales (bull max 28, bear max 100), so raw
    sums are not comparable: a 25-point bear score is a quarter of its
    scale, while a 25-point bull score is over half of its. Comparing
    fractions fixes the old tilt toward SELL on borderline names. Ties
    still go bullish.
    """
    bull = res["tech_score"] / BULL_TECH_MAX if BULL_TECH_MAX else 0.0
    bear = res["bear_score"] / BEAR_TECH_MAX if BEAR_TECH_MAX else 0.0
    return "BUY" if bull >= bear else "SELL"


# ---------------------------------------------------------------------------
# Fundamental overlay (top technical scorers only)
# ---------------------------------------------------------------------------
def _dcf_usable(dcf: dict) -> bool:
    """True only when the DCF leg may score: the model ran on a positive
    base FCF. A fair value derived from zero/negative free cash flow (or
    from a DCF that never ran) is meaningless and must neither add nor
    subtract points. (A financials-sector exclusion can't be done here —
    the fundamentals scores payload carries no industry field.)"""
    fcf = (dcf or {}).get("base_fcf")
    return fcf is not None and fcf > 0


def fundamental_bonus(store: Store, symbol: str,
                      price: float | None) -> tuple[int, list[str]]:
    """Piotroski/Altman/DCF bonus from the 24h cache. (0, []) on any miss."""
    bonus = 0
    reasons: list[str] = []
    try:
        data = fundamentals.get_scores(store, symbol, price=price)
    except Exception as e:  # noqa: BLE001 - fundamentals are advisory
        log.warning("fundamentals unavailable for %s: %s", symbol, e)
        return 0, []
    if data.get("error"):
        return 0, []

    pio = data.get("piotroski") or {}
    pscore = pio.get("score")
    if pscore is not None:
        if pscore >= 8:
            bonus += 10
            reasons.append(f"Piotroski {pscore}/9 (strong)")
        elif pscore >= 6:
            bonus += 5
            reasons.append(f"Piotroski {pscore}/9")
        elif pscore <= 2:
            bonus -= 10
            reasons.append(f"Piotroski {pscore}/9 (weak)")

    alt = data.get("altman") or {}
    zone = alt.get("zone")
    if zone == "safe":
        bonus += 5
        reasons.append("Altman safe zone")
    elif zone == "distress":
        bonus -= 10
        reasons.append("Altman distress zone")

    dcf = data.get("dcf") or {}
    fair = dcf.get("fair_value")
    if _dcf_usable(dcf) and fair and price:
        mos = (fair - price) / fair
        if mos >= 0.20:
            bonus += 10
            reasons.append(f"DCF {mos:.0%} below fair value")
        elif mos <= -0.20:
            bonus -= 5
            reasons.append(f"DCF {abs(mos):.0%} above fair value")

    return bonus, reasons


def short_bonus(store: Store, symbol: str,
                price: float | None) -> tuple[int, list[str]]:
    """Mirror of fundamental_bonus for SELL candidates: weak fundamentals ADD
    to the sell score, strong fundamentals subtract. (0, []) on any miss."""
    bonus = 0
    reasons: list[str] = []
    try:
        data = fundamentals.get_scores(store, symbol, price=price)
    except Exception as e:  # noqa: BLE001 - fundamentals are advisory
        log.warning("fundamentals unavailable for %s: %s", symbol, e)
        return 0, []
    if data.get("error"):
        return 0, []

    pio = data.get("piotroski") or {}
    pscore = pio.get("score")
    if pscore is not None:
        if pscore <= 2:
            bonus += 10
            reasons.append(f"Piotroski {pscore}/9 (weak)")
        elif pscore >= 8:
            bonus -= 10
            reasons.append(f"Piotroski {pscore}/9 (strong — against short)")

    alt = data.get("altman") or {}
    zone = alt.get("zone")
    if zone == "distress":
        bonus += 10
        reasons.append("Altman distress zone")
    elif zone == "safe":
        bonus -= 10
        reasons.append("Altman safe zone (against short)")

    dcf = data.get("dcf") or {}
    fair = dcf.get("fair_value")
    if _dcf_usable(dcf) and fair and price:
        premium = (price - fair) / fair
        if premium >= 0.20:
            bonus += 10
            reasons.append(f"DCF {premium:.0%} above fair value")
        elif premium <= -0.20:
            bonus -= 10
            reasons.append(f"DCF {abs(premium):.0%} below fair (against short)")

    return bonus, reasons


# ---------------------------------------------------------------------------
# Earnings proximity flag (Finnhub /calendar/earnings, 24h cache) + regime
# gate helpers. Both are advisory layers over the push lists only:
#   * the earnings flag APPENDS a warning note to a candidate's reasons —
#     it never suppresses anything;
#   * the regime gate RAISES one side's push bar when the regime fights it —
#     the full dashboard lists keep every setup with its true score.
# Both fail open: missing key, API error, or unknown regime changes nothing.
# ---------------------------------------------------------------------------
EARNINGS_API = "https://finnhub.io/api/v1/calendar/earnings"
EARNINGS_TTL_SEC = 24 * 3600     # poller_state key earnings_<SYM>
EARNINGS_WINDOW_TRADING_DAYS = 3  # flag earnings this many TRADING days out
EARNINGS_LOOKAHEAD_DAYS = 14     # fetch window; the cached nearest date is
                                 # re-checked against the sliding trading-day
                                 # window on every call, so one fetch stays
                                 # correct as the window moves


def _earnings_cache_key(symbol: str) -> str:
    return f"earnings_{symbol.upper()}"


def _iso_to_epoch(s: str) -> float:
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _fetch_earnings_date(symbol: str, key: str,
                         today: dt.date) -> str | None:
    """Nearest earnings date (ISO) in [today, today + LOOKAHEAD]. Raises on
    any API failure — callers fail open."""
    r = requests.get(EARNINGS_API,
                     params={"from": today.isoformat(),
                             "to": (today + dt.timedelta(
                                 days=EARNINGS_LOOKAHEAD_DAYS)).isoformat(),
                             "symbol": symbol.upper(), "token": key},
                     timeout=15)
    r.raise_for_status()
    cal = (r.json() or {}).get("earningsCalendar") or []
    dates = sorted(str(e["date"]) for e in cal if e.get("date"))
    return dates[0] if dates else None


def get_earnings_date(store: Store, symbol: str,
                      key: str | None = None) -> str | None:
    """Nearest upcoming earnings date (ISO) for a symbol, or None.

    Cached 24h per symbol in poller_state (key earnings_<SYM>) — at most
    one Finnhub call per symbol per day. Never raises: a missing key, an
    API failure, or "nothing scheduled" all degrade to None (and failures
    are cached too, so a dead feed never hammers the API).
    """
    symbol = symbol.upper()
    raw = store.poller_get(_earnings_cache_key(symbol))
    if raw:
        try:
            data = json.loads(raw)
            if (time.time() - _iso_to_epoch(data["fetched_at"])
                    < EARNINGS_TTL_SEC):
                return data["payload"].get("date")
        except (ValueError, KeyError, TypeError):
            pass
    if key is None:
        key = Settings.from_env().finnhub_key
    found: str | None = None
    if key:
        try:
            found = _fetch_earnings_date(symbol, key, _et_now().date())
        except Exception as e:  # noqa: BLE001 - advisory only, fail open
            log.warning("earnings: %s calendar fetch failed (%s) — no flag",
                        symbol, e)
            found = None
    try:
        store.poller_set(_earnings_cache_key(symbol), json.dumps(
            {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "payload": {"date": found}}))
    except Exception as e:  # noqa: BLE001 - cache failure is non-fatal
        log.warning("earnings: cache write failed for %s (%s)", symbol, e)
    return found


def _trading_days_until(store: Store, today: dt.date,
                        target: dt.date) -> int:
    """Trading days from today to target: 0 for today itself, 1 for the
    next trading day, -1 for a past date.

    Uses the cached sessions calendar (sessions_mod.is_trading_day); days
    the cache doesn't know fall back to weekday (Mon–Fri), so a Friday
    scan still counts Monday as 1 trading day out — and flags Monday
    earnings — even on a cold calendar cache.
    """
    if target < today:
        return -1
    count = 0
    d = today
    while d < target:
        d += dt.timedelta(days=1)
        known = sessions_mod.is_trading_day(store, d)
        if known is None:
            known = d.weekday() < 5
        if known:
            count += 1
    return count


def earnings_note(store: Store, symbol: str, key: str | None = None,
                  today: dt.date | None = None) -> str | None:
    """"⚠ earnings Oct 22" when the symbol reports within the next
    EARNINGS_WINDOW_TRADING_DAYS trading days (today counts as day 0 —
    so a Friday scan flags Monday earnings). None otherwise.
    Never raises."""
    try:
        d = get_earnings_date(store, symbol, key)
    except Exception as e:  # noqa: BLE001 - the flag must never break a scan
        log.warning("earnings: lookup failed for %s (%s) — no flag",
                    symbol, e)
        return None
    if not d:
        return None
    try:
        edate = dt.date.fromisoformat(str(d)[:10])
    except ValueError:
        return None
    today = today or _et_now().date()
    if 0 <= _trading_days_until(store, today, edate) \
            <= EARNINGS_WINDOW_TRADING_DAYS:
        return f"⚠ earnings {edate.strftime('%b')} {edate.day}"
    return None


def _attach_earnings_note(store: Store, row: dict,
                          key: str | None = None) -> None:
    """Set row["earnings_note"] when earnings are imminent (fail open)."""
    note = earnings_note(store, row["symbol"], key)
    if note:
        row["earnings_note"] = note


def _with_note(reasons: list[str], row: dict) -> list[str]:
    """Reasons + the earnings flag, when the row carries one."""
    note = row.get("earnings_note")
    if note:
        reasons.append(note)
    return reasons


def _current_regime(store: Store, rest: AlpacaREST) -> str | None:
    """'bull' | 'bear' | 'sideways' from regime.py (SPY vs SMA200) or None.

    Reuses regime.RegimeCache with the scanner's own Alpaca client; SPY
    daily bars persist in the candles table, so this costs ~1 REST call
    per day. Never raises — an unknown regime means no gating.
    """
    try:
        def _daily_bars(symbol: str, days: int) -> list[dict]:
            return rest.daily_bars_multi([symbol],
                                         days=days).get(symbol, [])

        return regime_mod.RegimeCache(store, _daily_bars).current()
    except Exception as e:  # noqa: BLE001 - regime is advisory, fail open
        log.warning("regime check failed (%s) — no gating", e)
        return None


def _regime_fights(side: str, regime: str | None) -> bool:
    """True when the regime works against the side: bear vs buys, bull vs
    sells, sideways vs BOTH (no trend to lean on in either direction).
    Unknown regime fights nothing (fail open)."""
    if regime == "bear":
        return side == "BUY"
    if regime == "bull":
        return side == "SELL"
    if regime == "sideways":
        return True
    return False


def _morning_bar(side: str, regime: str | None) -> int:
    """Effective raw morning-push bar for a side (on the composite):
    MORNING_BUY_BAR / MORNING_SELL_BAR, plus the raw regime-gate bonus
    when the regime fights the side. Effective today: buy 20 (gated 25),
    sell 50 (gated 65)."""
    bar = MORNING_BUY_BAR if side == "BUY" else MORNING_SELL_BAR
    if _regime_fights(side, regime):
        bar += GATE_BUY_BONUS if side == "BUY" else GATE_SELL_BONUS
    return bar


def _watch_bar(side: str, regime: str | None) -> int:
    """Effective raw watch-push bar for a side (tech score only — watch
    runs no fundamental overlay), gated like the morning bar with the
    same raw bonuses. Effective today: buy 20 (gated 25), sell 30
    (gated 45)."""
    bar = WATCH_BUY_BAR if side == "BUY" else WATCH_SELL_BAR
    if _regime_fights(side, regime):
        bar += GATE_BUY_BONUS if side == "BUY" else GATE_SELL_BONUS
    return bar


# ---------------------------------------------------------------------------
# Real-time watch mode
# ---------------------------------------------------------------------------
def _et_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(ET)


def _et_today_str() -> str:
    return _et_now().date().isoformat()


def _session_fraction(now: dt.datetime | None = None) -> float:
    """Elapsed fraction of the 09:30–16:00 ET session, clamped [0.05, 1].

    Watch mode compares today's PARTIAL volume against full-day medians;
    technical_screen scales its spike threshold by this fraction so the
    effective bar is the same at 10:00 as at 15:00.
    """
    now = now or _et_now()
    open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_min = (now - open_dt).total_seconds() / 60.0
    return min(1.0, max(0.05, elapsed_min / 390.0))


def _in_market_hours(store: Store) -> bool:
    """True only on weekdays, trading days, between open and close (ET).

    Hours come from the cached sessions.py calendar; falls back to
    09:30-16:00 ET when the calendar has no hours for today.
    """
    now = _et_now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    today = now.date().isoformat()
    is_open = sessions_mod.is_trading_day(store, today)
    if is_open is None:
        sessions_mod.refresh_cache(store)
        is_open = sessions_mod.is_trading_day(store, today)
    if is_open is not True:
        return False
    open_t, close_t = "09:30", "16:00"
    try:
        row = store.conn.execute(
            "SELECT open_time, close_time FROM market_calendar WHERE date=?",
            (today,)).fetchone()
        if row and row["open_time"]:
            open_t = str(row["open_time"])[:5]
            close_t = str(row["close_time"])[:5] if row["close_time"] else "16:00"
    except Exception:  # noqa: BLE001 - fall back to regular hours
        pass
    return open_t <= now.strftime("%H:%M") < close_t


def _load_seen(store: Store) -> dict:
    """Today's already-flagged symbols; resets when the date rolls over."""
    today = _et_today_str()
    raw = store.poller_get(WATCH_SEEN_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if data.get("date") == today:
                return {"date": today,
                        "symbols": list(data.get("symbols", []))}
        except (ValueError, TypeError):
            pass
    return {"date": today, "symbols": []}


def _save_seen(store: Store, seen: dict) -> None:
    store.poller_set(WATCH_SEEN_KEY, json.dumps(seen))


def _seen_key(symbol: str, side: str) -> str:
    """Per-side dedupe entry: "SYM|SIDE". A symbol alerted as BUY can
    still alert the same day as SELL (and vice versa)."""
    return f"{symbol}|{side}"


def _seen_covers(seen_set: set, symbol: str, side: str) -> bool:
    """True when (symbol, side) already alerted today. A legacy plain
    "SYM" entry (written before per-side dedupe) covers BOTH sides."""
    return symbol in seen_set or _seen_key(symbol, side) in seen_set


def _seen_covers_both(seen_set: set, symbol: str) -> bool:
    """True when neither side can still alert for the symbol today —
    lets the watch skip screening it at all."""
    return (symbol in seen_set
            or (_seen_key(symbol, "BUY") in seen_set
                and _seen_key(symbol, "SELL") in seen_set))


def _reset_seen(store: Store) -> None:
    """Clear the watch seen-list (called by the morning scan each trading day)."""
    _save_seen(store, {"date": _et_today_str(), "symbols": []})


def _watch_entry(r: dict) -> dict:
    """Dashboard-ready entry for one NEW watch match — mirrors the push line.

    Carries everything the alert shows: symbol, side, price, the 0–100
    score, the X.X/10 display rating, and the human-readable breakdown.
    """
    setups = r["setups"] if r["side"] == "BUY" else r["bear_setups"]
    reasons = [reason for _, _, reason in setups]
    if sentiment.has_signal(r.get("sentiment_payload")):
        reasons.append(sentiment.format_combined(r["sentiment_payload"]))
    reasons = _with_note(reasons, r)
    return {"symbol": r["symbol"],
            "side": r["side"],
            "price": r["price"],
            "score": r["score"],
            "rating": sentiment.adjusted_rating_str(
                r["score"], r.get("sentiment", 0.0)),
            "breakdown": "; ".join(reasons),
            "reasons": reasons,
            "time": _et_now().isoformat()}


def _persist_watch(store: Store, fresh: list[dict]) -> None:
    """Accumulate today's NEW watch matches under poller key scanner_watch.

    Merges with entries already stored today; resets when the ET date rolls
    over. Never raises — persistence must not break the watch or the push.
    """
    try:
        today = _et_today_str()
        entries: list = []
        raw = store.poller_get(SCAN_WATCH_KEY)
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today:
                    entries = list(data.get("entries", []))
            except (ValueError, TypeError):
                pass
        entries.extend(_watch_entry(r) for r in fresh)
        store.poller_set(SCAN_WATCH_KEY, json.dumps(
            {"kind": "watch", "date": today, "entries": entries}))
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log.warning("watch: could not persist matches: %s", e)


def watch_scan(store: Store | None = None, rest: AlpacaREST | None = None,
               dry_run: bool = False, push_fn=None) -> list[dict]:
    """One watch pass: technical screen over the universe, push NEW matches.

    Guards: weekdays + trading day + market hours (ET) — exits quietly
    otherwise. Liquidity gate applied. Each new match is labeled by
    classify_side (score fractions of each side's scale). Dedupe via the
    persisted seen-list: one alert per symbol PER SIDE per day, so a
    BUY -> SELL flip re-alerts. Today's bar is partial, so the volume
    spike threshold is scaled by the elapsed session fraction (see
    technical_screen). Pushes require price >= PUSH_MIN_PRICE. Technical
    screen only, no fundamentals. Returns new matches (each with "side").
    """
    settings = Settings.from_env()
    store = store or Store(settings.db_path)
    rest = rest or AlpacaREST(settings.alpaca_key, settings.alpaca_secret)

    if not _in_market_hours(store):
        log.info("watch: outside market hours — skipping quietly")
        return []

    universe = get_universe(store, rest)
    seen = _load_seen(store)
    seen_set = set(seen["symbols"])

    # Regime gate (push only): in a bear regime a new BUY match must clear
    # a higher bar, and vice versa in a bull regime; sideways raises both.
    # Fail open on unknown. Bars are explicit raw values (see the
    # calibration note at the bar constants).
    regime = _current_regime(store, rest)
    buy_bar = _watch_bar("BUY", regime)
    sell_bar = _watch_bar("SELL", regime)
    if buy_bar != _watch_bar("BUY", None):
        log.info("watch: %s regime — buy push bar raised to %d",
                 regime, buy_bar)
    if sell_bar != _watch_bar("SELL", None):
        log.info("watch: %s regime — sell push bar raised to %d",
                 regime, sell_bar)

    # Today's bar is still forming: scale the volume-spike threshold by
    # the elapsed fraction of the session (see technical_screen).
    volume_scale = _session_fraction()

    # Screen chunk-by-chunk (memory): bars are discarded after each chunk.
    fresh: list[dict] = []
    for ci in range(0, len(universe), 100):
        bars_by_sym = rest.daily_bars_multi(universe[ci:ci + 100], days=300)
        for sym in universe[ci:ci + 100]:
            if _seen_covers_both(seen_set, sym):
                continue
            bars = bars_by_sym.get(sym)
            if not bars:
                continue
            res = technical_screen(bars, volume_scale=volume_scale)
            if res is None or not passes_liquidity(bars):
                continue
            # The labeled side (higher score fraction) must clear ITS bar.
            # With equal bars this is exactly the old either-side test;
            # under the regime gate it stops a gated-side label sneaking
            # in on the strength of the other side's lower bar.
            side = classify_side(res)
            if _seen_covers(seen_set, sym, side):
                continue  # this side already alerted today; a later
                # flip to the other side can still alert
            score = (res["tech_score"] if side == "BUY"
                     else res["bear_score"])
            if score < (buy_bar if side == "BUY" else sell_bar):
                continue
            fresh.append({"symbol": sym, "side": side,
                          "score": score,
                          **res})
    # Push-only price floor: sub-$3 matches never reach the phone (and are
    # NOT marked seen — the dashboard morning lists are unaffected).
    floored = [r for r in fresh if r["price"] >= PUSH_MIN_PRICE]
    if len(floored) != len(fresh):
        log.info("watch: price floor %s held back %d match(es)",
                 _fmt_price(PUSH_MIN_PRICE), len(fresh) - len(floored))
    fresh = floored
    fresh.sort(key=lambda r: r["score"], reverse=True)

    # Sentiment overlay for newly matched symbols only (each leg cached 24h,
    # so the 15-minute watch stays cheap). Same /10 adjustment as the
    # morning scan, now on the combined news + analyst total.
    for r in fresh:
        s = sentiment.get_sentiment(store, r["symbol"])
        r["sentiment"] = s["sentiment"]
        r["sentiment_n"] = s["n"]
        r["sentiment_payload"] = s

    # Earnings proximity flag for the new matches (info only, fail open).
    for r in fresh:
        _attach_earnings_note(store, r, settings.finnhub_key)

    if fresh:
        seen["symbols"].extend(_seen_key(r["symbol"], r["side"])
                               for r in fresh)
        _save_seen(store, seen)
        buys = [r for r in fresh if r["side"] == "BUY"]
        sells = [r for r in fresh if r["side"] == "SELL"]

        def _line(r: dict) -> str:
            setups = r["setups"] if r["side"] == "BUY" else r["bear_setups"]
            reasons = [reason for _, _, reason in setups]
            if sentiment.has_signal(r.get("sentiment_payload")):
                reasons.append(sentiment.format_combined(
                    r["sentiment_payload"]))
            reasons = _with_note(reasons, r)
            return (f"{r['side']}: {r['symbol']} @ {_fmt_price(r['price'])} — "
                    f"{sentiment.adjusted_rating_str(r['score'], r.get('sentiment', 0.0))} — "
                    f"{'; '.join(reasons)}")

        shown = fresh[:WATCH_PUSH_MAX]
        lines = [_line(r) for r in shown]
        if len(fresh) > WATCH_PUSH_MAX:
            lines.append(f"…and {len(fresh) - WATCH_PUSH_MAX} more")

        parts = []
        if buys:
            parts.append(f"BUY SETUP: {len(buys)} new"
                         if len(buys) != 1
                         else f"BUY SETUP: {buys[0]['symbol']} @ "
                              f"{_fmt_price(buys[0]['price'])} — "
                              f"{sentiment.adjusted_rating_str(buys[0]['score'], buys[0].get('sentiment', 0.0))}")
        if sells:
            parts.append(f"SELL SETUP: {len(sells)} new"
                         if len(sells) != 1
                         else f"SELL SETUP: {sells[0]['symbol']} @ "
                              f"{_fmt_price(sells[0]['price'])} — "
                              f"{sentiment.adjusted_rating_str(sells[0]['score'], sells[0].get('sentiment', 0.0))}")
        title = " · ".join(parts)
        body = "\n".join(lines)
        if dry_run:
            print(title)
            print(body)
        elif push_fn is None:
            ok = send_ntfy(settings.ntfy_server, settings.ntfy_topic,
                            title, body, priority=4,
                            tags=["rotating_light", "chart_with_upwards_trend"],
                            timeout=30)
            log.info("watch: ntfy push %s (%d new: %d buy, %d sell)",
                     "sent" if ok else "FAILED",
                     len(fresh), len(buys), len(sells))
        else:
            push_fn(title, body)
        if not dry_run:
            _persist_watch(store, fresh)
    else:
        log.info("watch: no new setups")
        if dry_run:
            print("(no new setups)")
    return fresh


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _candidate_reasons(c: dict, side: str) -> list[str]:
    """The same reason strings the morning push lines print for a candidate."""
    setups = c["setups"] if side == "BUY" else c["bear_setups"]
    extra = c.get("fund_reasons") if side == "BUY" else c.get("short_reasons")
    reasons = [r for _, _, r in setups] + list(extra or [])
    if sentiment.has_signal(c.get("sentiment_payload")):
        reasons.append(sentiment.format_combined(c["sentiment_payload"]))
    return _with_note(reasons, c)


def _morning_entry(c: dict, side: str, top_pick: bool = False) -> dict:
    """Dashboard-ready entry for one morning-scan candidate — everything the
    push line shows: symbol, price, the 0–100 composite, the X.X/10 display
    rating, and the human-readable breakdown."""
    score = c["composite"] if side == "BUY" else c["short_composite"]
    reasons = _candidate_reasons(c, side)
    return {"symbol": c["symbol"],
            "price": c["price"],
            "score": score,
            "rating": sentiment.adjusted_rating_str(
                score, c.get("sentiment", 0.0)),
            "breakdown": "; ".join(reasons),
            "reasons": reasons,
            "sentiment": c.get("sentiment", 0.0),
            "top_pick": top_pick}


def _persist_morning(store: Store, buys: list[dict],
                     sells: list[dict],
                     top_buys: list[dict] | None = None,
                     top_sells: list[dict] | None = None) -> None:
    """Stash the ranked lists for the dashboard (poller key scanner_morning).

    The caller passes the FULL ranked bull/bear rows -- the dashboard shows
    every setup the screen found, flagged with top_pick for the ones that
    made the push. Never raises -- persistence must not break the scan."""
    try:
        tb = {c["symbol"] for c in (top_buys or [])}
        ts = {c["symbol"] for c in (top_sells or [])}
        store.poller_set(SCAN_MORNING_KEY, json.dumps({
            "kind": "morning",
            "ts": _et_now().isoformat(),
            "buys": [_morning_entry(c, "BUY", c["symbol"] in tb) for c in buys],
            "sells": [_morning_entry(c, "SELL", c["symbol"] in ts) for c in sells],
        }))
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log.warning("scan: could not persist morning results: %s", e)


def run_scan(store: Store | None = None, rest: AlpacaREST | None = None,
             dry_run: bool = False, push_fn=None,
             top_n: int = SCAN_TOP_N) -> dict:
    """Run the morning scan. Returns {"buys": [...], "sells": [...]} — the
    ranked candidate lists.

    push_fn(title, message) defaults to the real ntfy push; dry_run=True
    never pushes. One push total, with a BUY CANDIDATES section and a SELL
    CANDIDATES section (whichever sides are non-empty). Quiet [] lists on
    non-trading days.
    """
    settings = Settings.from_env()
    store = store or Store(settings.db_path)
    rest = rest or AlpacaREST(settings.alpaca_key, settings.alpaca_secret)

    # --- trading-day guard --------------------------------------------------
    today = dt.date.today().isoformat()
    is_open = sessions_mod.is_trading_day(store, today)
    if is_open is None:
        sessions_mod.refresh_cache(store)
        is_open = sessions_mod.is_trading_day(store, today)
    if is_open is not True:
        log.info("scan: %s is not a trading day — skipping quietly", today)
        return {"buys": [], "sells": []}

    # fresh day, fresh watch: the morning scan resets the watch seen-list
    _reset_seen(store)

    # --- universe -----------------------------------------------------------
    universe = get_universe(store, rest)
    log.info("scan: screening %d symbols", len(universe))

    # --- technical screen (bullish + bearish, liquidity-gated) ---------------
    # Screen chunk-by-chunk: with full pagination coverage the universe is
    # ~13.5k symbols x ~230 bars — holding all of it at once would blow the
    # memory of a small server. Bars are discarded after each chunk.
    bull_rows: list[dict] = []
    bear_rows: list[dict] = []
    evaluated = 0
    for ci in range(0, len(universe), 100):
        bars_by_sym = rest.daily_bars_multi(universe[ci:ci + 100], days=300)
        for sym in universe[ci:ci + 100]:
            bars = bars_by_sym.get(sym)
            if not bars:
                continue
            res = technical_screen(bars)
            if res is None or not passes_liquidity(bars):
                continue
            evaluated += 1
            row = {"symbol": sym,
                   "avg_dollar_vol_20d": avg_dollar_volume_20d(bars), **res}
            if res["setups"]:
                bull_rows.append(row)
            if res["bear_setups"]:
                bear_rows.append(row)
    log.info("scan: evaluated %d/%d symbols with usable bars",
             evaluated, len(universe))
    bull_rows.sort(key=lambda r: r["tech_score"], reverse=True)
    bear_rows.sort(key=lambda r: r["bear_score"], reverse=True)
    log.info("scan: %d bullish / %d bearish symbols with setups",
             len(bull_rows), len(bear_rows))

    # --- fundamental overlays (top-N per side only) --------------------------
    for row in bull_rows[:FUND_TOP_N]:
        bonus, freasons = fundamental_bonus(store, row["symbol"], row["price"])
        row["fund_bonus"] = bonus
        row["fund_reasons"] = freasons
        row["composite"] = row["tech_score"] + bonus
        time.sleep(1.5)  # Finnhub free tier: stay well under 60/min
    for row in bull_rows[FUND_TOP_N:]:
        row["fund_bonus"] = 0
        row["fund_reasons"] = []
        row["composite"] = row["tech_score"]
    for row in bear_rows[:FUND_TOP_N]:
        bonus, breasons = short_bonus(store, row["symbol"], row["price"])
        row["short_bonus"] = bonus
        row["short_reasons"] = breasons
        row["short_composite"] = row["bear_score"] + bonus
        time.sleep(1.5)
    for row in bear_rows[FUND_TOP_N:]:
        row["short_bonus"] = 0
        row["short_reasons"] = []
        row["short_composite"] = row["bear_score"]
    bull_rows.sort(key=lambda r: r["composite"], reverse=True)
    bear_rows.sort(key=lambda r: r["short_composite"], reverse=True)

    # Regime gate (push only): a side fighting the regime must clear a
    # higher bar to reach the phone. bull_rows/bear_rows themselves (and
    # therefore the persisted dashboard lists) are NOT gated — they keep
    # every setup at its true score. Fail open when the regime is unknown.
    # Bars are explicit raw values (see the calibration note at the bar
    # constants).
    regime = _current_regime(store, rest)
    buy_bar = _morning_bar("BUY", regime)
    sell_bar = _morning_bar("SELL", regime)
    if buy_bar != _morning_bar("BUY", None):
        log.info("scan: %s regime — buy push bar raised to %d",
                 regime, buy_bar)
    if sell_bar != _morning_bar("SELL", None):
        log.info("scan: %s regime — sell push bar raised to %d",
                 regime, sell_bar)
    buy_cands = [r for r in bull_rows if r["composite"] >= buy_bar]
    sell_cands = [r for r in bear_rows
                  if r["short_composite"] >= sell_bar]
    # Push-only price floor: penny names are the noisiest (and most
    # manipulable) spike/breakout sources — they stay on the dashboard
    # lists but never reach the phone below PUSH_MIN_PRICE.
    held = sum(1 for r in buy_cands + sell_cands
               if r["price"] < PUSH_MIN_PRICE)
    if held:
        log.info("scan: price floor %s held back %d above-bar candidate%s",
                 _fmt_price(PUSH_MIN_PRICE), held,
                 "s" if held != 1 else "")
    buys = [r for r in buy_cands if r["price"] >= PUSH_MIN_PRICE][:top_n]
    sells = [r for r in sell_cands if r["price"] >= PUSH_MIN_PRICE][:top_n]
    log.info("scan: %d buy / %d sell candidates above bar",
             len(buys), len(sells))

    # --- sentiment overlay (ranked candidates only, each leg cached 24h) ----
    # Combined news + analyst total shifts the displayed /10 rating by
    # total × 1.5, clamped to [0, 10]; the breakdown is shown in each
    # alert line, e.g. "news: +0.6 | analysts: 14 buy / 4 hold / 1 sell".
    # Candidates with no coverage on either leg show no sentiment line.
    for c in buys + sells:
        s = sentiment.get_sentiment(store, c["symbol"])
        c["sentiment"] = s["sentiment"]
        c["sentiment_n"] = s["n"]
        c["sentiment_payload"] = s

    # --- earnings proximity flag (push candidates only, info only) --------
    for c in buys + sells:
        _attach_earnings_note(store, c, settings.finnhub_key)

    # --- output ---------------------------------------------------------------
    def _buy_line(i: int, c: dict) -> str:
        return (f"{i}. BUY: {c['symbol']} @ {_fmt_price(c['price'])} — "
                f"{sentiment.adjusted_rating_str(c['composite'], c.get('sentiment', 0.0))} — "
                f"{'; '.join(_candidate_reasons(c, 'BUY'))}")

    def _sell_line(i: int, c: dict) -> str:
        return (f"{i}. SELL: {c['symbol']} @ {_fmt_price(c['price'])} — "
                f"{sentiment.adjusted_rating_str(c['short_composite'], c.get('sentiment', 0.0))} — "
                f"{'; '.join(_candidate_reasons(c, 'SELL'))}")

    sections: list[str] = []
    title_parts: list[str] = []
    if buys:
        title_parts.append(f"{len(buys)} buy candidate"
                           f"{'s' if len(buys) != 1 else ''}")
        sections.append("BUY CANDIDATES\n"
                        + "\n".join(_buy_line(i, c)
                                    for i, c in enumerate(buys, 1)))
        for i, c in enumerate(buys, 1):
            log.info("scan: %s", _buy_line(i, c))
    if sells:
        title_parts.append(f"{len(sells)} sell candidate"
                           f"{'s' if len(sells) != 1 else ''}")
        sections.append("SELL CANDIDATES\n"
                        + "\n".join(_sell_line(i, c)
                                    for i, c in enumerate(sells, 1)))
        for i, c in enumerate(sells, 1):
            log.info("scan: %s", _sell_line(i, c))

    if not dry_run and (buys or sells):
        title = "Morning scan: " + " + ".join(title_parts)
        body = "\n\n".join(sections)
        if push_fn is None:
            ok = send_ntfy(settings.ntfy_server, settings.ntfy_topic,
                            title, body, priority=3,
                            tags=["sunrise", "chart_with_upwards_trend"],
                            timeout=30)
        else:
            ok = push_fn(title, body)
        log.info("scan: ntfy push %s", "sent" if ok else "FAILED")
    elif dry_run:
        print("\n\n".join(sections) if sections else "(no candidates)")
    if not dry_run:
        _persist_morning(store, bull_rows, bear_rows, buys, sells)
    return {"buys": buys, "sells": sells}


def main() -> None:
    ap = argparse.ArgumentParser(description="Morning market scanner")
    ap.add_argument("--dry-run", action="store_true",
                    help="print results without pushing to ntfy")
    ap.add_argument("--watch", action="store_true",
                    help="real-time watch pass: push immediately on NEW setup "
                         "matches (technical screen only)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    if args.watch:
        watch_scan(dry_run=args.dry_run)
    else:
        run_scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
