"""
UltimateQuantSystem — Entry point.

Architecture:
  BotRegistry manages N BotRunners, each in its own thread.
  Each BotRunner wraps one agent + one market (pluggable).
  HeadAI (Claude) monitors all bots, ranks by performance, sends Telegram alerts.
  TerminalDashboard shows live status.

Usage:
  python main.py            # Start all bots + dashboard (default)
  python main.py --test     # Single cycle for all bots, then exit
  python main.py --train    # Train HMM, then exit
  python main.py --live     # Live mode (requires YES confirmation)

To add a new bot:
  1. Create agents/your_agent.py
  2. Instantiate it in boot() below
  3. registry.register(BotRunner(agent=your_agent, market=your_market, risk_engine=risk))
  Done.

To add a new market (Crypto, Forex, MCX):
  1. Create markets/your_market.py (inherit BaseMarket)
  2. Instantiate it in boot() below
  3. Create BotRunners pointing to that market
  Done. NSE bots are untouched.
"""

import sys
import os
import time
from loguru import logger

from config import config
from risk import RiskEngine
from journal import TradeJournal
from broker import DhanClient

from agents import (
    PairsTradingAgent, MeanReversionAgent,
    MomentumAgent, MomentumScalper, OptionsBot,
    CryptoMomentumBot,
)

from markets import NSEMarket, CryptoMarket
from bots import BotRunner
from registry import BotRegistry
from head_ai import HeadAI
from dashboard import TerminalDashboard


# ── Logging ───────────────────────────────────────────────────────────────────
logger.add(
    "logs/ultimate_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="90 days",
    level="INFO",
    format="{time:HH:mm:ss} | {level} | {message}",
)

if os.name == "nt":
    os.system("")   # Enable ANSI on Windows


# ── Boot ──────────────────────────────────────────────────────────────────────
def boot():
    if "--live" in sys.argv:
        confirm = input("LIVE MODE — real orders will be placed. Type YES to confirm: ")
        if confirm.strip() != "YES":
            logger.info("Live mode cancelled.")
            sys.exit(0)
        config.trading_mode = "live"

    logger.info(f"UltimateQuantSystem | mode={config.trading_mode} | capital=₹{config.starting_capital:,.0f}")

    # ── Shared infrastructure ──────────────────────────────────────────────
    risk    = RiskEngine(starting_capital=config.starting_capital)
    journal = TradeJournal(journal_dir="logs")
    broker  = DhanClient()

    # ── Markets ───────────────────────────────────────────────────────────
    nse    = NSEMarket()
    crypto = CryptoMarket()     # 24/7 — runs alongside NSE, never waits

    # ── NSE Agents ────────────────────────────────────────────────────────
    pairs    = PairsTradingAgent(  agent_id="pairs_trading",    risk_engine=risk, journal=journal, broker=broker)
    meanrev  = MeanReversionAgent( agent_id="mean_reversion",   risk_engine=risk, journal=journal, broker=broker)
    momentum = MomentumAgent(      agent_id="momentum",         risk_engine=risk, journal=journal, broker=broker)
    scalper  = MomentumScalper(    agent_id="momentum_scalper", risk_engine=risk, journal=journal, broker=broker)

    options_bot = None
    if config.starting_capital >= 25000:
        options_bot = OptionsBot(
            risk_engine=risk, journal=journal, broker=broker, underlying="NIFTY",
        )

    # ── BotRegistry — each bot runs in its own thread ─────────────────────
    registry = BotRegistry()

    registry.register(BotRunner(agent=pairs,    market=nse, risk_engine=risk))
    registry.register(BotRunner(agent=meanrev,  market=nse, risk_engine=risk))
    registry.register(BotRunner(agent=momentum, market=nse, risk_engine=risk))
    registry.register(BotRunner(agent=scalper,  market=nse, risk_engine=risk))

    if options_bot:
        # OptionsBot has a different run() signature — handled via run_fn
        registry.register(BotRunner(
            agent=options_bot,
            market=nse,
            risk_engine=risk,
            run_fn=lambda a, data, regime: a.run(regime=regime, market_data=data),
        ))

    # ── Crypto Bot (24/7 — never blocked by market hours) ─────────────────
    crypto_bot = CryptoMomentumBot(
        agent_id="crypto_momentum",
        risk_engine=risk,
        journal=journal,
        broker=broker,      # paper mode — logs only, no real Binance orders
    )
    registry.register(BotRunner(agent=crypto_bot, market=crypto, risk_engine=risk))

    # ── HeadAI — Claude monitors + ranks all bots, sends Telegram alerts ──
    head_ai = HeadAI(
        registry=registry,
        anthropic_api_key=config.anthropic_api_key,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    return registry, head_ai, risk, journal, nse


# ── Test mode — single manual tick for all bots ───────────────────────────────
def run_test(registry, head_ai, risk, journal, nse):
    logger.info("TEST MODE — single cycle for all bots")

    market_data = nse.get_data()
    regime      = nse.get_regime(market_data)
    logger.info(f"Regime: {regime} | Market open: {nse.is_open()}")

    for agent_id in registry.bot_ids():
        runner = registry._runners[agent_id]
        try:
            runner._tick()
            logger.info(f"{agent_id} | tick done | status={runner.status}")
        except Exception as e:
            logger.error(f"{agent_id} | tick error: {e}")

    report = head_ai.analyze()
    logger.info(f"HeadAI insights: {report['insights']}")
    logger.info(f"Risk: {risk.status()}")


# ── Train HMM ─────────────────────────────────────────────────────────────────
def run_train(nse):
    logger.info("TRAIN MODE — training HMM on historical Nifty data")
    data   = nse.market_fetcher.get_market_data(["NIFTY"])
    closes = data.get("NIFTY", {}).get("closes", [])
    if len(closes) >= 60:
        nse.regime_hmm.fit(closes)
        logger.info(f"HMM trained on {len(closes)} days")
    else:
        logger.error(f"Not enough data: {len(closes)} days (need 60+)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    registry, head_ai, risk, journal, nse = boot()

    if "--train" in sys.argv:
        run_train(nse)
        return

    if "--test" in sys.argv:
        run_test(registry, head_ai, risk, journal, nse)
        return

    # ── Default: start all bots in parallel + HeadAI + dashboard ──────────
    logger.info("Starting all bots in parallel threads...")
    registry.start_all()

    logger.info("Starting HeadAI monitor...")
    head_ai.start()

    # Give bots 30s to complete first tick before dashboard renders
    time.sleep(30)
    head_ai.analyze()   # immediate first analysis

    dashboard = TerminalDashboard(
        registry=registry,
        head_ai=head_ai,
        risk_engine=risk,
        refresh_s=30,
    )

    try:
        dashboard.run()     # blocks until Ctrl+C
    finally:
        logger.info("Shutting down...")
        registry.stop_all()
        head_ai.stop()
        risk.end_of_day()
        summary = journal.summary()
        if summary.get("trades", 0) > 0:
            logger.info(f"Session summary: {summary}")
            journal.export_json()
        logger.info("UltimateQuantSystem stopped.")


if __name__ == "__main__":
    main()
