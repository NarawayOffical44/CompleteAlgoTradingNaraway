"""
QuantSystem — Entry point.

Usage:
  python main.py            # Scheduler mode: signals fire at 09:20 / 15:35 IST
  python main.py --paper    # Continuous paper trading + live price ticker
  python main.py --test     # Single cycle, exit immediately
  python main.py --live     # Live mode (requires manual YES confirmation)
  python main.py --train    # Train HMM on historical data, then exit
"""

import sys
import os
import schedule
import time
from datetime import datetime
from loguru import logger

from config import config
from risk import RiskEngine
from journal import TradeJournal
from broker import DhanClient
from agents import MeanReversionAgent, PairsTradingAgent, MomentumAgent, MomentumScalper, OptionsBot
from ai import Orchestrator
from notify import notify
from reports import send_daily_report


logger.add(
    "logs/quant_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="90 days",
    level="INFO",
    format="{time:HH:mm:ss} | {level} | {message}",
)

# Enable ANSI colour codes on Windows 10+
if os.name == "nt":
    os.system("")

# Stocks shown in the live price ticker (subset of Nifty 50)
TICKER_STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "BHARTIARTL", "KOTAKBANK", "SBIN", "BAJFINANCE", "LT",
    "AXISBANK", "WIPRO", "TATAMOTORS", "SUNPHARMA", "MARUTI",
    "TITAN", "BAJAJFINSV", "HCLTECH", "JSWSTEEL", "NTPC",
]

# Paper loop timings (seconds)
TICKER_INTERVAL_S = 60          # refresh price display every 60 s
SIGNAL_INTERVAL_S = 15 * 60    # run full agent scan every 15 minutes


# ── ANSI helpers ──────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"


def _color(val: float, text: str) -> str:
    c = _GREEN if val >= 0 else _RED
    return f"{c}{text}{_RESET}"


def _market_status() -> str:
    now = datetime.now()
    if now.weekday() >= 5:
        return f"{_YELLOW}WEEKEND{_RESET}"
    t = now.hour * 60 + now.minute
    if t < 9 * 60 + 15:
        return f"{_YELLOW}PRE-MARKET{_RESET}"
    if t <= 15 * 60 + 30:
        return f"{_GREEN}MARKET OPEN{_RESET}"
    return f"{_YELLOW}MARKET CLOSED{_RESET}"


# ── Live price ticker ─────────────────────────────────────────────────────────
def display_price_ticker(market_fetcher, next_signal_secs: int = 0, last_scan: str = "—"):
    """
    Clear screen and print a live price dashboard.
    Fetches NIFTY + TICKER_STOCKS (cached 5 min so it's fast after first call).
    """
    try:
        data = market_fetcher.get_market_data(TICKER_STOCKS)
    except Exception as e:
        print(f"[Ticker fetch error: {e}]")
        return

    nifty = data.get("NIFTY", {})
    vix   = data.get("VIX",   {})
    now   = datetime.now().strftime("%H:%M:%S")

    nifty_ltp = nifty.get("ltp", 0)
    nifty_1d  = nifty.get("1d_return", 0)
    nifty_5d  = nifty.get("5d_return", 0)
    vix_val   = vix.get("ltp", 0)
    pcr       = data.get("PCR", 0)
    adr       = data.get("ADR", 0)
    fii       = data.get("FII_FLOW", 0)

    n_arrow = "▲" if nifty_1d >= 0 else "▼"

    W = 76
    sep = "─" * W

    os.system("cls" if os.name == "nt" else "clear")

    print(sep)
    print(f"  {_BOLD}QuantSystem  PAPER{_RESET}  │  {now}  │  {_market_status()}")
    print(sep)

    nifty_str = _color(nifty_1d, f"NIFTY 50  {nifty_ltp:>10,.2f}  {n_arrow} {nifty_1d:+.2f}%  (5d {nifty_5d:+.2f}%)")
    fii_str   = _color(fii, f"FII {fii:+.0f} Cr")
    print(f"  {nifty_str}   VIX {vix_val:.1f}   PCR {pcr:.2f}   ADR {adr:.2f}   {fii_str}")
    print(sep)

    # Column headers
    col_hdr = f"  {'Symbol':<13} {'LTP':>10}  {'1d%':>6}  {'5d%':>6}  {'Vol':>5}"
    print(f"{col_hdr}  {col_hdr.lstrip()}")
    print(f"  {'─'*46}  {'─'*46}")

    stocks = [(sym, data.get(sym, {})) for sym in TICKER_STOCKS if sym in data]
    half   = (len(stocks) + 1) // 2

    for i in range(half):
        l_sym, l_d = stocks[i]
        l_ltp = l_d.get("ltp", 0)
        l_1d  = l_d.get("1d_return", 0)
        l_5d  = l_d.get("5d_return", 0)
        l_vol = l_d.get("volume_ratio", 0)
        l_line = f"  {l_sym:<13} {l_ltp:>10,.2f}  {_color(l_1d, f'{l_1d:>+6.2f}%')}  {l_5d:>+6.2f}%  {l_vol:>4.1f}x"

        r_line = ""
        if i + half < len(stocks):
            r_sym, r_d = stocks[i + half]
            r_ltp = r_d.get("ltp", 0)
            r_1d  = r_d.get("1d_return", 0)
            r_5d  = r_d.get("5d_return", 0)
            r_vol = r_d.get("volume_ratio", 0)
            r_line = f"  {r_sym:<13} {r_ltp:>10,.2f}  {_color(r_1d, f'{r_1d:>+6.2f}%')}  {r_5d:>+6.2f}%  {r_vol:>4.1f}x"

        print(f"{l_line}  {r_line}")

    print(sep)

    if next_signal_secs > 0:
        m, s = divmod(next_signal_secs, 60)
        scan_str = f"Next signal scan: {m:02d}:{s:02d}"
    else:
        scan_str = "Signal scan RUNNING..."

    print(f"  {scan_str}  │  Last scan: {last_scan}  │  Ctrl+C to stop")
    print(sep)


# ── Boot ──────────────────────────────────────────────────────────────────────
def boot():
    if "--live" in sys.argv:
        confirm = input("LIVE MODE — real orders will be placed. Type YES to confirm: ")
        if confirm.strip() != "YES":
            logger.info("Live mode cancelled.")
            sys.exit(0)
        config.trading_mode = "live"

    logger.info(f"QuantSystem | mode={config.trading_mode} | capital=₹{config.starting_capital:,.0f}")
    notify(
        f"<b>QuantSystem STARTED</b>\n"
        f"Mode: <code>{config.trading_mode.upper()}</code>  "
        f"Capital: <b>₹{config.starting_capital:,.0f}</b>"
    )

    risk    = RiskEngine(starting_capital=config.starting_capital)
    journal = TradeJournal(journal_dir="logs")
    broker  = DhanClient()

    agents = [
        PairsTradingAgent(
            agent_id="pairs_trading",
            risk_engine=risk, journal=journal, broker=broker,
        ),
        MeanReversionAgent(
            agent_id="mean_reversion",
            risk_engine=risk, journal=journal, broker=broker,
        ),
        MomentumAgent(
            agent_id="momentum",
            risk_engine=risk, journal=journal, broker=broker,
        ),
        MomentumScalper(
            agent_id="momentum_scalper",
            risk_engine=risk, journal=journal, broker=broker,
        ),
    ]

    # OptionsBot requires ~₹15,000+ margin per Iron Condor — activate at ₹25,000+
    options_bot = None
    if config.starting_capital >= 25000:
        options_bot = OptionsBot(
            risk_engine=risk, journal=journal, broker=broker, underlying="NIFTY",
        )

    orchestrator = Orchestrator(
        risk_engine=risk,
        agents=agents,
        options_bot=options_bot,
    )

    return orchestrator, risk, journal


# ── Market open / close helpers ───────────────────────────────────────────────
def market_open(orchestrator, risk, journal):
    logger.info("=" * 60)
    logger.info("MARKET OPEN")
    logger.info(f"Risk: {risk.status()}")

    results = orchestrator.run()

    logger.info(f"Ran:     {results['ran']}")
    logger.info(f"Skipped: {results['skipped']}")
    if results.get("reason"):
        logger.warning(f"Block:   {results['reason']}")
    if results.get("correlation_alerts"):
        logger.warning(f"Corr alerts: {results['correlation_alerts']}")


def market_close(risk, journal, orchestrator=None):
    logger.info("MARKET CLOSE")
    risk.end_of_day()
    summary = journal.summary()
    logger.info(f"Summary: {summary}")
    if summary.get("trades", 0) > 0:
        logger.info(f"Journal: {journal.export_json()}")
    agents = orchestrator.agents if orchestrator else {}
    send_daily_report(journal, risk, agents)


# ── Continuous paper trading loop ─────────────────────────────────────────────
def paper_loop(orchestrator, risk, journal):
    """
    Runs indefinitely:
      • Every 60 s  — refresh live price ticker in terminal
      • Every 15 min — run full agent signal scan (paper trades logged to journal)
    Press Ctrl+C to stop cleanly.
    """
    logger.info("Paper loop started | ticker=60s | signals=15min | Ctrl+C to stop")

    last_ticker = 0.0
    last_signal = 0.0
    last_scan_ts = "—"

    try:
        while True:
            try:
                now_ts = time.time()

                # ── Signal scan (every 15 min) ──────────────────────────────
                if now_ts - last_signal >= SIGNAL_INTERVAL_S:
                    logger.info("=" * 60)
                    logger.info("PAPER SIGNAL SCAN")
                    logger.info(f"Risk: {risk.status()}")
                    results = orchestrator.run()
                    logger.info(f"Ran:     {results['ran']}")
                    logger.info(f"Skipped: {results['skipped']}")
                    if results.get("reason"):
                        logger.warning(f"Block:   {results['reason']}")
                    last_signal  = now_ts
                    last_scan_ts = datetime.now().strftime("%H:%M:%S")
                    last_ticker  = 0.0   # force immediate ticker refresh after scan

                # ── Price ticker (every 60 s) ───────────────────────────────
                if now_ts - last_ticker >= TICKER_INTERVAL_S:
                    secs_left = max(0, int(SIGNAL_INTERVAL_S - (now_ts - last_signal)))
                    display_price_ticker(orchestrator.market_fetcher, secs_left, last_scan_ts)
                    last_ticker = now_ts

            except Exception as e:
                logger.error(f"Paper loop error (continuing): {e}")
                time.sleep(10)
                continue

            time.sleep(5)

    except KeyboardInterrupt:
        print()
        logger.info("Paper loop stopped by user.")
        market_close(risk, journal)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    orchestrator, risk, journal = boot()

    if "--train" in sys.argv:
        logger.info("Training HMM on historical Nifty data...")
        data   = orchestrator.market_fetcher.get_market_data(["NIFTY"])
        closes = data.get("NIFTY", {}).get("closes", [])
        if len(closes) >= 60:
            orchestrator.train_hmm(closes)
            logger.info("HMM training complete")
        else:
            logger.error(f"Not enough data: {len(closes)} days (need 60+)")
        return

    if "--test" in sys.argv:
        logger.info("Test mode — single cycle")
        market_open(orchestrator, risk, journal)
        market_close(risk, journal, orchestrator)
        logger.info(f"Status: {orchestrator.status()}")
        return

    if "--paper" in sys.argv:
        paper_loop(orchestrator, risk, journal)
        return

    # Default — IST production schedule (fires at 09:20 open and 15:35 close)
    schedule.every().day.at("09:20").do(market_open,  orchestrator, risk, journal)
    schedule.every().day.at("15:35").do(market_close, risk, journal, orchestrator)

    logger.info("Scheduler running | 09:20 open | 15:35 close")
    logger.info(f"Status: {orchestrator.status()}")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"Scheduler error (continuing): {e}")
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
        notify("<b>QuantSystem stopped</b> (clean exit)")
    except KeyboardInterrupt:
        notify("<b>QuantSystem stopped</b> (Ctrl+C)")
    except Exception as e:
        notify(f"<b>QuantSystem CRASHED</b>\n<code>{e}</code>")
        raise
