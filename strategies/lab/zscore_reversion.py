"""
Z-Score Mean Reversion Strategy — Custom (v2)
----------------------------------------------
User's concept:
  - Rolling 100-candle window → mean + std
  - Price drops considerably below mean (Z-score <= threshold) → BUY
  - Hold until CONSIDERABLE gain (covers 30% Indian tax + fees + profit)
  - Wide stop — hold through noise

Flaws fixed in v2:
  1. 200 SMA filter     — only buy in uptrend/ranging (prevents falling knife)
  2. RSI filter         — RSI 20-45 = oversold but not in freefall (< 20 = avoid)
  3. Volume spike check — high volume on dip = capitulation = stronger reversal signal
  4. ATR-based stop     — adapts to current volatility instead of fixed %
  5. Minimum drop %     — price must be X% below mean (not just z-score)

Indian tax math:
  Need ~20% gross gain minimum:
    30% tax on profit → keeps 70%
    0.2% commission round trip
    Net on 20% gross = (20% * 0.70) - 0.2% = ~13.8% net
"""
import pandas as pd
import numpy as np
import pandas_ta as ta
from strategies.base import BaseStrategy, TradeSignal, Signal
from utils.logger import get_logger

logger = get_logger("zscore_reversion")


class ZScoreReversionStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "zscore_reversion"
        cfg = config["zscore_reversion"]
        self.window          = cfg["window"]            # 100 candles
        self.entry_zscore    = cfg["entry_zscore"]      # -2.0
        self.take_profit_pct = cfg["take_profit_pct"]   # 0.20 = 20%
        self.atr_multiplier  = cfg["atr_multiplier"]    # stop = entry - ATR * mult
        self.rsi_min         = cfg["rsi_min"]           # 20 — below this = freefall, skip
        self.rsi_max         = cfg["rsi_max"]           # 45 — above this = not oversold
        self.min_drop_pct    = cfg["min_drop_pct"]      # 0.05 = must be 5% below mean
        self.volume_factor   = cfg["volume_factor"]     # volume must be X× avg volume
        self.atr_period      = cfg.get("atr_period", 14)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["mean"]       = df["close"].rolling(self.window).mean()
        df["std"]        = df["close"].rolling(self.window).std()
        df["zscore"]     = (df["close"] - df["mean"]) / df["std"]
        df["rsi"]        = ta.rsi(df["close"], length=14)
        df["atr"]        = ta.atr(df["high"], df["low"], df["close"], length=self.atr_period)
        df["sma200"]     = ta.sma(df["close"], length=200)
        df["vol_avg"]    = df["volume"].rolling(20).mean()
        df["vol_ratio"]  = df["volume"] / df["vol_avg"]
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        df   = self.calculate_indicators(df)
        last = df.iloc[-1]
        entry = last["close"]

        # Warmup check
        if pd.isna(last["mean"]) or pd.isna(last["sma200"]):
            return TradeSignal(Signal.HOLD, entry, 0, 0, "Warming up")

        zscore    = last["zscore"]
        mean      = last["mean"]
        rsi       = last["rsi"]
        atr       = last["atr"]
        sma200    = last["sma200"]
        vol_ratio = last["vol_ratio"]
        drop_pct  = (mean - entry) / mean  # how far below mean

        # ── Filters ──────────────────────────────────────────────
        reasons = []

        # 1. Trend filter — skip if enabled AND price below SMA200
        use_sma_filter = self.config["zscore_reversion"].get("use_sma_filter", True)
        if use_sma_filter and entry < sma200:
            reasons.append(f"SKIP: below SMA200 ({sma200:.2f}) — downtrend")
            logger.debug(f"Z-score={zscore:.2f} | {reasons[-1]}")
            return TradeSignal(Signal.HOLD, entry, 0, 0, " | ".join(reasons))

        # 2. Z-score threshold
        if zscore > self.entry_zscore:
            return TradeSignal(Signal.HOLD, entry, 0, 0,
                               f"Z-score={zscore:.2f} waiting for {self.entry_zscore}")

        # 3. RSI filter — not in freefall, but actually oversold
        if rsi < self.rsi_min:
            reasons.append(f"SKIP: RSI={rsi:.1f} < {self.rsi_min} — freefall, avoid")
            return TradeSignal(Signal.HOLD, entry, 0, 0, " | ".join(reasons))
        if rsi > self.rsi_max:
            reasons.append(f"SKIP: RSI={rsi:.1f} > {self.rsi_max} — not oversold enough")
            return TradeSignal(Signal.HOLD, entry, 0, 0, " | ".join(reasons))

        # 4. Minimum drop from mean
        if drop_pct < self.min_drop_pct:
            reasons.append(f"SKIP: only {drop_pct:.1%} below mean, need {self.min_drop_pct:.1%}")
            return TradeSignal(Signal.HOLD, entry, 0, 0, " | ".join(reasons))

        # 5. Volume spike confirmation
        if vol_ratio < self.volume_factor:
            reasons.append(f"SKIP: volume ratio={vol_ratio:.1f}x, need {self.volume_factor}x")
            return TradeSignal(Signal.HOLD, entry, 0, 0, " | ".join(reasons))

        # ── All filters passed — BUY ──────────────────────────────
        stop_loss   = entry - (atr * self.atr_multiplier)
        take_profit = entry * (1 + self.take_profit_pct)

        reason = (
            f"Z={zscore:.2f} | RSI={rsi:.1f} | "
            f"Drop={drop_pct:.1%} below mean | "
            f"Vol={vol_ratio:.1f}x | SMA200={sma200:.0f}"
        )
        logger.info(
            f"[green]BUY signal[/green] | {reason} | "
            f"SL={stop_loss:.4f} | TP={take_profit:.4f}"
        )
        return TradeSignal(Signal.BUY, entry, stop_loss, take_profit, reason)
