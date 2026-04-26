"""
TIER 4: Safest Long-Term Holdings
Symbol: BTC/USDT | Timeframe: 4h | Leverage: 2x
Strategy: Long-term trend hold - buy strong uptrends, hold
Risk: -2% SL | Reward: +5% TP (long-term goal)
Purpose: Capital preservation + ultimate compound growth hedge
"""
import pandas as pd
from strategies.base import BaseStrategy, TradeSignal, Signal


class Tier4LongTerm(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "tier4_longterm"

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate long-term trend indicators."""
        # SMA 50 and SMA 200 for trend
        df['sma_50'] = df['close'].rolling(window=50).mean()
        df['sma_200'] = df['close'].rolling(window=200).mean()

        # MACD for momentum
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()

        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """
        Entry: Strong uptrend (price > SMA 50 > SMA 200) + MACD bullish
        Exit: +5% TP (long-term goal) or -2% SL
        """
        if len(df) < 200:
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
        sma_50 = df['sma_50'].iloc[-1]
        sma_200 = df['sma_200'].iloc[-1]
        macd = df['macd'].iloc[-1]
        signal_line = df['signal_line'].iloc[-1]
        prev_macd = df['macd'].iloc[-2]
        prev_signal = df['signal_line'].iloc[-2]

        # Check for strong uptrend: price > SMA 50 > SMA 200
        uptrend = current_price > sma_50 > sma_200

        # MACD bullish crossover
        macd_bullish = prev_macd <= prev_signal and macd > signal_line

        if uptrend and macd_bullish:
            entry_price = current_price
            stop_loss = entry_price * 0.98  # -2% SL
            take_profit = entry_price * 1.05  # +5% TP (long-term)

            return TradeSignal(
                signal=Signal.BUY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=f"Strong uptrend (Price>{sma_50:.0f}>SMA200), MACD bullish",
                confidence=0.88
            )

        return TradeSignal(
            signal=Signal.HOLD,
            entry_price=current_price,
            stop_loss=current_price * 0.98,
            take_profit=current_price * 1.05,
            reason="Waiting for strong uptrend confirmation",
            confidence=0.0
        )
