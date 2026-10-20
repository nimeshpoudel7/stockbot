"""Headline sentiment for scanner candidates.

News source: Finnhub ``/company-news`` (free tier), 7-day window per symbol.
Only symbols that already passed the technical screen are ever fetched —
never the full 13,520-symbol universe. Results are cached 24h per symbol in
SQLite (``poller_state`` key ``sentiment_<SYM>``), so repeat scans and the
15-minute watch are free after the first fetch. Fetch failures (including a
missing FINNHUB_KEY) degrade to neutral 0.0 and are cached too, so a dead
feed never hammers the API.

Method: finance-tuned keyword lexicon, no new dependencies. Each headline
scores in [-1, +1] as (positive hits − negative hits) / total hits, with a
simple negation check — a negation word ("not", "never", "failed to", ...)
within 3 tokens before a keyword flips that hit's polarity, so "did not
beat estimates" doesn't count as good news. Symbol sentiment is the
recency-weighted mean of its headlines (weight 1/(1+age_days)); zero
headlines → 0.0 (neutral), never a crash.

Rating math: the scanner's display rating is composite/10. Sentiment shifts
it by ``sentiment × 1.5`` points, clamped to [0, 10]:

    adjusted = clamp(rating10 + sentiment * 1.5, 0, 10)

So strongly positive sentiment can add up to +1.5 and strongly negative
sentiment can subtract up to 1.5. The contribution is shown in alerts, e.g.
``news: +0.6 | analysts: 14 buy / 4 hold / 1 sell``. Symbols with no
headline coverage and no analyst coverage show no sentiment line at all.

Analyst recommendations (the trusted signal): Finnhub
``/stock/recommendation`` (free tier), latest monthly period only. Score =
(2×strongBuy + buy − sell − 2×strongSell) / total analysts, clamped to
[-1, +1]; no coverage → 0.0 (neutral), never a crash. Cached 24h per
symbol (analyst data updates monthly; fresher polling buys nothing).

Combination: total = 0.65 × news + 0.35 × analyst (news-heavier —
analyst consensus skews bullish, since almost nobody rates "sell").
The rating adjustment above applies to this total — one module, one
adjustment path.

Marketaux (second news source, optional): when MARKETAUX_API_TOKEN is
set, per-entity sentiment scores from the Marketaux news API join the
news side. The news leg becomes the mean of the *available* news legs —
the Finnhub lexicon score (available when it saw headlines) and the
Marketaux score (available when it produced one) — over the same 7-day
window with the same 1/(1+age_days) recency weighting. Cached 24h per
symbol (``poller_state`` key ``marketaux_<SYM>``). The free tier allows
100 requests/day, so a daily counter (``marketaux_calls_<ET date>``)
stops new fetches at 90/day. Unset token = feature off and the
Finnhub-only behavior is exactly as before.

Free-tier discipline: at most one Finnhub call per candidate per 24h; the
morning scan fetches only for ranked candidates (≤24), watch mode only for
newly matched symbols. Finnhub free allows 60 calls/min — a cold morning
scan stays far under that.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("stockbot.sentiment")

NEWS_API = "https://finnhub.io/api/v1/company-news"
NEWS_WINDOW_DAYS = 7
CACHE_TTL_SEC = 24 * 3600
SENTIMENT_WEIGHT = 1.5  # rating points shifted per unit of sentiment

ANALYST_API = "https://finnhub.io/api/v1/stock/recommendation"
ANALYST_TTL_SEC = 24 * 3600  # analyst data updates monthly; 24h is plenty
NEWS_WEIGHT = 0.65     # combined total = 0.65 * news + 0.35 * analyst
ANALYST_WEIGHT = 0.35  # analyst-light: consensus skews bullish (few "sell"s)

MARKETAUX_API = "https://api.marketaux.com/v1/news/all"
MARKETAUX_TTL_SEC = 24 * 3600
MARKETAUX_DAILY_BUDGET = 90  # free tier is 100 requests/day; keep headroom

# ---------------------------------------------------------------------------
# Lexicon: finance-tuned keyword phrases (lowercase). Each hit counts ±1;
# per-headline score = (pos − neg) / (pos + neg), in [-1, +1].
# ---------------------------------------------------------------------------
POSITIVE_PHRASES = (
    "beats", "beat estimates", "beating estimates", "topped estimates",
    "exceeded expectations", "better than expected", "stronger than expected",
    "raises guidance", "raised guidance", "raising guidance",
    "lifts guidance", "lifts outlook", "raises outlook", "boosts forecast",
    "raises forecast",
    "upgrade", "upgraded", "upgrades",
    "record revenue", "record profit", "record earnings", "record sales",
    "surge", "surged", "surges", "soar", "soared", "soars",
    "jump", "jumped", "jumps", "rally", "rallied", "rallies",
    "approval", "approved", "approves", "fda approval",
    "wins contract", "contract win", "awarded contract", "new contract",
    "wins",
    "partnership", "partners with", "strategic deal",
    "buyback", "share repurchase", "repurchase program",
    "dividend hike", "raises dividend", "increases dividend",
    "special dividend",
    "acquisition", "acquires", "takeover", "buyout", "to acquire",
    "outperform", "overweight", "price target raised", "raises price target",
    "initiated at buy", "initiate at buy", "top pick",
    "margin expansion", "profitable quarter",
    "breakthrough", "positive trial", "positive data",
    "meets primary endpoint",
)

NEGATIVE_PHRASES = (
    "misses", "missed estimates", "missed expectations",
    "worse than expected", "weaker than expected",
    "falls short", "fell short",
    "cuts guidance", "cut guidance", "cutting guidance",
    "lowers guidance", "lowering guidance", "slashes guidance",
    "cuts outlook", "lowers outlook",
    "profit warning", "issues warning", "warns of",
    "downgrade", "downgraded", "downgrades",
    "lawsuit", "sued", "class action",
    "probe", "investigation", "investigated", "sec probe",
    "fraud", "fraudulent",
    "restatement", "restates earnings", "accounting irregularities",
    "bankruptcy", "chapter 11", "insolvent", "insolvency",
    "layoffs", "job cuts", "lay off", "workforce reduction",
    "dilution", "dilutive", "share offering", "secondary offering",
    "reverse split",
    "recall", "recalls", "recalling",
    "underperform", "underweight", "price target cut", "cuts price target",
    "lowers price target", "initiated at sell",
    "plunge", "plunged", "plunges", "tumble", "tumbled",
    "crash", "crashed", "slump", "slumped", "sinks",
    "dividend cut", "cuts dividend", "suspends dividend",
    "delisting", "delisted", "going concern",
    "short report", "short seller",
    "data breach", "cyberattack",
)

# A negation word within NEGATION_WINDOW tokens before a keyword flips it.
NEGATIONS = frozenset({
    "not", "no", "never", "without", "fails", "failed", "failing",
    "unable", "denies", "denied", "rejects", "rejected", "lack", "lacks",
})
NEGATION_WINDOW = 3


def _phrase_pattern(phrase: str) -> re.Pattern:
    return re.compile(r"\b" + r"\s+".join(map(re.escape, phrase.split())) + r"\b")


_PHRASE_RES = {p: _phrase_pattern(p) for p in POSITIVE_PHRASES + NEGATIVE_PHRASES}


def _negated(text: str, match_start: int) -> bool:
    """True if a negation word (or n't) sits just before the match."""
    window = text[max(0, match_start - 48):match_start].lower()
    tokens = re.findall(r"[a-z]+", window)[-NEGATION_WINDOW:]
    return "n't" in window or any(t in NEGATIONS for t in tokens)


def score_headline(text: str) -> float:
    """One headline → [-1, +1]. 0.0 when nothing in the lexicon matches."""
    t = text.lower()
    pos = neg = 0
    for phrase in POSITIVE_PHRASES:
        for m in _PHRASE_RES[phrase].finditer(t):
            if _negated(t, m.start()):
                neg += 1
            else:
                pos += 1
    for phrase in NEGATIVE_PHRASES:
        for m in _PHRASE_RES[phrase].finditer(t):
            if _negated(t, m.start()):
                pos += 1
            else:
                neg += 1
    total = pos + neg
    return (pos - neg) / total if total else 0.0


def aggregate(headlines: list[dict]) -> tuple[float, int]:
    """Recency-weighted mean of headline scores → (sentiment, n).

    headlines: [{"text": str, "age_days": float}]. Weight 1/(1+age_days).
    """
    num = den = 0.0
    for h in headlines:
        w = 1.0 / (1.0 + max(0.0, h.get("age_days", 3.5)))
        num += w * score_headline(h.get("text", ""))
        den += w
    return (num / den if den else 0.0), len(headlines)


def label_for(sentiment: float) -> str:
    if sentiment > 0.15:
        return "positive"
    if sentiment < -0.15:
        return "negative"
    return "neutral"


# ---------------------------------------------------------------------------
# Rating adjustment (documented math, also in README)
# ---------------------------------------------------------------------------
def apply_to_rating(rating10: float, sentiment: float) -> tuple[float, float]:
    """(adjusted_rating, contribution).

    contribution = sentiment × 1.5; adjusted = clamp(rating10 + contribution,
    0, 10). Strong positive news adds up to +1.5, strong negative subtracts
    up to 1.5.
    """
    contrib = sentiment * SENTIMENT_WEIGHT
    adjusted = min(10.0, max(0.0, rating10 + contrib))
    return adjusted, contrib


def adjusted_rating_str(score100: float, sentiment: float) -> str:
    """Display rating out of 10 (one decimal) with sentiment applied."""
    adj, _ = apply_to_rating(score100 / 10.0, sentiment)
    return f"{adj:.1f}/10"


def format_contribution(sentiment: float) -> str:
    """Alert-ready news line, e.g. 'news: positive (+0.8)'."""
    contrib = sentiment * SENTIMENT_WEIGHT
    sign = "+" if contrib >= 0 else ""
    return f"news: {label_for(sentiment)} ({sign}{contrib:.1f})"


def neutral() -> dict:
    return {"sentiment": 0.0, "n": 0, "label": "neutral",
            "fetched_at": _utcnow()}


# ---------------------------------------------------------------------------
# Fetch + 24h cache (poller_state, same convention as the scanner universe)
# ---------------------------------------------------------------------------
def _cache_key(symbol: str) -> str:
    return f"sentiment_{symbol.upper()}"


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _iso_ts(s: str) -> float:
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return 0.0


def fetch_headlines(symbol: str, days: int = NEWS_WINDOW_DAYS) -> list[dict]:
    """Recent Finnhub headlines for one symbol. Raises on any failure."""
    key = os.environ.get("FINNHUB_KEY", "")
    if not key:
        raise RuntimeError("FINNHUB_KEY not set")
    to_d = dt.date.today()
    from_d = to_d - dt.timedelta(days=days)
    r = requests.get(NEWS_API,
                     params={"symbol": symbol.upper(),
                             "from": from_d.isoformat(),
                             "to": to_d.isoformat(), "token": key},
                     timeout=15)
    r.raise_for_status()
    now = time.time()
    out: list[dict] = []
    for item in r.json():
        text = f"{item.get('headline', '')} {item.get('summary', '')}".strip()
        if not text:
            continue
        ts = item.get("datetime", 0) or 0
        age_days = max(0.0, (now - ts) / 86400) if ts else days / 2
        out.append({"text": text, "age_days": age_days,
                    "headline": item.get("headline", ""),
                    "url": item.get("url", "")})
    return out


def get_news(store, symbol: str) -> dict:
    """News-headline sentiment, cached. Never raises — failures degrade to
    neutral.

    {"sentiment": float in [-1, 1], "n": headline count,
     "label": "positive"|"negative"|"neutral", "fetched_at": iso}
    At most one Finnhub call per symbol per 24h (failures cached too).
    """
    symbol = symbol.upper()
    raw = store.poller_get(_cache_key(symbol))
    if raw:
        try:
            data = json.loads(raw)
            if time.time() - _iso_ts(data["fetched_at"]) < CACHE_TTL_SEC:
                return data["payload"]
        except (ValueError, KeyError, TypeError):
            pass
    try:
        headlines = fetch_headlines(symbol)
        s, n = aggregate(headlines)
    except Exception as e:  # noqa: BLE001 - news is advisory, never fatal
        log.warning("sentiment: %s news fetch failed (%s) — neutral",
                    symbol, e)
        s, n = 0.0, 0
    payload = {"sentiment": round(s, 3), "n": n, "label": label_for(s),
               "fetched_at": _utcnow()}
    try:
        store.poller_set(_cache_key(symbol),
                         json.dumps({"fetched_at": payload["fetched_at"],
                                     "payload": payload}))
    except Exception as e:  # noqa: BLE001 - cache failure is non-fatal
        log.warning("sentiment: cache write failed for %s (%s)", symbol, e)
    return payload


# ---------------------------------------------------------------------------
# Analyst recommendations (Finnhub /stock/recommendation, free tier)
# ---------------------------------------------------------------------------
def _analyst_cache_key(symbol: str) -> str:
    return f"analyst_{symbol.upper()}"


def _int(v) -> int:
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def analyst_score_from_period(p: dict) -> tuple[float, dict]:
    """Latest recommendation period → (score, counts).

    score = (2×strongBuy + buy − sell − 2×strongSell) / total analysts,
    clamped to [-1, +1]. Empty/missing period → (0.0, zero counts).
    counts carries the raw buckets plus the period string and total.
    """
    p = p or {}
    sb = _int(p.get("strongBuy"))
    b = _int(p.get("buy"))
    h = _int(p.get("hold"))
    s = _int(p.get("sell"))
    ss = _int(p.get("strongSell"))
    total = sb + b + h + s + ss
    counts = {"strongBuy": sb, "buy": b, "hold": h, "sell": s,
              "strongSell": ss, "total": total,
              "period": str(p.get("period", ""))}
    if total <= 0:
        return 0.0, counts
    raw = (2 * sb + b - s - 2 * ss) / total
    return max(-1.0, min(1.0, raw)), counts


def fetch_recommendations(symbol: str) -> list[dict]:
    """All recommendation periods for one symbol, latest first. Raises."""
    key = os.environ.get("FINNHUB_KEY", "")
    if not key:
        raise RuntimeError("FINNHUB_KEY not set")
    r = requests.get(ANALYST_API,
                     params={"symbol": symbol.upper(), "token": key},
                     timeout=15)
    r.raise_for_status()
    periods = r.json() or []
    periods.sort(key=lambda d: str(d.get("period", "")), reverse=True)
    return periods


def get_analyst(store, symbol: str) -> dict:
    """Analyst recommendation sentiment, cached. Never raises.

    {"score": float in [-1, 1], "counts": {strongBuy, buy, hold, sell,
     strongSell, total, period}, "fetched_at": iso}
    At most one Finnhub call per symbol per 24h (failures cached too).
    """
    symbol = symbol.upper()
    raw = store.poller_get(_analyst_cache_key(symbol))
    if raw:
        try:
            data = json.loads(raw)
            if time.time() - _iso_ts(data["fetched_at"]) < ANALYST_TTL_SEC:
                return data["payload"]
        except (ValueError, KeyError, TypeError):
            pass
    try:
        periods = fetch_recommendations(symbol)
        score, counts = analyst_score_from_period(
            periods[0] if periods else {})
    except Exception as e:  # noqa: BLE001 - advisory, never fatal
        log.warning("sentiment: %s analyst fetch failed (%s) — neutral",
                    symbol, e)
        score, counts = analyst_score_from_period({})
    payload = {"score": round(score, 3), "counts": counts,
               "fetched_at": _utcnow()}
    try:
        store.poller_set(_analyst_cache_key(symbol),
                         json.dumps({"fetched_at": payload["fetched_at"],
                                     "payload": payload}))
    except Exception as e:  # noqa: BLE001 - cache failure is non-fatal
        log.warning("sentiment: analyst cache write failed for %s (%s)",
                    symbol, e)
    return payload


# ---------------------------------------------------------------------------
# Marketaux news sentiment (per-entity scores, second news source)
# ---------------------------------------------------------------------------
def _marketaux_cache_key(symbol: str) -> str:
    return f"marketaux_{symbol.upper()}"


def _marketaux_token() -> str:
    return os.environ.get("MARKETAUX_API_TOKEN", "").strip()


def _et_today() -> str:
    """Today's date in US Eastern — the budget counter's day boundary."""
    return dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _marketaux_counter_key() -> str:
    return f"marketaux_calls_{_et_today()}"


def _marketaux_calls_today(store) -> int:
    try:
        return int(store.poller_get(_marketaux_counter_key()) or 0)
    except (ValueError, TypeError):
        return 0


def _parse_published(value) -> float:
    """Marketaux published_at (ISO 8601, usually '...Z') → epoch seconds.
    0.0 when missing/unparseable."""
    if not value:
        return 0.0
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _iso_ts(text)


def marketaux_score_from_articles(articles,
                                  symbol: str) -> tuple[float | None, int]:
    """Per-entity Marketaux scores → (recency-weighted score, article count).

    Only the entity matching `symbol` counts, and only articles inside
    the 7-day window. Weight 1/(1+age_days), same as the Finnhub news
    leg. No matching coverage → (None, 0), never a defaulted 0.0.
    """
    symbol = symbol.upper()
    now = time.time()
    num = den = 0.0
    n = 0
    for art in articles or []:
        ts = _parse_published(art.get("published_at"))
        if not ts:
            continue
        age_days = max(0.0, (now - ts) / 86400)
        if age_days > NEWS_WINDOW_DAYS:
            continue
        for ent in art.get("entities") or []:
            if str(ent.get("symbol", "")).upper() != symbol:
                continue
            raw = ent.get("sentiment_score")
            if raw is None:
                break
            try:
                s = float(raw)
            except (ValueError, TypeError):
                break
            s = max(-1.0, min(1.0, s))
            w = 1.0 / (1.0 + age_days)
            num += w * s
            den += w
            n += 1
            break
    if not den:
        return None, 0
    return num / den, n


def fetch_marketaux(symbol: str, token: str) -> tuple[float | None, int]:
    """One Marketaux news call for one symbol. Raises on any failure."""
    r = requests.get(MARKETAUX_API,
                     params={"symbols": symbol.upper(),
                             "filter_entities": "true",
                             "language": "en",
                             "limit": 10,
                             "api_token": token},
                     timeout=15)
    r.raise_for_status()
    data = r.json() or {}
    return marketaux_score_from_articles(data.get("data") or [], symbol)


def get_marketaux(store, symbol: str) -> dict:
    """Marketaux entity sentiment, cached 24h. Never raises.

    {"score": float in [-1, 1] or None (no coverage / feature off /
     budget exhausted / fetch failed), "n": contributing article count,
     "fetched_at": iso}
    Feature is off when MARKETAUX_API_TOKEN is unset: no call, no cache
    write, Finnhub-only behavior unchanged. Free tier is 100
    requests/day — a poller_state counter stops new fetches at
    MARKETAUX_DAILY_BUDGET per ET day. HTTP failures (incl. 429) cache a
    None result for 24h so a dead feed never hammers the API; successful
    no-coverage results are cached too.
    """
    symbol = symbol.upper()
    raw = store.poller_get(_marketaux_cache_key(symbol))
    if raw:
        try:
            data = json.loads(raw)
            if time.time() - _iso_ts(data["fetched_at"]) < MARKETAUX_TTL_SEC:
                return data["payload"]
        except (ValueError, KeyError, TypeError):
            pass
    token = _marketaux_token()
    if not token:
        return {"score": None, "n": 0, "fetched_at": _utcnow()}
    if _marketaux_calls_today(store) >= MARKETAUX_DAILY_BUDGET:
        log.info("sentiment: Marketaux daily budget reached — skipping %s",
                 symbol)
        return {"score": None, "n": 0, "fetched_at": _utcnow()}
    try:
        store.poller_set(_marketaux_counter_key(),
                         str(_marketaux_calls_today(store) + 1))
    except Exception as e:  # noqa: BLE001 - counter is best-effort
        log.warning("sentiment: Marketaux counter write failed (%s)", e)
    try:
        score, n = fetch_marketaux(symbol, token)
    except Exception as e:  # noqa: BLE001 - advisory, never fatal
        log.warning("sentiment: %s Marketaux fetch failed (%s) — no score",
                    symbol, e)
        score, n = None, 0
    payload = {"score": (round(score, 3) if score is not None else None),
               "n": n, "fetched_at": _utcnow()}
    try:
        store.poller_set(_marketaux_cache_key(symbol),
                         json.dumps({"fetched_at": payload["fetched_at"],
                                     "payload": payload}))
    except Exception as e:  # noqa: BLE001 - cache failure is non-fatal
        log.warning("sentiment: Marketaux cache write failed for %s (%s)",
                    symbol, e)
    return payload


# ---------------------------------------------------------------------------
# Combined sentiment: total = 0.65 * news + 0.35 * analyst
# ---------------------------------------------------------------------------
def combine(news: float, analyst: float) -> float:
    """Merge the two signals into one total in [-1, +1]."""
    total = NEWS_WEIGHT * news + ANALYST_WEIGHT * analyst
    return max(-1.0, min(1.0, total))


def blend_news(news_score: float, news_n: int,
               marketaux_score: float | None) -> float:
    """Mean of the *available* news legs. The Finnhub lexicon leg counts
    as available only when it saw headlines (n > 0) — its no-coverage
    0.0 must not dilute a real Marketaux score; Marketaux counts only
    when it produced a score. Neither available → 0.0 (neutral)."""
    legs = []
    if news_n > 0:
        legs.append(news_score)
    if marketaux_score is not None:
        legs.append(marketaux_score)
    return sum(legs) / len(legs) if legs else 0.0


def get_sentiment(store, symbol: str) -> dict:
    """Combined news + analyst sentiment. Never raises — failures degrade
    to neutral on each leg independently.

    {"sentiment": combined total in [-1, 1] (the rating input),
     "news": Finnhub news score, "analyst": analyst score,
     "n": Finnhub headline count,
     "analyst_counts": {strongBuy, buy, hold, sell, strongSell, total,
                        period},
     "marketaux": Marketaux score or None, "marketaux_n": its article
     count,
     "label": label_for(total), "fetched_at": iso}
    The news side of the total is blend_news(Finnhub, Marketaux).
    One API call per leg per symbol per 24h (cached independently).
    """
    symbol = symbol.upper()
    news = get_news(store, symbol)
    analyst = get_analyst(store, symbol)
    marketaux = get_marketaux(store, symbol)
    blended = blend_news(news["sentiment"], news["n"], marketaux["score"])
    total = combine(blended, analyst["score"])
    return {
        "sentiment": round(total, 3),
        "news": news["sentiment"],
        "analyst": analyst["score"],
        "n": news["n"],
        "analyst_counts": analyst["counts"],
        "marketaux": marketaux["score"],
        "marketaux_n": marketaux["n"],
        "label": label_for(total),
        "fetched_at": _utcnow(),
    }


def has_signal(payload: dict | None) -> bool:
    """True when there is anything worth showing: headlines or analyst
    coverage. Tolerates the legacy neutral stub (no analyst keys)."""
    if not payload:
        return False
    if payload.get("n", 0) > 0:
        return True
    if payload.get("marketaux_n", 0) > 0:
        return True
    return (payload.get("analyst_counts") or {}).get("total", 0) > 0


def format_combined(payload: dict) -> str:
    """Alert-ready line, e.g. 'news: +0.6 | analysts: 14 buy / 4 hold / 1
    sell'. Strong Buy folds into buy, Strong Sell into sell. When
    Marketaux contributed a score it is appended, e.g.
    ' | marketaux: +0.4 (6 articles)'."""
    news = payload.get("news", 0.0)
    ac = payload.get("analyst_counts") or {}
    b = ac.get("buy", 0) + ac.get("strongBuy", 0)
    h = ac.get("hold", 0)
    s = ac.get("sell", 0) + ac.get("strongSell", 0)
    out = (f"news: {news:+.1f} | analysts: {b} buy / {h} hold / "
           f"{s} sell")
    mx = payload.get("marketaux")
    if mx is not None:
        n = payload.get("marketaux_n", 0)
        out += (f" | marketaux: {mx:+.1f} "
                f"({n} article{'s' if n != 1 else ''})")
    return out
