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
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    "scans_today":    0,
}


def load_state() -> dict:
    today = str(date.today())
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("date") == today:
            s.setdefault("scans_today", 0)
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


def _mode_tag() -> str:
    if SIM_MODE:
        return "SIM"
    return "TESTNET" if TESTNET else "LIVE"


# ── Heartbeat ─────────────────────────────────────────────────────────────────
_last_heartbeat: datetime | None = None
HEARTBEAT_INTERVAL_MINUTES = 60


def _maybe_send_heartbeat(state: dict, ind: dict | None = None) -> None:
    global _last_heartbeat
    now = datetime.now(timezone.utc)
    if _last_heartbeat and (now - _last_heartbeat).total_seconds() < HEARTBEAT_INTERVAL_MINUTES * 60:
        return
    _last_heartbeat = now

    summary = daily_summary()
    mode    = _mode_tag()
    price   = float(ind["close"][-1]) if ind is not None else 0.0

    lines = [
        f"<b>Heartbeat [{mode}]</b>  {now.strftime('%H:%M UTC')}",
        f"BTC: ${price:,.2f}" if price else "",
    ]

    if ind is not None:
        rsi      = ind["rsi"][-1]
        ef       = ind["ema_fast"][-1]
        es       = ind["ema_slow"][-1]
        vr       = ind["vol_ratio"][-1]
        vwap     = ind["vwap"][-1]
        trend    = "BULL" if ef > es else "BEAR"
        lines += [
            f"RSI: {rsi:.1f}  EMA9: {ef:.2f}  EMA21: {es:.2f}",
            f"VWAP: {vwap:.2f}  Vol: {vr:.2f}x  Trend: {trend}",
        ]

    open_trade = state.get("open_trade")
    if open_trade:
        entry  = open_trade["entry_price"]
        side   = open_trade["side"]
        pnl_pct = ((price - entry) / entry) if side == "long" else ((entry - price) / entry)
        lines.append(f"Open {side.upper()} @ {entry:.2f}  PnL: {pnl_pct*100:+.2f}%")
    else:
        lines.append("No open position")

    lines += [
        f"Scans today: {state.get('scans_today', 0)}  Trades: {summary['trades']}",
        f"Daily PnL — Gross: ₹{summary['gross_inr']:.2f}  Tax: ₹{summary['tax_inr']:.2f}  Net: ₹{summary['net_inr']:.2f}",
        f"Target: {'MET ✓' if summary['target_met'] else f'₹{max(0, 20 - summary[\"net_inr\"]):.2f} to go'}",
    ]

    tg("\n".join(l for l in lines if l))


# ── Position reconciliation on startup ───────────────────────────────────────
def reconcile(ex, state: dict) -> dict:
    if SIM_MODE:
        return state
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
        logger.info(f"Outside trading window ({now.strftime('%H:%M')} UTC). Sleeping.")
        return state

    daily_net = daily_net_inr()

    # Daily limits
    if daily_net >= 20.0:
        logger.info(f"Target met: ₹{daily_net:.2f} net. Done for today.")
        _maybe_send_heartbeat(state)
        return state
    if _daily_gross_pnl_inr(state) <= -MAX_DAILY_LOSS_INR:
        logger.info("Loss limit hit. Stopped for today.")
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
    state["scans_today"] = state.get("scans_today", 0) + 1

    # Send heartbeat if due
    _maybe_send_heartbeat(state, ind)

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
            live_pos = {"side": side, "contracts": 1} if SIM_MODE else get_position(ex)
            if live_pos:
                order = close_position(ex, live_pos)
                if order:
                    gross_usdt = pnl_from_prices(entry, current_price, side, margin)
                    tax_entry  = record_trade(gross_usdt)
                    hold_min   = round(hold_h * 60)
                    pnl_pct    = (current_price - entry) / entry if side == "long" else (entry - current_price) / entry

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

                    summary    = daily_summary()
                    mode       = _mode_tag()
                    result_tag = "WIN" if gross_usdt > 0 else "LOSS"

                    msg = (
                        f"<b>CLOSED [{reason}] {result_tag} [{mode}]</b>\n"
                        f"Side: {side.upper()}  BTC @ ${current_price:,.2f}\n"
                        f"Entry: ${entry:,.2f}  Exit: ${current_price:,.2f}\n"
                        f"Move: {pnl_pct*100:+.3f}%  Leveraged: {pnl_pct*LEVERAGE*100:+.2f}%\n"
                        f"Hold: {hold_min}m\n"
                        f"Gross: ₹{tax_entry['gross_inr']:.2f}  Tax: ₹{tax_entry['tax_inr']:.2f}  <b>Net: ₹{tax_entry['net_inr']:.2f}</b>\n"
                        f"Day total — Trades: {summary['trades']}  Net: ₹{summary['net_inr']:.2f}"
                    )
                    logger.info(msg)
                    tg(msg)
                    _print_summary()

                    if daily_net_inr() >= 20.0:
                        tg(f"<b>TARGET MET</b>  Net today: ₹{daily_net_inr():.2f}  ({summary['trades']} trade(s))")
            else:
                state["open_trade"] = None

        return state

    # ── Look for entry ────────────────────────────────────────────────────────
    rsi  = ind["rsi"][-1]
    ef   = ind["ema_fast"][-1]
    es   = ind["ema_slow"][-1]
    vr   = ind["vol_ratio"][-1]
    vwap = ind["vwap"][-1]

    signal = get_signal(ind)
    if not signal:
        logger.info(
            f"BTC ${current_price:,.2f}  "
            f"EMA9={ef:.2f} EMA21={es:.2f}  "
            f"RSI={rsi:.1f}  VWAP={vwap:.2f}  "
            f"Vol={vr:.2f}x  — waiting  [scan #{state['scans_today']}]"
        )
        return state

    balance     = get_balance(ex)
    margin_usdt = min(CAPITAL_USDT, balance)
    if margin_usdt < 1.0:
        logger.warning(f"Low balance ${balance:.4f}. Skipping.")
        tg(f"<b>LOW BALANCE</b>  ${balance:.4f} USDT — cannot enter {signal.upper()}. Deposit needed.")
        return state

    order = open_position(ex, signal, margin_usdt)
    if order:
        sl_price = current_price * (1 - STOP_LOSS_PCT if signal == "long" else 1 + STOP_LOSS_PCT)
        tp_price = current_price * (1 + TAKE_PROFIT_PCT if signal == "long" else 1 - TAKE_PROFIT_PCT)
        mode     = _mode_tag()

        state["open_trade"] = {
            "side":        signal,
            "entry_price": current_price,
            "sl_price":    round(sl_price, 2),
            "qty_usdt":    margin_usdt,
            "entry_time":  datetime.now(timezone.utc).isoformat(),
        }

        msg = (
            f"<b>ENTERED {signal.upper()} [{mode}]</b>\n"
            f"BTC @ ${current_price:,.2f}\n"
            f"TP: ${tp_price:,.2f}  SL: ${sl_price:,.2f}\n"
            f"Margin: ${margin_usdt:.2f} x {LEVERAGE}x = ${margin_usdt*LEVERAGE:.2f} notional\n"
            f"RSI: {rsi:.1f}  EMA9: {ef:.2f}  EMA21: {es:.2f}\n"
            f"VWAP: {vwap:.2f}  Vol: {vr:.2f}x\n"
            f"Trades today: {state['trades_today']+1}/{MAX_TRADES_PER_DAY}"
        )
        logger.info(msg)
        tg(msg)

    return state


def _print_summary() -> None:
    s = daily_summary()
    logger.info(
        f"── Day summary: {s['trades']} trades | "
        f"Gross ₹{s['gross_inr']:.2f} | Tax ₹{s['tax_inr']:.2f} | Net ₹{s['net_inr']:.2f} | "
        f"{'TARGET MET' if s['target_met'] else 'pending Rs20'}"
    )


# ── Entry point ───────────────────────────────────────────────────────────────
def _start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DailyBot running")
        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health server listening on port {port}")


def main() -> None:
    mode   = _mode_tag()
    banner = f"{AGENT_NAME} | BTC/USDT Perp | {mode} | Target Rs20/day after 30% tax"
    logger.info("=" * len(banner))
    logger.info(banner)
    logger.info("=" * len(banner))

    _start_health_server()
    ex    = create_exchange()
    init_leverage(ex)
    state = load_state()
    state = reconcile(ex, state)
    save_state(state)

    summary = daily_summary()
    logger.info(f"Trades today: {state['trades_today']}  Open position: {bool(state['open_trade'])}")
    _print_summary()

    tg(
        f"<b>Bot started [{mode}]</b>\n"
        f"Capital: ${CAPITAL_USDT} x {LEVERAGE}x | Target: Rs20/day\n"
        f"Trades today so far: {state['trades_today']}  Net: Rs{summary['net_inr']:.2f}\n"
        f"Heartbeat every {HEARTBEAT_INTERVAL_MINUTES}min"
    )

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
            tg(f"<b>ERROR</b>  {str(e)[:200]}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
