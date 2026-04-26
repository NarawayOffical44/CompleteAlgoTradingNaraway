"""
AI Signal Trainer
------------------
Trains a RandomForest classifier on historical trade data.
Learns which market conditions at signal time lead to profitable trades.

Features used (what the model learns from):
  - zscore       : how far below mean price is
  - rsi          : momentum state at entry
  - vol_ratio    : volume spike strength
  - atr_pct      : volatility level (ATR as % of price)
  - drop_pct     : how far price dropped from mean
  - sma200_ratio : price relative to 200 SMA (trend strength)

Label: 1 = trade was profitable, 0 = trade was a loss

Usage:
  python ai/signal_trainer.py          — train on current backtest data
  from ai.signal_trainer import load_model, should_trade  — use in strategy
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pickle
import numpy as np
import pandas as pd
import yaml
import ccxt
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from backtest.engine import BacktestEngine, Trade
from strategies.lab.zscore_reversion import ZScoreReversionStrategy
from utils.logger import get_logger

logger = get_logger("ai_trainer")
MODEL_PATH = "models/zscore_signal_filter.pkl"


def extract_features(df: pd.DataFrame, signal_idx: int) -> dict:
    """Extract features from market state at signal time."""
    window = df.iloc[:signal_idx + 1]
    last   = window.iloc[-1]

    mean     = window["close"].rolling(100).mean().iloc[-1]
    std      = window["close"].rolling(100).std().iloc[-1]
    price    = last["close"]
    vol_avg  = window["volume"].rolling(20).mean().iloc[-1]

    return {
        "zscore"      : (price - mean) / std if std > 0 else 0,
        "rsi"         : last.get("rsi", 50),
        "vol_ratio"   : last["volume"] / vol_avg if vol_avg > 0 else 1,
        "atr_pct"     : last.get("atr", 0) / price if price > 0 else 0,
        "drop_pct"    : (mean - price) / mean if mean > 0 else 0,
        "sma200_ratio": price / last.get("sma200", price) if last.get("sma200", 0) > 0 else 1,
    }


def collect_training_data(df: pd.DataFrame, trades: list, config: dict) -> pd.DataFrame:
    """
    Build training dataset from backtest trades.
    Each row = features at entry + label (1=win, 0=loss).
    """
    rows = []
    strategy = ZScoreReversionStrategy(config)
    df_ind   = strategy.calculate_indicators(df)

    for trade in trades:
        # Find index of entry candle
        try:
            idx = df_ind.index.get_loc(trade.entry_time)
        except KeyError:
            continue

        features = extract_features(df_ind, idx)
        features["label"] = 1 if trade.pnl > 0 else 0
        features["pnl"]   = trade.pnl
        rows.append(features)

    return pd.DataFrame(rows)


def train(df: pd.DataFrame, config: dict):
    """Run backtest, collect trade data, train model, save to disk."""
    logger.info("Running backtest to collect training data...")
    strategy = ZScoreReversionStrategy(config)
    engine   = BacktestEngine(strategy, config)
    result   = engine.run(df)

    if len(result.trades) < 10:
        logger.warning(f"Only {len(result.trades)} trades — need 10+ to train. Run on more data.")
        return None

    dataset = collect_training_data(df, result.trades, config)
    logger.info(f"Training dataset: {len(dataset)} trades | "
                f"Wins: {dataset['label'].sum()} | Losses: {(dataset['label']==0).sum()}")

    X = dataset[["zscore", "rsi", "vol_ratio", "atr_pct", "drop_pct", "sma200_ratio"]]
    y = dataset["label"]

    if len(X) < 10:
        logger.warning("Not enough data to split train/test. Training on all data.")
        model = RandomForestClassifier(n_estimators=100, random_state=42, class_weight="balanced")
        model.fit(X, y)
    else:
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)
        model = RandomForestClassifier(n_estimators=100, random_state=42, class_weight="balanced")
        model.fit(X_train, y_train)

        print("\n" + "="*50)
        print("  AI MODEL — TRAINING RESULTS")
        print("="*50)
        print(classification_report(y_test, model.predict(X_test),
                                    target_names=["Loss", "Win"]))

        # Feature importance
        importance = pd.Series(model.feature_importances_, index=X.columns).sort_values(ascending=False)
        print("  Feature Importance (what matters most):")
        for feat, imp in importance.items():
            bar = "█" * int(imp * 40)
            print(f"    {feat:<15} {imp:.3f} {bar}")
        print("="*50)

    os.makedirs("models", exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    logger.info(f"Model saved to {MODEL_PATH}")
    return model


def load_model():
    """Load trained model from disk."""
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def should_trade(features: dict, threshold: float = 0.60) -> bool:
    """
    Use trained model to decide if signal is worth taking.
    Returns True only if model confidence >= threshold.
    threshold=0.60 means model must be 60%+ confident it's a winner.
    """
    model = load_model()
    if model is None:
        return True  # no model trained yet — allow all signals

    X = pd.DataFrame([{
        "zscore"      : features.get("zscore", 0),
        "rsi"         : features.get("rsi", 50),
        "vol_ratio"   : features.get("vol_ratio", 1),
        "atr_pct"     : features.get("atr_pct", 0),
        "drop_pct"    : features.get("drop_pct", 0),
        "sma200_ratio": features.get("sma200_ratio", 1),
    }])
    proba = model.predict_proba(X)[0][1]  # probability of win
    logger.info(f"AI confidence: {proba:.1%} | {'TRADE' if proba >= threshold else 'SKIP'}")
    return proba >= threshold


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    exchange = ccxt.binance({"enableRateLimit": True})
    symbol   = config["trading"]["symbol"]
    tf       = config["trading"]["timeframe"]
    tf_ms    = exchange.parse_timeframe(tf) * 1000
    limit    = 4380
    candles  = []
    end_ts   = exchange.milliseconds()

    while len(candles) < limit:
        batch = min(1000, limit - len(candles))
        since = end_ts - (batch * tf_ms)
        c     = exchange.fetch_ohlcv(symbol, tf, since=since, limit=batch)
        if not c: break
        candles = c + candles
        end_ts  = c[0][0]
        if len(c) < batch: break

    df = pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="first")].sort_index()

    train(df, config)
