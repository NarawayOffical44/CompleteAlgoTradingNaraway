"""
Walk-Forward Backtester — No data leakage, production-grade.

Protocol:
  - Splits data into N windows (train + test)
  - Trains on train window, tests on out-of-sample test window
  - Reports per-window + aggregated metrics
  - Default: 252-day train, 63-day test (1yr train, 1Q test), step=63

Usage:
    from backtester.engine import WalkForwardBacktester
    bt = WalkForwardBacktester(strategy_fn, data)
    results = bt.run()
    print(bt.summary())

strategy_fn signature:
    def strategy_fn(train_data: dict, test_data: dict) -> list[float]:
        # train_data: fit your model / calibrate parameters here
        # test_data:  generate out-of-sample signals here
        # return:     list of trade P&L values (rupees) from the test period
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class WindowResult:
    window_id:   int
    train_start: int
    train_end:   int
    test_start:  int
    test_end:    int
    trades:      list          # list of net pnl values (after costs)
    cagr:        float = 0.0
    sharpe:      float = 0.0
    max_dd:      float = 0.0
    win_rate:    float = 0.0
    profit_factor: float = 0.0
    n_trades:    int   = 0


@dataclass
class BacktestResult:
    windows:           list[WindowResult] = field(default_factory=list)
    aggregated_trades: list               = field(default_factory=list)
    cagr:              float = 0.0
    sharpe:            float = 0.0
    max_dd:            float = 0.0
    win_rate:          float = 0.0
    profit_factor:     float = 0.0
    consistency:       float = 0.0   # % of windows with positive returns
    go_nogo:           str   = "NO_GO"


class WalkForwardBacktester:

    def __init__(self, strategy_fn: Callable, data: dict,
                 train_bars: int = 252, test_bars: int = 63, step: int = 63,
                 starting_capital: float = 100_000,
                 cost_per_trade: float = 0.0):
        """
        strategy_fn:      Callable[[train_data, test_data], list[float]]
        data:             dict with at minimum {"closes": list, ...}
        train_bars:       number of bars in each training window (default 252 = 1yr)
        test_bars:        number of bars in each test window (default 63 = 1Q)
        step:             bars to advance each window (default 63)
        starting_capital: portfolio starting value (used for CAGR calculation)
        cost_per_trade:   transaction cost per trade in rupees (brokerage + slippage).
                          Deducted from each P&L before metrics are computed.
                          Typical: 40-100 for NSE equity, 100-200 for F&O.
        """
        self.strategy_fn      = strategy_fn
        self.data             = data
        self.train_bars       = train_bars
        self.test_bars        = test_bars
        self.step             = step
        self.starting_capital = starting_capital
        self.cost_per_trade   = cost_per_trade
        self.result: BacktestResult = None

    def run(self) -> BacktestResult:
        closes = self.data.get("closes", [])
        n      = len(closes)
        result = BacktestResult()

        idx = 0
        wid = 0
        while (idx + self.train_bars + self.test_bars) <= n:
            train_s = idx
            train_e = idx + self.train_bars
            test_s  = train_e
            test_e  = test_s + self.test_bars

            train_slice = {k: v[train_s:train_e] if isinstance(v, list) else v
                           for k, v in self.data.items()}
            test_slice  = {k: v[test_s:test_e] if isinstance(v, list) else v
                           for k, v in self.data.items()}

            raw_trades = self.strategy_fn(train_slice, test_slice)

            # Deduct transaction costs from each trade
            trades = [t - self.cost_per_trade for t in raw_trades] if self.cost_per_trade > 0 else raw_trades

            wr = WindowResult(
                window_id=wid, train_start=train_s, train_end=train_e,
                test_start=test_s, test_end=test_e, trades=trades,
            )
            if trades:
                test_n_bars   = test_e - test_s
                equity        = self._equity_curve(trades)
                wr.cagr       = self._cagr(equity, test_n_bars, self.starting_capital)
                wr.sharpe     = self._sharpe(trades)
                wr.max_dd     = self._max_drawdown(equity)
                wins          = [t for t in trades if t > 0]
                losses        = [t for t in trades if t <= 0]
                wr.win_rate      = len(wins) / len(trades)
                wr.profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
                wr.n_trades      = len(trades)

            result.windows.append(wr)
            result.aggregated_trades.extend(trades)
            idx += self.step
            wid += 1

        self._aggregate(result)
        self.result = result
        return result

    def summary(self) -> str:
        if not self.result:
            return "Run backtester first"
        r = self.result
        cost_note = f" | cost/trade=₹{self.cost_per_trade:.0f}" if self.cost_per_trade else ""
        lines = [
            f"Walk-Forward Summary ({len(r.windows)} windows{cost_note})",
            f"  CAGR:          {r.cagr:>8.1f}%",
            f"  Sharpe:        {r.sharpe:>8.2f}",
            f"  Max Drawdown:  {r.max_dd:>8.1f}%",
            f"  Win Rate:      {r.win_rate:>8.1f}%",
            f"  Profit Factor: {r.profit_factor:>8.2f}",
            f"  Consistency:   {r.consistency:>8.1f}%  (windows profitable)",
            f"  Decision:      {r.go_nogo}",
        ]
        return "\n".join(lines)

    # ── Aggregation ───────────────────────────────────────────────────────
    def _aggregate(self, result: BacktestResult):
        all_trades = result.aggregated_trades
        if not all_trades:
            result.go_nogo = "NO_GO (no trades)"
            return

        equity      = self._equity_curve(all_trades)
        total_bars  = sum(w.test_end - w.test_start for w in result.windows)
        result.cagr   = self._cagr(equity, total_bars, self.starting_capital)
        result.sharpe = self._sharpe(all_trades)
        result.max_dd = self._max_drawdown(equity)

        wins   = [t for t in all_trades if t > 0]
        losses = [t for t in all_trades if t <= 0]
        result.win_rate      = len(wins) / len(all_trades) * 100
        result.profit_factor = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
        result.consistency   = (sum(1 for w in result.windows if w.cagr > 0) / len(result.windows) * 100
                                 if result.windows else 0)

        # Go/No-Go gate
        if (result.cagr > 15 and result.sharpe > 0.8 and
                result.max_dd < 20 and result.consistency > 55):
            result.go_nogo = "GO"
        elif result.cagr > 8 and result.sharpe > 0.5 and result.max_dd < 25:
            result.go_nogo = "CONDITIONAL (reduce size)"
        else:
            result.go_nogo = "NO_GO"

    # ── Metrics ───────────────────────────────────────────────────────────
    @staticmethod
    def _equity_curve(pnls: list) -> list:
        equity = [0.0]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)
        return equity

    @staticmethod
    def _cagr(equity: list, n_bars: int, starting_capital: float = 100_000) -> float:
        """
        CAGR = ((1 + total_return) ^ (1/years)) - 1
        total_return = cumulative_pnl / starting_capital
        """
        if n_bars <= 0 or starting_capital <= 0:
            return 0.0
        years = n_bars / 252
        if years <= 0:
            return 0.0
        total_return = equity[-1] / starting_capital
        return round(((1 + total_return) ** (1 / years) - 1) * 100, 2)

    @staticmethod
    def _sharpe(pnls: list, risk_free: float = 0.07) -> float:
        if len(pnls) < 2:
            return 0.0
        a        = np.array(pnls, dtype=float)
        daily_rf = risk_free / 252
        excess   = a - daily_rf * abs(np.mean(a)) if np.mean(a) != 0 else a
        std      = np.std(excess)
        return round((np.mean(excess) / std) * np.sqrt(252), 2) if std > 0 else 0.0

    @staticmethod
    def _max_drawdown(equity: list) -> float:
        e    = np.array(equity, dtype=float)
        peak = np.maximum.accumulate(e)
        dd   = (e - peak) / (np.abs(peak) + 1e-9) * 100
        return round(float(np.min(dd)), 2)
