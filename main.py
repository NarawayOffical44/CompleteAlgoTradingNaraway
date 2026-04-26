"""
AlgoTrading Bot — Entry Point
==============================
Usage:
  python main.py backtest    — Run backtest on historical data
  python main.py trade       — Start live/paper trading bot
  python main.py             — Shows this help
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.logger import get_logger
logger = get_logger("main")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    mode = sys.argv[1].lower()

    if mode == "backtest":
        from backtest.run_backtest import run
        run()

    elif mode == "trade":
        from bot.trader import Trader
        config_path = sys.argv[2] if len(sys.argv) > 2 else "config/config.yaml"
        trader = Trader(config_path)
        trader.run()

    else:
        print(f"Unknown mode: {mode}")
        print(__doc__)


if __name__ == "__main__":
    main()
