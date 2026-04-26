"""
CryptoMarket — Binance spot market implementation.

Key differences from NSEMarket:
  - is_open() always returns True (24/7, 365 days)
  - is_safe() always True (no news filter yet)
  - Data from Binance public API via ccxt (no API key needed for OHLCV)
  - Regime based on BTC trend (SMA20 vs SMA50)
  - No fundamentals (crypto has no ROE/D-E)
  - Sentiment: Fear & Greed index (optional, falls back to neutral)
  - 5-min cache on data (same as NSE, prevents hammering Binance)

To add more symbols: edit CRYPTO_SYMBOLS below.
"""

import time
import threading
import requests
import numpy as np
from loguru import logger

from markets.base_market import BaseMarket


CRYPTO_SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]

# Allocation per bot per crypto regime
_ALLOC_TABLE = {
    "BULL_LOW_VOL": {
        "crypto_momentum": 1.0,
    },
    "CHOPPY": {
        "crypto_momentum": 0.5,
    },
    "BEAR_HIGH_VOL": {
        "crypto_momentum": 0.0,   # don't long in a bear market
    },
}

CACHE_TTL_S = 300   # 5 minutes


class CryptoMarket(BaseMarket):

    market_id = "CRYPTO"

    def __init__(self):
        try:
            import ccxt
            self._exchange = ccxt.binance({"enableRateLimit": True})
            logger.info("CryptoMarket | Binance (ccxt) connected")
        except ImportError:
            logger.error("CryptoMarket | ccxt not installed — run: pip install ccxt")
            raise

        self._data_cache      = {}
        self._data_cache_time = 0
        self._regime          = "BULL_LOW_VOL"
        self._lock            = threading.Lock()

    # ── Market hours — always open ────────────────────────────────────────
    def is_open(self) -> bool:
        return True     # crypto never closes

    # ── Safety — no news filter yet ───────────────────────────────────────
    def is_safe(self) -> tuple[bool, str]:
        return True, "ok"

    # ── Market data (5-min cached) ────────────────────────────────────────
    def get_data(self) -> dict:
        with self._lock:
            if time.time() - self._data_cache_time < CACHE_TTL_S and self._data_cache:
                return dict(self._data_cache)

        result = {}
        for symbol in CRYPTO_SYMBOLS:
            try:
                ohlcv = self._exchange.fetch_ohlcv(symbol, "1d", limit=60)
                if not ohlcv or len(ohlcv) < 6:
                    continue

                opens   = [c[1] for c in ohlcv]
                highs   = [c[2] for c in ohlcv]
                lows    = [c[3] for c in ohlcv]
                closes  = [c[4] for c in ohlcv]
                volumes = [c[5] for c in ohlcv]

                avg_vol  = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
                vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
                ret_1d   = (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else 0
                ret_5d   = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 5 else 0

                result[symbol] = {
                    "ltp":          closes[-1],
                    "opens":        opens,
                    "highs":        highs,
                    "lows":         lows,
                    "closes":       closes,
                    "volumes":      volumes,
                    "volume_ratio": round(vol_ratio, 2),
                    "1d_return":    round(ret_1d, 3),
                    "5d_return":    round(ret_5d, 3),
                }
                logger.debug(f"CryptoMarket | {symbol} ltp={closes[-1]:,.2f} 1d={ret_1d:+.2f}%")

            except Exception as e:
                logger.warning(f"CryptoMarket | {symbol} fetch error: {e}")

        with self._lock:
            self._data_cache      = result
            self._data_cache_time = time.time()

        return dict(result)

    # ── Regime (BTC-based trend) ──────────────────────────────────────────
    def get_regime(self, market_data: dict = None) -> str:
        if market_data is None:
            market_data = self.get_data()

        btc = market_data.get("BTC/USDT", {})
        closes = btc.get("closes", [])

        if len(closes) < 50:
            logger.debug("CryptoMarket | not enough BTC data for regime — default BULL_LOW_VOL")
            return "BULL_LOW_VOL"

        sma20 = float(np.mean(closes[-20:]))
        sma50 = float(np.mean(closes[-50:]))
        ltp   = closes[-1]

        if ltp > sma20 > sma50:
            regime = "BULL_LOW_VOL"
        elif ltp < sma20 < sma50:
            regime = "BEAR_HIGH_VOL"
        else:
            regime = "CHOPPY"

        with self._lock:
            self._regime = regime

        logger.debug(f"CryptoMarket | regime={regime} | BTC={ltp:,.0f} SMA20={sma20:,.0f} SMA50={sma50:,.0f}")
        return regime

    # ── Allocation ────────────────────────────────────────────────────────
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        return _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

    # ── Fundamentals — N/A for crypto ─────────────────────────────────────
    def get_fundamentals(self, symbols: list) -> dict:
        return {}   # crypto has no balance sheets

    # ── Sentiment (Fear & Greed Index, fallback to neutral) ───────────────
    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        stock_sent = {s: {"score": 0.0, "label": "neutral"} for s in symbols}
        mkt_sent   = {"score": 0.0, "label": "neutral"}

        try:
            resp = requests.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=5,
            )
            if resp.ok:
                val       = int(resp.json()["data"][0]["value"])
                score     = (val - 50) / 50   # normalise to [-1, +1]
                label     = resp.json()["data"][0]["value_classification"]
                mkt_sent  = {"score": round(score, 2), "label": label, "fng": val}
                logger.debug(f"CryptoMarket | Fear&Greed={val} ({label}) → score={score:.2f}")
        except Exception as e:
            logger.debug(f"CryptoMarket | Fear&Greed fetch failed: {e} — using neutral")

        return stock_sent, mkt_sent
