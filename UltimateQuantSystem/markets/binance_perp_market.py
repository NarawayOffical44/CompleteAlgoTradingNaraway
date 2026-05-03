"""
BinancePerpMarket — USDT-margined perpetual futures on Binance.

24/7 always open. Uses ccxt Binance futures mode.
4h candles, 50 bars (same as CryptoMarket).

Symbols: High-beta altcoins selected for maximum volatility/opportunity.
Leverage: Controlled by PerpFuturesBot (default 3x).

Regime: based on BTC perp trend (SMA20 vs SMA50 on 4h).

Allocation:
  BULL:    perp_futures = 1.0
  CHOPPY:  perp_futures = 0.5  (reduce size, chop kills leveraged positions)
  BEAR:    perp_futures = 0.3  (shorts only — reduced size)
"""

import time
import threading
import numpy as np
from loguru import logger

from markets.base_market import BaseMarket


PERP_SYMBOLS = [
    "SOL/USDT:USDT",       "AVAX/USDT:USDT",
    "DOGE/USDT:USDT",      "1000PEPE/USDT:USDT",   # micro-token → 1000x contract
    "FET/USDT:USDT",       "INJ/USDT:USDT",
    "WIF/USDT:USDT",       "1000BONK/USDT:USDT",   # micro-token → 1000x contract
]

_ALLOC_TABLE = {
    "BULL":   {"perp_futures": 1.0},
    "CHOPPY": {"perp_futures": 0.5},
    "BEAR":   {"perp_futures": 0.3},
}

CACHE_TTL_S = 300


class BinancePerpMarket(BaseMarket):

    market_id = "PERP"

    def __init__(self):
        try:
            import ccxt
            self._exchange = ccxt.binance({
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            })
            logger.info("BinancePerpMarket | Binance Futures (ccxt) connected")
        except ImportError:
            logger.error("BinancePerpMarket | ccxt not installed")
            raise

        self._data_cache      = {}
        self._data_cache_time = 0
        self._regime          = "BULL"
        self._lock            = threading.Lock()

    def is_open(self) -> bool:
        return True

    def is_safe(self) -> tuple[bool, str]:
        return True, "ok"

    def get_data(self) -> dict:
        with self._lock:
            if time.time() - self._data_cache_time < CACHE_TTL_S and self._data_cache:
                return dict(self._data_cache)

        result = {}
        for symbol in PERP_SYMBOLS:
            try:
                ohlcv = self._exchange.fetch_ohlcv(symbol, "4h", limit=50)
                if not ohlcv or len(ohlcv) < 10:
                    continue

                opens   = [c[1] for c in ohlcv]
                highs   = [c[2] for c in ohlcv]
                lows    = [c[3] for c in ohlcv]
                closes  = [c[4] for c in ohlcv]
                volumes = [c[5] for c in ohlcv]

                avg_vol   = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
                vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
                ret_1d    = (closes[-1] / closes[-7] - 1) * 100 if len(closes) > 6 else 0
                ret_5d    = (closes[-1] / closes[-31] - 1) * 100 if len(closes) > 30 else 0

                # Funding rate (positive = longs pay shorts → bearish signal)
                funding_rate = 0.0
                try:
                    fr = self._exchange.fetch_funding_rate(symbol)
                    funding_rate = float(fr.get("fundingRate", 0) or 0)
                except Exception:
                    pass

                result[symbol] = {
                    "ltp":           closes[-1],
                    "opens":         opens,
                    "highs":         highs,
                    "lows":          lows,
                    "closes":        closes,
                    "volumes":       volumes,
                    "volume_ratio":  round(vol_ratio, 2),
                    "1d_return":     round(ret_1d, 3),
                    "5d_return":     round(ret_5d, 3),
                    "funding_rate":  round(funding_rate * 100, 4),  # as %
                }
                logger.debug(
                    f"BinancePerpMarket | {symbol} ltp={closes[-1]:,.4f} "
                    f"vol={vol_ratio:.2f}x funding={funding_rate*100:.4f}%"
                )

            except Exception as e:
                logger.warning(f"BinancePerpMarket | {symbol} fetch error: {e}")

        with self._lock:
            self._data_cache      = result
            self._data_cache_time = time.time()

        return dict(result)

    def get_regime(self, market_data: dict = None) -> str:
        if market_data is None:
            market_data = self.get_data()

        # Use SOL perp as high-beta regime indicator (moves before BTC confirms)
        # Fall back to first available symbol
        ref = market_data.get("SOL/USDT:USDT") or next(iter(market_data.values()), {})
        closes = ref.get("closes", [])

        if len(closes) < 50:
            return "BULL"

        sma20 = float(np.mean(closes[-20:]))
        sma50 = float(np.mean(closes[-50:]))
        ltp   = closes[-1]

        if ltp > sma20 > sma50:
            regime = "BULL"
        elif ltp < sma20 < sma50:
            regime = "BEAR"
        else:
            regime = "CHOPPY"

        with self._lock:
            self._regime = regime

        logger.debug(f"BinancePerpMarket | regime={regime} SMA20={sma20:.4f} SMA50={sma50:.4f}")
        return regime

    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        return _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

    def get_fundamentals(self, symbols: list) -> dict:
        return {}

    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        # Funding rate as sentiment proxy: high positive funding = overleveraged longs = bearish
        mkt_sent   = {"score": 0.0, "label": "neutral"}
        stock_sent = {s: {"score": 0.0, "label": "neutral"} for s in symbols}

        avg_funding = 0.0
        count = 0
        for sym, data in market_data.items():
            if sym.startswith("_"):
                continue
            fr = data.get("funding_rate", 0)
            avg_funding += fr
            count += 1

        if count > 0:
            avg_funding /= count
            # funding > 0.05%/8h = longs overloaded = bearish signal
            # funding < -0.01%/8h = shorts overloaded = bullish signal
            if avg_funding > 0.05:
                mkt_sent = {"score": -0.4, "label": "overleveraged_longs", "funding": avg_funding}
            elif avg_funding < -0.01:
                mkt_sent = {"score": 0.4, "label": "overleveraged_shorts", "funding": avg_funding}
            else:
                mkt_sent = {"score": 0.0, "label": "balanced", "funding": avg_funding}

        return stock_sent, mkt_sent
