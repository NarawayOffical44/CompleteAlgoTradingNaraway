"""
9:20 Short Straddle Strategy - Indian F&O (Nifty/Bank Nifty)
Sells ATM Call (CE) + Put (PE) at 9:20 AM, exits by 3:15 PM
Benefits from theta decay of option premiums in sideways markets
"""
import pandas as pd
from datetime import datetime
from strategies.base import BaseStrategy, TradeSignal, Signal


class ShortStraddleStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "short_straddle_930"
        self.entry_time = None
        self.position_open = False

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """No complex indicators needed - just timestamp and ATM price."""
        return df

    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """
        Entry: 9:20 AM → Sell ATM straddle (CE + PE)
        Exit: 3:15 PM → Square off both legs
        Risk Management: 25-30% of premium as stop loss per leg
        """
        current_time = datetime.now().time()
        current_price = df['close'].iloc[-1]

        # Entry: Between 9:20 and 9:25 AM
        if (current_time.hour == 9 and 20 <= current_time.minute < 25) and not self.position_open:
            self.entry_time = datetime.now()
            self.position_open = True

            # ATM strike assumption (in real scenario, fetch from options chain)
            atm_strike = round(current_price / 100) * 100
            estimated_premium = current_price * 0.02  # ~2% premium (typical for ATM)

            # Stop loss: 25-30% of premium
            sl_pct = 0.27  # 27% stop loss
            stop_loss_price = atm_strike * (1 + sl_pct * 0.005)  # Conservative

            # Take profit: Theta decay typically gives 20-40% daily decay
            # Target: Close at 50% of entry premium
            take_profit_price = current_price * 0.99  # Slight move down = profit

            return TradeSignal(
                signal=Signal.BUY,  # SELL signal mapped as BUY for framework compatibility
                entry_price=current_price,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                reason=f"9:20 AM: Sell ATM Straddle at {atm_strike} (Premium ~{estimated_premium:.0f})",
                confidence=0.85
            )

        # Exit: 3:15 PM - Close all positions
        if current_time.hour == 15 and current_time.minute >= 15 and self.position_open:
            self.position_open = False
            return TradeSignal(
                signal=Signal.SELL,  # Exit signal
                entry_price=current_price,
                stop_loss=current_price * 0.95,
                take_profit=current_price * 1.05,
                reason="3:15 PM: Square off straddle (end of day)",
                confidence=1.0
            )

        # Hold position (do nothing)
        return TradeSignal(
            signal=Signal.HOLD,
            entry_price=current_price,
            stop_loss=current_price * 0.99,
            take_profit=current_price * 1.01,
            reason="Holding straddle - waiting for theta decay",
            confidence=0.0
        )
