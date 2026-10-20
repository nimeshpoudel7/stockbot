"""Fundamental scores, Finnhub-backed, SQLite-cached (refresh at most daily).

- Piotroski F-Score (0-9): the 9 tests from the guide.
- Altman Z family: the original Z (market value of equity + sales terms)
  for manufacturing industries; Z'' (book equity, no sales term) for other
  non-financials; not valid for financials.
- Simplified FCF-based DCF fair value (5yr projection + terminal value),
  FCF = operating cash flow - capex from the cash-flow statement; skipped
  for financials and when FCF is missing or <= 0.

Free-tier budget: TWO Finnhub calls per symbol per refresh — the
``/stock/financials-reported`` annual response carries 2+ fiscal years
(everything Piotroski/Altman/DCF need from statements), and
``/stock/profile2`` supplies the industry, current shares outstanding and
market cap the statements can't. Results live in the
``fundamentals_cache`` table with a 24h TTL.

Missing fields never crash: any score that can't be computed comes back None
with the reason recorded in ``notes``.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time

import requests

log = logging.getLogger("stockbot.fundamentals")

TTL_SEC = 24 * 3600
API = "https://finnhub.io/api/v1/stock/financials-reported"


# ---------------------------------------------------------------------------
# label matching: Finnhub returns human-readable labels ("Total assets"),
# not taxonomy codes, so we match on keyword sets per concept.
# ---------------------------------------------------------------------------
def _find(section: list[dict], *needles: str, exclude: tuple[str, ...] = ()) -> float | None:
    """First label containing ALL needles (and none of exclude); value->float.

    Exact label matches win over substring matches, so e.g. "Total
    liabilities" is preferred to "Total liabilities and shareholders' equity".
    """
    def ok(label: str) -> bool:
        return (all(n.lower() in label for n in needles)
                and not any(x.lower() in label for x in exclude))

    needles_l = [n.lower() for n in needles]
    for row in section:  # pass 1: exact label match
        label = str(row.get("label", "")).strip().lower()
        if label and label in needles_l and \
                not any(x.lower() in label for x in exclude):
            return _num(row.get("value"))
    for row in section:  # pass 2: substring match
        if ok(str(row.get("label", "")).lower()):
            return _num(row.get("value"))
    return None


def _first_not_none(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _concepts(section: list[dict]) -> dict[str, float]:
    """{stripped XBRL concept: value} — 'us-gaap_Assets' -> 'Assets'.

    Finnhub rows carry both a taxonomy ``concept`` and a human ``label``;
    concepts are the stable identifier, so statement extraction prefers
    them and falls back to label matching. First row wins per concept.
    """
    out: dict[str, float] = {}
    for row in section:
        concept = str(row.get("concept") or "").split("_", 1)[-1]
        v = _num(row.get("value"))
        if concept and v is not None:
            out.setdefault(concept, v)
    return out


def _pick(mapping: dict, *names: str) -> float | None:
    for n in names:
        if n in mapping:
            return mapping[n]
    return None


# Finnhub profile2 `finnhubIndustry` values where the FCF-DCF and Altman
# models don't apply (banks/insurers hold financial assets as inventory),
# and where the original Altman Z was calibrated (manufacturers).
FINANCIAL_INDUSTRIES = {"Banking", "Insurance", "Financial Services",
                        "Real Estate"}
MANUFACTURING_INDUSTRIES = {"Machinery", "Automobiles",
                            "Aerospace & Defense", "Chemicals",
                            "Metals & Mining", "Electrical Equipment",
                            "Semiconductors", "Building"}


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _lt_debt(bs: list[dict]) -> float | None:
    """Long-term debt: the 'Term debt' row listed AFTER 'Total current
    liabilities' (i.e. the non-current one). Fallbacks: a 'non-current
    debt' row, total non-current liabilities, then total liabilities."""
    seen_current_total = False
    for row in bs:
        label = str(row.get("label", "")).lower()
        if "total current liabilities" in label:
            seen_current_total = True
        elif seen_current_total and label.strip() == "term debt":
            return _num(row.get("value"))
    v = _first_not_none(_find(bs, "noncurrent debt"),
                        _find(bs, "non-current debt",
                              exclude=("liabilities",)))
    if v is not None:
        return v
    v = _find(bs, "total non-current liabilities")
    if v is not None:
        return v
    return _find(bs, "total liabilities")


def extract_annual(report: dict) -> dict:
    """Pull the fields the scores need from one annual report dict.

    XBRL concepts are tried first (stable across filers), human-label
    keyword matching is the fallback. Total liabilities / equity are
    derived from each other when a 10-K prints only one of them.
    """
    bs = report["report"].get("bs", [])
    ic = report["report"].get("ic", [])
    cf = report["report"].get("cf", [])
    bc, icc, cfc = _concepts(bs), _concepts(ic), _concepts(cf)
    revenue = _first_not_none(
        _pick(icc, "RevenueFromContractWithCustomerExcludingAssessedTax",
              "RevenueFromContractWithCustomerIncludingAssessedTax",
              "Revenues", "SalesRevenueNet"),
        _find(ic, "net sales"),
        _find(ic, "total revenue"),
        _find(ic, "revenue", exclude=("deferred",)))
    gross = _first_not_none(_pick(icc, "GrossProfit"),
                            _find(ic, "gross margin"),
                            _find(ic, "gross profit"))
    ebit = _first_not_none(_pick(icc, "OperatingIncomeLoss"),
                           _find(ic, "operating income"),
                           _find(ic, "income before provision"))
    net = _first_not_none(_pick(icc, "NetIncomeLoss"),
                          _find(ic, "net income", exclude=("comprehensive",)))
    ca = _first_not_none(_pick(bc, "AssetsCurrent"),
                         _find(bs, "total current assets"))
    cl = _first_not_none(_pick(bc, "LiabilitiesCurrent"),
                         _find(bs, "total current liabilities"))
    ta = _first_not_none(_pick(bc, "Assets"), _find(bs, "total assets"))
    tl = _first_not_none(_pick(bc, "Liabilities"),
                         _find(bs, "total liabilities"))
    equity = _first_not_none(
        _pick(bc, "StockholdersEquity",
              "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        _find(bs, "stockholders", "equity", exclude=("liabilities and",)))
    if tl is None and ta is not None and equity is not None:
        tl = ta - equity  # many 10-Ks never print a liabilities total
    if equity is None and ta is not None and tl is not None:
        equity = ta - tl
    retained = _first_not_none(
        _pick(bc, "RetainedEarningsAccumulatedDeficit"),
        _find(bs, "accumulated deficit"),
        _find(bs, "retained earnings"))
    cash = _first_not_none(_pick(bc, "CashAndCashEquivalentsAtCarryingValue"),
                           _find(bs, "cash and cash equivalents"))
    ocf = _first_not_none(
        _pick(cfc, "NetCashProvidedByUsedInOperatingActivities",
              "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        _find(cf, "cash generated by operating activities"),
        _find(cf, "net cash provided by operating activities"),
        _find(cf, "operating activities", exclude=("generated", "used")))
    capex = _first_not_none(
        _pick(cfc, "PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"),
        _find(cf, "capital expenditures"),
        _find(cf, "payments for acquisition of property"))
    shares = _first_not_none(
        _pick(icc, "WeightedAverageNumberOfDilutedSharesOutstanding",
              "WeightedAverageNumberOfSharesOutstandingBasic"),
        _find(bs, "shares outstanding", exclude=("issued",)),
        _find(ic, "diluted", "(in shares)"))
    lt_debt = _first_not_none(
        _pick(bc, "LongTermDebtNoncurrent", "LongTermDebt"),
        _lt_debt(bs))
    fcf = None
    if ocf is not None and capex is not None:
        fcf = ocf - abs(capex)  # capex is a payment; its sign varies by filer
    return {
        "year": report.get("year"),
        "end_date": report.get("endDate"),
        "revenue": revenue, "gross_profit": gross, "ebit": ebit,
        "net_income": net, "current_assets": ca, "current_liab": cl,
        "total_assets": ta, "total_liab": tl, "lt_debt": lt_debt,
        "equity": equity, "retained": retained, "cash": cash,
        "ocf": ocf, "capex": capex, "fcf": fcf, "shares": shares,
    }


# ---------------------------------------------------------------------------
# scores (pure functions over extracted annuals)
# ---------------------------------------------------------------------------
PIOTROSKI_TESTS = (
    "net_income_positive", "ocf_positive", "roa_up", "ocf_gt_net_income",
    "leverage_down", "current_ratio_up", "no_dilution",
    "gross_margin_up", "asset_turnover_up",
)


def piotroski(cur: dict, prev: dict) -> dict:
    """Each test -> True/False/None; score counts True over evaluable tests."""
    t: dict[str, bool | None] = {}
    prev = {k: prev.get(k) for k in cur}  # missing prior year -> all None

    def num(v):
        return v if isinstance(v, (int, float)) else None

    ni, ocf = num(cur["net_income"]), num(cur["ocf"])
    ta, ta_p = num(cur["total_assets"]), num(prev["total_assets"])
    t["net_income_positive"] = (ni > 0) if ni is not None else None
    t["ocf_positive"] = (ocf > 0) if ocf is not None else None
    roa = ni / ta if ni is not None and ta else None
    ni_p, ta_pp = num(prev["net_income"]), num(prev["total_assets"])
    roa_p = ni_p / ta_p if ni_p is not None and ta_p else None
    t["roa_up"] = (roa > roa_p) if roa is not None and roa_p is not None else None
    t["ocf_gt_net_income"] = (ocf > ni) if ocf is not None and ni is not None else None

    ltd, ltd_p = num(cur["lt_debt"]), num(prev["lt_debt"])
    lev = ltd / ta if ltd is not None and ta else None
    lev_p = ltd_p / ta_p if ltd_p is not None and ta_p else None
    t["leverage_down"] = (lev < lev_p) if lev is not None and lev_p is not None else None

    ca, cl = num(cur["current_assets"]), num(cur["current_liab"])
    ca_p, cl_p = num(prev["current_assets"]), num(prev["current_liab"])
    cr = ca / cl if ca is not None and cl else None
    cr_p = ca_p / cl_p if ca_p is not None and cl_p else None
    t["current_ratio_up"] = (cr > cr_p) if cr is not None and cr_p is not None else None

    sh, sh_p = num(cur["shares"]), num(prev["shares"])
    t["no_dilution"] = (sh <= sh_p) if sh is not None and sh_p else None

    rev, rev_p = num(cur["revenue"]), num(prev["revenue"])
    gp, gp_p = num(cur["gross_profit"]), num(prev["gross_profit"])
    gm = gp / rev if gp is not None and rev else None
    gm_p = gp_p / rev_p if gp_p is not None and rev_p else None
    t["gross_margin_up"] = (gm > gm_p) if gm is not None and gm_p is not None else None

    at = rev / ta if rev is not None and ta else None
    at_p = rev_p / ta_p if rev_p is not None and ta_p else None
    t["asset_turnover_up"] = (at > at_p) if at is not None and at_p is not None else None

    score = sum(1 for v in t.values() if v is True)
    evaluated = sum(1 for v in t.values() if v is not None)
    return {"score": score if evaluated else None, "of": 9,
            "evaluated": evaluated, "evaluable": evaluated, "tests": t}


def altman_z(cur: dict, price: float | None = None,
             market_cap: float | None = None,
             industry: str | None = None) -> dict | None:
    """Altman bankruptcy-risk score, model chosen by industry.

    Original Z (market value of equity + sales/turnover terms) for
    manufacturing industries — the population it was calibrated on.
    Z'' (book equity instead of market value, no sales term) for every
    other non-financial. Financials: not valid at all. ``industry=None``
    (profile unavailable) keeps the legacy original-Z behaviour.

    Zones — Z: safe >= 2.99, distress < 1.81. Z'': safe > 2.6,
    distress < 1.1. Zone strings are "safe"/"grey"/"distress" either way.

    Returns None when the chosen model's inputs are missing; a dict with
    z/zone None plus a ``reason`` when the model itself doesn't apply.
    """
    if industry in FINANCIAL_INDUSTRIES:
        return {"z": None, "zone": None, "model": None,
                "reason": "Altman not valid for financials"}
    ta = cur.get("total_assets")
    if not ta:
        return None
    ca, cl = cur.get("current_assets"), cur.get("current_liab")
    wc = (ca - cl) if ca is not None and cl is not None else None
    retained = cur.get("retained")
    ebit = cur.get("ebit")
    tl = cur.get("total_liab")
    equity = cur.get("equity")
    if equity is None and tl is not None:
        equity = ta - tl
    if tl is None and equity is not None:
        tl = ta - equity
    x1 = wc / ta if wc is not None else None
    x2 = retained / ta if retained is not None else None
    x3 = ebit / ta if ebit is not None else None

    if industry is None or industry in MANUFACTURING_INDUSTRIES:
        rev = cur.get("revenue")
        mcap = market_cap
        if mcap is None and price and cur.get("shares"):
            mcap = price * cur["shares"]
        if x1 is None or x2 is None or x3 is None or not tl or not rev \
                or not mcap:
            return None
        d = mcap / tl
        e = rev / ta
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * d + 1.0 * e
        zone = ("distress" if z < 1.81 else "grey" if z < 2.99 else "safe")
        return {"z": z, "zone": zone, "model": "Z",
                "parts": {"A": x1, "B": x2, "C": x3, "D": d, "E": e}}

    # Z'' — non-manufacturing: book equity / liabilities, no sales term.
    if x1 is None or x2 is None or x3 is None or not tl or equity is None:
        return None
    d = equity / tl
    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * d
    zone = ("safe" if z > 2.6 else "distress" if z < 1.1 else "grey")
    return {"z": z, "zone": zone, "model": "Z''",
            "parts": {"A": x1, "B": x2, "C": x3, "D": d}}


def dcf_fair_value(cur: dict, prev: dict | None,
                   wacc: float = 0.10, growth: float = 0.10,
                   terminal_growth: float = 0.03) -> dict | None:
    """Simplified FCF DCF: 5yr projection + Gordon terminal value.

    equity value = PV(FCF_1..5) + PV(terminal) - debt + cash,
    per share = equity / shares. None when FCF/shares are unusable.
    """
    fcf = cur["fcf"]
    if (fcf is None or fcf <= 0) and prev:
        pf = prev["fcf"]
        if fcf is None:
            fcf = pf
        elif pf and pf > 0:
            fcf = (fcf + pf) / 2
    if fcf is None or fcf <= 0:
        return None
    shares = cur["shares"]
    if not shares:
        return None
    if wacc <= terminal_growth:
        return None
    pv = 0.0
    f = fcf
    for yr in range(1, 6):
        f *= (1 + growth)
        pv += f / (1 + wacc) ** yr
    terminal = f * (1 + terminal_growth) / (wacc - terminal_growth)
    pv += terminal / (1 + wacc) ** 5
    # Enterprise -> equity: subtract DEBT, add cash. Subtracting total
    # liabilities also removed payables, deferred revenue, leases etc.,
    # which the operating cash flow already accounts for. Falls back to
    # total liabilities only when no debt figure was extracted.
    debt = cur.get("lt_debt")
    if debt is None:
        debt = cur.get("total_liab") or 0
    equity = pv - debt + (cur.get("cash") or 0)
    return {"fair_value": equity / shares, "wacc": wacc, "growth": growth,
            "terminal_growth": terminal_growth, "base_fcf": fcf}


def dcf_block(cur: dict, prev: dict | None, industry: str | None = None,
              wacc: float = 0.10, growth: float = 0.10) -> dict:
    """DCF gate + valuation. Always returns a dict: on a skip, fair_value
    is None and ``reason`` says why (financials, no FCF, FCF <= 0, or no
    share count) so callers show the reason instead of a bare 'n/a'.

    ``base_fcf`` is the free cash flow the model would run on (current
    year's, else prior year's) — scanner-side guards key off it.
    """
    fcf = cur.get("fcf")
    if fcf is None and prev:
        fcf = prev.get("fcf")
    out = {"fair_value": None, "base_fcf": fcf, "wacc": wacc,
           "growth": growth, "terminal_growth": 0.03, "reason": None}
    if industry in FINANCIAL_INDUSTRIES:
        out["reason"] = "DCF skipped for financials"
        return out
    if fcf is None:
        out["reason"] = ("DCF: no usable FCF (operating cash flow "
                         "less capex not reported)")
        return out
    if fcf <= 0:
        out["reason"] = "DCF: negative FCF — not meaningful"
        return out
    eff = dict(cur)
    eff["fcf"] = fcf  # pin the base year so no re-averaging happens inside
    d = dcf_fair_value(eff, None, wacc=wacc, growth=growth)
    if d is None:
        out["reason"] = "DCF: no usable share count"
        return out
    d["reason"] = None
    return d


# ---------------------------------------------------------------------------
# fetch + cache
# ---------------------------------------------------------------------------
PROFILE_API = "https://finnhub.io/api/v1/stock/profile2"


def fetch_annuals(symbol: str) -> list[dict]:
    key = os.environ.get("FINNHUB_KEY", "")
    if not key:
        raise RuntimeError("FINNHUB_KEY not set")
    r = requests.get(API, params={"symbol": symbol.upper(), "freq": "annual",
                                  "token": key}, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", [])
    annuals = [d for d in data if d.get("form") in ("10-K", "20-F", "40-F")]
    # Latest fiscal period first. Sort by period END date, not Finnhub's
    # "year" (the year the period STARTED — two different 10-Ks can share
    # it, e.g. Salesforce FY2025 and FY2026 both carry year=2025).
    annuals.sort(key=lambda d: d.get("endDate")
                 or f"{d.get('year', 0)}-00-00", reverse=True)
    return annuals


def fetch_profile(symbol: str) -> dict:
    """profile2: industry, shares outstanding, market cap. Best-effort —
    {} on any failure; statement-derived fallbacks take over."""
    key = os.environ.get("FINNHUB_KEY", "")
    if not key:
        return {}
    try:
        r = requests.get(PROFILE_API,
                         params={"symbol": symbol.upper(), "token": key},
                         timeout=30)
        r.raise_for_status()
        return r.json() or {}
    except Exception as e:  # noqa: BLE001 - profile is advisory only
        log.warning("profile fetch failed for %s: %s", symbol, e)
        return {}


def get_scores(store, symbol: str, price: float | None = None,
               force: bool = False, wacc: float = 0.10,
               growth: float = 0.10) -> dict:
    """Cached scores dict. At most one Finnhub call per symbol per 24h."""
    symbol = symbol.upper()
    notes: list[str] = []
    cached = None if force else store.fund_cache_get(symbol)
    if cached:
        age = time.time() - _ts(cached["fetched_at"])
        if age < TTL_SEC:
            return cached["data"]
        notes.append("cache older than 24h; refreshing")

    annuals = fetch_annuals(symbol)
    if len(annuals) < 1:
        result = {"symbol": symbol, "error": "no annual statements found",
                  "fetched_at": _now(), "notes": notes}
        return result
    cur = extract_annual(annuals[0])
    prev = extract_annual(annuals[1]) if len(annuals) > 1 else None

    # Profile supplies what statements can't: industry (model selection),
    # CURRENT shares outstanding (statements carry period-weighted ones)
    # and market cap (Altman's market-value term, no price×shares proxy).
    # Piotroski's dilution test compares like with like: statement
    # (weighted-average) shares this year vs last year. Run it BEFORE the
    # profile's current share count replaces cur["shares"] below.
    pio = piotroski(cur, prev or {})

    profile = fetch_profile(symbol)
    industry = profile.get("finnhubIndustry")
    shares = None
    so = _num(profile.get("shareOutstanding"))  # reported in millions
    if so:
        shares = so * 1e6
    if not shares:
        shares = cur.get("shares")
    market_cap = None
    mc = _num(profile.get("marketCapitalization"))  # reported in $ millions
    if mc:
        market_cap = mc * 1e6
    if market_cap is None and price and shares:
        market_cap = price * shares
    if not shares and market_cap and price:
        shares = market_cap / price
    if shares:
        cur = dict(cur, shares=shares)

    if pio["score"] is None:
        notes.append("Piotroski: insufficient data")
    elif pio["evaluated"] < 9:
        notes.append(f"Piotroski: {pio['evaluated']}/9 tests evaluable")

    alt = altman_z(cur, price=price, market_cap=market_cap,
                   industry=industry)
    if alt is None:
        notes.append("Altman Z: missing fields (need WC, retained "
                     "earnings, EBIT, liabilities, equity/revenue, "
                     "market cap)")
    elif alt.get("reason"):
        notes.append(f"Altman: {alt['reason']}")

    dcf = dcf_block(cur, prev, industry=industry, wacc=wacc, growth=growth)
    if dcf.get("reason"):
        notes.append(dcf["reason"])

    # Label by the fiscal period END (Salesforce's "2025" 10-K covers the
    # year ended Jan 31, 2026 — its FY2026), not the period-start year.
    end = str(cur.get("end_date") or "")
    data_year = (int(end[:4]) if len(end) >= 4 and end[:4].isdigit()
                 else cur.get("year"))

    result = {
        "symbol": symbol,
        "price": price,
        "data_year": data_year,
        "period_end": end[:10] or None,
        "industry": industry,
        "shares_outstanding": shares,
        "market_cap": market_cap,
        "fetched_at": _now(),
        "piotroski": pio,
        "altman": alt,
        "dcf": dcf,
        "notes": notes,
    }
    store.fund_cache_set(symbol, result)
    log.info("%s scores: Piotroski %s/9, Altman %s, DCF %s", symbol,
             pio["score"], f"{alt['z']:.2f}" if alt and alt.get("z") else None,
             f"{dcf['fair_value']:.2f}" if dcf.get("fair_value") else None)
    return result


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts(s: str) -> float:
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        try:
            return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            return 0.0
