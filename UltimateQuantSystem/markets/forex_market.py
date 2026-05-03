"""
ForexMarket — Major FX pairs via Yahoo Finance (1h OHLCV, 7-day history).

Pairs: EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CHF
Hours: Mon 00:00 UTC – Fri 23:59 UTC (closed Sat/Sun)
Data:  Yahoo Finance v8 API, 1h candles, 5-min cache (~168 candles)
Regime: USD strength proxy via EUR/USD SMA20 vs SMA50

Allocation per bot per regime:
  BULL_USD  (USD strong, EUR/USD falling)  → momentum=0.7, mean_rev=0.8
  BEAR_USD  (USD weak,  EUR/USD rising)    → momentum=0.7, mean_rev=0.8
  NEUTRAL   (range-bound)                 → momentum=0.4, mean_rev=1.0
"""

import time
import threading
import requests
import numpy as np
from datetime import datetime, timezone
from loguru import logger

from markets.base_market import BaseMarket


FOREX_PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CHF"]

_YAHOO_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CHF": "USDCHF=X",
}

_ALLOC_TABLE = {
    "BULL_USD": {"forex_momentum": 0.7, "forex_mean_rev": 0.8},
    "BEAR_USD": {"forex_momentum": 0.7, "forex_mean_rev": 0.8},
    "NEUTRAL":  {"forex_momentum": 0.4, "forex_mean_rev": 1.0},
}

CACHE_TTL_S = 300   # 5 minutes
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


class ForexMarket(BaseMarket):

    market_id = "FOREX"

    def __init__(self):
        self._data_cache      = {}
        self._data_cache_time = 0
        self._regime          = "NEUTRAL"
        self._lock            = threading.Lock()

    # ── Market hours: Mon–Fri UTC ─────────────────────────────────────────
    def is_open(self) -> bool:
        now = datetime.now(timezone.utc)
        # 0=Mon … 4=Fri are open; 5=Sat, 6=Sun closed
        return now.weekday() <= 4

    # ── Safety: no news filter for forex yet ─────────────────────────────
    def is_safe(self) -> tuple[bool, str]:
        return True, "ok"

    # ── Market data (1h OHLCV, 5-min cached) ─────────────────────────────
    def get_data(self) -> dict:
        with self._lock:
            if time.time() - self._data_cache_time < CACHE_TTL_S and self._data_cache:
                return dict(self._data_cache)

        result = {}
        for pair in FOREX_PAIRS:
            data = self._fetch_pair(pair)
            if data:
                result[pair] = data

        with self._lock:
            self._data_cache      = result
            self._data_cache_time = time.time()

        return dict(result)

    def _fetch_pair(self, pair: str) -> dict | None:
        yahoo_sym = _YAHOO_MAP.get(pair)
        if not yahoo_sym:
            return None

        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
               f"?interval=1h&range=7d")
        try:
            resp = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
            resp.raise_for_status()
            body   = resp.json()
            result = body["chart"]["result"][0]

            timestamps = result.get("timestamp", [])
            q          = result["indicators"]["quote"][0]
            opens   = q.get("open",   [])
            highs   = q.get("high",   [])
            lows    = q.get("low",    [])
            closes  = q.get("close",  [])
            volumes = q.get("volume", [])

            # Clean NaN / None from Yahoo (forex volumes are often null)
            def clean(lst):
                return [v if v is not None else 0.0 for v in lst]

            opens   = clean(opens)
            highs   = clean(highs)
            lows    = clean(lows)
            closes  = clean(closes)
            volumes = [v if v else 1.0 for v in volumes]   # avoid 0 div

            if len(closes) < 52:
                logger.debug(f"ForexMarket | {pair} insufficient data ({len(closes)} bars)")
                return None

            # Vol ratio (1h bar vs 24h avg)
            avg_vol_24  = sum(volumes[-25:-1]) / 24 if len(volumes) > 24 else 1.0
            vol_ratio   = volumes[-1] / avg_vol_24 if avg_vol_24 > 0 else 1.0

            ret_1d  = (closes[-1] / closes[-25] - 1) * 100 if len(closes) > 24 else 0
            ret_5d  = (closes[-1] / closes[-121] - 1) * 100 if len(closes) > 120 else 0

            ltp = closes[-1]
            logger.debug(f"ForexMarket | {pair}={ltp:.5f} 1d={ret_1d:+.3f}%")

            return {
                "ltp":          ltp,
                "opens":        opens,
                "highs":        highs,
                "lows":         lows,
                "closes":       closes,
                "volumes":      volumes,
                "volume_ratio": round(vol_ratio, 2),
                "1d_return":    round(ret_1d, 4),
                "5d_return":    round(ret_5d, 4),
            }

        except Exception as e:
            logger.warning(f"ForexMarket | {pair} fetch error: {e}")
            return None

    # ── Regime: USD strength via EUR/USD SMA20 vs SMA50 ──────────────────
    def get_regime(self, market_data: dict = None) -> str:
        if market_data is None:
            market_data = self.get_data()

        eurusd = market_data.get("EUR/USD", {})
        closes = eurusd.get("closes", [])

        if len(closes) < 52:
            logger.debug("ForexMarket | not enough EUR/USD data — default NEUTRAL")
            return "NEUTRAL"

        sma20 = float(np.mean(closes[-20:]))
        sma50 = float(np.mean(closes[-50:]))
        diff_pct = abs(sma20 - sma50) / sma50 * 100

        if diff_pct < 0.05:           # SMAs nearly equal → range
            regime = "NEUTRAL"
        elif sma20 > sma50:           # EUR/USD rising → USD weakening
            regime = "BEAR_USD"
        else:                          # EUR/USD falling → USD strengthening
            regime = "BULL_USD"

        with self._lock:
            self._regime = regime

        logger.debug(
            f"ForexMarket | regime={regime} | EUR/USD SMA20={sma20:.5f} SMA50={sma50:.5f} diff={diff_pct:.3f}%"
        )
        return regime

    # ── Allocation ────────────────────────────────────────────────────────
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        return _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

    # ── Fundamentals: N/A for forex ───────────────────────────────────────
    def get_fundamentals(self, symbols: list) -> dict:
        return {}

    # ── Sentiment: neutral fallback ───────────────────────────────────────
    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        stock_sent = {s: {"score": 0.0, "label": "neutral"} for s in symbols}
        mkt_sent   = {"score": 0.0, "label": "neutral"}
        return stock_sent, mkt_sent
