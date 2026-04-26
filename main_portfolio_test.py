"""
PORTFOLIO CASCADE TRADER — TEST MODE
Simulates profitable Tier 1 trades to validate cascade logic.
Once cascade fires correctly, switch to live trading.

Usage:
  python main_portfolio_test.py --test-mode --cascade-trigger-profit 500
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
import random


class CascadeRules:
    """Manages profit cascading between tiers."""

    def __init__(self, state_file: str = "portfolio_state.json"):
        self.state_file = Path(state_file)
        self.cascade_thresholds = {
            1: 500.0,   # Tier 1: transfer when profit >= Rs.500
            2: 300.0,   # Tier 2: transfer when profit >= Rs.300
            3: 200.0,   # Tier 3: transfer when profit >= Rs.200
        }
        self.load_state()

    def load_state(self):
        """Load cascade state from file."""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                self.state = json.load(f)
        else:
            self.state = {
                "tier1_capital": 1000.0,
                "tier2_capital": 0.0,
                "tier3_capital": 0.0,
                "tier4_capital": 0.0,
                "tier1_profit": 0.0,
                "tier2_profit": 0.0,
                "tier3_profit": 0.0,
                "tier4_profit": 0.0,
                "cascades": [],
                "trades": [],
            }

    def save_state(self):
        """Save cascade state to file."""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def add_trade(self, tier_num: int, pnl: float):
        """Record a trade and update profit."""
        profit_key = f"tier{tier_num}_profit"
        self.state[profit_key] += pnl
        self.state["trades"].append({
            "timestamp": datetime.now().isoformat(),
            "tier": tier_num,
            "pnl": pnl,
            "cumulative_profit": self.state[profit_key]
        })
        self.save_state()
        return self.state[profit_key]

    def check_cascade(self, tier_num: int) -> float:
        """Check if tier should cascade profits to next tier."""
        if tier_num >= 3:  # Tier 4 doesn't cascade
            return 0.0

        profit_key = f"tier{tier_num}_profit"
        tier_profit = self.state[profit_key]
        threshold = self.cascade_thresholds.get(tier_num, 0)

        if tier_profit >= threshold:
            # Cascade: transfer profit to next tier
            next_tier_capital_key = f"tier{tier_num + 1}_capital"
            transfer_amount = tier_profit

            self.state[next_tier_capital_key] += transfer_amount
            self.state[profit_key] = 0.0  # Reset profit after transfer

            cascade_event = {
                "timestamp": datetime.now().isoformat(),
                "from_tier": tier_num,
                "to_tier": tier_num + 1,
                "amount": transfer_amount,
            }
            self.state["cascades"].append(cascade_event)
            self.save_state()

            return transfer_amount

        return 0.0


class TestPortfolioTrader:
    """Test mode: Simulate Tier 1 trades, validate cascades."""

    def __init__(self):
        self.cascade_rules = CascadeRules()
        self.trade_count = 0
        self.cascade_events = []

    def simulate_tier1_trades(self, num_trades: int = 50, win_rate: float = 0.60):
        """Simulate Tier 1 trades with given win rate."""
        print(f"\n{'='*80}")
        print(f"SIMULATING TIER 1 TRADES: {num_trades} trades @ {win_rate*100:.0f}% win rate")
        print(f"{'='*80}\n")

        for i in range(num_trades):
            # Simulate trade result
            is_win = random.random() < win_rate
            pnl = 15.0 if is_win else -5.0  # +Rs.15 win, -Rs.5 loss
            # (60% WR: 0.6*15 - 0.4*5 = 9 - 2 = +Rs.7 expected per trade)

            # Record trade
            cumulative_profit = self.cascade_rules.add_trade(1, pnl)

            print(f"[Trade {i+1}] {'WIN' if is_win else 'LOSS'} | PnL: Rs.{pnl:+.2f} | "
                  f"Tier 1 Profit: Rs.{cumulative_profit:.2f}")

            # Check cascade every trade
            cascade_amt = self.cascade_rules.check_cascade(1)
            if cascade_amt > 0:
                print(f"  >>> CASCADE FIRED! Rs.{cascade_amt:.2f} → Tier 2")
                self.cascade_events.append({
                    "trade": i+1,
                    "amount": cascade_amt,
                    "from": 1,
                    "to": 2
                })

                # Simulate Tier 2 trades now that it has capital
                self._simulate_tier2_after_cascade(cascade_amt)

        self.print_summary()

    def _simulate_tier2_after_cascade(self, tier2_capital: float):
        """Simulate Tier 2 trades after receiving capital from Tier 1."""
        print(f"\n  [Tier 2 ACTIVATED] Capital: Rs.{tier2_capital:.2f}")
        print(f"  Running 20 Tier 2 trades @ 50% win rate...\n")

        for i in range(20):
            is_win = random.random() < 0.50
            pnl = 6.0 if is_win else -4.0  # Lower PnL (lower leverage, 20x vs 50x)

            cumulative_profit = self.cascade_rules.add_trade(2, pnl)

            print(f"  [T2 Trade {i+1}] {'WIN' if is_win else 'LOSS'} | PnL: Rs.{pnl:+.2f} | "
                  f"Tier 2 Profit: Rs.{cumulative_profit:.2f}")

            # Check cascade for Tier 2
            cascade_amt = self.cascade_rules.check_cascade(2)
            if cascade_amt > 0:
                print(f"    >>> CASCADE T2→T3! Rs.{cascade_amt:.2f} → Tier 3")
                self.cascade_events.append({
                    "trade": i+1,
                    "amount": cascade_amt,
                    "from": 2,
                    "to": 3
                })

    def print_summary(self):
        """Print final portfolio state."""
        state = self.cascade_rules.state
        print(f"\n{'='*80}")
        print("FINAL PORTFOLIO STATE")
        print(f"{'='*80}\n")

        print(f"Tier 1 (High-Risk):      Capital: Rs.{state['tier1_capital']:.2f}, Profit: Rs.{state['tier1_profit']:.2f}")
        print(f"Tier 2 (Medium-Risk):    Capital: Rs.{state['tier2_capital']:.2f}, Profit: Rs.{state['tier2_profit']:.2f}")
        print(f"Tier 3 (Lower-Risk):     Capital: Rs.{state['tier3_capital']:.2f}, Profit: Rs.{state['tier3_profit']:.2f}")
        print(f"Tier 4 (Long-Term):      Capital: Rs.{state['tier4_capital']:.2f}, Profit: Rs.{state['tier4_profit']:.2f}")

        total_capital = sum([state[f'tier{i}_capital'] for i in range(1, 5)])
        total_profit = sum([state[f'tier{i}_profit'] for i in range(1, 5)])

        print(f"\n{'─'*80}")
        print(f"Total Capital:           Rs.{total_capital:.2f}")
        print(f"Total Profit:            Rs.{total_profit:.2f}")
        print(f"Return:                  {(total_profit / 1000.0) * 100:.1f}%")

        print(f"\n{'─'*80}")
        print(f"Cascade Events:          {len(state['cascades'])}")
        for cascade in state['cascades']:
            print(f"  - Tier {cascade['from_tier']} → {cascade['to_tier']}: Rs.{cascade['amount']:.2f}")

        print(f"\n{'='*80}\n")


async def main():
    """Run test mode simulation."""
    print("\n" + "="*80)
    print("PORTFOLIO CASCADE TEST MODE")
    print("Simulating Tier 1 trades to validate cascade logic")
    print("="*80)

    trader = TestPortfolioTrader()

    # Simulate 80 Tier 1 trades with 60% win rate (60% WR = +Rs.7 expected per trade)
    trader.simulate_tier1_trades(num_trades=80, win_rate=0.60)

    print("\nState saved to portfolio_state.json")
    print("Ready to run live trading with: python main_portfolio.py --paper --live-data")


if __name__ == "__main__":
    asyncio.run(main())
