"""
Monte Carlo Simulator — Bootstrap resampling of trade P&L.

Resamples with replacement from observed trade P&Ls to estimate the
distribution of possible outcomes. Quantifies tail risk and validates
the strategy is robust (not just lucky).

IMPORTANT ASSUMPTIONS:
  1. Trade independence: each trade's P&L is treated as independent of
     prior trades. In reality, correlated regimes can create streaks.
     Results are optimistic if strategy trades heavily in similar regimes.
  2. trade_bars approximation: CAGR annualisation uses average trade
     duration. If actual holding periods vary widely, CAGR estimates are
     approximate. Use the walk-forward CAGR for primary evaluation.
  3. No correlation modelling between simultaneous positions (e.g., paired
     trades or multi-leg options). Treat MC as a rough tail-risk validator,
     not an exact prediction.

Typical workflow:
    # 1. Walk-forward backtest first
    bt = WalkForwardBacktester(strategy_fn, data)
    wf_result = bt.run()
    if wf_result.go_nogo != "GO":
        return  # Do NOT run MC or go live

    # 2. Monte Carlo robustness check
    mc = MonteCarloSimulator(trade_pnls=wf_result.aggregated_trades)
    mc_result = mc.run(n_sims=5000)
    print(mc_result.summary())
    if mc_result.go_nogo != "GO":
        return  # Strategy too risky

    # 3. Only then consider live deployment

Usage:
    mc = MonteCarloSimulator(trade_pnls, starting_capital=100_000)
    result = mc.run(n_sims=5000)
    print(result.summary())
    print("Go?", result.go_nogo)
"""

import numpy as np
from dataclasses import dataclass


@dataclass
class MCResult:
    n_sims:     int
    n_trades:   int

    # CAGR distribution (annualised, %)
    cagr_p5:    float
    cagr_p50:   float
    cagr_p95:   float

    # Sharpe distribution
    sharpe_p5:  float
    sharpe_p50: float
    sharpe_p95: float

    # Max drawdown distribution (%)
    maxdd_p5:   float
    maxdd_p50:  float
    maxdd_p95:  float

    # Tail risks
    ruin_prob:       float   # P(terminal equity < 50% of start)
    neg_cagr_prob:   float   # P(CAGR < 0)
    severe_dd_prob:  float   # P(MaxDD > 20%)

    # Decision
    go_nogo: str

    def summary(self) -> str:
        lines = [
            f"Monte Carlo ({self.n_sims:,} simulations | {self.n_trades} trades)",
            "",
            f"  CAGR      P5={self.cagr_p5:>6.1f}%  P50={self.cagr_p50:>6.1f}%  P95={self.cagr_p95:>6.1f}%",
            f"  Sharpe    P5={self.sharpe_p5:>6.2f}   P50={self.sharpe_p50:>6.2f}   P95={self.sharpe_p95:>6.2f}",
            f"  MaxDD     P5={self.maxdd_p5:>6.1f}%  P50={self.maxdd_p50:>6.1f}%  P95={self.maxdd_p95:>6.1f}%",
            "",
            f"  Ruin probability  (equity < 50%):  {self.ruin_prob * 100:>5.1f}%",
            f"  Negative CAGR probability:          {self.neg_cagr_prob * 100:>5.1f}%",
            f"  Severe drawdown   (MaxDD > 20%):    {self.severe_dd_prob * 100:>5.1f}%",
            "",
            f"  Decision: {self.go_nogo}",
        ]
        return "\n".join(lines)


class MonteCarloSimulator:

    GO_NOGO_THRESHOLDS = {
        "cagr_p5_min":     5.0,   # worst-case 5th pct CAGR must be > 5%
        "sharpe_p5_min":   0.4,   # worst-case Sharpe > 0.4
        "maxdd_p5_max":   30.0,   # worst-case MaxDD must be < 30%
        "ruin_max":        0.05,  # ruin < 5%
        "neg_cagr_max":    0.25,  # P(CAGR<0) < 25%
    }

    def __init__(self, trade_pnls: list, starting_capital: float = 100_000,
                 trade_bars: float = 5.0, seed: int = 42):
        """
        trade_pnls:       list of observed trade P&L values (rupees, net of costs)
        starting_capital: portfolio starting value
        trade_bars:       average trade duration in calendar days.
                          Approximate — actual holding periods may vary.
                          Used for CAGR annualisation only.
                          Typical values: pairs=15, mean_rev=8, momentum=30, options=7
        seed:             random seed for reproducibility
        """
        self.pnls       = np.array(trade_pnls, dtype=float)
        self.capital    = starting_capital
        self.trade_bars = trade_bars
        self.rng        = np.random.default_rng(seed)

    def run(self, n_sims: int = 5000) -> MCResult:
        n_trades = len(self.pnls)
        if n_trades < 5:
            return self._empty_result(n_sims, n_trades, "NO_GO (insufficient trades)")

        # Vectorised bootstrap: (n_sims, n_trades)
        indices  = self.rng.integers(0, n_trades, size=(n_sims, n_trades))
        sim_pnls = self.pnls[indices]                          # (n_sims, n_trades)

        # Equity curves: (n_sims, n_trades)
        sim_equity   = np.cumsum(sim_pnls, axis=1)
        total_days   = n_trades * self.trade_bars
        years        = total_days / 252

        # CAGR per simulation — standard formula: (1 + return)^(1/years) - 1
        final_equity = sim_equity[:, -1]
        total_return = final_equity / self.capital              # fractional return
        sim_cagr     = ((1 + total_return) ** (1 / years) - 1) * 100 if years > 0 else np.zeros(n_sims)

        # Sharpe per simulation (trade-level, annualised)
        sim_std    = np.std(sim_pnls, axis=1, ddof=1) + 1e-9
        sim_mean   = np.mean(sim_pnls, axis=1)
        sim_sharpe = (sim_mean / sim_std) * np.sqrt(252 / self.trade_bars)

        # Max drawdown per simulation
        peak      = np.maximum.accumulate(sim_equity, axis=1)
        drawdowns = (sim_equity - peak) / (np.abs(peak) + 1e-9) * 100
        sim_maxdd = np.min(drawdowns, axis=1)

        # Tail risks
        ruin_threshold = -self.capital * 0.5
        ruin_prob      = float(np.mean(final_equity < ruin_threshold))
        neg_cagr_prob  = float(np.mean(sim_cagr < 0))
        severe_dd_prob = float(np.mean(sim_maxdd < -20))

        result = MCResult(
            n_sims=n_sims, n_trades=n_trades,
            cagr_p5=float(np.percentile(sim_cagr, 5)),
            cagr_p50=float(np.percentile(sim_cagr, 50)),
            cagr_p95=float(np.percentile(sim_cagr, 95)),
            sharpe_p5=float(np.percentile(sim_sharpe, 5)),
            sharpe_p50=float(np.percentile(sim_sharpe, 50)),
            sharpe_p95=float(np.percentile(sim_sharpe, 95)),
            maxdd_p5=float(np.percentile(sim_maxdd, 5)),
            maxdd_p50=float(np.percentile(sim_maxdd, 50)),
            maxdd_p95=float(np.percentile(sim_maxdd, 95)),
            ruin_prob=ruin_prob,
            neg_cagr_prob=neg_cagr_prob,
            severe_dd_prob=severe_dd_prob,
            go_nogo="",
        )
        result.go_nogo = self._gate(result)
        return result

    def _gate(self, r: MCResult) -> str:
        t = self.GO_NOGO_THRESHOLDS
        if (r.cagr_p5       >= t["cagr_p5_min"]  and
                r.sharpe_p5 >= t["sharpe_p5_min"] and
                r.maxdd_p5  >= -t["maxdd_p5_max"] and
                r.ruin_prob <= t["ruin_max"]       and
                r.neg_cagr_prob <= t["neg_cagr_max"]):
            return "GO"
        elif (r.cagr_p5 >= 0 and r.ruin_prob <= 0.10 and r.maxdd_p5 >= -40):
            return "CONDITIONAL (reduce position size by 50%)"
        else:
            return "NO_GO"

    @staticmethod
    def _empty_result(n_sims, n_trades, reason) -> MCResult:
        return MCResult(
            n_sims=n_sims, n_trades=n_trades,
            cagr_p5=0, cagr_p50=0, cagr_p95=0,
            sharpe_p5=0, sharpe_p50=0, sharpe_p95=0,
            maxdd_p5=0, maxdd_p50=0, maxdd_p95=0,
            ruin_prob=1, neg_cagr_prob=1, severe_dd_prob=1,
            go_nogo=reason,
        )
