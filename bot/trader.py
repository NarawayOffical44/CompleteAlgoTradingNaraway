"""
Live/Paper Trader
------------------
Runs the strategy on a schedule, manages open trades,
enforces daily loss limits, and logs everything.
"""
import time
import yaml
from datetime import datetime
from bot.exchange import Exchange
from strategies.base import Signal
from strategies.ema_crossover import EMACrossoverStrategy
from strategies.mean_reversion import MeanReversionStrategy
from utils.logger import get_logger

logger = get_logger("trader")

STRATEGY_MAP = {
    "ema_crossover": EMACrossoverStrategy,
    "mean_reversion": MeanReversionStrategy,
}


class Trader:
    def __init__(self, config_path: str = "config/config.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.exchange = Exchange(self.config)
        self.symbol = self.config["trading"]["symbol"]
        self.timeframe = self.config["trading"]["timeframe"]
        self.capital = float(self.config["trading"]["capital"])
        self.risk_pct = self.config["trading"]["risk_per_trade"]
        self.max_daily_loss = self.config["trading"]["max_daily_loss"]

        strategy_name = self.config["strategy"]["active"]
        StrategyClass = STRATEGY_MAP[strategy_name]
        self.strategy = StrategyClass(self.config)

        # P&L tracking against managed capital (not full exchange balance)
        self.managed_capital = self.capital
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0

        self.daily_start_capital = None
        self.open_trade = None
        logger.info(f"Trader initialized | Strategy: {strategy_name} | Symbol: {self.symbol} | Capital: ${self.capital:.2f}")

    def _check_daily_loss_limit(self) -> bool:
        """Returns True if we've hit the daily loss limit — stop trading."""
        loss_pct = (self.capital - self.managed_capital) / self.capital
        if loss_pct >= self.max_daily_loss:
            logger.warning(
                f"[red]Daily loss limit hit: {loss_pct:.1%} loss. Stopping.[/red]"
            )
            return True
        return False

    def _manage_open_trade(self, current_price: float):
        """Check if SL or TP hit on open trade. Updates managed capital."""
        if not self.open_trade:
            return

        trade = self.open_trade
        hit_sl = hit_tp = False

        if trade["side"] == "BUY":
            hit_sl = current_price <= trade["sl"]
            hit_tp = current_price >= trade["tp"]
        else:
            hit_sl = current_price >= trade["sl"]
            hit_tp = current_price <= trade["tp"]

        if hit_tp:
            pnl = abs(trade["tp"] - trade["entry"]) * trade["qty"]
            self.managed_capital += pnl
            self.total_pnl += pnl
            self.wins += 1
            logger.info(f"[green]TAKE PROFIT hit @ {current_price:.2f} | +${pnl:.2f} | Capital: ${self.managed_capital:.2f}[/green]")
            self.exchange.place_market_order(self.symbol, "sell" if trade["side"] == "BUY" else "buy", trade["qty"])
            self.open_trade = None

        elif hit_sl:
            pnl = -abs(trade["entry"] - trade["sl"]) * trade["qty"]
            self.managed_capital += pnl
            self.total_pnl += pnl
            self.losses += 1
            logger.warning(f"[red]STOP LOSS hit @ {current_price:.2f} | {pnl:.2f} | Capital: ${self.managed_capital:.2f}[/red]")
            self.exchange.place_market_order(self.symbol, "sell" if trade["side"] == "BUY" else "buy", trade["qty"])
            self.open_trade = None

    def _print_summary(self):
        pnl_pct = ((self.managed_capital - self.capital) / self.capital) * 100
        total_trades = self.wins + self.losses
        wr = (self.wins / total_trades * 100) if total_trades > 0 else 0
        logger.info(
            f"\n{'='*50}\n"
            f"  SUMMARY\n"
            f"{'='*50}\n"
            f"  Started with : ${self.capital:.2f}\n"
            f"  Now          : ${self.managed_capital:.2f}\n"
            f"  P&L          : ${self.total_pnl:+.2f} ({pnl_pct:+.2f}%)\n"
            f"  Trades       : {total_trades} | Wins: {self.wins} | Losses: {self.losses}\n"
            f"  Win Rate     : {wr:.1f}%\n"
            f"{'='*50}"
        )

    def tick(self):
        """Called every poll interval — the main trading loop."""
        try:
            ticker = self.exchange.get_ticker(self.symbol)
            current_price = ticker["last"]

            pnl_pct = ((self.managed_capital - self.capital) / self.capital) * 100
            logger.info(
                f"[cyan]Tick[/cyan] | {self.symbol} @ {current_price:.2f} | "
                f"Capital: ${self.managed_capital:.2f} | P&L: {pnl_pct:+.2f}% | "
                f"W:{self.wins} L:{self.losses}"
            )

            if self._check_daily_loss_limit():
                return

            # Manage open trade
            self._manage_open_trade(current_price)

            # Only look for new entry if not in trade
            if self.open_trade:
                logger.info(f"In trade | Side: {self.open_trade['side']} | "
                            f"Entry: {self.open_trade['entry']:.2f} | "
                            f"SL: {self.open_trade['sl']:.2f} | "
                            f"TP: {self.open_trade['tp']:.2f}")
                return

            # Get candles and generate signal
            df = self.exchange.get_ohlcv(self.symbol, self.timeframe, limit=200)
            signal = self.strategy.generate_signal(df)

            if signal.signal == Signal.HOLD:
                logger.info("Signal: HOLD")
                return

            # Spot trading: skip SELL signals when we have no open position
            if signal.signal == Signal.SELL and not self.open_trade:
                logger.info("Signal: SELL skipped — no position to close (spot only)")
                return

            # Calculate position size based on managed capital
            qty = self.strategy.calculate_position_size(
                self.managed_capital, signal.entry_price, signal.stop_loss, self.risk_pct
            )

            if qty <= 0:
                logger.warning("Position size too small, skipping trade")
                return

            # Execute trade
            side = "buy" if signal.signal == Signal.BUY else "sell"
            order = self.exchange.place_market_order(self.symbol, side, qty)

            self.open_trade = {
                "side": signal.signal.value,
                "entry": signal.entry_price,
                "sl": signal.stop_loss,
                "tp": signal.take_profit,
                "qty": qty,
                "order_id": order["id"],
            }

        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)

    def run(self):
        """Main loop."""
        timeframe_seconds = {
            "1m": 60, "5m": 300, "15m": 900,
            "1h": 3600, "4h": 14400, "1d": 86400
        }
        interval = self.config["trading"].get("poll_interval") or timeframe_seconds.get(self.timeframe, 3600)
        duration_min = self.config["trading"].get("run_duration_minutes")
        end_time = time.time() + (duration_min * 60) if duration_min else None

        logger.info(f"[bold green]Bot started[/bold green] | Checking every {interval}s | Capital: ${self.capital:.2f}")
        if end_time:
            logger.info(f"Auto-stop in {duration_min} minutes")
        logger.info("Press Ctrl+C to stop")

        try:
            while True:
                if end_time and time.time() >= end_time:
                    logger.info(f"[yellow]{duration_min}min session complete — stopping.[/yellow]")
                    break
                self.tick()
                time.sleep(interval)
        except KeyboardInterrupt:
            pass

        self._print_summary()
