"""
Extreme Scalping Strategy - RSI oversold bounces with high volume.
Works with 100x leverage for ultra-tight stop losses.
"""
import pandas as pd
from strategies.base import BaseStrategy, TradeSignal, Signal


class ExtremeScalpStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "extreme_scalp"

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate RSI and volume indicators."""
        # RSI (14-period)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # Volume moving average
        df['volume_ma'] = df['volume'].rolling(window=20).mean()

        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """
        Entry: RSI < threshold (oversold) + Volume > volume_ma * multiplier
        Exit: Take profit +1% or stop loss -0.5%
        """
        df = self.calculate_indicators(df)

        entry_rules = self.config.get("entry_rules", {})
        exit_rules = self.config.get("exit_rules", {})

        rsi_threshold = entry_rules.get("rsi_threshold", 30)
        volume_multiplier = entry_rules.get("volume_multiplier", 2)

        tp_pct = exit_rules.get("take_profit_pct", 1.0) / 100
        sl_pct = exit_rules.get("stop_loss_pct", 0.5) / 100

        current_price = df['close'].iloc[-1]
        current_rsi = df['rsi'].iloc[-1]
        current_volume = df['volume'].iloc[-1]
        avg_volume = df['volume_ma'].iloc[-1]

        # Check entry conditions
        if current_rsi < rsi_threshold and current_volume > (avg_volume * volume_multiplier):
            entry_price = current_price
            stop_loss = entry_price * (1 - sl_pct)
            take_profit = entry_price * (1 + tp_pct)

            return TradeSignal(
                signal=Signal.BUY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=f"RSI={current_rsi:.1f} (threshold={rsi_threshold}), Vol spike",
                confidence=0.8
            )

        return TradeSignal(
            signal=Signal.HOLD,
            entry_price=current_price,
            stop_loss=current_price * 0.99,
            take_profit=current_price * 1.01,
            reason="Waiting for oversold bounce",
            confidence=0.0
        )
