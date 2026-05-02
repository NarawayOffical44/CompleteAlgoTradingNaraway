"""
DailyBot — BTC/USDT Perp Scalper
Target: ₹20/day NET after 30% India crypto tax on ₹1000 capital.

Run:  ../venv/Scripts/python.exe main.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
print("DailyBot loading...", flush=True)

import json
import logging
import os
import time
from datetime import date, datetime, timezone

import requests

from config import (
    CAPITAL_USDT, INR_PER_USD,
    TAKE_PROFIT_PCT, STOP_LOSS_PCT, BREAKEVEN_TRIGGER_PCT,
    MAX_HOLD_HOURS, FEES_PCT_ROUNDTRIP, LEVERAGE,
    MAX_TRADES_PER_DAY, DAILY_GROSS_TARGET_INR, MAX_DAILY_LOSS_INR,
    CHECK_INTERVAL_SECONDS, TRADING_HOURS_UTC_START, TRADING_HOURS_UTC_END,
    TESTNET, SIM_MODE,
)
from exchange import (
    create_exchange, init_leverage,
    fetch_ohlcv, get_last_price, get_balance, get_position,
    open_position, close_position,
)
from strategy import compute_indicators, get_signal, check_exit
from tax import record_trade, daily_summary, daily_net_inr
from journal import log_trade

AGENT_NAME = "DailyBot"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("dailybot.log", encoding="utf-8"),
        logging.StreamHandler(stream=sys.stdout),
    ],
)
logger = logging.getLogger(AGENT_NAME)

# ── Telegram ──────────────────────────────────────────────────────────────────
_TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def tg(msg: str) -> None:
    """Send a Telegram message. Silent if no token configured."""
    if not _TG_TOKEN or not _TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT_ID, "text": f"[{AGENT_NAME}] {msg}", "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ── State persistence ─────────────────────────────────────────────────────────
STATE_FILE = "daily_state.json"

_EMPTY_STATE = {
    "date":           "",
    "trades_today":   0,
    "daily_pnl_usdt": 0.0,
    "open_trade":     None,
}


def load_state() -> dict:
    today = str(date.today())
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("date") == today:
            return s
    s = dict(_EMPTY_STATE)
    s["date"] = today
    return s


def save_state(s: dict) -> None:
    s["date"] = str(date.today())
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


# ── Helpers ───────────────────────────────────────────────────────────────────
def pnl_from_prices(entry: float, exit_: float, side: str, margin_usdt: float) -> float:
    """Gross PnL in USDT after fees (taker 0.05% × 2 sides)."""
    raw = (exit_ - entry) / entry if side == "long" else (entry - exit_) / entry
    return margin_usdt * LEVERAGE * (raw - FEES_PCT_ROUNDTRIP)


def _trading_window() -> bool:
    if TRADING_HOURS_UTC_START == 0 and TRADING_HOURS_UTC_END >= 24:
        return True   # 24/7 mode
    hour = datetime.now(timezone.utc).hour
    return TRADING_HOURS_UTC_START <= hour < TRADING_HOURS_UTC_END


def _daily_gross_pnl_inr(state: dict) -> float:
    return state["daily_pnl_usdt"] * INR_PER_USD


# ── Position reconciliation on startup ───────────────────────────────────────
def reconcile(ex, state: dict) -> dict:
    if SIM_MODE:
        return state   # state IS the position in sim mode
    live_pos = get_position(ex)
    if state["open_trade"] and not live_pos:
        logger.warning("Stale open_trade in state — no exchange position found. Clearing.")
        state["open_trade"] = None
    if live_pos and not state["open_trade"]:
        logger.warning(f"Untracked {live_pos['side']} position on exchange. Close manually.")
    return state


# ── Main tick ─────────────────────────────────────────────────────────────────
def tick(ex, state: dict) -> dict:
    if not _trading_window():
        now = datetime.now(timezone.utc)
        logger.info(f"Outside trading window ({now.strftime('%H:%M')} UTC, window={TRADING_HOURS_UTC_START}:00-{TRADING_HOURS_UTC_END}:00 UTC). Sleeping.")
        return state

    daily_net = daily_net_inr()

    # Daily limits
    if daily_net >= 20.0:
        logger.info(f"Target met: ₹{daily_net:.2f} net. Done for today.")
        return state
    if _daily_gross_pnl_inr(state) <= -MAX_DAILY_LOSS_INR:
        logger.info(f"Loss limit hit. Stopped for today.")
        return state
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        logger.info(f"Max {MAX_TRADES_PER_DAY} trades reached. Done for today.")
        return state

    # Market data
    ohlcv = fetch_ohlcv(ex)
    if not ohlcv:
        return state
    ind           = compute_indicators(ohlcv)
    current_price = float(ind["close"][-1])

    # ── Manage open position ──────────────────────────────────────────────────
    if state["open_trade"]:
        trade      = state["open_trade"]
        entry      = trade["entry_price"]
        side       = trade["side"]
        sl         = trade["sl_price"]
        margin     = trade["qty_usdt"]
        entry_time = datetime.fromisoformat(trade["entry_time"])

        should_exit, new_sl, reason = check_exit(current_price, entry, side, sl)
        state["open_trade"]["sl_price"] = new_sl

        hold_h = (datetime.now(timezone.utc) - entry_time.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if hold_h >= MAX_HOLD_HOURS:
            should_exit, reason = True, "max_hold"

        if should_exit:
            # In sim mode use a fake position dict; in live mode fetch from exchange
            live_pos = {"side": side, "contracts": 1} if SIM_MODE else get_position(ex)
            if live_pos:
                order = close_position(ex, live_pos)
                if order:
                    gross_usdt = pnl_from_prices(entry, current_price, side, margin)
                    tax_entry  = record_trade(gross_usdt)
                    hold_min   = round(hold_h * 60)

                    log_trade({
                        "date":         str(date.today()),
                        "side":         side,
                        "entry_price":  round(entry, 2),
                        "exit_price":   round(current_price, 2),
                        "reason":       reason,
                        "hold_minutes": hold_min,
                        "gross_inr":    tax_entry["gross_inr"],
                        "tax_inr":      tax_entry["tax_inr"],
                        "net_inr":      tax_entry["net_inr"],
                    })

                    state["daily_pnl_usdt"] += gross_usdt
                    state["open_trade"]      = None
                    state["trades_today"]   += 1

                    msg = (
                        f"CLOSED [{reason}] {side.upper()} BTC @ {current_price:.2f}\n"
                        f"Hold: {hold_min}m\n"
                        f"Gross: ₹{tax_entry['gross_inr']:.2f}  Tax: ₹{tax_entry['tax_inr']:.2f}  Net: ₹{tax_entry['net_inr']:.2f}"
                    )
                    logger.info(msg)
                    tg(msg)
                    _print_summary()

                    if daily_net_inr() >= 20.0:
                        tg(f"TARGET MET ✓  Net today: ₹{daily_net_inr():.2f}")
            else:
                state["open_trade"] = None

        return state

    # ── Look for entry ────────────────────────────────────────────────────────
    signal = get_signal(ind)
    if not signal:
        logger.info(
            f"BTC {current_price:.2f}  "
            f"ema9={ind['ema_fast'][-1]:.2f} ema21={ind['ema_slow'][-1]:.2f}  "
            f"rsi={ind['rsi'][-1]:.1f}  vwap={ind['vwap'][-1]:.2f}  "
            f"vol={ind['vol_ratio'][-1]:.2f}x  — waiting"
        )
        return state

    balance     = get_balance(ex)
    margin_usdt = min(CAPITAL_USDT, balance)
    if margin_usdt < 1.0:
        logger.warning(f"Low balance ${balance:.4f}. Skipping.")
        return state

    order = open_position(ex, signal, margin_usdt)
    if order:
        sl_price = current_price * (1 - STOP_LOSS_PCT if signal == "long" else 1 + STOP_LOSS_PCT)
        tp_price = current_price * (1 + TAKE_PROFIT_PCT if signal == "long" else 1 - TAKE_PROFIT_PCT)

        state["open_trade"] = {
            "side":        signal,
            "entry_price": current_price,
            "sl_price":    round(sl_price, 2),
            "qty_usdt":    margin_usdt,
            "entry_time":  datetime.now(timezone.utc).isoformat(),
        }

        msg = (
            f"ENTERED {signal.upper()} BTC @ {current_price:.2f}\n"
            f"TP: {tp_price:.2f}  SL: {sl_price:.2f}  Margin: ${margin_usdt:.2f}×{LEVERAGE}x"
        )
        logger.info(msg)
        tg(msg)

    return state


def _print_summary() -> None:
    s = daily_summary()
    logger.info(
        f"── Day summary: {s['trades']} trades | "
        f"Gross ₹{s['gross_inr']:.2f} | Tax ₹{s['tax_inr']:.2f} | Net ₹{s['net_inr']:.2f} | "
        f"{'TARGET MET ✓' if s['target_met'] else 'pending ₹20'}"
    )


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    mode = "SIM (real prices, no orders)" if SIM_MODE else ("TESTNET (paper)" if TESTNET else "LIVE")
    banner = f"{AGENT_NAME} DailyBot | BTC/USDT Perp | {mode} | Target ₹20/day after 30% tax"
    logger.info("=" * len(banner))
    logger.info(banner)
    logger.info("=" * len(banner))

    ex    = create_exchange()
    init_leverage(ex)
    state = load_state()
    state = reconcile(ex, state)
    save_state(state)

    logger.info(f"Trades today: {state['trades_today']}  Open position: {bool(state['open_trade'])}")
    _print_summary()
    tg(f"Bot started — {mode}")

    while True:
        try:
            state = tick(ex, state)
            save_state(state)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            tg("Bot stopped (Ctrl+C)")
            break
        except Exception as e:
            logger.error(f"Tick error: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
