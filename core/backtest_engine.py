"""
Backtesting Engine
-------------------
Simulates strategy on historical OHLCV data.
Tracks trades, P&L, drawdown, win rate.
No external dependencies — pure pandas.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List
from strategies.base import BaseStrategy, Signal
from utils.logger import get_logger

logger = get_logger("backtest")


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str
    entry_price: float
    exit_price: float
    qty: float
    stop_loss: float
    take_profit: float
    exit_reason: str
    pnl: float = 0.0
    pnl_pct: float = 0.0

    def __post_init__(self):
        if self.side == "BUY":
            self.pnl = (self.exit_price - self.entry_price) * self.qty
            self.pnl_pct = (self.exit_price - self.entry_price) / self.entry_price
        else:
            self.pnl = (self.entry_price - self.exit_price) * self.qty
            self.pnl_pct = (self.entry_price - self.exit_price) / self.entry_price


@dataclass
class BacktestResult:
    trades: List[Trade]
    initial_capital: float
    final_capital: float
    equity_curve: pd.Series

    @property
    def total_return_pct(self) -> float:
        return (self.final_capital - self.initial_capital) / self.initial_capital * 100

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades > 0 else 0

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        return np.mean(wins) if wins else 0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        return np.mean(losses) if losses else 0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl <= 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.equity_curve.cummax()
        drawdown = (self.equity_curve - peak) / peak
        return drawdown.min() * 100

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0
        returns = [t.pnl_pct for t in self.trades]
        return np.mean(returns) / np.std(returns) * np.sqrt(365) if np.std(returns) > 0 else 0

    def summary(self) -> str:
        lines = [
            "\n" + "="*50,
            "         BACKTEST RESULTS",
            "="*50,
            f"  Initial Capital : ${self.initial_capital:,.2f}",
            f"  Final Capital   : ${self.final_capital:,.2f}",
            f"  Total Return    : {self.total_return_pct:+.2f}%",
            f"  Max Drawdown    : {self.max_drawdown_pct:.2f}%",
            f"  Sharpe Ratio    : {self.sharpe_ratio:.2f}",
            f"  Profit Factor   : {self.profit_factor:.2f}",
            "-"*50,
            f"  Total Trades    : {self.total_trades}",
            f"  Win Rate        : {self.win_rate:.1%}",
            f"  Avg Win         : ${self.avg_win:.2f}",
            f"  Avg Loss        : ${self.avg_loss:.2f}",
            "="*50,
        ]
        return "\n".join(lines)


class BacktestEngine:
    def __init__(self, strategy: BaseStrategy, config: dict):
        self.strategy = strategy
        self.config = config
        self.capital = config["trading"]["capital"]
        self.risk_pct = config["trading"]["risk_per_trade"]
        self.commission = 0.001  # 0.1% Binance taker fee

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """Run backtest over full DataFrame."""
        logger.info(f"Running backtest: {self.strategy.name} | {len(df)} candles")

        capital = self.capital
        equity_curve = [capital]
        trades = []
        in_trade = False
        current_trade = None

        # Need enough data for indicators (at least 50 candles warmup)
        warmup = 50

        for i in range(warmup, len(df)):
            window = df.iloc[:i+1]
            last = window.iloc[-1]

            # Check if in trade — manage SL/TP
            if in_trade and current_trade:
                price = last["close"]
                hit_sl = hit_tp = False

                if current_trade["side"] == "BUY":
                    hit_sl = last["low"] <= current_trade["sl"]
                    hit_tp = last["high"] >= current_trade["tp"]
                else:
                    hit_sl = last["high"] >= current_trade["sl"]
                    hit_tp = last["low"] <= current_trade["tp"]

                if hit_tp or hit_sl:
                    exit_price = current_trade["tp"] if hit_tp else current_trade["sl"]
                    exit_reason = "TP" if hit_tp else "SL"
                    fee = exit_price * current_trade["qty"] * self.commission
                    trade = Trade(
                        entry_time=current_trade["entry_time"],
                        exit_time=window.index[-1],
                        side=current_trade["side"],
                        entry_price=current_trade["entry"],
                        exit_price=exit_price,
                        qty=current_trade["qty"],
                        stop_loss=current_trade["sl"],
                        take_profit=current_trade["tp"],
                        exit_reason=exit_reason,
                    )
                    trade.pnl -= fee  # subtract commission
                    capital += trade.pnl
                    trades.append(trade)
                    in_trade = False
                    current_trade = None

            # Generate signal only if not in trade
            if not in_trade:
                signal = self.strategy.generate_signal(window)

                if signal.signal in (Signal.BUY, Signal.SELL):
                    qty = self.strategy.calculate_position_size(
                        capital, signal.entry_price, signal.stop_loss, self.risk_pct
                    )
                    if qty > 0:
                        fee = signal.entry_price * qty * self.commission
                        capital -= fee  # entry commission
                        in_trade = True
                        current_trade = {
                            "entry_time": window.index[-1],
                            "side": signal.signal.value,
                            "entry": signal.entry_price,
                            "sl": signal.stop_loss,
                            "tp": signal.take_profit,
                            "qty": qty,
                        }

            equity_curve.append(capital)

        result = BacktestResult(
            trades=trades,
            initial_capital=self.capital,
            final_capital=capital,
            equity_curve=pd.Series(equity_curve),
        )
        print(result.summary())
        return result
