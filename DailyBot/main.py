"""
DailyBot — BTC/USDT Perp Scalper
Target: Rs20/day NET after 30% India crypto tax on Rs1000 capital.

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


# ── Shared status (read by health server) ─────────────────────────────────────
_status = {
    "started_at":   "",
    "mode":         "",
    "last_scan":    "never",
    "scans_today":  0,
    "btc_price":    0.0,
    "rsi":          0.0,
    "ema9":         0.0,
    "ema21":        0.0,
    "vwap":         0.0,
    "vol_ratio":    0.0,
    "trend":        "",
    "open_trade":   None,
    "trades_today": 0,
    "gross_inr":    0.0,
    "tax_inr":      0.0,
    "net_inr":      0.0,
    "target_met":   False,
    "last_error":   "",
    "bot_alive":    True,
}
_status_lock = threading.Lock()


def _update_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


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
    raw = (exit_ - entry) / entry if side == "long" else (entry - exit_) / entry
    return margin_usdt * LEVERAGE * (raw - FEES_PCT_ROUNDTRIP)


def _trading_window() -> bool:
    if TRADING_HOURS_UTC_START == 0 and TRADING_HOURS_UTC_END >= 24:
        return True
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
        rsi   = ind["rsi"][-1]
        ef    = ind["ema_fast"][-1]
        es    = ind["ema_slow"][-1]
        vr    = ind["vol_ratio"][-1]
        vwap  = ind["vwap"][-1]
        trend = "BULL" if ef > es else "BEAR"
        lines += [
            f"RSI: {rsi:.1f}  EMA9: {ef:.2f}  EMA21: {es:.2f}",
            f"VWAP: {vwap:.2f}  Vol: {vr:.2f}x  Trend: {trend}",
        ]

    open_trade = state.get("open_trade")
    if open_trade and price:
        entry   = open_trade["entry_price"]
        side    = open_trade["side"]
        pnl_pct = ((price - entry) / entry) if side == "long" else ((entry - price) / entry)
        lines.append(f"Open {side.upper()} @ ${entry:,.2f}  PnL: {pnl_pct*100:+.2f}%")
    else:
        lines.append("No open position")

    remaining   = f"Rs{max(0, 20 - summary['net_inr']):.2f} remaining"
    target_str  = "MET" if summary["target_met"] else remaining
    lines += [
        f"Scans today: {state.get('scans_today', 0)}  Trades: {summary['trades']}",
        f"Daily — Gross: Rs{summary['gross_inr']:.2f}  Tax: Rs{summary['tax_inr']:.2f}  Net: Rs{summary['net_inr']:.2f}",
        f"Target: {target_str}",
    ]

    tg("\n".join(l for l in lines if l))


# ── Position reconciliation ───────────────────────────────────────────────────
def reconcile(ex, state: dict) -> dict:
    if SIM_MODE:
        return state
    live_pos = get_position(ex)
    if state["open_trade"] and not live_pos:
        logger.warning("Stale open_trade in state — clearing.")
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

    if daily_net >= 20.0:
        logger.info(f"Target met: Rs{daily_net:.2f} net. Continuing to trade.")
    if _daily_gross_pnl_inr(state) <= -MAX_DAILY_LOSS_INR:
        logger.info("Loss limit hit. Stopped for today.")
        return state
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        logger.info(f"Max {MAX_TRADES_PER_DAY} trades reached. Done for today.")
        return state

    ohlcv = fetch_ohlcv(ex)
    if not ohlcv:
        return state

    ind           = compute_indicators(ohlcv)
    current_price = float(ind["close"][-1])
    rsi           = ind["rsi"][-1]
    ef            = ind["ema_fast"][-1]
    es            = ind["ema_slow"][-1]
    vr            = ind["vol_ratio"][-1]
    vwap          = ind["vwap"][-1]
    trend         = "BULL" if ef > es else "BEAR"

    state["scans_today"] = state.get("scans_today", 0) + 1

    # Update shared status for web dashboard
    summary = daily_summary()
    _update_status(
        last_scan    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        scans_today  = state["scans_today"],
        btc_price    = current_price,
        rsi          = rsi,
        ema9         = ef,
        ema21        = es,
        vwap         = vwap,
        vol_ratio    = vr,
        trend        = trend,
        open_trade   = state.get("open_trade"),
        trades_today = state["trades_today"],
        gross_inr    = summary["gross_inr"],
        tax_inr      = summary["tax_inr"],
        net_inr      = summary["net_inr"],
        target_met   = summary["target_met"],
    )

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
                        f"Gross: Rs{tax_entry['gross_inr']:.2f}  Tax: Rs{tax_entry['tax_inr']:.2f}  <b>Net: Rs{tax_entry['net_inr']:.2f}</b>\n"
                        f"Day total — Trades: {summary['trades']}  Net: Rs{summary['net_inr']:.2f}"
                    )
                    logger.info(msg)
                    tg(msg)
                    _print_summary()

                    if daily_net_inr() >= 20.0:
                        tg(f"<b>TARGET MET - still running</b>  Net today: Rs{daily_net_inr():.2f}  ({summary['trades']} trade(s))")
            else:
                state["open_trade"] = None

        return state

    # ── Look for entry ────────────────────────────────────────────────────────
    signal = get_signal(ind)
    if not signal:
        logger.info(
            f"BTC ${current_price:,.2f}  EMA9={ef:.2f} EMA21={es:.2f}  "
            f"RSI={rsi:.1f}  VWAP={vwap:.2f}  Vol={vr:.2f}x  [{trend}]  "
            f"— waiting  [scan #{state['scans_today']}]"
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
            f"VWAP: {vwap:.2f}  Vol: {vr:.2f}x  Trend: {trend}\n"
            f"Trades today: {state['trades_today']+1}/{MAX_TRADES_PER_DAY}"
        )
        logger.info(msg)
        tg(msg)

    return state


def _print_summary() -> None:
    s = daily_summary()
    logger.info(
        f"Day summary: {s['trades']} trades | "
        f"Gross Rs{s['gross_inr']:.2f} | Tax Rs{s['tax_inr']:.2f} | Net Rs{s['net_inr']:.2f} | "
        f"{'TARGET MET' if s['target_met'] else 'pending Rs20'}"
    )


# ── Health / status web page ───────────────────────────────────────────────────
def _build_status_page() -> str:
    with _status_lock:
        s = dict(_status)

    mode        = s["mode"]
    mode_color  = "#00cc44" if "SIM" in mode else ("#ff9900" if "TESTNET" in mode else "#ff4444")
    alive_dot   = "#00cc44" if s["bot_alive"] else "#ff4444"
    alive_text  = "RUNNING" if s["bot_alive"] else "STOPPED"

    open_html = ""
    if s["open_trade"]:
        t = s["open_trade"]
        price = s["btc_price"]
        side  = t["side"]
        entry = t["entry_price"]
        pnl_pct = ((price - entry) / entry) if side == "long" else ((entry - price) / entry)
        color = "#00cc44" if pnl_pct >= 0 else "#ff4444"
        open_html = f"""
        <div class="card">
          <h3>Open Position</h3>
          <p><b>Side:</b> {side.upper()} &nbsp; <b>Entry:</b> ${entry:,.2f}</p>
          <p><b>Current PnL:</b> <span style="color:{color}">{pnl_pct*100:+.3f}% &nbsp; ({pnl_pct*10*100:+.2f}% leveraged)</span></p>
        </div>"""
    else:
        open_html = '<div class="card"><h3>Open Position</h3><p style="color:#888">No open position</p></div>'

    target_color = "#00cc44" if s["target_met"] else "#ff9900"
    target_text  = "MET" if s["target_met"] else f"Rs{max(0, 20 - s['net_inr']):.2f} to go"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <title>DailyBot Status</title>
  <style>
    body {{ background:#0d0d0d; color:#e0e0e0; font-family:monospace; padding:20px; margin:0; }}
    h1   {{ color:#fff; margin-bottom:4px; }}
    .tag {{ display:inline-block; padding:3px 10px; border-radius:4px; font-size:13px; font-weight:bold; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:16px; }}
    .card {{ background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:16px; }}
    .card h3 {{ margin:0 0 10px; color:#aaa; font-size:13px; text-transform:uppercase; }}
    .card p  {{ margin:4px 0; font-size:15px; }}
    .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }}
    .big {{ font-size:28px; font-weight:bold; }}
    @media(max-width:600px) {{ .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <h1>DailyBot &nbsp;
    <span class="tag" style="background:{mode_color}22; color:{mode_color}; border:1px solid {mode_color}">{mode}</span>
    &nbsp;
    <span class="tag" style="background:{alive_dot}22; color:{alive_dot}; border:1px solid {alive_dot}">
      <span class="dot" style="background:{alive_dot}"></span>{alive_text}
    </span>
  </h1>
  <p style="color:#666; font-size:12px">Auto-refreshes every 30s &nbsp;|&nbsp; Started: {s["started_at"]} &nbsp;|&nbsp; Last scan: {s["last_scan"]}</p>

  <div class="grid">
    <div class="card">
      <h3>BTC Price</h3>
      <p class="big">${s["btc_price"]:,.2f}</p>
      <p>Trend: <b>{s["trend"]}</b> &nbsp; Vol: <b>{s["vol_ratio"]:.2f}x</b></p>
    </div>

    <div class="card">
      <h3>Indicators</h3>
      <p>RSI: <b>{s["rsi"]:.1f}</b></p>
      <p>EMA9: <b>{s["ema9"]:.2f}</b> &nbsp; EMA21: <b>{s["ema21"]:.2f}</b></p>
      <p>VWAP: <b>{s["vwap"]:.2f}</b></p>
    </div>

    {open_html}

    <div class="card">
      <h3>Daily PnL</h3>
      <p>Gross: <b>Rs{s["gross_inr"]:.2f}</b></p>
      <p>Tax (30%): <b>Rs{s["tax_inr"]:.2f}</b></p>
      <p>Net: <b class="big">Rs{s["net_inr"]:.2f}</b></p>
      <p>Target: <span style="color:{target_color}"><b>{target_text}</b></span></p>
    </div>

    <div class="card">
      <h3>Activity</h3>
      <p>Scans today: <b>{s["scans_today"]}</b></p>
      <p>Trades today: <b>{s["trades_today"]} / {MAX_TRADES_PER_DAY}</b></p>
      <p style="color:#555; font-size:12px">Last error: {s["last_error"] or "none"}</p>
    </div>
  </div>
</body>
</html>"""


def _start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            page = _build_status_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Status page: http://0.0.0.0:{port}/")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    mode   = _mode_tag()
    banner = f"{AGENT_NAME} | BTC/USDT Perp | {mode} | Target Rs20/day after 30% tax"
    logger.info("=" * len(banner))
    logger.info(banner)
    logger.info("=" * len(banner))

    _update_status(
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        mode       = mode,
        bot_alive  = True,
    )

    _start_health_server()
    ex    = create_exchange()
    init_leverage(ex)
    state = load_state()
    state = reconcile(ex, state)
    save_state(state)

    summary = daily_summary()
    logger.info(f"Trades today: {state['trades_today']}  Open: {bool(state['open_trade'])}")
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
            err = str(e)[:200]
            logger.error(f"Tick error: {e}", exc_info=True)
            _update_status(last_error=err)
            tg(f"<b>ERROR</b>  {err}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
