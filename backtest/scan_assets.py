"""
Asset Scanner — Find the best asset + strategy for current market conditions.
Runs regime analysis on multiple assets and ranks them.

Usage: python backtest/scan_assets.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import ccxt
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

ASSETS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "DOGE/USDT", "AVAX/USDT", "MATIC/USDT",
]

def fetch(exchange, symbol, timeframe="1h", limit=1000):
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df.astype(float)
    except Exception as e:
        print(f"  Failed to fetch {symbol}: {e}")
        return None

def hurst(ts):
    lags = range(2, min(100, len(ts)//2))
    tau  = [np.std(np.subtract(ts[lag:], ts[:-lag])) for lag in lags]
    poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
    return poly[0]

def analyse(df, symbol):
    prices = df["close"].values
    adf_p  = adfuller(prices, autolag="AIC")[1]
    H      = hurst(prices)
    trend  = ((prices[-1] - prices[0]) / prices[0]) * 100  # % change over period

    if H < 0.45:
        regime = "MEAN-REVERTING"
        best   = "mean_reversion"
        # Penalise heavy downtrends — spot-only bot can't short
        score  = 3 if trend > -5 else 1
    elif H > 0.55:
        regime = "TRENDING"
        best   = "ema_crossover"
        score  = 3 if trend > 0 else 1  # trending up = good, trending down = bad
    else:
        regime = "RANDOM WALK"
        best   = "wait"
        score  = 0

    return {
        "symbol" : symbol,
        "hurst"  : round(H, 3),
        "adf_p"  : round(adf_p, 4),
        "trend"  : round(trend, 1),
        "regime" : regime,
        "best"   : best,
        "score"  : score,
    }

def run():
    exchange = ccxt.binance({"enableRateLimit": True})
    print("\n" + "="*75)
    print("  ASSET SCANNER — Finding best asset + strategy for NOW")
    print("="*75)
    print(f"  {'Symbol':<12} {'Hurst':>6} {'ADF-p':>7} {'Trend%':>8} {'Regime':<16} {'Use'}")
    print("-"*75)

    results = []
    for symbol in ASSETS:
        df = fetch(exchange, symbol)
        if df is None:
            continue
        r = analyse(df, symbol)
        results.append(r)
        flag = " <-- DEPLOY" if r["score"] == 3 else ""
        print(f"  {r['symbol']:<12} {r['hurst']:>6} {r['adf_p']:>7} "
              f"{r['trend']:>7}% {r['regime']:<16} {r['best']}{flag}")

    print("="*75)

    best = [r for r in results if r["score"] == 3]
    # Sort: prefer positive trend, then flattest downtrend
    best.sort(key=lambda r: r["trend"], reverse=True)

    if best:
        print(f"\n  Best candidates:")
        for r in best:
            print(f"    {r['symbol']} -> {r['best']} (Hurst={r['hurst']}, Trend={r['trend']}%)")
        top = best[0]

        # ── Auto-update config.yaml ───────────────────────────────────
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "config", "config.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)

        prev_symbol   = config["trading"]["symbol"]
        prev_strategy = config["strategy"]["active"]

        config["trading"]["symbol"]   = top["symbol"]
        config["strategy"]["active"]  = top["best"]

        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        print(f"\n  config.yaml AUTO-UPDATED:")
        if prev_symbol != top["symbol"]:
            print(f"    symbol:   {prev_symbol} -> {top['symbol']}")
        else:
            print(f"    symbol:   {top['symbol']} (no change)")
        if prev_strategy != top["best"]:
            print(f"    strategy: {prev_strategy} -> {top['best']}")
        else:
            print(f"    strategy: {top['best']} (no change)")
    else:
        print("\n  No strong candidates right now. Market-wide random walk. Wait.")
        print("  config.yaml left unchanged.")
    print()

if __name__ == "__main__":
    run()
