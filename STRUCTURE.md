# Project Structure

```
AlgoTrading/
│
├── core/                        ← NEVER TOUCH — stable engine
│   ├── base_strategy.py         │  Base class all strategies inherit
│   ├── backtest_engine.py       │  Backtesting logic + metrics
│   ├── exchange.py              │  ccxt Binance connector
│   └── logger.py                │  Logging setup
│
├── strategies/
│   ├── live/                    ← PROVEN — currently deployed
│   │   ├── ema_crossover.py     │  Trend following (EMA + RSI)
│   │   └── mean_reversion.py    │  Bollinger Band + RSI
│   │
│   ├── lab/                     ← EXPERIMENTAL — test here first
│   │   └── TEMPLATE.py          │  Copy this to build new strategy
│   │
│   └── archive/                 ← FAILED — kept for learning
│       └── README.md            │  Log of why each failed
│
├── config/
│   ├── live/config.yaml         ← Settings for live/paper trading
│   ├── lab/                     ← Separate configs for experiments
│   └── .env.example             ← API keys template (copy to .env)
│
├── results/
│   ├── live/                    ← Backtest results of live strategies
│   ├── lab/                     ← Backtest results of experiments
│   └── archive/                 ← Results of failed strategies
│
├── data/historical/             ← Cached OHLCV data (auto-downloaded)
├── logs/                        ← Daily log files
│
├── backtest/run_backtest.py     ← Run backtests
├── bot/trader.py                ← Live trading loop
├── main.py                      ← Entry point
├── promote.py                   ← Move strategies between live/lab/archive
│
├── INNOVATIONS.md               ← Auto-updated innovation log
└── STRUCTURE.md                 ← This file
```

## The Golden Rule

> **Never test in live/. Always test in lab/ first.**

## Workflow

1. New idea → create in `strategies/lab/`
2. Backtest → save results to `results/lab/`
3. Passes criteria → `python promote.py lab→live strategy.py`
4. Fails → `python promote.py lab→archive strategy.py` + add note to archive README
5. Live strategy stops working → `python promote.py live→archive strategy.py`

## Promotion Criteria (lab → live)

- Profit factor > 1.3
- Win rate > 45%
- Max drawdown < 15%
- Min 30 trades in backtest
- Tested on 6+ months of data
