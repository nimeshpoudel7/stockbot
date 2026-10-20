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
   batches of 100), read the way a trader reads a chart: trend context
   first, then where price sits in it, then confirmation, then risk.
   Signals were rebuilt Oct 20, 2026 and validated on ~3.5 years of real
   daily bars (465 S&P 500 names + 360 random liquid small/mid caps,
   entry at the NEXT open, 10-session forward returns vs a random-day
   baseline, both halves of the window checked separately).

   BUY has two playbooks; a symbol's BUY score is the stronger of them.

   DIP (buy weakness — mean reversion; strongest in pullbacks/corrections)
   | setup                 | condition                                  | pts |
   |-----------------------|--------------------------------------------|-----|
   | oversold_rsi30        | RSI(14) < 30                               |  40 |
   | oversold_rsi35        | RSI(14) < 35 (if not < 30)                 |  25 |
   | below_lower_band      | close < lower Bollinger band (prior 20)    |  20 |
   | stretched_below_sma20 | close > 2x ATR(14) below SMA20             |  15 |
   | capitulation_volume   | RSI(14) < 35 AND volume > 2x 20d median     |  25 |

   MOMENTUM (buy strength — leaders; strongest in steady bull runs)
   | setup             | condition                                      | pts |
   |-------------------|------------------------------------------------|-----|
   | leader_rs20       | 63-day return beats SPY by > 20 pts            |  30 |
   | leader_rs10       | ... by > 10 pts (if not > 20)                  |  15 |
   | extended15        | close > 15% above SMA50                        |  20 |
   | extended10        | close > 10% above SMA50 (if not > 15%)         |  10 |
   | volume_up_day     | volume > 2x 20d median, close >= open          |  20 |
   | breakout20_volume | close > prior 20-day high, top 30% of range,   |  15 |
   |                   | volume >= 1.5x median                          |     |
   | uptrend           | close > SMA200 > rising, SMA50 > SMA200        |  15 |

   SELL (sell strength in a stock that is in its OWN downtrend)
   | setup                     | condition                              | pts |
   |---------------------------|----------------------------------------|-----|
   | rally_in_downtrend        | downtrend AND close > 2x ATR above     |  40 |
   |                           | SMA20 (a rip into resistance)          |     |
   | overbought_below_200      | RSI(14) > 70 while below SMA200        |  30 |
   | short_term_overbought_dn  | downtrend AND RSI(2) > 90              |  20 |
   | downtrend                 | close < SMA200 < falling, SMA50 < 200  |  10 |

   What the evidence threw out (and why the rules look "backwards" in
   places): buying near the 52-week high (-0.4% vs baseline), plain
   uptrend / MACD / RSI 60-80 (~0), pullbacks to SMA20/50 (~0 or worse),
   and EVERY "sell weakness" rule — breakdowns, lower-band closes,
   heavy-volume red days, oversold — lost money as shorts (-0.4% to
   -1.6%): weak stocks bounced. Selling into oversold is the single
   worst signal measured, so SELL rules only ever fire on strength.

   A symbol is classified BUY when its BUY score >= its SELL score (both
   are 0-100 now), else SELL.

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
decimal (e.g. 6.8/10): the technical score as a fraction of its own
side's scale, plus up to +/-2 for the fundamental bonus (neutral when
absent) and the sentiment shift (side_rating) — display-only; internal
scores and thresholds stay raw.

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

Push bars (technical score, 0-100; the fundamental overlay only ranks and
rates, it never decides the push): BUY dip 60, BUY momentum 75, SELL 40.
Regime decides which BUY playbook gets the easier bar — dips worked in
corrections but stopped working in the 2025-26 bull run, momentum did
the reverse: bull regime raises the dip bar to 75; sideways/bear raises
the momentum bar to 90. SELL works on stock-specific downtrends inside
bull/sideways markets; in a market-wide bear regime its bar rises to 90
(bear-market rallies squeezed shorts in the one bear episode sampled).
Validated edge of the pushed signals (production code, 10 sessions, vs
same-regime random days): S&P 500 BUY +0.6% (58% winners), SELL +1.1%
(55%); small/mid BUY +0.4% (55%), SELL +0.6% (53%); both halves of the
window positive. Baseline win rate ~52%. These are tilts
of the odds, not certainties. Every alert carries an ATR(14) stop
(2x ATR) and target (3x ATR).

Regime (regime.py, SPY vs SMA200 + slope) only shifts the BUY playbook
bars as described above; unknown regime -> base bars (fail open). The
full dashboard lists are never gated: every setup persists with its
true score/rating.

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
DIP_WEIGHTS = {  # BUY playbook 1: buy weakness (mean reversion)
    "oversold_rsi30": 40,
    "oversold_rsi35": 25,          # exclusive with oversold_rsi30
    "below_lower_band": 20,
    "stretched_below_sma20": 15,
    "capitulation_volume": 25,
}
MOMENTUM_WEIGHTS = {  # BUY playbook 2: buy strength (leaders)
    "leader_rs20": 30,
    "leader_rs10": 15,             # exclusive with leader_rs20
    "extended15": 20,
    "extended10": 10,              # exclusive with extended15
    "volume_up_day": 20,
    "breakout20_volume": 15,
    "uptrend": 15,
}
WEIGHTS = {**DIP_WEIGHTS, **MOMENTUM_WEIGHTS}  # all bullish setups
BEAR_WEIGHTS = {  # SELL: strength inside a downtrend
    "rally_in_downtrend": 40,
    "overbought_below_200": 30,
    "short_term_overbought_dn": 20,
    "downtrend": 10,
}
TECH_CAP = 100
BEAR_CAP = 100
MIN_TECH_BARS = 60        # need at least this many daily bars to score
FUND_TOP_N = 25           # fundamental overlay applies to top-N per side
SCAN_TOP_N = 6            # candidates pushed per side (6 + 6 readable
                          # blocks fit in one ntfy message, 4 KB cap)
UNIVERSE_TTL_DAYS = 7
UNIVERSE_CACHE_KEY = "scanner_universe_v2"  # bumped: universe is now all US equities
WATCH_SEEN_KEY = "scanner_watch_seen"  # {"date": "YYYY-MM-DD", "symbols": [...]}
SCAN_MORNING_KEY = "scanner_morning"  # poller key: ranked lists for the dashboard
SCAN_WATCH_KEY = "scanner_watch"      # poller key: today's watch matches
SCAN_WATCH_LAST_KEY = "scanner_watch_last"  # poller key: last watch pass ran
WATCH_INTERVAL_MIN = 15    # cron cadence of the watch pass (deploy/crontab)
WATCH_PUSH_MAX = 12        # cap symbols listed in one watch push (4 KB)

# --- Scales and push bars ---------------------------------------------------
# Every playbook scores 0-100, so BUY and SELL share one scale and the
# /10 rating is simply score / 10 (before fundamentals and sentiment).
# Bars are on the technical score; see the module docstring for the
# validation numbers behind them.
BULL_TECH_MAX = 100   # each BUY playbook tops out at 100
BEAR_TECH_MAX = 100
# Fundamental bonus ceilings: push bars no longer derive from these; the
# /10 rating maps a bonus onto ±FUND_RATING_MAX with them (side_rating).
FUND_BONUS_MAX = 25    # fundamental_bonus ceiling: Piotroski +10, Altman +5, DCF +10
SHORT_BONUS_MAX = 30   # short_bonus ceiling: Piotroski +10, Altman +10, DCF +10
# Push bars on the TECHNICAL score (0-100), same for morning and watch.
BUY_BARS = {"dip": 60, "momentum": 75}
SELL_BAR = 40
# Regime picks the BUY playbook: the out-of-favour one needs this extra.
REGIME_BUY_PENALTY = 15   # bull: dip 75 · sideways/bear: momentum 90
# Bear regime: SELL bar 90 (effectively paused). The only bear-regime
# sample (Apr-May 2025: SPY's 200-day still falling while the market
# ripped off the low) saw "sell the rip" lose ~1-2% / 13-28% winners in
# both universes — bear-market rallies squeeze shorts. One episode, so
# treat as a caution, not a law.
REGIME_SELL_PENALTY_BEAR = 50
STOP_ATR = 2.0            # stop distance in ATR(14) multiples
TARGET_ATR = 3.0          # target distance in ATR(14) multiples
RS_LOOKBACK = 63          # sessions for relative strength vs SPY
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
        # `days` is a SESSION count: ~252 sessions per 365 calendar days.
        # (days + 30 calendar days gave ~228 sessions for days=300, so the
        # "52-week" high/low really covered ~11 months.)
        start = end - dt.timedelta(days=int(days * 365 / 252) + 15)
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


FUND_RATING_MAX = 2.0  # most a full fundamental bonus can move the /10 rating


def side_scale(side: str) -> int:
    """Max technical score for a side (BUY 28, SELL 100)."""
    return BULL_TECH_MAX if side == "BUY" else BEAR_TECH_MAX


def side_rating(tech: float, side: str, sent: float = 0.0,
                bonus: float = 0.0) -> str:
    """X.X/10 display rating for a BUY or SELL setup.

    rating = technical score as a fraction of ITS OWN SIDE'S tech scale
             (0–10; a perfect BUY 28/28 and a perfect SELL 100/100 both
             read 10.0)
           + fundamental bonus as a fraction of its own max, worth up to
             ±FUND_RATING_MAX (morning scan only; watch has no overlay)
           + sentiment × 1.5, signed by side
    clamped to [0, 10].

    Fundamentals start from NEUTRAL: no data or a 0 bonus leaves the
    technical rating untouched (it used to count the 25 possible bonus
    points as missing, so a perfect tech-only BUY read 5.3/10 in the
    morning list but 10.0/10 in watch mode). Rating raw points read a
    perfect BUY as 2.8/10 against 3.0/10 for a one-signal SELL.

    Sentiment: good news raises a BUY and LOWERS a SELL (it argues
    against the short); bad news does the reverse.
    """
    rating10 = max(0.0, min(10.0, tech / side_scale(side) * 10.0))
    bonus_max = FUND_BONUS_MAX if side == "BUY" else SHORT_BONUS_MAX
    rating10 += max(-1.0, min(1.0, bonus / bonus_max)) * FUND_RATING_MAX
    rating10 = max(0.0, min(10.0, rating10))
    signed = sent if side == "BUY" else -sent
    return sentiment.adjusted_rating_str(rating10 * 10.0, signed)


def _trend_context(closes: list[float]) -> str | None:
    """'up' | 'down' | None — the chart's trend before anything else.

    up:   close > SMA200, SMA50 > SMA200, SMA200 rising over 20 sessions
    down: close < SMA200, SMA50 < SMA200, SMA200 falling over 20 sessions
    """
    if len(closes) < 220:
        return None
    s50, s200 = indicators.sma(closes, 50), indicators.sma(closes, 200)
    s200_prev = indicators.sma(closes[:-20], 200)
    c = closes[-1]
    if c > s200 and s50 > s200 and s200 > s200_prev:
        return "up"
    if c < s200 and s50 < s200 and s200 < s200_prev:
        return "down"
    return None


def risk_levels(price: float, atr14: float | None,
                side: str) -> tuple[float, float] | None:
    """(stop, target) from ATR(14): stop STOP_ATR x ATR against the trade,
    target TARGET_ATR x ATR in its favour (1.5 : 1 reward/risk)."""
    if not atr14 or atr14 <= 0:
        return None
    if side == "BUY":
        return price - STOP_ATR * atr14, price + TARGET_ATR * atr14
    return price + STOP_ATR * atr14, price - TARGET_ATR * atr14


def technical_screen(bars: list[dict],
                     volume_scale: float = 1.0,
                     spy_closes: list[float] | None = None) -> dict | None:
    """Score one symbol's daily bars like a chart reader. None when there
    isn't enough history.

    Returns {price, tech_score, setups:[(name, pts, reason)] (the winning
    BUY playbook's setups), playbook ('dip' | 'momentum' | None),
    bear_score, bear_setups, trend ('up' | 'down' | None), levels
    {BUY: (stop, target), SELL: (stop, target)}, indicators:{...}}.

    volume_scale (< 1 only in watch mode, where the last bar is today's
    PARTIAL session): volume conditions compare today's volume with
    multiple x median x volume_scale — the volume expected by now.
    spy_closes (oldest-first, same end date) enables relative strength;
    without it the leader setups simply don't fire.
    """
    if len(bars) < MIN_TECH_BARS:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [b["v"] for b in bars]
    price, open_ = closes[-1], bars[-1]["o"]
    hi, lo = highs[-1], lows[-1]
    ind: dict = {}

    trend = _trend_context(closes)
    ind["trend"] = trend
    rsi14 = indicators.rsi_wilder(closes, 14)[-1]
    rsi2 = indicators.rsi_wilder(closes, 2)[-1]
    ind["rsi"], ind["rsi2"] = rsi14, rsi2
    atr14 = indicators.atr(bars, 14)
    ind["atr14"] = atr14
    sma20 = indicators.sma(closes, 20)
    sma50 = indicators.sma(closes, 50)
    sma200 = indicators.sma(closes, 200)
    ind["sma20"], ind["sma50"], ind["sma200"] = sma20, sma50, sma200
    # Bollinger from the 20 sessions BEFORE today: "closed outside the
    # bands" means outside the envelope the market had established.
    _, bb_up, bb_lo = indicators.bollinger_bands(closes[:-1])
    ind["bb_upper"], ind["bb_lower"] = bb_up, bb_lo
    base = vols[-21:-1]
    med = statistics.median(base) if len(base) >= 20 else None
    vrel = (vols[-1] / (med * volume_scale)) if med else None
    ind["vol_ratio"] = (vols[-1] / med) if med else None
    rng = hi - lo
    cpos = (price - lo) / rng if rng > 0 else 0.5  # close within day range
    hi20 = max(highs[-21:-1]) if len(highs) >= 21 else None
    dist20 = ((price - sma20) / atr14) if (sma20 and atr14) else None
    ind["dist_sma20_atr"] = dist20
    rs = None
    if spy_closes and len(spy_closes) > RS_LOOKBACK and \
            len(closes) > RS_LOOKBACK and closes[-1 - RS_LOOKBACK] and \
            spy_closes[-1 - RS_LOOKBACK]:
        rs = (closes[-1] / closes[-1 - RS_LOOKBACK]
              - spy_closes[-1] / spy_closes[-1 - RS_LOOKBACK])
    ind["rs63"] = rs
    lookback = min(252, len(highs))
    ind["high52"], ind["low52"] = max(highs[-lookback:]), min(lows[-lookback:])

    # --- BUY playbook 1: DIP (buy weakness) ---------------------------------
    dip: list[tuple[str, int, str]] = []
    if rsi14 is not None and rsi14 < 30:
        dip.append(("oversold_rsi30", DIP_WEIGHTS["oversold_rsi30"],
                    f"RSI {rsi14:.1f} — deeply oversold"))
    elif rsi14 is not None and rsi14 < 35:
        dip.append(("oversold_rsi35", DIP_WEIGHTS["oversold_rsi35"],
                    f"RSI {rsi14:.1f} — oversold"))
    if bb_lo is not None and price < bb_lo:
        dip.append(("below_lower_band", DIP_WEIGHTS["below_lower_band"],
                    f"closed below lower Bollinger band ({_fmt_price(bb_lo)})"))
    if dist20 is not None and dist20 < -2:
        dip.append(("stretched_below_sma20",
                    DIP_WEIGHTS["stretched_below_sma20"],
                    f"{abs(dist20):.1f} ATR below 20-day average"))
    if rsi14 is not None and rsi14 < 35 and vrel is not None and vrel > 2:
        dip.append(("capitulation_volume", DIP_WEIGHTS["capitulation_volume"],
                    f"capitulation volume {ind['vol_ratio']:.1f}x median"))

    # --- BUY playbook 2: MOMENTUM (buy strength) ----------------------------
    mom: list[tuple[str, int, str]] = []
    if rs is not None and rs > 0.20:
        mom.append(("leader_rs20", MOMENTUM_WEIGHTS["leader_rs20"],
                    f"leader: beat SPY by {rs * 100:.0f} pts in 3 months"))
    elif rs is not None and rs > 0.10:
        mom.append(("leader_rs10", MOMENTUM_WEIGHTS["leader_rs10"],
                    f"outperforming SPY by {rs * 100:.0f} pts in 3 months"))
    if sma50 and price > sma50 * 1.15:
        mom.append(("extended15", MOMENTUM_WEIGHTS["extended15"],
                    f"{(price / sma50 - 1) * 100:.0f}% above 50-day — strong trend"))
    elif sma50 and price > sma50 * 1.10:
        mom.append(("extended10", MOMENTUM_WEIGHTS["extended10"],
                    f"{(price / sma50 - 1) * 100:.0f}% above 50-day"))
    if vrel is not None and vrel > 2 and price >= open_:
        mom.append(("volume_up_day", MOMENTUM_WEIGHTS["volume_up_day"],
                    f"volume {ind['vol_ratio']:.1f}x median on an up day"))
    if hi20 is not None and price > hi20 and cpos >= 0.7 and \
            vrel is not None and vrel >= 1.5:
        mom.append(("breakout20_volume", MOMENTUM_WEIGHTS["breakout20_volume"],
                    f"broke 20-day high ({_fmt_price(hi20)}) on volume"))
    if trend == "up":
        mom.append(("uptrend", MOMENTUM_WEIGHTS["uptrend"],
                    "uptrend (above rising 200-day, 50 > 200)"))

    dip_score = min(TECH_CAP, sum(p for _, p, _ in dip))
    mom_score = min(TECH_CAP, sum(p for _, p, _ in mom))
    ind["dip_score"], ind["momentum_score"] = dip_score, mom_score
    if dip_score == 0 and mom_score == 0:
        playbook, setups, tech_score = None, [], 0
    elif dip_score >= mom_score:
        playbook, setups, tech_score = "dip", dip, dip_score
    else:
        playbook, setups, tech_score = "momentum", mom, mom_score

    # --- SELL: strength inside the stock's own downtrend --------------------
    bear: list[tuple[str, int, str]] = []
    if trend == "down" and dist20 is not None and dist20 > 2:
        bear.append(("rally_in_downtrend", BEAR_WEIGHTS["rally_in_downtrend"],
                     f"downtrend rally {dist20:.1f} ATR above 20-day — "
                     f"into resistance"))
    if sma200 and price < sma200 and rsi14 is not None and rsi14 > 70:
        bear.append(("overbought_below_200",
                     BEAR_WEIGHTS["overbought_below_200"],
                     f"RSI {rsi14:.0f} overbought while below the 200-day"))
    if trend == "down" and rsi2 is not None and rsi2 > 90:
        bear.append(("short_term_overbought_dn",
                     BEAR_WEIGHTS["short_term_overbought_dn"],
                     f"RSI(2) {rsi2:.0f} — short-term overbought in a downtrend"))
    if bear and trend == "down":
        bear.append(("downtrend", BEAR_WEIGHTS["downtrend"],
                     "downtrend (below falling 200-day, 50 < 200)"))
    bear_score = min(BEAR_CAP, sum(p for _, p, _ in bear))

    return {"price": price, "tech_score": tech_score, "setups": setups,
            "playbook": playbook, "bear_score": bear_score,
            "bear_setups": bear, "trend": trend,
            "levels": {"BUY": risk_levels(price, atr14, "BUY"),
                       "SELL": risk_levels(price, atr14, "SELL")},
            "indicators": ind}


def classify_side(res: dict) -> str:
    """'BUY' when the BUY score >= the SELL score (one 0-100 scale for both
    sides); ties go bullish."""
    return "BUY" if res["tech_score"] >= res["bear_score"] else "SELL"


def risk_note(row: dict, side: str) -> str | None:
    """'stop $X · target $Y (ATR)' for an alert line, or None."""
    lv = (row.get("levels") or {}).get(side)
    if not lv:
        return None
    stop, tgt = lv
    return f"stop {_fmt_price(stop)} · target {_fmt_price(tgt)}"


# ---------------------------------------------------------------------------
# Fundamental overlay (top technical scorers only)
# ---------------------------------------------------------------------------
PIOTROSKI_MIN_TESTS = 7  # tests that must be evaluable before it scores


def _piotroski_usable(pio: dict) -> int | None:
    """Piotroski score, or None when too few of the 9 tests could run.

    Untestable items count as failures in the raw score, so a company
    with no prior-year filing maxes out at 3/9 and read as "weak" — a
    data gap scored as bad fundamentals. Missing data now scores 0.
    """
    evaluated = pio.get("evaluated")
    if evaluated is not None and evaluated < PIOTROSKI_MIN_TESTS:
        return None
    return pio.get("score")


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
    pscore = _piotroski_usable(pio)
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
    pscore = _piotroski_usable(pio)
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


def _spy_closes(rest: AlpacaREST) -> list[float] | None:
    """SPY daily closes for relative strength (one REST call per pass).
    Fetched alongside the universe so both series end on the same day.
    None on failure — the leader setups then just don't fire."""
    try:
        bars = rest.daily_bars_multi(["SPY"], days=300).get("SPY") or []
        return [b["c"] for b in bars] or None
    except Exception as e:  # noqa: BLE001 - advisory, fail open
        log.warning("SPY bars unavailable (%s) — no relative strength", e)
        return None


def push_bar(side: str, regime: str | None,
             playbook: str | None = None) -> int:
    """Technical-score bar a setup must clear to reach the phone.

    SELL: SELL_BAR (+ REGIME_SELL_PENALTY_BEAR in a bear regime). BUY:
    the playbook's bar, plus
    REGIME_BUY_PENALTY for the playbook the regime disfavours — dips in
    a bull regime, momentum in a sideways/bear regime. Unknown regime or
    playbook -> base bar (fail open)."""
    if side == "SELL":
        return SELL_BAR + (REGIME_SELL_PENALTY_BEAR if regime == "bear" else 0)
    pb = playbook if playbook in BUY_BARS else "dip"
    bar = BUY_BARS[pb]
    if (pb == "dip" and regime == "bull") or \
            (pb == "momentum" and regime in ("bear", "sideways")):
        bar += REGIME_BUY_PENALTY
    return bar


def _morning_bar(side: str, regime: str | None,
                 playbook: str | None = None) -> int:
    """Morning push bar (technical score) — same bars as watch mode."""
    return push_bar(side, regime, playbook)


def _watch_bar(side: str, regime: str | None,
               playbook: str | None = None) -> int:
    """Watch push bar (technical score) — same bars as the morning scan."""
    return push_bar(side, regime, playbook)


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


def _structured(row: dict, side: str) -> dict:
    """Machine-readable fields for the dashboard (no text parsing needed)."""
    lv = (row.get("levels") or {}).get(side) or (None, None)
    setups = row.get("setups" if side == "BUY" else "bear_setups") or []
    return {"side": side,
            "playbook": row.get("playbook") if side == "BUY" else None,
            "stop": lv[0], "target": lv[1],
            "tech_score": row.get("tech_score" if side == "BUY"
                                  else "bear_score"),
            "setups": [{"name": n, "points": p, "reason": r}
                       for n, p, r in setups]}


PUSH_REASON_LINES = 3     # setup lines per pick in a phone push


def _pct(a: float, b: float) -> str:
    v = (a - b) / b * 100 if b else 0.0
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def _friendly(line: str) -> str:
    """'news: +0.4 | analysts: 25 buy / …' -> 'News +0.4 · Analysts 25 buy / …'"""
    return (line.replace("news: ", "News ", 1).replace(" | analysts: ", " · Analysts ")
            .replace(" | marketaux: ", " · Marketaux "))


def push_block(row: dict, side: str, rating: str,
               reasons: list[str]) -> str:
    """One pick as a short, phone-readable block:

        🟢 BUY: NVDA  $217.09  ·  8.4/10  ·  dip buy
        • RSI 27 — deeply oversold
        • closed below lower Bollinger band ($210.10)
        • news: +0.4 | analysts: 40 buy / 6 hold / 1 sell
        Stop $205.28 (−5.4%)  ·  Target $234.79 (+8.2%)
    """
    tag = _playbook_tag(row, side)
    head = (f"{'🟢' if side == 'BUY' else '🔴'} {side}: {row['symbol']}  "
            f"{_fmt_price(row['price'])}  ·  {rating}"
            + (f"  ·  {tag}" if tag else ""))
    body = [r for r in reasons if r and r != tag
            and not r.startswith("stop $")]
    setup = [r for r in body if not r.startswith(("news:", "⚠"))]
    extra = [r for r in body if r.startswith(("news:", "⚠"))]
    lines = [head] + [f"• {r}" for r in setup[:PUSH_REASON_LINES]]
    lines += [f"• {_friendly(r)}" for r in extra]
    lv = (row.get("levels") or {}).get(side)
    if lv:
        stop, tgt = lv
        lines.append(f"Stop {_fmt_price(stop)} ({_pct(stop, row['price'])})"
                     f"  ·  Target {_fmt_price(tgt)} "
                     f"({_pct(tgt, row['price'])})")
    return "\n".join(lines)


def _playbook_tag(row: dict, side: str) -> str | None:
    if side == "BUY" and row.get("playbook"):
        return "dip buy" if row["playbook"] == "dip" else "momentum"
    return None


def _watch_reasons(r: dict) -> list[str]:
    """Reason strings for a watch match: setups, sentiment, risk, earnings."""
    side = r["side"]
    setups = r["setups"] if side == "BUY" else r["bear_setups"]
    reasons = [reason for _, _, reason in setups]
    tag = _playbook_tag(r, side)
    if tag:
        reasons.insert(0, tag)
    if sentiment.has_signal(r.get("sentiment_payload")):
        reasons.append(sentiment.format_combined(r["sentiment_payload"]))
    note = risk_note(r, side)
    if note:
        reasons.append(note)
    return _with_note(reasons, r)


def _log_bars(mode: str, regime: str | None) -> None:
    if regime in ("bull", "bear", "sideways"):
        log.info("%s: %s regime — buy bars dip %d / momentum %d, sell %d",
                 mode, regime, push_bar("BUY", regime, "dip"),
                 push_bar("BUY", regime, "momentum"),
                 push_bar("SELL", regime))


def _watch_entry(r: dict) -> dict:
    """Dashboard-ready entry for one NEW watch match — mirrors the push line.

    Carries everything the alert shows: symbol, side, price, the 0–100
    score, the X.X/10 display rating, and the human-readable breakdown.
    """
    reasons = _watch_reasons(r)
    return {"symbol": r["symbol"],
            "side": r["side"],
            "price": r["price"],
            "score": r["score"],
            "rating": side_rating(r["score"], r["side"],
                                  r.get("sentiment", 0.0)),
            "breakdown": "; ".join(reasons),
            "reasons": reasons,
            "time": _et_now().isoformat(),
            **_structured(r, r["side"])}


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

    # Regime picks the favoured BUY playbook (see push_bar). Fail open.
    regime = _current_regime(store, rest)
    _log_bars("watch", regime)
    spy = _spy_closes(rest)

    # Today's bar is still forming: scale the volume-spike threshold by
    # the elapsed fraction of the session (see technical_screen).
    volume_scale = _session_fraction()

    # Screen chunk-by-chunk (memory): bars are discarded after each chunk.
    fresh: list[dict] = []
    screened = 0
    push_ok: bool | None = None
    for ci in range(0, len(universe), 100):
        bars_by_sym = rest.daily_bars_multi(universe[ci:ci + 100], days=300)
        for sym in universe[ci:ci + 100]:
            if _seen_covers_both(seen_set, sym):
                continue
            bars = bars_by_sym.get(sym)
            if not bars:
                continue
            screened += 1
            res = technical_screen(bars, volume_scale=volume_scale,
                                   spy_closes=spy)
            if res is None or not passes_liquidity(bars):
                continue
            # The labeled side must clear ITS bar (BUY: its playbook's).
            side = classify_side(res)
            if _seen_covers(seen_set, sym, side):
                continue  # this side already alerted today; a later
                # flip to the other side can still alert
            score = (res["tech_score"] if side == "BUY"
                     else res["bear_score"])
            if score < push_bar(side, regime, res.get("playbook")):
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
        buys = [r for r in fresh if r["side"] == "BUY"]
        sells = [r for r in fresh if r["side"] == "SELL"]

        def _line(r: dict) -> str:
            return push_block(r, r["side"], side_rating(
                r["score"], r["side"], r.get("sentiment", 0.0)),
                _watch_reasons(r))

        shown = fresh[:WATCH_PUSH_MAX]
        lines = [_line(r) for r in shown]
        if len(fresh) > WATCH_PUSH_MAX:
            lines.append(f"…and {len(fresh) - WATCH_PUSH_MAX} more on the "
                         f"dashboard")

        parts = []
        if buys:
            parts.append(f"BUY SETUP: {len(buys)} new"
                         if len(buys) != 1
                         else f"BUY SETUP: {buys[0]['symbol']} @ "
                              f"{_fmt_price(buys[0]['price'])} — "
                              f"{side_rating(buys[0]['score'], 'BUY', buys[0].get('sentiment', 0.0))}")
        if sells:
            parts.append(f"SELL SETUP: {len(sells)} new"
                         if len(sells) != 1
                         else f"SELL SETUP: {sells[0]['symbol']} @ "
                              f"{_fmt_price(sells[0]['price'])} — "
                              f"{side_rating(sells[0]['score'], 'SELL', sells[0].get('sentiment', 0.0))}")
        title = " · ".join(parts)
        body = "\n\n".join(lines)
        if dry_run:
            # dry run: print only — never touch the seen-list, or the
            # real 15-minute watch would stay silent on these symbols
            print(title)
            print(body)
        else:
            if push_fn is None:
                ok = send_ntfy(settings.ntfy_server, settings.ntfy_topic,
                               title, body, priority=4,
                               tags=["rotating_light",
                                     "chart_with_upwards_trend"],
                               timeout=30)
            else:
                ok = push_fn(title, body)
            push_ok = bool(ok)
            log.info("watch: ntfy push %s (%d new: %d buy, %d sell)",
                     "sent" if ok else "FAILED",
                     len(fresh), len(buys), len(sells))
            if ok:
                # mark seen only once the alert actually reached the
                # phone; a failed push is retried on the next pass
                seen["symbols"].extend(_seen_key(r["symbol"], r["side"])
                                       for r in fresh)
                _save_seen(store, seen)
                _persist_watch(store, fresh)
    else:
        log.info("watch: no new setups")
        if dry_run:
            print("(no new setups)")
    if not dry_run:
        _persist_watch_last(store, screened, len(fresh), push_ok)
    return fresh


def _persist_watch_last(store: Store, screened: int, new: int,
                        push_ok: bool | None) -> None:
    """Heartbeat for the dashboard: when the 15-minute pass last ran, how
    many stocks it screened and what it found — written on EVERY pass,
    including the quiet ones that find nothing (those used to leave no
    trace, so a quiet day looked the same as a stopped scanner)."""
    try:
        store.poller_set(SCAN_WATCH_LAST_KEY, json.dumps({
            "ts": _et_now().isoformat(), "screened": screened, "new": new,
            "push_ok": push_ok, "interval_min": WATCH_INTERVAL_MIN}))
    except Exception as e:  # noqa: BLE001 - heartbeat is best-effort
        log.warning("watch: could not record last-run time: %s", e)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _candidate_reasons(c: dict, side: str) -> list[str]:
    """The same reason strings the morning push lines print for a candidate."""
    setups = c["setups"] if side == "BUY" else c["bear_setups"]
    extra = c.get("fund_reasons") if side == "BUY" else c.get("short_reasons")
    reasons = [r for _, _, r in setups] + list(extra or [])
    tag = _playbook_tag(c, side)
    if tag:
        reasons.insert(0, tag)
    if sentiment.has_signal(c.get("sentiment_payload")):
        reasons.append(sentiment.format_combined(c["sentiment_payload"]))
    note = risk_note(c, side)
    if note:
        reasons.append(note)
    return _with_note(reasons, c)


def _morning_rating(c: dict, side: str) -> str:
    """Morning-list rating: technical score + fundamental bonus, each on
    its own scale (see side_rating)."""
    if side == "BUY":
        return side_rating(c["tech_score"], "BUY", c.get("sentiment", 0.0),
                           c.get("fund_bonus", 0))
    return side_rating(c["bear_score"], "SELL", c.get("sentiment", 0.0),
                       c.get("short_bonus", 0))


def _morning_entry(c: dict, side: str, top_pick: bool = False) -> dict:
    """Dashboard-ready entry for one morning-scan candidate — everything the
    push line shows: symbol, price, the 0–100 composite, the X.X/10 display
    rating, and the human-readable breakdown."""
    score = c["composite"] if side == "BUY" else c["short_composite"]
    reasons = _candidate_reasons(c, side)
    return {"symbol": c["symbol"],
            "price": c["price"],
            "score": score,
            "rating": _morning_rating(c, side),
            "breakdown": "; ".join(reasons),
            "reasons": reasons,
            "sentiment": c.get("sentiment", 0.0),
            "top_pick": top_pick,
            **_structured(c, side)}


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
    today = _et_today_str()
    is_open = sessions_mod.is_trading_day(store, today)
    if is_open is None:
        sessions_mod.refresh_cache(store)
        is_open = sessions_mod.is_trading_day(store, today)
    if is_open is not True:
        log.info("scan: %s is not a trading day — skipping quietly", today)
        return {"buys": [], "sells": []}

    # fresh day, fresh watch: the morning scan resets the watch seen-list
    # (not on a dry run — that would re-fire every alert already sent today)
    if not dry_run:
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
    spy = _spy_closes(rest)
    for ci in range(0, len(universe), 100):
        bars_by_sym = rest.daily_bars_multi(universe[ci:ci + 100], days=300)
        for sym in universe[ci:ci + 100]:
            bars = bars_by_sym.get(sym)
            if not bars:
                continue
            res = technical_screen(bars, spy_closes=spy)
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

    # Push decision on the TECHNICAL score against the playbook's bar
    # (regime shifts the BUY playbook bars — see push_bar). Fundamentals
    # and sentiment only rank and rate. The persisted dashboard lists are
    # never gated: every setup keeps its true score. One side per symbol.
    regime = _current_regime(store, rest)
    _log_bars("scan", regime)
    buy_cands = [r for r in bull_rows if classify_side(r) == "BUY"
                 and r["tech_score"] >= push_bar("BUY", regime,
                                                 r.get("playbook"))]
    sell_cands = [r for r in bear_rows if classify_side(r) == "SELL"
                  and r["bear_score"] >= push_bar("SELL", regime)]
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
        return push_block(c, "BUY", _morning_rating(c, "BUY"),
                          _candidate_reasons(c, "BUY"))

    def _sell_line(i: int, c: dict) -> str:
        return push_block(c, "SELL", _morning_rating(c, "SELL"),
                          _candidate_reasons(c, "SELL"))

    sections: list[str] = []
    title_parts: list[str] = []
    if buys:
        title_parts.append(f"{len(buys)} buy candidate"
                           f"{'s' if len(buys) != 1 else ''}")
        sections.append(f"BUY CANDIDATES ({len(buys)})\n\n"
                        + "\n\n".join(_buy_line(i, c)
                                      for i, c in enumerate(buys, 1)))
        for i, c in enumerate(buys, 1):
            log.info("scan: %s", _buy_line(i, c).replace("\n", " | "))
    if sells:
        title_parts.append(f"{len(sells)} sell candidate"
                           f"{'s' if len(sells) != 1 else ''}")
        sections.append(f"SELL CANDIDATES ({len(sells)})\n\n"
                        + "\n\n".join(_sell_line(i, c)
                                      for i, c in enumerate(sells, 1)))
        for i, c in enumerate(sells, 1):
            log.info("scan: %s", _sell_line(i, c).replace("\n", " | "))

    if not dry_run and (buys or sells):
        title = "Morning scan: " + " + ".join(title_parts)
        body = "\n\n\n".join(sections)
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
