"""
Binance exchange wrapper.

SIM_MODE=true  — real live prices from public Binance API, no keys needed,
                 all orders simulated internally. Use for paper trading.
SIM_MODE=false — real orders on testnet (TESTNET=true) or live (TESTNET=false).
"""
import logging
import ccxt
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, TESTNET, SIM_MODE,
    SYMBOL, LEVERAGE, CAPITAL_USDT,
)

logger = logging.getLogger(__name__)

_TIMEOUT_MS = 8000


def create_exchange() -> ccxt.binanceusdm:
    if SIM_MODE:
        # Public-only: no API keys — only used for OHLCV + ticker (public endpoints)
        ex = ccxt.binanceusdm({
            "enableRateLimit": True,
            "timeout":         _TIMEOUT_MS,
        })
        logger.info("Exchange: Binance PUBLIC (sim mode — real prices, no orders)")
    else:
        ex = ccxt.binanceusdm({
            "apiKey":          BINANCE_API_KEY,
            "secret":          BINANCE_API_SECRET,
            "options":         {"defaultType": "future"},
            "enableRateLimit": True,
            "timeout":         _TIMEOUT_MS,
        })
        if TESTNET:
            ex.set_sandbox_mode(True)
            logger.info("Exchange: Binance FUTURES TESTNET")
        else:
            logger.info("Exchange: Binance FUTURES LIVE")
    return ex


def init_leverage(ex: ccxt.binanceusdm) -> None:
    if SIM_MODE:
        logger.info(f"Sim mode: leverage {LEVERAGE}x (simulated, no API call)")
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


def get_balance(ex: ccxt.binanceusdm) -> float:
    if SIM_MODE:
        return CAPITAL_USDT
    try:
        bal = ex.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))
    except Exception as e:
        logger.error(f"fetch_balance: {e}")
        return 0.0


def get_position(ex: ccxt.binanceusdm) -> dict | None:
    """In sim mode always returns None — position is tracked via state["open_trade"]."""
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


def fetch_ohlcv(ex: ccxt.binanceusdm, limit: int = 150) -> list:
    from config import TIMEFRAME
    try:
        return ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
    except Exception as e:
        logger.error(f"fetch_ohlcv: {e}")
        return []


def get_last_price(ex: ccxt.binanceusdm) -> float:
    try:
        return float(ex.fetch_ticker(SYMBOL)["last"])
    except Exception as e:
        logger.error(f"fetch_ticker: {e}")
        return 0.0


def open_position(ex: ccxt.binanceusdm, side: str, usdt_margin: float) -> dict | None:
    if SIM_MODE:
        price = get_last_price(ex)
        logger.info(f"[SIM] Opened {side} @ {price:.2f}  margin=${usdt_margin:.2f}×{LEVERAGE}x")
        return {"sim": True, "side": side, "price": price}
    try:
        price = get_last_price(ex)
        if price <= 0:
            return None
        qty = float(ex.amount_to_precision(SYMBOL, (usdt_margin * LEVERAGE) / price))
        order = ex.create_market_order(SYMBOL, "buy" if side == "long" else "sell", qty)
        logger.info(f"Opened {side} {qty} BTC @ ~{price:.2f}")
        return order
    except Exception as e:
        logger.error(f"open_position: {e}")
        return None


def close_position(ex: ccxt.binanceusdm, position: dict) -> dict | None:
    if SIM_MODE:
        price = get_last_price(ex)
        logger.info(f"[SIM] Closed {position.get('side')} @ {price:.2f}")
        return {"sim": True, "price": price}
    try:
        side = position["side"]
        qty  = abs(float(position["contracts"]))
        order = ex.create_market_order(
            SYMBOL,
            "sell" if side == "long" else "buy",
            qty,
            params={"reduceOnly": True},
        )
        logger.info(f"Closed {side} ({qty} BTC)")
        return order
    except Exception as e:
        logger.error(f"close_position: {e}")
        return None
