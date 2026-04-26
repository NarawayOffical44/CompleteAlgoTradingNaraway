"""
TIER 2: Safer Forex/Swing Trader
Symbol: ETH/USDT (proxy) | Timeframe: 5m | Leverage: 20x
Strategy: EMA crossover (9/21) - trend following
Risk: -1% SL | Reward: +1.5% TP
Purpose: Compound profits from Tier 1 with lower volatility
"""
import pandas as pd
from strategies.base import BaseStrategy, TradeSignal, Signal


class Tier2Forex(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "tier2_forex"

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate EMA indicators."""
        df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """
        Entry: EMA 9 > EMA 21 (bullish crossover)
        Exit: +1.5% TP or -1% SL (safer than Tier 1)
        """
        if len(df) < 21:
            return TradeSignal(
                signal=Signal.HOLD,
                entry_price=df['close'].iloc[-1],
                stop_loss=0,
                take_profit=0,
                reason="Insufficient data",
                confidence=0.0
            )

        df = self.calculate_indicators(df)

        current_price = df['close'].iloc[-1]
        ema_9 = df['ema_9'].iloc[-1]
        ema_21 = df['ema_21'].iloc[-1]
        prev_ema_9 = df['ema_9'].iloc[-2]
        prev_ema_21 = df['ema_21'].iloc[-2]

        # Check for bullish crossover: EMA 9 crosses above EMA 21
        if prev_ema_9 <= prev_ema_21 and ema_9 > ema_21:
            entry_price = current_price
            stop_loss = entry_price * 0.99  # -1% SL
            take_profit = entry_price * 1.015  # +1.5% TP

            return TradeSignal(
                signal=Signal.BUY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=f"EMA 9 ({ema_9:.2f}) > EMA 21 ({ema_21:.2f}) - Bullish",
                confidence=0.80
            )

        return TradeSignal(
            signal=Signal.HOLD,
            entry_price=current_price,
            stop_loss=current_price * 0.99,
            take_profit=current_price * 1.015,
            reason="Waiting for bullish EMA crossover",
            confidence=0.0
        )
