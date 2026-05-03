"""
Bybit exchange wrapper (USDT-margined perpetuals).

SIM_MODE=true  — real live prices from Bybit public API, no keys needed,
                 all orders simulated internally.
SIM_MODE=false — real orders on live Bybit (TESTNET=false).
"""
import logging
import ccxt
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, TESTNET, SIM_MODE,
    SYMBOL, LEVERAGE, CAPITAL_USDT,
)

logger = logging.getLogger(__name__)

_TIMEOUT_MS = 8000


def create_exchange():
    if SIM_MODE:
        ex = ccxt.bybit({
            "enableRateLimit": True,
            "timeout":         _TIMEOUT_MS,
        })
        logger.info("Exchange: Bybit PUBLIC (sim mode — real prices, no orders)")
    else:
        ex = ccxt.bybit({
            "apiKey":          BYBIT_API_KEY,
            "secret":          BYBIT_API_SECRET,
            "enableRateLimit": True,
            "timeout":         _TIMEOUT_MS,
            "options":         {"defaultType": "linear"},  # USDT-margined perps
        })
        if TESTNET:
            ex.set_sandbox_mode(True)
            logger.info("Exchange: Bybit TESTNET")
        else:
            logger.info("Exchange: Bybit LIVE")
    return ex


def init_leverage(ex: ccxt.bybit) -> None:
    if SIM_MODE:
        logger.info(f"Sim mode: leverage {LEVERAGE}x (simulated)")
        return
    try:
        ex.set_margin_mode("isolated", SYMBOL)
    except Exception:
        pass
    try:
        ex.set_leverage(LEVERAGE, SYMBOL)
        logger.info(f"Leverage: {LEVERAGE}x isolated on {SYMBOL}")
    except Exception as e:
        logger.warning(f"set_leverage skipped: {e}")


def get_balance(ex: ccxt.bybit) -> float:
    if SIM_MODE:
        return CAPITAL_USDT
    try:
        bal = ex.fetch_balance({"type": "linear"})
        return float(bal.get("USDT", {}).get("free", 0))
    except Exception as e:
        logger.error(f"fetch_balance: {e}")
        return 0.0


def get_position(ex: ccxt.bybit) -> dict | None:
    """In sim mode always returns None — position tracked via state['open_trade']."""
    if SIM_MODE:
        return None
    try:
        positions = ex.fetch_positions([SYMBOL])
        for p in positions:
            if p["symbol"] == SYMBOL and abs(p.get("contracts") or 0) > 0:
                return p
    except Exception as e:
        logger.error(f"fetch_positions: {e}")
    return None


def fetch_ohlcv(ex, limit: int = 150) -> list:
    from config import TIMEFRAME
    sym = "BTC/USDT:USDT" if not SIM_MODE else "BTC/USDT:USDT"
    try:
        return ex.fetch_ohlcv(sym, TIMEFRAME, limit=limit)
    except Exception as e:
        logger.error(f"fetch_ohlcv: {e}")
        return []


def get_last_price(ex) -> float:
    try:
        return float(ex.fetch_ticker("BTC/USDT:USDT")["last"])
    except Exception as e:
        logger.error(f"fetch_ticker: {e}")
        return 0.0


def open_position(ex: ccxt.bybit, side: str, usdt_margin: float) -> dict | None:
    if SIM_MODE:
        price = get_last_price(ex)
        logger.info(f"[SIM] Opened {side} @ {price:.2f}  margin=${usdt_margin:.2f}×{LEVERAGE}x")
        return {"sim": True, "side": side, "price": price}
    try:
        price = get_last_price(ex)
        if price <= 0:
            return None
        qty   = float(ex.amount_to_precision(SYMBOL, (usdt_margin * LEVERAGE) / price))
        order = ex.create_market_order(
            SYMBOL,
            "buy" if side == "long" else "sell",
            qty,
            params={"positionIdx": 0},  # one-way mode
        )
        logger.info(f"Opened {side} {qty} BTC @ ~{price:.2f}")
        return order
    except Exception as e:
        logger.error(f"open_position: {e}")
        return None


def close_position(ex: ccxt.bybit, position: dict) -> dict | None:
    if SIM_MODE:
        price = get_last_price(ex)
        logger.info(f"[SIM] Closed {position.get('side')} @ {price:.2f}")
        return {"sim": True, "price": price}
    try:
        side  = position["side"]
        qty   = abs(float(position["contracts"]))
        order = ex.create_market_order(
            SYMBOL,
            "sell" if side == "long" else "buy",
            qty,
            params={"reduceOnly": True, "positionIdx": 0},
        )
        logger.info(f"Closed {side} ({qty} BTC)")
        return order
    except Exception as e:
        logger.error(f"close_position: {e}")
        return None
