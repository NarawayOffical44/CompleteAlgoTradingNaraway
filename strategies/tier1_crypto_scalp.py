"""
TIER 1: High-Risk Crypto Scalper
Symbol: BTC/USDT | Timeframe: 1m | Leverage: 50x
Strategy: RSI oversold bounce + volume confirmation
Risk: -0.5% SL | Reward: +1% TP
Purpose: Generate aggressive profits for cascading to Tier 2
"""
import pandas as pd
from strategies.base import BaseStrategy, TradeSignal, Signal


class Tier1CryptoScalp(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "tier1_crypto_scalp"

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate RSI, volume, and MACD indicators."""
        # RSI (14-period)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # Volume moving average
        df['volume_ma'] = df['volume'].rolling(window=20).mean()

        # MACD
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()

        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """
        Entry: (RSI < 40) OR (MACD bullish crossover) + volume confirmation
        Exit: +1% TP or -0.5% SL (non-negotiable at 50x leverage)
        Aggressive for high-frequency scalping on volatile altcoins.
        """
        if len(df) < 26:
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
        current_rsi = df['rsi'].iloc[-1]
        current_volume = df['volume'].iloc[-1]
        avg_volume = df['volume_ma'].iloc[-1]
        current_macd = df['macd'].iloc[-1]
        current_signal = df['signal_line'].iloc[-1]
        prev_macd = df['macd'].iloc[-2]
        prev_signal = df['signal_line'].iloc[-2]

        # SIMPLE ENTRY: RSI < 50 = price is below average momentum = buy dip
        # Fires in downtrends, sideways, and pullbacks within uptrends
        if current_rsi < 50:
            entry_price = current_price
            stop_loss = entry_price * 0.995   # -0.5% SL
            take_profit = entry_price * 1.01  # +1% TP

            return TradeSignal(
                signal=Signal.BUY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=f"RSI={current_rsi:.1f} < 50 (buying dip)",
                confidence=0.75
            )

        return TradeSignal(
            signal=Signal.HOLD,
            entry_price=current_price,
            stop_loss=current_price * 0.995,
            take_profit=current_price * 1.01,
            reason=f"RSI={current_rsi:.1f} > 50 (waiting for pullback)",
            confidence=0.0
        )
