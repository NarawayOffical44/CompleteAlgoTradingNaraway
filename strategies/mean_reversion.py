"""
Bollinger Band Mean Reversion + RSI + Trend Filter Strategy
------------------------------------------------------------
Logic:
  BUY  when price touches LOWER BB AND RSI oversold AND price ABOVE 200 SMA
  SELL when price touches UPPER BB AND RSI overbought AND price BELOW 200 SMA

Trend filter (200 SMA) prevents "catching a falling knife":
  - In downtrend (price < 200 SMA): skip BUY signals, only SELL
  - In uptrend  (price > 200 SMA): skip SELL signals, only BUY

Stop loss  : entry - (ATR * multiplier)
Take profit: middle band (mean reversion target)
"""
import pandas as pd
import numpy as np
import pandas_ta as ta
from strategies.base import BaseStrategy, TradeSignal, Signal
from utils.logger import get_logger

logger = get_logger("mean_reversion")


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "mean_reversion"
        cfg = config["mean_reversion"]
        self.bb_period = cfg["bb_period"]
        self.bb_std = cfg["bb_std"]
        self.rsi_period = cfg["rsi_period"]
        self.rsi_oversold = cfg["rsi_oversold"]
        self.rsi_overbought = cfg["rsi_overbought"]
        self.atr_period = cfg["atr_period"]
        self.atr_multiplier = cfg["atr_multiplier"]
        self.tp_ratio = cfg["take_profit_ratio"]

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        bb = ta.bbands(df["close"], length=self.bb_period, std=self.bb_std)
        df["bb_upper"]  = bb[[c for c in bb.columns if c.startswith("BBU")][0]]
        df["bb_middle"] = bb[[c for c in bb.columns if c.startswith("BBM")][0]]
        df["bb_lower"]  = bb[[c for c in bb.columns if c.startswith("BBL")][0]]
        df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]
        df["rsi"]       = ta.rsi(df["close"], length=self.rsi_period)
        df["atr"]       = ta.atr(df["high"], df["low"], df["close"], length=self.atr_period)
        df["sma200"]    = ta.sma(df["close"], length=200)  # trend filter
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        df = self.calculate_indicators(df)
        last  = df.iloc[-1]
        entry = last["close"]
        atr   = last["atr"]

        sma200 = last["sma200"]
        if pd.isna(sma200):
            return TradeSignal(Signal.HOLD, entry, 0, 0, "Waiting for SMA200")
        in_uptrend   = last["close"] > sma200
        in_downtrend = last["close"] < sma200
        trend_label  = "UPTREND" if in_uptrend else "DOWNTREND"

        # BUY: lower BB + oversold RSI + price above 200 SMA (uptrend/range only)
        if last["close"] <= last["bb_lower"] and last["rsi"] < self.rsi_oversold and in_uptrend:
            stop_loss   = entry - (atr * self.atr_multiplier)
            take_profit = last["bb_middle"]
            reason = (f"Price at lower BB ({last['bb_lower']:.2f}) | "
                      f"RSI={last['rsi']:.1f} | {trend_label} | SMA200={last['sma200']:.0f}")
            logger.info(f"[green]BUY signal[/green] | {reason}")
            return TradeSignal(Signal.BUY, entry, stop_loss, take_profit, reason)

        # SELL: upper BB + overbought RSI + price below 200 SMA (downtrend/range only)
        if last["close"] >= last["bb_upper"] and last["rsi"] > self.rsi_overbought and in_downtrend:
            stop_loss   = entry + (atr * self.atr_multiplier)
            take_profit = last["bb_middle"]
            reason = (f"Price at upper BB ({last['bb_upper']:.2f}) | "
                      f"RSI={last['rsi']:.1f} | {trend_label} | SMA200={last['sma200']:.0f}")
            logger.info(f"[red]SELL signal[/red] | {reason}")
            return TradeSignal(Signal.SELL, entry, stop_loss, take_profit, reason)

        return TradeSignal(Signal.HOLD, entry, 0, 0, f"Filtered | {trend_label}")
