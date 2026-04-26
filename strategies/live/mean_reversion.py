"""
Bollinger Band Mean Reversion + RSI Strategy
---------------------------------------------
Logic:
  BUY  when price touches LOWER Bollinger Band AND RSI < oversold
  SELL when price touches UPPER Bollinger Band AND RSI > overbought

This strategy THRIVES in high-volatility ranging markets.
The more volatile the crypto, the bigger the bands, the bigger the moves.

Stop loss  : entry - (ATR * multiplier)
Take profit: middle band (mean reversion target)

Best for: ALT coins, volatile periods, sideways BTC
"""
import pandas as pd
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
        df["bb_upper"] = bb[f"BBU_{self.bb_period}_{self.bb_std}"]
        df["bb_middle"] = bb[f"BBM_{self.bb_period}_{self.bb_std}"]
        df["bb_lower"] = bb[f"BBL_{self.bb_period}_{self.bb_std}"]
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]  # volatility measure
        df["rsi"] = ta.rsi(df["close"], length=self.rsi_period)
        atr = ta.atr(df["high"], df["low"], df["close"], length=self.atr_period)
        df["atr"] = atr
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        df = self.calculate_indicators(df)
        last = df.iloc[-1]
        entry = last["close"]
        atr = last["atr"]

        # BUY: price at or below lower band + RSI oversold
        if last["close"] <= last["bb_lower"] and last["rsi"] < self.rsi_oversold:
            stop_loss = entry - (atr * self.atr_multiplier)
            take_profit = last["bb_middle"]  # target = mean
            reason = (f"Price at lower BB ({last['bb_lower']:.2f}) | "
                      f"RSI={last['rsi']:.1f} | BB Width={last['bb_width']:.3f}")
            logger.info(f"[green]BUY signal[/green] | {reason}")
            return TradeSignal(Signal.BUY, entry, stop_loss, take_profit, reason)

        # SELL: price at or above upper band + RSI overbought
        if last["close"] >= last["bb_upper"] and last["rsi"] > self.rsi_overbought:
            stop_loss = entry + (atr * self.atr_multiplier)
            take_profit = last["bb_middle"]  # target = mean
            reason = (f"Price at upper BB ({last['bb_upper']:.2f}) | "
                      f"RSI={last['rsi']:.1f} | BB Width={last['bb_width']:.3f}")
            logger.info(f"[red]SELL signal[/red] | {reason}")
            return TradeSignal(Signal.SELL, entry, stop_loss, take_profit, reason)

        return TradeSignal(Signal.HOLD, entry, 0, 0, "Price within bands")
