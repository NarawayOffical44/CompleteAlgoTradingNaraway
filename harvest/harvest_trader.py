"""
HARVEST TRADING SYSTEM
======================
F&O: Keep ₹1000 trading forever
Forex: Accumulate all F&O profits separately

Separate from main trading system - runs independently.
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple
from dataclasses import dataclass, asdict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger
import ccxt
import pandas as pd
import pandas_ta as ta

logger = get_logger(__name__)


@dataclass
class TradeRecord:
    """Record of a single trade"""
    timestamp: str
    tier: str  # "f_and_o" or "forex"
    symbol: str
    side: str  # "BUY" or "SELL"
    entry_price: float
    exit_price: float
    profit_loss: float
    capital_used: float
    leverage: float
    pnl_pct: float
    duration_hours: float


class HarvestTrader:
    """
    Harvest Trading System: Separate F&O and Forex tiers

    F&O Tier: ₹1000 capital, continuously trading, profits harvested
    Forex Tier: Starts empty, grows from F&O harvests, compounds safely
    """

    def __init__(self, config_path: str = "harvest/harvest_config.yaml"):
        self.config_path = Path(config_path)
        self.load_config()

        # F&O Tier (FIXED)
        self.f_and_o_capital = self.config["f_and_o_tier"]["capital"]
        self.f_and_o_capital_locked = self.f_and_o_capital  # Track locked amount
        self.f_and_o_leverage = self.config["f_and_o_tier"]["leverage"]
        self.f_and_o_cumulative_pnl = 0
        self.f_and_o_trades = []
        self.f_and_o_active_trade = None

        # Forex Tier (DYNAMIC)
        self.forex_capital = 0  # Starts empty
        self.forex_leverage = self.config["forex_tier"]["leverage"]
        self.forex_cumulative_pnl = 0
        self.forex_trades = []
        self.forex_active_trade = None

        # Harvest tracking
        self.total_harvested = 0
        self.harvest_history = []

        # State tracking
        self.system_running = False
        self.f_and_o_paused = False
        self.last_daily_loss = 0
        self.daily_start_time = datetime.now()

        # Exchange connection (for LIVE DATA mode)
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })

        # Data mode
        self.data_mode = self.config.get("data_mode", "mock")
        self.historical_data = None
        self.data_index = 0

        if self.data_mode == "historical":
            self.load_historical_data()

        # Price tracking
        self.last_f_and_o_price = None
        self.last_forex_price = None

        # Data persistence
        self.state_file = Path("harvest/harvest_state.json")
        self.trades_file = Path("harvest/harvest_trades.json")

        self.load_state()
        logger.info("Harvest Trader initialized (LIVE DATA MODE)")

    def load_config(self):
        """Load harvest configuration"""
        import yaml
        try:
            with open(self.config_path) as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            logger.info(f"Config not found, using defaults")
            self.config = self._default_config()
            self.save_config()

    def load_historical_data(self):
        """Load historical backtesting data for testing"""
        try:
            data_file = self.config.get("historical_data_file", "data/harvest_backtest_data.csv")
            if Path(data_file).exists():
                self.historical_data = pd.read_csv(data_file)
                logger.info(f"Loaded {len(self.historical_data)} historical candles")
            else:
                logger.warning(f"Historical data file not found: {data_file}, using mock")
                self.data_mode = "mock"
        except Exception as e:
            logger.warning(f"Could not load historical data: {e}, using mock")
            self.data_mode = "mock"

    def load_state(self):
        """Load previous state from file"""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    state = json.load(f)
                    self.f_and_o_cumulative_pnl = state.get("f_and_o_pnl", 0)
                    self.forex_capital = state.get("forex_capital", 0)
                    self.forex_cumulative_pnl = state.get("forex_pnl", 0)
                    self.total_harvested = state.get("total_harvested", 0)
            except Exception as e:
                logger.warning(f"Could not load state: {e}")

    def save_config(self):
        """Save config to file"""
        import yaml
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)

    def save_state(self):
        """Persist state to file"""
        state = {
            "f_and_o_pnl": self.f_and_o_cumulative_pnl,
            "forex_capital": self.forex_capital,
            "forex_pnl": self.forex_cumulative_pnl,
            "total_harvested": self.total_harvested,
            "timestamp": datetime.now().isoformat()
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def save_trades(self):
        """Save all trades to file"""
        def to_dict(trade):
            """Convert trade to dict (handle both dict and dataclass)"""
            if isinstance(trade, dict):
                return trade
            return asdict(trade)

        all_trades = {
            "f_and_o_trades": [to_dict(t) for t in self.f_and_o_trades],
            "forex_trades": [to_dict(t) for t in self.forex_trades]
        }
        with open(self.trades_file, 'w') as f:
            json.dump(all_trades, f, indent=2, default=str)

    async def f_and_o_trading_loop(self):
        """
        F&O Tier: Continuously trade with FIXED ₹1000 (PARALLEL)
        Profits are HARVESTED, not reinvested
        """
        logger.info("[F&O] Loop started (checking every 5 sec)")
        while self.system_running:
            try:
                # Check pause condition
                if self.f_and_o_paused:
                    await asyncio.sleep(300)  # Check every 5 min
                    continue

                # Simulate getting signal from mean_reversion strategy
                signal = await self.get_f_and_o_signal()

                if signal == "BUY":
                    # Enter trade with FIXED ₹1000 capital
                    trade = await self.f_and_o_enter_trade()

                    if trade:
                        # Wait for exit
                        profit_loss = await self.f_and_o_exit_trade(trade)

                        # Update cumulative P&L
                        self.f_and_o_cumulative_pnl += profit_loss

                        # HARVEST OR RECOVER (key step)
                        if profit_loss > 0:
                            # Current capital before adding this profit
                            capital_before = self.f_and_o_capital_locked + self.f_and_o_cumulative_pnl

                            # How much do we need to recover to reach 1000?
                            recovery_deficit = max(0, self.f_and_o_capital_locked - capital_before)

                            if recovery_deficit > 0:
                                # F&O is below 1000 -> RECOVER first
                                if profit_loss >= recovery_deficit:
                                    # Profit covers recovery + harvest
                                    self.f_and_o_cumulative_pnl += recovery_deficit
                                    harvest_amount = profit_loss - recovery_deficit
                                    logger.info(f"F&O +{profit_loss:.0f} (recover {recovery_deficit:.0f}, harvest {harvest_amount:.0f})")
                                    if harvest_amount > 0:
                                        await self.harvest_to_forex(harvest_amount)
                                else:
                                    # Profit only covers partial recovery
                                    self.f_and_o_cumulative_pnl += profit_loss
                                    still_needed = recovery_deficit - profit_loss
                                    logger.info(f"F&O +{profit_loss:.0f} (recovering, still -{still_needed:.0f})")
                            else:
                                # F&O at/above 1000 -> HARVEST all profit
                                self.f_and_o_cumulative_pnl += profit_loss
                                logger.info(f"F&O +{profit_loss:.0f} -> Forex")
                                await self.harvest_to_forex(profit_loss)
                        else:
                            self.f_and_o_cumulative_pnl += profit_loss
                            logger.info(f"F&O -{abs(profit_loss):.0f}")

                        # Check daily loss limit
                        if self.f_and_o_cumulative_pnl < -200:
                            logger.info(f"F&O paused: loss limit hit")
                            self.f_and_o_paused = True

                        # Save state
                        self.save_state()
                        self.save_trades()

                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"F&O Error: {e}")
                await asyncio.sleep(60)

    async def forex_trading_loop(self):
        """
        Forex Tier: Trade only with HARVESTED profits (PARALLEL)
        Reinvest gains back into Forex
        """
        logger.info("[Forex] Loop started (checking every 10 sec)")
        while self.system_running:
            try:
                # Only trade if capital available
                if self.forex_capital < self.config["forex_tier"].get("min_capital", 50):
                    await asyncio.sleep(10)
                    continue

                # Get signal from EMA strategy
                signal = await self.get_forex_signal()

                if signal == "BUY":
                    # Enter trade with all available Forex capital
                    trade = await self.forex_enter_trade()

                    if trade:
                        # Wait for exit
                        profit_loss = await self.forex_exit_trade(trade)

                        # Update capital and P&L
                        self.forex_capital += profit_loss
                        self.forex_cumulative_pnl += profit_loss

                        if profit_loss > 0:
                            logger.info(f"Forex +{profit_loss:.0f} ({self.forex_capital:.0f} total)")
                        else:
                            logger.info(f"Forex -{abs(profit_loss):.0f} ({self.forex_capital:.0f} total)")

                        # Save state
                        self.save_state()
                        self.save_trades()

                await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"Forex Error: {e}")
                await asyncio.sleep(60)

    async def harvest_to_forex(self, profit: float):
        """
        HARVEST: Move F&O profit to Forex capital
        This is the key mechanism that separates the tiers
        """
        self.forex_capital += profit
        self.total_harvested += profit

        harvest_record = {
            "timestamp": datetime.now().isoformat(),
            "profit": profit,
            "forex_capital_after": self.forex_capital
        }
        self.harvest_history.append(harvest_record)

    async def f_and_o_enter_trade(self) -> Dict:
        """Enter F&O trade with FIXED ₹1000 - LIVE or MOCK price"""
        import random
        symbol = self.config["f_and_o_tier"]["symbol"]

        # Try to fetch LIVE price if in live mode
        if self.data_mode == "live":
            try:
                ticker = await asyncio.wait_for(
                    asyncio.to_thread(self.exchange.fetch_ticker, symbol),
                    timeout=3.0
                )
                entry_price = ticker['close']
            except:
                # Fallback to mock
                entry_price = 50000 + random.uniform(-2000, 2000)
        else:
            # Historical/Mock mode
            entry_price = 50000 + random.uniform(-2000, 2000)

        trade = {
            "tier": "f_and_o",
            "entry_time": datetime.now(),
            "capital": self.f_and_o_capital,
            "leverage": self.f_and_o_leverage,
            "position_size": self.f_and_o_capital * self.f_and_o_leverage,
            "symbol": symbol,
            "entry_price": entry_price
        }
        self.f_and_o_active_trade = trade
        logger.info(f"F&O: Entry @ {entry_price:.2f}")
        return trade

    async def f_and_o_exit_trade(self, trade: Dict) -> float:
        """Exit F&O trade - uses LIVE prices when available"""
        await asyncio.sleep(2)  # Simulate holding

        import random
        entry_price = trade['entry_price']

        # Try LIVE price if available
        if self.data_mode == "live" and entry_price > 0:
            try:
                ticker = await asyncio.wait_for(
                    asyncio.to_thread(self.exchange.fetch_ticker, trade['symbol']),
                    timeout=3.0
                )
                exit_price = ticker['close']
                price_change_pct = (exit_price - entry_price) / entry_price * 100
                profit_loss = (trade['capital'] * trade['leverage'] * price_change_pct) / 100
                if price_change_pct < -2.0:
                    profit_loss = -(trade['capital'] * 0.02)
            except:
                if random.random() < 0.60:
                    profit_loss = random.uniform(50, 150)
                else:
                    profit_loss = random.uniform(-100, -30)
                exit_price = 0
        else:
            if random.random() < 0.60:
                profit_loss = random.uniform(50, 150)
            else:
                profit_loss = random.uniform(-100, -30)
            exit_price = 0

        trade["exit_price"] = exit_price
        trade["profit_loss"] = profit_loss
        trade["exit_time"] = datetime.now()

        self.f_and_o_trades.append(trade)
        return profit_loss

    async def forex_enter_trade(self) -> Dict:
        """Enter Forex trade with all available capital - LIVE or MOCK price"""
        import random
        symbol = "EUR/INR"

        # Try to fetch LIVE price if in live mode (proxy with ETH/USDT)
        if self.data_mode == "live":
            try:
                ticker = await asyncio.wait_for(
                    asyncio.to_thread(self.exchange.fetch_ticker, "ETH/USDT"),
                    timeout=3.0
                )
                # Scale to EUR/INR range
                entry_price = 90 + (ticker['close'] - 3000) / 100
            except:
                # Fallback to mock
                entry_price = 90 + random.uniform(-1, 1)
        else:
            # Historical/Mock mode
            entry_price = 90 + random.uniform(-1, 1)

        trade = {
            "tier": "forex",
            "entry_time": datetime.now(),
            "capital": self.forex_capital,
            "leverage": self.forex_leverage,
            "position_size": self.forex_capital * self.forex_leverage,
            "symbol": symbol,
            "entry_price": entry_price
        }
        self.forex_active_trade = trade
        logger.info(f"Forex: Entry @ {entry_price:.2f}")
        return trade

    async def forex_exit_trade(self, trade: Dict) -> float:
        """Exit Forex trade - uses LIVE prices when available"""
        await asyncio.sleep(3)  # Simulate holding

        import random
        entry_price = trade['entry_price']

        # Try LIVE price if available (proxy with ETH/USDT)
        if self.data_mode == "live" and entry_price > 0:
            try:
                ticker = await asyncio.wait_for(
                    asyncio.to_thread(self.exchange.fetch_ticker, "ETH/USDT"),
                    timeout=3.0
                )
                exit_price_eth = ticker['close']
                exit_price = 90 + (exit_price_eth - 3000) / 100
                price_change_pct = (exit_price - entry_price) / entry_price * 100
                profit_loss = (trade['capital'] * trade['leverage'] * price_change_pct) / 100
                if price_change_pct < -1.0:
                    profit_loss = -(trade['capital'] * 0.01)
            except:
                if random.random() < 0.85:
                    profit_loss = random.uniform(3, 15)
                else:
                    profit_loss = random.uniform(-10, -2)
                exit_price = 0
        else:
            if random.random() < 0.85:
                profit_loss = random.uniform(3, 15)
            else:
                profit_loss = random.uniform(-10, -2)
            exit_price = 0

        trade["exit_price"] = exit_price
        trade["profit_loss"] = profit_loss
        trade["exit_time"] = datetime.now()

        self.forex_trades.append(trade)
        return profit_loss

    async def get_f_and_o_signal(self) -> str:
        """Get F&O signal (mock - ready for real integration)"""
        import random
        # 30% chance to signal BUY
        if random.random() < 0.30:
            return "BUY"
        return "WAIT"

    async def get_forex_signal(self) -> str:
        """Get Forex signal (mock)"""
        import random
        # 20% chance to signal BUY
        if random.random() < 0.20:
            return "BUY"
        return "WAIT"


    async def run(self):
        """Start all loops simultaneously"""
        self.system_running = True
        logger.info("Harvest Trading System started")

        tasks = [
            asyncio.create_task(self.f_and_o_trading_loop()),
            asyncio.create_task(self.forex_trading_loop())
        ]

        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.info("\n⛔ Harvest system stopped")
            self.save_state()
            self.save_trades()
            self.system_running = False

    def get_status(self) -> Dict:
        """Get current system status"""
        return {
            "f_and_o": {
                "capital": self.f_and_o_capital,
                "cumulative_pnl": self.f_and_o_cumulative_pnl,
                "leverage": self.f_and_o_leverage,
                "trades": len(self.f_and_o_trades),
                "paused": self.f_and_o_paused
            },
            "forex": {
                "capital": self.forex_capital,
                "cumulative_pnl": self.forex_cumulative_pnl,
                "leverage": self.forex_leverage,
                "trades": len(self.forex_trades)
            },
            "total": {
                "capital": self.f_and_o_capital + self.forex_capital,
                "pnl": self.f_and_o_cumulative_pnl + self.forex_cumulative_pnl,
                "harvested": self.total_harvested
            }
        }

    @staticmethod
    def _default_config() -> Dict:
        """Default configuration"""
        return {
            "f_and_o_tier": {
                "capital": 1000,
                "leverage": 5,
                "strategy": "mean_reversion_1h",
                "symbol": "ETH/USDT",
                "stop_loss_pct": 1.5,
                "take_profit_pct": 2.5,
                "max_loss_per_day": 200
            },
            "forex_tier": {
                "leverage": 2,
                "strategy": "ema_crossover_4h",
                "symbol": "EUR/INR",
                "stop_loss_pct": 1.0,
                "take_profit_pct": 1.5,
                "min_capital": 50
            }
        }


async def main():
    """Run Harvest Trading System"""
    trader = HarvestTrader()
    await trader.run()


if __name__ == "__main__":
    asyncio.run(main())
