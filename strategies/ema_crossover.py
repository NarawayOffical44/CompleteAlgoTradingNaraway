"""
EMA Crossover + RSI Filter Strategy
------------------------------------
Logic:
  BUY  when fast EMA crosses ABOVE slow EMA AND RSI < overbought threshold
  SELL when fast EMA crosses BELOW slow EMA AND RSI > oversold threshold

Stop loss  : entry - (ATR * multiplier)  — dynamic, adapts to volatility
Take profit: entry + (risk * RR ratio)

Works best in: trending markets with momentum
Timeframes  : 1h, 4h (avoids HFT noise)
"""
import pandas as pd
import pandas_ta as ta
from strategies.base import BaseStrategy, TradeSignal, Signal
from utils.logger import get_logger

logger = get_logger("ema_crossover")


class EMACrossoverStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "ema_crossover"
        cfg = config["ema_crossover"]
        self.ema_fast = cfg["ema_fast"]
        self.ema_slow = cfg["ema_slow"]
        self.rsi_period = cfg["rsi_period"]
        self.rsi_oversold = cfg["rsi_oversold"]
        self.rsi_overbought = cfg["rsi_overbought"]
        self.atr_period = cfg["atr_period"]
        self.atr_multiplier = cfg["atr_multiplier"]
        self.tp_ratio = cfg["take_profit_ratio"]

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df[f"ema_fast"] = ta.ema(df["close"], length=self.ema_fast)
        df[f"ema_slow"] = ta.ema(df["close"], length=self.ema_slow)
        df["rsi"] = ta.rsi(df["close"], length=self.rsi_period)
        atr = ta.atr(df["high"], df["low"], df["close"], length=self.atr_period)
        df["atr"] = atr
        # Crossover detection
        df["cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        df["cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        df = self.calculate_indicators(df)
        last = df.iloc[-1]
        entry = last["close"]
        atr = last["atr"]

        # BUY signal
        if last["cross_up"] and last["rsi"] < self.rsi_overbought:
            stop_loss = entry - (atr * self.atr_multiplier)
            risk = entry - stop_loss
            take_profit = entry + (risk * self.tp_ratio)
            reason = f"EMA{self.ema_fast} crossed above EMA{self.ema_slow} | RSI={last['rsi']:.1f}"
            logger.info(f"[green]BUY signal[/green] | {reason} | SL={stop_loss:.2f} | TP={take_profit:.2f}")
            return TradeSignal(Signal.BUY, entry, stop_loss, take_profit, reason)

        # SELL signal
        if last["cross_down"] and last["rsi"] > self.rsi_oversold:
            stop_loss = entry + (atr * self.atr_multiplier)
            risk = stop_loss - entry
            take_profit = entry - (risk * self.tp_ratio)
            reason = f"EMA{self.ema_fast} crossed below EMA{self.ema_slow} | RSI={last['rsi']:.1f}"
            logger.info(f"[red]SELL signal[/red] | {reason} | SL={stop_loss:.2f} | TP={take_profit:.2f}")
            return TradeSignal(Signal.SELL, entry, stop_loss, take_profit, reason)

        return TradeSignal(Signal.HOLD, entry, 0, 0, "No crossover")
