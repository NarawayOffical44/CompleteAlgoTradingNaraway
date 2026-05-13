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
  python main.py --headless # all bots without terminal dashboard
  python main.py --live     # live mode (YES confirmation)
"""

import sys
import os
import time
import threading
from html import escape
from loguru import logger

from config import config
from notify import notify
from risk import RiskEngine
from journal import TradeJournal
from broker import build_broker_router
from resources import SharedResourceHub

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
from telegram_control import TelegramControlBot


logger.add(
    "logs/ultimate_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="90 days", level="INFO",
    format="{time:HH:mm:ss} | {level} | {message}",
)

if os.name == "nt":
    os.system("")


def boot():
    if "--live" in sys.argv:
        confirm = os.getenv("LIVE_CONFIRM", "")
        if confirm != "YES":
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

    risk      = RiskEngine(starting_capital=config.starting_capital)
    journal   = TradeJournal(journal_dir="logs")
    resources = SharedResourceHub()
    broker    = build_broker_router()

    # ── Markets ───────────────────────────────────────────────────────────
    nse        = NSEMarket(resources=resources)
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

    def runner(agent, market, **kwargs):
        return BotRunner(agent=agent, market=market, risk_engine=risk, resources=resources, **kwargs)

    # NSE — 15-min interval
    registry.register(runner(pairs,    nse))
    registry.register(runner(meanrev,  nse))
    registry.register(runner(momentum, nse))
    registry.register(runner(scalper,  nse))
    registry.register(runner(
        bnk_straddle, nse,
        run_fn=lambda a, d, r: a.run(regime=r, market_data=d),
    ))
    if options_bot:
        registry.register(runner(
            options_bot, nse,
            run_fn=lambda a, d, r: a.run(regime=r, market_data=d),
        ))

    # Crypto — 15-min interval, 10 symbols
    crypto_bot = CryptoMomentumBot(agent_id="crypto_momentum", risk_engine=risk, journal=journal, broker=broker)
    registry.register(runner(crypto_bot, crypto))

    # Forex — 15-min interval
    registry.register(runner(ForexMomentumBot(    agent_id="forex_momentum", risk_engine=risk, journal=journal, broker=broker), forex))
    registry.register(runner(ForexMeanReversionBot(agent_id="forex_mean_rev", risk_engine=risk, journal=journal, broker=broker), forex))

    # Solana meme sniper — 2-min interval
    registry.register(runner(
        MemeSniper(agent_id="meme_sniper", risk_engine=risk, journal=journal, broker=broker),
        solana,
        interval_s=120,
    ))

    # Perp futures — 3x leverage, 15-min interval
    registry.register(runner(
        PerpFuturesBot(agent_id="perp_futures", risk_engine=risk, journal=journal, broker=broker, leverage=3),
        perp_mkt,
    ))

    # Polymarket — prediction markets, 90s polling
    registry.register(runner(
        PolymarketBot(agent_id="polymarket_bot", risk_engine=risk, journal=journal, broker=broker),
        poly_mkt,
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


def start_hosted_status_reporter(registry, risk, journal):
    interval_min = int(os.getenv("STATUS_REPORT_INTERVAL_MIN", "60"))
    if interval_min <= 0:
        return None

    stop_event = threading.Event()

    def loop():
        while not stop_event.wait(interval_min * 60):
            notify(format_hosted_status_report(registry, risk, journal))

    thread = threading.Thread(target=loop, name="hosted-status", daemon=True)
    thread.start()
    return stop_event


def format_hosted_status_report(registry, risk, journal) -> str:
    rs = risk.status()
    portfolio = journal.summary()
    bot_metrics = registry.status_all()
    open_trades = journal.open_trades()

    ranked = sorted(
        bot_metrics,
        key=lambda m: (m.get("total_pnl", 0), m.get("win_rate", 0), -m.get("error_count", 0)),
        reverse=True,
    )
    top_n = max(3, int(os.getenv("STATUS_REPORT_TOP_N", "12")))

    lines = [
        "<b>UltimateQuantSystem portfolio report</b>",
        f"Mode: <code>{escape(config.trading_mode.upper())}</code> | "
        f"Capital: <b>Rs{rs['capital']:,.2f}</b>",
        f"PnL: <b>Rs{portfolio.get('total_pnl', 0):+,.2f}</b> | "
        f"DD: <b>{rs['drawdown_pct']:.2f}%</b> | "
        f"Daily loss: <b>{rs['daily_loss_pct']:.2f}%</b>",
        f"Open risk: <b>Rs{rs['open_risk']:,.2f}</b> | "
        f"Open positions: <b>{rs['open_positions']}</b> | "
        f"Bots alive: <b>{registry.alive_count()}/{len(bot_metrics)}</b>",
        "",
        "<b>Bot ranking</b>",
    ]

    for i, m in enumerate(ranked[:top_n], start=1):
        status = escape(str(m.get("status", "?"))[:18])
        aid = escape(str(m.get("agent_id", "?"))[:24])
        pnl = m.get("total_pnl", 0)
        trades = m.get("trades", 0)
        win = m.get("win_rate", 0)
        mult = m.get("head_ai_mult", 1.0)
        err = m.get("error_count", 0)
        lines.append(
            f"#{i} <code>{aid}</code> | {status} | "
            f"Rs{pnl:+,.0f} | T{trades} W{win:.0f}% | AI {mult:.2f} | E{err}"
        )

    if open_trades:
        lines.extend(["", "<b>Open trades</b>"])
        for t in open_trades[:10]:
            lines.append(
                f"<code>{escape(t.agent_id[:20])}</code> {escape(t.symbol[:26])} "
                f"{escape(t.direction)} qty={t.quantity:g} risk=Rs{t.risk_amount:,.0f}"
            )
        if len(open_trades) > 10:
            lines.append(f"... {len(open_trades) - 10} more open trades")
    else:
        lines.extend(["", "<b>Open trades</b>", "None"])

    decisions = registry.recent_decisions(5)
    if decisions:
        lines.extend(["", "<b>Recent controls</b>"])
        for d in decisions:
            lines.append(
                f"{escape(d.get('time', ''))} | <code>{escape(d.get('agent_id', '')[:20])}</code> "
                f"{escape(d.get('action', ''))}: {escape(d.get('reason', '')[:70])}"
            )

    msg = "\n".join(lines)
    return msg[:3900]


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
    hosted_status_stop = start_hosted_status_reporter(registry, risk, journal)
    telegram_control = TelegramControlBot(
        registry=registry,
        risk=risk,
        journal=journal,
        head_ai=head_ai,
        report_fn=lambda: format_hosted_status_report(registry, risk, journal),
    )
    telegram_control.start()

    time.sleep(30)
    head_ai.analyze()

    if "--headless" in sys.argv:
        logger.info("Headless mode active. Bots will run until process shutdown.")
        try:
            while True:
                time.sleep(60)
        finally:
            registry.stop_all(join=True)
            head_ai.stop(join=True)
            auto_trainer.stop(join=True)
            telegram_control.stop(join=True)
            if hosted_status_stop:
                hosted_status_stop.set()
            risk.end_of_day()
            summary = journal.summary()
            if summary.get("trades", 0) > 0:
                journal.export_json()
            logger.info("UltimateQuantSystem stopped.")
        return

    dashboard = TerminalDashboard(
        registry=registry, head_ai=head_ai,
        risk_engine=risk, refresh_s=30,
    )

    try:
        dashboard.run()
    finally:
        registry.stop_all(join=True)
        head_ai.stop(join=True)
        auto_trainer.stop(join=True)
        telegram_control.stop(join=True)
        if hosted_status_stop:
            hosted_status_stop.set()
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
