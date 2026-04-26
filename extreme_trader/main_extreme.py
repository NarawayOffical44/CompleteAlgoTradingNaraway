"""Entry point for Extreme Trader - Works with any strategy"""

import asyncio
import sys
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from extreme_trader.extreme_trader import ExtremeTrader
from strategies.extreme_scalp import ExtremeScalpStrategy
from strategies.ema_crossover import EMACrossoverStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.short_straddle_930 import ShortStraddleStrategy
from utils.logger import get_logger

logger = get_logger(__name__)


async def main():
    logger.info("\n")
    logger.info("*" * 70)
    logger.info("EXTREME LEVERAGE TRADING BOT")
    logger.info("*" * 70)
    logger.warning("WARNING: HIGH RISK")
    logger.warning("TEST ON PAPER TRADING FIRST!")
    logger.warning("ONE BAD TRADE = ACCOUNT WIPED")
    logger.info("*" * 70)
    logger.info("")

    try:
        # Load config
        config_path = Path("extreme_trader/extreme_config.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Instantiate strategy based on config
        strategy_name = config.get("strategy", "extreme_scalp")

        strategies = {
            "extreme_scalp": ExtremeScalpStrategy,
            "ema_crossover": EMACrossoverStrategy,
            "mean_reversion": MeanReversionStrategy,
            "short_straddle_930": ShortStraddleStrategy,
        }

        if strategy_name not in strategies:
            logger.error(f"Unknown strategy: {strategy_name}")
            logger.info(f"Available: {', '.join(strategies.keys())}")
            return

        strategy = strategies[strategy_name](config)

        # Create trader with strategy
        trader = ExtremeTrader(strategy, config_path="extreme_trader/extreme_config.yaml")
        await trader.run()
    except KeyboardInterrupt:
        logger.info("System stopped")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
