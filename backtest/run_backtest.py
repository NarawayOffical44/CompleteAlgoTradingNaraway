"""
Run a backtest on historical data.
Usage: python backtest/run_backtest.py
"""
import yaml
import ccxt
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
import statsmodels.api as sm
from backtest.engine import BacktestEngine
from strategies.ema_crossover import EMACrossoverStrategy
from strategies.mean_reversion import MeanReversionStrategy
from utils.logger import get_logger

logger = get_logger("run_backtest")

STRATEGY_MAP = {
    "ema_crossover": EMACrossoverStrategy,
    "mean_reversion": MeanReversionStrategy,
}


def fetch_historical_data(symbol: str, timeframe: str, limit: int = 4380) -> pd.DataFrame:
    """
    Fetch historical data from Binance with pagination.
    Binance caps at 1000 per request — fetches backwards in batches.
    4380 candles = ~6 months on 1h timeframe.
    """
    logger.info(f"Fetching {limit} candles of {symbol} {timeframe} from Binance...")
    exchange   = ccxt.binance({"enableRateLimit": True})
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    all_candles  = []

    end_ts = exchange.milliseconds()

    while len(all_candles) < limit:
        batch_size = min(1000, limit - len(all_candles))
        since      = end_ts - (batch_size * timeframe_ms)
        batch      = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=batch_size)
        if not batch:
            break
        all_candles = batch + all_candles
        end_ts = batch[0][0]
        if len(batch) < batch_size:
            break

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    logger.info(f"Fetched {len(df)} candles | {df.index[0]} to {df.index[-1]}")
    return df


def hurst_exponent(ts: np.ndarray) -> float:
    """
    Calculate Hurst exponent.
    H < 0.5 = mean-reverting
    H = 0.5 = random walk
    H > 0.5 = trending
    """
    lags = range(2, min(100, len(ts) // 2))
    tau = [np.std(np.subtract(ts[lag:], ts[:-lag])) for lag in lags]
    poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
    return poly[0]


def half_life(ts: pd.Series) -> float:
    """
    Calculate half-life of mean reversion (from Chan 2013).
    Shorter = faster reversion = better for mean reversion strategy.
    """
    delta = ts.diff().dropna()
    lag   = ts.shift(1).dropna()
    lag   = sm.add_constant(lag)
    model = OLS(delta, lag).fit()
    beta  = model.params.iloc[1]
    if beta >= 0:
        return float("inf")  # not mean-reverting
    return -np.log(2) / np.log(1 + beta)


def run_regime_analysis(df: pd.DataFrame, symbol: str):
    """
    Run ADF test + Hurst exponent + half-life.
    Tells us which strategy suits current market conditions.
    """
    prices = df["close"].values

    # ADF Test
    adf_result = adfuller(prices, autolag="AIC")
    adf_pvalue = adf_result[1]
    is_stationary = adf_pvalue < 0.05

    # Hurst Exponent
    H = hurst_exponent(prices)
    if H < 0.45:
        regime = "MEAN-REVERTING"
        recommended = "mean_reversion"
    elif H > 0.55:
        regime = "TRENDING"
        recommended = "ema_crossover"
    else:
        regime = "RANDOM WALK"
        recommended = "neither (wait)"

    # Half-life
    hl = half_life(df["close"])

    print("\n" + "="*50)
    print(f"  REGIME ANALYSIS — {symbol}")
    print("="*50)
    print(f"  ADF p-value    : {adf_pvalue:.4f}  {'(stationary = mean-reverting)' if is_stationary else '(non-stationary = trending)'}")
    print(f"  Hurst Exponent : {H:.3f}  (< 0.5 = mean-reverting | > 0.5 = trending)")
    print(f"  Regime         : {regime}")
    print(f"  Half-life      : {hl:.1f} candles  (how fast price reverts to mean)")
    print(f"  Recommended    : {recommended}")
    print("="*50)

    return {"adf_pvalue": adf_pvalue, "hurst": H, "half_life": hl, "regime": regime, "recommended": recommended}


def run():
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    symbol    = config["trading"]["symbol"]
    timeframe = config["trading"]["timeframe"]

    # Fetch 6 months of data
    df = fetch_historical_data(symbol, timeframe, limit=4380)

    # Save for offline use
    df.to_csv(f"data/historical/{symbol.replace('/', '_')}_{timeframe}.csv")

    # Regime analysis first — tells us which strategy to trust
    regime = run_regime_analysis(df, symbol)

    # Run both strategies and compare
    results = {}
    for name, StrategyClass in STRATEGY_MAP.items():
        logger.info(f"\nTesting strategy: {name}")
        strategy = StrategyClass(config)
        engine   = BacktestEngine(strategy, config)
        results[name] = engine.run(df)

    # Compare
    print("\n" + "="*50)
    print("         STRATEGY COMPARISON")
    print("="*50)
    for name, result in results.items():
        flag = " <-- REGIME MATCH" if name == regime["recommended"] else ""
        print(f"  {name:<20} | Return: {result.total_return_pct:+.2f}% | "
              f"WR: {result.win_rate:.1%} | "
              f"Trades: {result.total_trades} | "
              f"DD: {result.max_drawdown_pct:.2f}%"
              f"{flag}")
    print("="*50)

    best = max(results.items(), key=lambda x: x[1].total_return_pct)
    print(f"\n  Best strategy : {best[0]} ({best[1].total_return_pct:+.2f}%)")
    print(f"  Regime says   : {regime['recommended']}")
    print(f"\n  Set config.yaml strategy.active to the best one, then run main.py")


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    run()
