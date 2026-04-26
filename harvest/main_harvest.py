"""
HARVEST TRADING SYSTEM - Entry Point
====================================

Run SEPARATE from main.py
This system runs independently:
  - F&O trading with fixed ₹1000
  - Profits harvested to Forex tier
  - Both tiers run simultaneously

Usage:
  venv/Scripts/python.exe harvest/main_harvest.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvest.harvest_trader import HarvestTrader
from utils.logger import get_logger

logger = get_logger(__name__)


async def main():
    """Main entry point"""
    logger.info("Harvest System: F&O (1000 fixed) + Forex (from harvests)")
    logger.info("Config: harvest/harvest_config.yaml")
    logger.info("Ctrl+C to stop")

    try:
        trader = HarvestTrader()
        await trader.run()
    except KeyboardInterrupt:
        logger.info("Harvest system stopped")


if __name__ == "__main__":
    asyncio.run(main())
