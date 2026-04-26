"""
LAB STRATEGY TEMPLATE
======================
Copy this file, rename it, and build your experimental strategy here.
When it passes backtesting → promote to live/
When it fails → move to archive/ with a note

Status: TEMPLATE (not runnable)
Tested on: -
Result: -
Reason moved to archive (if applicable): -
"""
import pandas as pd
from core.base_strategy import BaseStrategy, TradeSignal, Signal


class MyExperimentalStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "my_experimental_strategy"
        # Load your config params here

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # Add your indicators here
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        df = self.calculate_indicators(df)
        last = df.iloc[-1]
        entry = last["close"]

        # Your signal logic here
        # Return BUY / SELL / HOLD
        return TradeSignal(Signal.HOLD, entry, 0, 0, "Template — no logic yet")
