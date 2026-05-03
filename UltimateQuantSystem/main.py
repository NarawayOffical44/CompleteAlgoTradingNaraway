"""
UltimateQuantSystem — Self-Governing Bot School.

Bots (11 total):
  NSE (6):    pairs_trading, mean_reversion, momentum, momentum_scalper,
              NIFTY (iron condor), banknifty_straddle
  CRYPTO (1): crypto_momentum  — BTC/ETH/BNB/SOL/AVAX/FET/RNDR/TAO/INJ/WIF
  FOREX  (2): forex_momentum, forex_mean_rev
  SOLANA (1): meme_sniper      — new Solana memecoins, 2-min polling
  PERP   (1): perp_futures     — 3x leveraged altcoin perps on Binance

Usage:
  python main.py            # all bots + dashboard
  python main.py --test     # single tick all bots, then exit
  python main.py --train    # train HMM + LightGBM
  python main.py --live     # live mode (YES confirmation)
"""

import sys
import os
import time
from loguru import logger

from config import config
from notify import notify
from risk import RiskEngine
from journal import TradeJournal
from broker import DhanClient

from agents import (
    PairsTradingAgent, MeanReversionAgent,
    MomentumAgent, MomentumScalper, OptionsBot,
    CryptoMomentumBot,
    ForexMomentumBot, ForexMeanReversionBot,
    BankNiftyStraddleBot,
    MemeSniper,
    PerpFuturesBot,
    PolymarketBot,
)

from markets import (
    NSEMarket, CryptoMarket, ForexMarket,
    SolanaMarket, BinancePerpMarket, PolymarketMarket,
)
from bots import BotRunner
from registry import BotRegistry
from head_ai import HeadAI
from dashboard import TerminalDashboard
from ai.auto_trainer import AutoTrainer


logger.add(
    "logs/ultimate_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="90 days", level="INFO",
    format="{time:HH:mm:ss} | {level} | {message}",
)

if os.name == "nt":
    os.system("")


def boot():
    if "--live" in sys.argv:
        confirm = input("LIVE MODE — real orders will be placed. Type YES to confirm: ")
        if confirm.strip() != "YES":
            sys.exit(0)
        config.trading_mode = "live"

    logger.info(f"UltimateQuantSystem | mode={config.trading_mode} | capital=Rs{config.starting_capital:,.0f}")
    notify(
        f"<b>UltimateQuantSystem STARTED</b>\n"
        f"Mode: <code>{config.trading_mode.upper()}</code>  "
        f"Capital: <b>Rs{config.starting_capital:,.0f}</b>"
    )

    risk    = RiskEngine(starting_capital=config.starting_capital)
    journal = TradeJournal(journal_dir="logs")
    broker  = DhanClient()

    # ── Markets ───────────────────────────────────────────────────────────
    nse        = NSEMarket()
    crypto     = CryptoMarket()       # 24/7  — Binance 4h
    forex      = ForexMarket()        # 24/5  — Yahoo Finance 1h
    solana     = SolanaMarket()       # 24/7  — DexScreener 90s
    perp_mkt   = BinancePerpMarket()  # 24/7  — Binance Futures 4h
    poly_mkt   = PolymarketMarket()   # 24/7  — Polymarket CLOB + Gamma

    # ── NSE agents ────────────────────────────────────────────────────────
    pairs        = PairsTradingAgent(  agent_id="pairs_trading",    risk_engine=risk, journal=journal, broker=broker)
    meanrev      = MeanReversionAgent( agent_id="mean_reversion",   risk_engine=risk, journal=journal, broker=broker)
    momentum     = MomentumAgent(      agent_id="momentum",         risk_engine=risk, journal=journal, broker=broker)
    scalper      = MomentumScalper(    agent_id="momentum_scalper", risk_engine=risk, journal=journal, broker=broker)
    bnk_straddle = BankNiftyStraddleBot(risk_engine=risk, journal=journal, broker=broker)

    options_bot = None
    if config.starting_capital >= 25000:
        options_bot = OptionsBot(risk_engine=risk, journal=journal, broker=broker, underlying="NIFTY")

    # ── Registry ──────────────────────────────────────────────────────────
    registry = BotRegistry()

    # NSE — 15-min interval
    registry.register(BotRunner(agent=pairs,    market=nse, risk_engine=risk))
    registry.register(BotRunner(agent=meanrev,  market=nse, risk_engine=risk))
    registry.register(BotRunner(agent=momentum, market=nse, risk_engine=risk))
    registry.register(BotRunner(agent=scalper,  market=nse, risk_engine=risk))
    registry.register(BotRunner(
        agent=bnk_straddle, market=nse, risk_engine=risk,
        run_fn=lambda a, d, r: a.run(regime=r, market_data=d),
    ))
    if options_bot:
        registry.register(BotRunner(
            agent=options_bot, market=nse, risk_engine=risk,
            run_fn=lambda a, d, r: a.run(regime=r, market_data=d),
        ))

    # Crypto — 15-min interval, 10 symbols
    crypto_bot = CryptoMomentumBot(agent_id="crypto_momentum", risk_engine=risk, journal=journal, broker=broker)
    registry.register(BotRunner(agent=crypto_bot, market=crypto, risk_engine=risk))

    # Forex — 15-min interval
    registry.register(BotRunner(agent=ForexMomentumBot(    agent_id="forex_momentum", risk_engine=risk, journal=journal, broker=broker), market=forex, risk_engine=risk))
    registry.register(BotRunner(agent=ForexMeanReversionBot(agent_id="forex_mean_rev", risk_engine=risk, journal=journal, broker=broker), market=forex, risk_engine=risk))

    # Solana meme sniper — 2-min interval
    registry.register(BotRunner(
        agent=MemeSniper(agent_id="meme_sniper", risk_engine=risk, journal=journal, broker=broker),
        market=solana, risk_engine=risk,
        interval_s=120,
    ))

    # Perp futures — 3x leverage, 15-min interval
    registry.register(BotRunner(
        agent=PerpFuturesBot(agent_id="perp_futures", risk_engine=risk, journal=journal, broker=broker, leverage=3),
        market=perp_mkt, risk_engine=risk,
    ))

    # Polymarket — prediction markets, 90s polling
    registry.register(BotRunner(
        agent=PolymarketBot(agent_id="polymarket_bot", risk_engine=risk, journal=journal, broker=broker),
        market=poly_mkt, risk_engine=risk,
        interval_s=90,
    ))

    head_ai = HeadAI(
        registry=registry,
        anthropic_api_key=config.anthropic_api_key,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    auto_trainer = AutoTrainer(nse_market=nse, mean_rev_agent=meanrev, notify_fn=notify)

    return registry, head_ai, auto_trainer, risk, journal, nse


def run_test(registry, head_ai, auto_trainer, risk, journal, nse):
    logger.info("TEST MODE — single cycle for all bots")
    for agent_id in registry.bot_ids():
        runner = registry._runners[agent_id]
        try:
            runner._tick()
            logger.info(f"{agent_id} | status={runner.status}")
        except Exception as e:
            logger.error(f"{agent_id} | error: {e}")
    report = head_ai.analyze()
    logger.info(f"HeadAI: {report.get('insights', [])}")
    logger.info(f"Risk: {risk.status()}")


def run_train(nse, auto_trainer):
    auto_trainer._run_cycle(reason="manual_train")


def main():
    registry, head_ai, auto_trainer, risk, journal, nse = boot()

    if "--train" in sys.argv:
        run_train(nse, auto_trainer)
        return

    if "--test" in sys.argv:
        run_test(registry, head_ai, auto_trainer, risk, journal, nse)
        return

    registry.start_all()
    head_ai.start()
    auto_trainer.start()

    time.sleep(30)
    head_ai.analyze()

    dashboard = TerminalDashboard(
        registry=registry, head_ai=head_ai,
        risk_engine=risk, refresh_s=30,
    )

    try:
        dashboard.run()
    finally:
        registry.stop_all()
        head_ai.stop()
        auto_trainer.stop()
        risk.end_of_day()
        summary = journal.summary()
        if summary.get("trades", 0) > 0:
            journal.export_json()
        logger.info("UltimateQuantSystem stopped.")


if __name__ == "__main__":
    try:
        main()
        notify("<b>UltimateQuantSystem stopped</b> (clean exit)")
    except KeyboardInterrupt:
        notify("<b>UltimateQuantSystem stopped</b> (Ctrl+C)")
    except Exception as e:
        notify(f"<b>UltimateQuantSystem CRASHED</b>\n<code>{e}</code>")
        raise
