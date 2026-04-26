"""
PORTFOLIO CASCADE TRADER
Four-tier trading system: High-Risk -> Safer -> Safer -> Safest
Each tier generates profits that cascade to the next safer tier.

Usage:
  python main_portfolio.py --paper --live-data
  python main_portfolio.py --paper --live-data --interval 60
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import ccxt
import pandas as pd

from strategies.tier1_crypto_scalp import Tier1CryptoScalp
from strategies.tier2_forex import Tier2Forex
from strategies.tier3_equity import Tier3Equity
from strategies.tier4_longterm import Tier4LongTerm


class PortfolioTier:
    """Represents one tier in the cascade with full trade execution tracking."""

    def __init__(self, tier_num: int, name: str, symbol: str, timeframe: str,
                 leverage: float, strategy_class, capital: float = 0.0):
        self.tier_num = tier_num
        self.name = name
        self.symbol = symbol
        self.timeframe = timeframe
        self.leverage = leverage
        self.capital = capital
        self.strategy = strategy_class({})
        self.cumulative_profit = 0.0
        self.open_trade = None  # Tracks current open trade
        self.closed_trades = []
        self.exchange = ccxt.binance({"enableRateLimit": True})

    async def fetch_ohlcv(self, limit: int = 100) -> pd.DataFrame:
        """Fetch live OHLCV data from Binance."""
        try:
            candles = await asyncio.to_thread(
                self.exchange.fetch_ohlcv,
                self.symbol,
                self.timeframe,
                limit=limit
            )
            df = pd.DataFrame(
                candles,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df.set_index('timestamp').sort_index()
        except Exception as e:
            print(f"  [Tier {self.tier_num}] Error fetching {self.symbol}: {e}")
            return pd.DataFrame()

    async def get_live_price(self) -> float:
        """Get current live price."""
        try:
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, self.symbol)
            return float(ticker['last'])
        except Exception:
            return 0.0

    async def generate_signal(self) -> dict:
        """Get signal from strategy using live data."""
        df = await self.fetch_ohlcv()
        if df.empty:
            return {"signal": "HOLD", "reason": "No data", "confidence": 0.0,
                    "entry_price": 0, "stop_loss": 0, "take_profit": 0}
        trade_signal = self.strategy.generate_signal(df)
        return {
            "signal": trade_signal.signal.value,
            "entry_price": trade_signal.entry_price,
            "stop_loss": trade_signal.stop_loss,
            "take_profit": trade_signal.take_profit,
            "reason": trade_signal.reason,
            "confidence": trade_signal.confidence,
        }

    def enter_trade(self, signal_data: dict):
        """Enter a paper trade."""
        position_size = (self.capital * self.leverage) / signal_data['entry_price']
        self.open_trade = {
            "entry_time": datetime.now().isoformat(),
            "entry_price": signal_data['entry_price'],
            "stop_loss": signal_data['stop_loss'],
            "take_profit": signal_data['take_profit'],
            "position_size": position_size,
            "reason": signal_data['reason'],
        }
        print(f"  [ENTER] Price={signal_data['entry_price']:.4f} | "
              f"SL={signal_data['stop_loss']:.4f} | TP={signal_data['take_profit']:.4f}")

    def check_exit(self, current_price: float) -> float:
        """Check if open trade hit TP or SL. Returns P&L or 0 if still open."""
        if not self.open_trade or current_price <= 0:
            return 0.0

        entry = self.open_trade['entry_price']
        sl = self.open_trade['stop_loss']
        tp = self.open_trade['take_profit']
        size = self.open_trade['position_size']
        fee_rate = 0.0004  # 0.04% Binance taker fee

        if current_price >= tp:
            # Take profit hit
            gross_pnl = (tp - entry) * size
            fees = (entry + tp) * size * fee_rate
            net_pnl = gross_pnl - fees
            self._close_trade(current_price, net_pnl, "TP")
            return net_pnl

        elif current_price <= sl:
            # Stop loss hit
            gross_pnl = (sl - entry) * size
            fees = (entry + sl) * size * fee_rate
            net_pnl = gross_pnl - fees
            self._close_trade(current_price, net_pnl, "SL")
            return net_pnl

        return 0.0  # Still open

    def _close_trade(self, exit_price: float, net_pnl: float, reason: str):
        """Close trade and record result."""
        trade = self.open_trade.copy()
        trade.update({
            "exit_time": datetime.now().isoformat(),
            "exit_price": exit_price,
            "net_pnl": net_pnl,
            "result": reason,
        })
        self.closed_trades.append(trade)
        self.cumulative_profit += net_pnl
        self.open_trade = None
        result_emoji = "WIN" if net_pnl > 0 else "LOSS"
        print(f"  [{result_emoji}/{reason}] Exit={exit_price:.4f} | "
              f"PnL=Rs.{net_pnl:+.2f} | Total=Rs.{self.cumulative_profit:.2f}")

    def __repr__(self):
        trade_status = f"IN TRADE @ {self.open_trade['entry_price']:.4f}" if self.open_trade else "No open trade"
        return (
            f"Tier {self.tier_num} ({self.name}): {self.symbol} {self.timeframe} "
            f"@ {self.leverage}x | Capital=Rs.{self.capital:.2f} | "
            f"Profit=Rs.{self.cumulative_profit:.2f} | {trade_status}"
        )


class CascadeRules:
    """Manages profit cascading between tiers."""

    def __init__(self, state_file: str = "portfolio_state.json"):
        self.state_file = Path(state_file)
        self.cascade_thresholds = {
            1: 500.0,
            2: 300.0,
            3: 200.0,
        }
        self.load_state()

    def load_state(self):
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
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def sync_profits(self, tiers: dict):
        """Sync tier profits from live trades into state."""
        for i in range(1, 5):
            self.state[f"tier{i}_profit"] = tiers[i].cumulative_profit
            self.state[f"tier{i}_capital"] = tiers[i].capital

    def check_cascade(self, tier_num: int, tiers: dict) -> float:
        """Check if tier should cascade profits to next tier."""
        if tier_num >= 3:
            return 0.0

        profit_key = f"tier{tier_num}_profit"
        tier_profit = self.state[profit_key]
        threshold = self.cascade_thresholds.get(tier_num, 0)

        if tier_profit >= threshold:
            next_capital_key = f"tier{tier_num + 1}_capital"
            transfer = tier_profit
            self.state[next_capital_key] += transfer
            self.state[profit_key] = 0.0
            tiers[tier_num].cumulative_profit = 0.0
            tiers[tier_num + 1].capital = self.state[next_capital_key]

            self.state["cascades"].append({
                "timestamp": datetime.now().isoformat(),
                "from_tier": tier_num,
                "to_tier": tier_num + 1,
                "amount": transfer,
            })
            self.save_state()
            print(f"\n  *** CASCADE: Tier {tier_num} -> Tier {tier_num+1}: Rs.{transfer:.2f} ***\n")
            return transfer
        return 0.0


class PortfolioTrader:
    """Main coordinator for all four tiers."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.cascade_rules = CascadeRules()
        self.tiers = self._init_tiers()
        self.running = False
        self.cycle_count = 0

    def _init_tiers(self) -> dict:
        state = self.cascade_rules.state
        return {
            1: PortfolioTier(1, "Crypto Scalper", "SOL/USDT", "1m", 50.0,
                             Tier1CryptoScalp, state.get("tier1_capital", 1000.0)),
            2: PortfolioTier(2, "Forex Trader",   "ETH/USDT", "5m", 20.0,
                             Tier2Forex, state.get("tier2_capital", 0.0)),
            3: PortfolioTier(3, "Equity Trader",  "ETH/USDT", "1h", 5.0,
                             Tier3Equity, state.get("tier3_capital", 0.0)),
            4: PortfolioTier(4, "Long-Term Hold", "BTC/USDT", "4h", 2.0,
                             Tier4LongTerm, state.get("tier4_capital", 0.0)),
        }

    async def run_single_cycle(self):
        """Run one cycle: check exits, get signals, enter trades, cascade."""
        self.cycle_count += 1
        print(f"\n{'='*70}")
        print(f"Cycle {self.cycle_count} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | PAPER")
        print(f"{'='*70}")

        for tier_num, tier in self.tiers.items():
            if tier.capital <= 0:
                print(f"\n[Tier {tier_num}] {tier.name}: No capital")
                continue

            print(f"\n[Tier {tier_num}] {tier.name} | Capital=Rs.{tier.capital:.2f} | Profit=Rs.{tier.cumulative_profit:.2f}")

            # Step 1: Check if open trade hit TP or SL
            if tier.open_trade:
                current_price = await tier.get_live_price()
                print(f"  Open trade @ {tier.open_trade['entry_price']:.4f} | Current={current_price:.4f}")
                pnl = tier.check_exit(current_price)
                if pnl != 0:
                    # Trade closed — sync and check cascade
                    self.cascade_rules.sync_profits(self.tiers)
                    self.cascade_rules.check_cascade(tier_num, self.tiers)
                    self.cascade_rules.save_state()
                continue  # One trade at a time per tier

            # Step 2: No open trade — look for new signal
            signal_data = await tier.generate_signal()
            confidence = signal_data.get('confidence', 0.0)
            print(f"  Signal: {signal_data['signal']} (RSI/reason: {signal_data['reason']})")

            # Step 3: Enter trade on BUY signal
            if signal_data['signal'] == 'BUY' and confidence > 0:
                tier.enter_trade(signal_data)

    async def run_continuous(self, interval_seconds: int = 60):
        """Run trading cycles continuously."""
        self.running = True
        self.print_portfolio_summary()
        try:
            while self.running:
                await self.run_single_cycle()
                print(f"\nNext cycle in {interval_seconds}s...")
                await asyncio.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n[STOP] Shutting down")
            self.running = False
        self.print_portfolio_summary()

    def print_portfolio_summary(self):
        print(f"\n{'='*70}")
        print("PORTFOLIO SUMMARY")
        print(f"{'='*70}")
        total_capital = 0
        total_profit = 0
        for i, tier in self.tiers.items():
            wins = len([t for t in tier.closed_trades if t.get('net_pnl', 0) > 0])
            losses = len([t for t in tier.closed_trades if t.get('net_pnl', 0) <= 0])
            print(f"  Tier {i}: Capital=Rs.{tier.capital:.2f} | "
                  f"Profit=Rs.{tier.cumulative_profit:.2f} | "
                  f"Trades={len(tier.closed_trades)} (W:{wins} L:{losses})")
            total_capital += tier.capital
            total_profit += tier.cumulative_profit

        print(f"  {'─'*60}")
        print(f"  TOTAL Capital=Rs.{total_capital:.2f} | Profit=Rs.{total_profit:.2f} | "
              f"Return={total_profit/1000*100:.1f}%")
        cascades = self.cascade_rules.state.get("cascades", [])
        if cascades:
            print(f"\n  Cascade Events: {len(cascades)}")
            for c in cascades:
                print(f"    Tier {c['from_tier']} -> {c['to_tier']}: Rs.{c['amount']:.2f}")
        print(f"{'='*70}\n")


async def main():
    paper_mode = "--paper" in sys.argv
    interval = 60
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        interval = int(sys.argv[idx + 1])

    print("Portfolio Cascade Trader | PAPER MODE | LIVE DATA")
    trader = PortfolioTrader(paper_mode=paper_mode)

    if "--single" in sys.argv:
        await trader.run_single_cycle()
        trader.print_portfolio_summary()
    else:
        await trader.run_continuous(interval_seconds=interval)


if __name__ == "__main__":
    asyncio.run(main())
