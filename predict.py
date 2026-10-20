"""EXPERIMENTAL prediction layer (guide p13-15).

HistGradientBoostingClassifier on technical features:
  RSI(14), MACD histogram, Bollinger %B, volume z-score, ATR%,
  5-day and 20-day returns, distance from SMA20.

Label: 1 if close is up 5 sessions ahead, else 0.
Validation: walk-forward — train on the past 252 sessions, test on the next
21, roll forward. The reported hit rate is the walk-forward hit rate, so
Nimesh sees exactly how (un)reliable it is.

This is an experimental signal, NEVER advice. Predictions are shown on the
dashboard with the hit rate, never pushed via ntfy.

Usage:
  python -m predict SYMBOL        # train + walk-forward, print summary
  python -m predict SYMBOL --predict   # also print P(up next 5 sessions)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

log = logging.getLogger("stockbot.predict")

FEATURES = ["rsi", "macd_hist", "pctb", "vol_z", "atr_pct",
            "ret_5d", "ret_20d", "dist_sma20"]

TRAIN_WINDOW = 252   # ~1 trading year
TEST_WINDOW = 21     # ~1 trading month


def build_features(bars: list[dict]) -> list[dict]:
    """Per-bar feature dicts (None where not warmed up). bars oldest-first."""
    import indicators as ind
    closes = [b["c"] for b in bars]
    vols = [b.get("v", 0) for b in bars]
    rsi = ind.rsi_wilder(closes, 14)
    _, _, hist = ind.macd_series(closes)
    out = []
    for i, b in enumerate(bars):
        mid, upper, lower = ind.bollinger_bands(closes[:i + 1])
        row = {
            "rsi": rsi[i],
            "macd_hist": hist[i],
            "pctb": ind.bollinger_pctb(closes[i], upper, lower)
            if upper is not None else None,
            "vol_z": ind.volume_zscore(vols[i], vols[:i]),
            "atr_pct": ind.atr_pct(bars[:i + 1]),
            "ret_5d": (closes[i] / closes[i - 5] - 1) if i >= 5 and closes[i - 5] else None,
            "ret_20d": (closes[i] / closes[i - 20] - 1) if i >= 20 and closes[i - 20] else None,
            "dist_sma20": ((closes[i] / s - 1) if (s := ind.sma(closes[:i + 1], 20)) else None),
        }
        out.append(row)
    return out


def latest_features(bars: list[dict]) -> list[float] | None:
    """Feature vector for the most recent bar (None if not warmed up)."""
    if not bars:
        return None
    row = build_features(bars)[-1]
    vals = [row[f] for f in FEATURES]
    if any(v is None for v in vals):
        return None
    return [float(v) for v in vals]


def dataset(bars: list[dict]) -> tuple[list[list[float]], list[int]]:
    """Feature matrix X and labels y (up-move over next 5 sessions).

    Rows with any missing feature or without a 5-session-ahead label are
    dropped. Returns (X, y) aligned.
    """
    feats = build_features(bars)
    closes = [b["c"] for b in bars]
    X, y = [], []
    n = len(bars)
    for i in range(n - 5):
        row = feats[i]
        vals = [row[f] for f in FEATURES]
        if any(v is None for v in vals):
            continue
        if not closes[i] or not closes[i + 5]:
            continue
        X.append([float(v) for v in vals])
        y.append(1 if closes[i + 5] > closes[i] else 0)
    return X, y


HORIZON = 5  # label looks this many sessions ahead


def walk_forward_splits(n: int, train: int = TRAIN_WINDOW,
                        test: int = TEST_WINDOW, gap: int = HORIZON):
    """Yield (train_idx, test_idx) rolling windows over n rows.

    ``gap`` rows are skipped between train and test: the last training
    labels look HORIZON sessions ahead, i.e. into the test window, which
    leaked future prices and inflated the hit rate.
    """
    start = 0
    while True:
        tr0, tr1 = start, start + train
        te0, te1 = tr1 + gap, tr1 + gap + test
        if te1 > n:
            break
        yield list(range(tr0, tr1)), list(range(te0, te1))
        start += test


def walk_forward(X: list[list[float]], y: list[int],
                 train: int = TRAIN_WINDOW,
                 test: int = TEST_WINDOW) -> dict:
    """Train HistGradientBoostingClassifier per split; return hit stats."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    hits = total = 0
    splits = 0
    proba_sum = 0.0
    for tr_idx, te_idx in walk_forward_splits(len(X), train, test):
        clf = HistGradientBoostingClassifier(max_iter=200, random_state=7)
        clf.fit([X[i] for i in tr_idx], [y[i] for i in tr_idx])
        preds = clf.predict([X[i] for i in te_idx])
        probas = clf.predict_proba([X[i] for i in te_idx])
        for j, i in enumerate(te_idx):
            total += 1
            proba_sum += probas[j][1]
            if preds[j] == y[i]:
                hits += 1
        splits += 1
    return {"splits": splits, "tested": total, "hits": hits,
            "hit_rate": hits / total if total else None,
            "avg_prob_up": proba_sum / total if total else None}


def train_final(X: list[list[float]], y: list[int]):
    from sklearn.ensemble import HistGradientBoostingClassifier
    clf = HistGradientBoostingClassifier(max_iter=200, random_state=7)
    clf.fit(X, y)
    return clf


MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "models")


def model_path(symbol: str) -> str:
    os.makedirs(MODEL_DIR, exist_ok=True)
    return os.path.join(MODEL_DIR, f"{symbol.upper()}.json")


def save_model(symbol: str, payload: dict) -> str:
    path = model_path(symbol)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def load_model(symbol: str) -> dict | None:
    path = os.path.join(MODEL_DIR, f"{symbol.upper()}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def train_symbol(symbol: str, daily_bars_fn, min_rows: int = 300) -> dict:
    """Fetch 2y daily bars, walk-forward validate, save final model."""
    bars = daily_bars_fn(symbol, 500)
    X, y = dataset(bars)
    if len(X) < min_rows:
        raise ValueError(f"only {len(X)} usable rows for {symbol} "
                         f"(need {min_rows})")
    wf = walk_forward(X, y)
    clf = train_final(X, y)
    # probability for the LATEST bar. X only holds labelled rows, so X[-1]
    # is the bar 6 sessions ago; score today's features instead.
    latest = latest_features(bars)
    proba_up = (float(clf.predict_proba([latest])[0][1])
                if latest is not None else None)
    payload = {
        "symbol": symbol.upper(),
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "features": FEATURES,
        "rows": len(X),
        "walk_forward": wf,
        "latest_proba_up": proba_up,
        "note": ("EXPERIMENTAL signal only — walk-forward hit rate "
                 f"{wf['hit_rate']:.1%}" if wf["hit_rate"] is not None
                 else "EXPERIMENTAL signal only — no validation data"),
    }
    save_model(symbol, payload)
    log.info("%s model trained: %d rows, walk-forward hit rate %s",
             symbol, len(X), payload["note"])
    return payload


def get_daily_bars_alpaca(symbol: str, days: int) -> list[dict]:
    """2y of daily bars via Alpaca (used by CLI)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".env"))
    from feed import AlpacaFeed
    feed = AlpacaFeed(os.environ.get("ALPACA_KEY", ""),
                      os.environ.get("ALPACA_SECRET", ""))
    return feed.get_daily_bars(symbol, days)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="EXPERIMENTAL price-move model")
    ap.add_argument("symbol", help="e.g. AAPL")
    args = ap.parse_args()
    print("EXPERIMENTAL — not investment advice.")
    payload = train_symbol(args.symbol.upper(), get_daily_bars_alpaca)
    wf = payload["walk_forward"]
    print(f"symbol:      {payload['symbol']}")
    print(f"rows:        {payload['rows']}")
    print(f"walk-forward splits: {wf['splits']} "
          f"({wf['tested']} test months)")
    hr = wf["hit_rate"]
    print(f"hit rate:    {hr:.1%}" if hr is not None else "hit rate:    n/a")
    p = payload["latest_proba_up"]
    print("P(up 5 sessions, latest bar): "
          + (f"{p:.1%}" if p is not None else "n/a (latest bar not warmed up)"))
    print("This is an experimental signal with the hit rate shown above; "
          "it is not a recommendation.")


if __name__ == "__main__":
    main()
