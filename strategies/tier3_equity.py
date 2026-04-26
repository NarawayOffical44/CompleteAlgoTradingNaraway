"""
TIER 3: Safer Equity/Position Trader
Symbol: ETH/USDT | Timeframe: 1h | Leverage: 5x
Strategy: Bollinger Bands mean reversion
Risk: -1.5% SL | Reward: +2% TP
Purpose: Stable, consistent returns with low drawdown
"""
import pandas as pd
from strategies.base import BaseStrategy, TradeSignal, Signal


class Tier3Equity(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "tier3_equity"

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Bollinger Bands and RSI."""
        # Bollinger Bands (20, 2)
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['std_20'] = df['close'].rolling(window=20).std()
        df['bb_upper'] = df['sma_20'] + (df['std_20'] * 2)
        df['bb_lower'] = df['sma_20'] - (df['std_20'] * 2)
        df['bb_mid'] = df['sma_20']

        # RSI (14)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # SMA 200 for trend filter
        df['sma_200'] = df['close'].rolling(window=200).mean()

        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """
        Entry: Price <= BB lower + RSI < 50 (oversold) + above SMA 200 (uptrend)
        Exit: +2% TP or -1.5% SL
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
        bb_lower = df['bb_lower'].iloc[-1]
        bb_mid = df['bb_mid'].iloc[-1]
        current_rsi = df['rsi'].iloc[-1]
        sma_200 = df['sma_200'].iloc[-1]

        # Mean reversion entry: price at lower BB, RSI oversold, above SMA 200
        if current_price <= bb_lower and current_rsi < 50 and current_price > sma_200:
            entry_price = current_price
            stop_loss = entry_price * 0.985  # -1.5% SL
            take_profit = bb_mid  # Target mean (Bollinger mid-band)

            return TradeSignal(
                signal=Signal.BUY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=f"Price at BB lower ({current_price:.2f}), RSI={current_rsi:.1f}<50, above SMA200",
                confidence=0.82
            )

        return TradeSignal(
            signal=Signal.HOLD,
            entry_price=current_price,
            stop_loss=current_price * 0.985,
            take_profit=current_price * 1.02,
            reason="Waiting for oversold mean reversion",
            confidence=0.0
        )
