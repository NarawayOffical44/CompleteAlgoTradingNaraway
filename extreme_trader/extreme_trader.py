"""EXTREME LEVERAGE TRADING BOT - Works with any strategy"""

import asyncio
import json
import ccxt
import pandas as pd
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger
from strategies.base import Signal

logger = get_logger(__name__)


class ExtremeTrader:
    """Generic leverage trader - works with any strategy"""

    def __init__(self, strategy, config_path="extreme_trader/extreme_config.yaml"):
        self.strategy = strategy
        self.config_path = Path(config_path)
        self.load_config()

        self.capital = self.config["capital"]
        self.leverage = self.config["leverage"]
        self.buying_power = self.capital * self.leverage
        self.symbol = self.config.get("symbol", "BTC/USDT")

        self.pnl = 0
        self.daily_pnl = 0
        self.trades = []
        self.max_daily_loss = self.config["max_daily_loss"]

        self.testing_mode = self.config.get("testing_mode", "paper")

        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })

        self.state_file = Path("extreme_trader/extreme_state.json")
        self.trades_file = Path("extreme_trader/extreme_trades.json")

        self.load_state()

        logger.info("=" * 70)
        logger.warning(f"EXTREME TRADER - {self.leverage}x LEVERAGE")
        logger.info("=" * 70)
        logger.warning(f"Mode: {self.testing_mode}")
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Strategy: {self.strategy.name}")
        logger.info(f"Capital: {self.capital}")
        logger.info(f"Leverage: {self.leverage}x")
        logger.info(f"Buying Power: {self.buying_power}")
        logger.warning(f"Daily Loss Limit: {self.max_daily_loss}")
        logger.warning("HIGH RISK - TEST ON PAPER FIRST!")
        logger.info("=" * 70 + "\n")

    def load_config(self):
        """Load config"""
        import yaml
        try:
            with open(self.config_path) as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            logger.error(f"Config not found: {self.config_path}")
            raise

    def load_state(self):
        """Load previous state"""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    state = json.load(f)
                    self.pnl = state.get("pnl", 0)
                    self.daily_pnl = state.get("daily_pnl", 0)
            except:
                pass

    def save_state(self):
        """Save state"""
        state = {
            "pnl": self.pnl,
            "daily_pnl": self.daily_pnl,
            "trades": len(self.trades),
            "timestamp": datetime.now().isoformat()
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f)

    def save_trades(self):
        """Save trades"""
        with open(self.trades_file, 'w') as f:
            json.dump(self.trades, f, default=str)

    async def get_signal(self):
        """Get trading signal from strategy"""
        try:
            ohlcv = await asyncio.wait_for(
                asyncio.to_thread(self.exchange.fetch_ohlcv, self.symbol, "1m", limit=50),
                timeout=3.0
            )

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # Get signal from strategy
            trade_signal = self.strategy.generate_signal(df)

            if trade_signal.signal == Signal.BUY:
                logger.info(f"SIGNAL: {trade_signal.reason}")
                return trade_signal

            return None
        except Exception as e:
            logger.warning(f"Signal error: {e}")
            return None

    async def enter_trade(self, trade_signal):
        """Enter trade with signal info"""
        try:
            entry_price = trade_signal.entry_price
            position_size = self.buying_power

            trade = {
                "entry_time": datetime.now().isoformat(),
                "entry_price": entry_price,
                "position_size": position_size,
                "leverage": self.leverage,
                "stop_loss": trade_signal.stop_loss,
                "take_profit": trade_signal.take_profit,
            }

            logger.info(f"ENTRY: {entry_price} ({self.leverage}x leverage)")
            return trade
        except Exception as e:
            logger.warning(f"Entry error: {e}")
            return None

    async def exit_trade(self, trade):
        """Exit trade - monitor for take profit/stop loss"""
        try:
            entry_price = trade['entry_price']
            stop_loss = trade['stop_loss']
            take_profit = trade['take_profit']
            max_hold_seconds = self.config.get("exit_rules", {}).get("max_hold_seconds", 30)

            start_time = datetime.now()
            exit_price = entry_price
            exit_reason = "max_hold"

            # Monitor price until TP/SL or max_hold
            while True:
                await asyncio.sleep(0.5)

                try:
                    ticker = await asyncio.wait_for(
                        asyncio.to_thread(self.exchange.fetch_ticker, self.symbol),
                        timeout=2.0
                    )
                    exit_price = ticker['close']
                except:
                    pass

                # Check take profit
                if exit_price >= take_profit:
                    exit_reason = "take_profit"
                    break

                # Check stop loss
                if exit_price <= stop_loss:
                    exit_reason = "stop_loss"
                    break

                # Check max hold time
                if (datetime.now() - start_time).total_seconds() >= max_hold_seconds:
                    exit_reason = "max_hold"
                    break

            # Calculate P&L with fees
            pnl_pct_val = (exit_price - entry_price) / entry_price * 100
            profit_loss = trade['position_size'] * pnl_pct_val / 100

            # BINANCE FEES (0.04% taker each way)
            entry_fee = trade['position_size'] * 0.0004
            exit_fee = trade['position_size'] * 0.0004
            total_fees = entry_fee + exit_fee

            # Deduct fees from profit
            profit_loss = profit_loss - total_fees

            self.pnl += profit_loss
            self.daily_pnl += profit_loss

            if profit_loss > 0:
                logger.info(f"WIN: +{profit_loss:.0f} ({pnl_pct_val:.3f}% via {exit_reason} - {total_fees:.0f} fees)")
            else:
                logger.warning(f"LOSS: {profit_loss:.0f} ({pnl_pct_val:.3f}% via {exit_reason} - {total_fees:.0f} fees)")

            self.trades.append({
                "entry": entry_price,
                "exit": exit_price,
                "pnl": profit_loss,
                "pnl_pct": pnl_pct_val,
                "fees": total_fees,
                "reason": exit_reason,
            })

            return profit_loss
        except Exception as e:
            logger.error(f"Exit error: {e}")
            return 0

    async def trading_loop(self):
        """Main loop"""
        logger.info("Trading started\n")

        count = 0
        while True:
            try:
                if self.daily_pnl < self.max_daily_loss:
                    logger.critical("Daily loss limit hit. Stopping.")
                    break

                trade_signal = await self.get_signal()

                if trade_signal:
                    trade = await self.enter_trade(trade_signal)
                    if trade:
                        profit_loss = await self.exit_trade(trade)
                        count += 1
                        logger.info(f"Trades: {count} | PnL: {self.pnl:.0f} | Daily: {self.daily_pnl:.0f}\n")

                        self.save_state()
                        self.save_trades()

                await asyncio.sleep(2)  # Wait before next signal check

            except KeyboardInterrupt:
                logger.info("Stopped")
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                await asyncio.sleep(2)

        logger.info("\n" + "=" * 70)
        logger.info(f"Final: Trades={count} | PnL={self.pnl:.0f}")
        logger.info("=" * 70)

    async def run(self):
        """Start"""
        await self.trading_loop()
