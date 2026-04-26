"""
MY STRATEGIES — Personal Lab Backtest Runner
=============================================
Separate from the main pipeline.
Add your own strategies to strategies/lab/ and test here.

Usage: python backtest/run_my_strategies.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import ccxt
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller
from backtest.engine import BacktestEngine
from strategies.lab.zscore_reversion import ZScoreReversionStrategy
from utils.logger import get_logger

logger = get_logger("my_strategies")

# ── Add your strategies here ──────────────────────────────────────
MY_STRATEGY_MAP = {
    "zscore_reversion": ZScoreReversionStrategy,
    # "my_next_idea": MyNextStrategy,
}
# ─────────────────────────────────────────────────────────────────


def fetch_data(symbol: str, timeframe: str, limit: int = 4380) -> pd.DataFrame:
    logger.info(f"Fetching {limit} candles of {symbol} {timeframe}...")
    exchange = ccxt.binance({"enableRateLimit": True})
    tf_ms    = exchange.parse_timeframe(timeframe) * 1000
    candles  = []
    end_ts   = exchange.milliseconds()

    while len(candles) < limit:
        batch_size = min(1000, limit - len(candles))
        since = end_ts - (batch_size * tf_ms)
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=batch_size)
        if not batch:
            break
        candles = batch + candles
        end_ts  = batch[0][0]
        if len(batch) < batch_size:
            break

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    logger.info(f"Fetched {len(df)} candles | {df.index[0]} to {df.index[-1]}")
    return df


def run():
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    symbol    = config["trading"]["symbol"]
    timeframe = config["trading"]["timeframe"]

    # Scanner confirmed DOGE = most mean-reverting (Hurst=0.316, ADF p=0.0006)
    symbol = "DOGE/USDT"
    df = fetch_data(symbol, timeframe, limit=4380)

    results = {}
    for name, StrategyClass in MY_STRATEGY_MAP.items():
        logger.info(f"\nTesting MY strategy: {name}")
        strategy = StrategyClass(config)
        engine   = BacktestEngine(strategy, config)
        results[name] = engine.run(df)

    print("\n" + "="*50)
    print("      MY STRATEGIES — RESULTS")
    print("="*50)
    for name, result in results.items():
        print(f"  {name:<22} | Return: {result.total_return_pct:+.2f}% | "
              f"WR: {result.win_rate:.1%} | "
              f"Trades: {result.total_trades} | "
              f"PF: {result.profit_factor:.2f} | "
              f"DD: {result.max_drawdown_pct:.2f}%")
    print("="*50)


if __name__ == "__main__":
    run()
