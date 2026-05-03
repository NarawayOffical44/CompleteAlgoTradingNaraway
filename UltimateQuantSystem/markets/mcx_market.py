"""
MCXMarket — Multi Commodity Exchange India.

Key differences from NSEMarket:
  - Longer hours: Mon-Sat 9:00 AM – 11:30 PM IST (agri) / 11:55 PM (non-agri)
  - Symbols: GOLD, SILVER, CRUDEOIL, NATURALGAS, COPPER
  - Data: Yahoo Finance commodity futures as price proxy (COMEX/NYMEX correlated)
    GOLD→GC=F, SILVER→SI=F, CRUDEOIL→CL=F, NATGAS→NG=F, COPPER→HG=F
  - Regime: Gold-trend based (SMA20 vs SMA50) — gold leads commodities
  - No equity fundamentals (balance sheets don't apply)
  - Sentiment: net speculator positions (COT) — falls back to neutral

To add this market:
  from markets import MCXMarket
  mcx = MCXMarket()
  registry.register(BotRunner(agent=MyCommodityBot(), market=mcx, risk_engine=risk))

To add a commodity bot:
  Create agents/commodity_bot.py (inherit BaseAgent)
  Add to _ALLOC_TABLE below with agent_id matching agent.agent_id
"""

import time
import threading
import requests
import numpy as np
from datetime import datetime
from loguru import logger

from markets.base_market import BaseMarket


# Yahoo Finance ticker → friendly name
_COMMODITY_TICKERS = {
    "GC=F":  "GOLD",
    "SI=F":  "SILVER",
    "CL=F":  "CRUDEOIL",
    "NG=F":  "NATURALGAS",
    "HG=F":  "COPPER",
}

# Allocation per bot per regime
# Add your commodity bot's agent_id here.
_ALLOC_TABLE = {
    "BULL_LOW_VOL": {
        "commodity_momentum": 1.0,
        "gold_mean_reversion": 0.8,
    },
    "CHOPPY": {
        "commodity_momentum": 0.4,
        "gold_mean_reversion": 1.0,
    },
    "BEAR_HIGH_VOL": {
        "commodity_momentum": 0.0,
        "gold_mean_reversion": 0.5,    # gold safe haven in bear
    },
}

CACHE_TTL_S = 300   # 5 minutes — same as other markets


class MCXMarket(BaseMarket):

    market_id = "MCX"

    def __init__(self, extra_tickers: dict = None):
        """
        extra_tickers: optional dict of {yahoo_ticker: friendly_name} to add custom symbols
        """
        self._tickers = dict(_COMMODITY_TICKERS)
        if extra_tickers:
            self._tickers.update(extra_tickers)

        self._data_cache      = {}
        self._data_cache_time = 0
        self._regime          = "BULL_LOW_VOL"
        self._lock            = threading.Lock()

    # ── Market hours — Mon-Sat 9:00 AM – 11:30 PM IST ────────────────────
    def is_open(self) -> bool:
        now = datetime.now()
        if now.weekday() == 6:          # Sunday only closed
            return False
        t = now.hour * 60 + now.minute
        # 9:00 AM → 11:30 PM (23:30)
        return 9 * 60 <= t <= 23 * 60 + 30

    # ── Safety — no specific news filter (use as-is) ──────────────────────
    def is_safe(self) -> tuple[bool, str]:
        return True, "ok"

    # ── Market data (5-min cached) ─────────────────────────────────────────
    def get_data(self) -> dict:
        with self._lock:
            if time.time() - self._data_cache_time < CACHE_TTL_S and self._data_cache:
                return dict(self._data_cache)

        result = {}
        for ticker, name in self._tickers.items():
            try:
                data = self._fetch_yahoo(ticker)
                if data:
                    result[name] = data
                    logger.debug(f"MCXMarket | {name} ltp={data['ltp']:.2f}")
            except Exception as e:
                logger.warning(f"MCXMarket | {ticker} ({name}) fetch error: {e}")

        with self._lock:
            self._data_cache      = result
            self._data_cache_time = time.time()

        return dict(result)

    # ── Regime (Gold-trend based) ──────────────────────────────────────────
    def get_regime(self, market_data: dict = None) -> str:
        if market_data is None:
            market_data = self.get_data()

        gold = market_data.get("GOLD", {})
        closes = gold.get("closes", [])

        if len(closes) < 50:
            logger.debug("MCXMarket | not enough Gold data — defaulting to BULL_LOW_VOL")
            return "BULL_LOW_VOL"

        sma20 = float(np.mean(closes[-20:]))
        sma50 = float(np.mean(closes[-50:]))
        ltp   = closes[-1]

        # Volatility check: std of last 10 daily returns
        returns  = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(-10, 0)]
        vol      = float(np.std(returns)) if returns else 0.0

        if ltp > sma20 > sma50 and vol < 1.5:
            regime = "BULL_LOW_VOL"
        elif ltp < sma20 < sma50:
            regime = "BEAR_HIGH_VOL"
        else:
            regime = "CHOPPY"

        with self._lock:
            self._regime = regime

        logger.debug(f"MCXMarket | regime={regime} | Gold={ltp:.0f} SMA20={sma20:.0f} SMA50={sma50:.0f} vol={vol:.2f}")
        return regime

    # ── Allocation ────────────────────────────────────────────────────────
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        return _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

    # ── Fundamentals — N/A for commodities ───────────────────────────────
    def get_fundamentals(self, symbols: list) -> dict:
        return {}   # commodities have no balance sheets

    # ── Sentiment — neutral fallback ──────────────────────────────────────
    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        # Future improvement: CFTC COT data for speculator positions
        stock_sent = {s: {"score": 0.0, "label": "neutral"} for s in symbols}
        mkt_sent   = {"score": 0.0, "label": "neutral"}

        # Simple proxy: gold 5d return as market sentiment indicator
        gold_5d = market_data.get("GOLD", {}).get("5d_return", 0.0)
        if gold_5d > 2.0:
            mkt_sent = {"score": 0.5, "label": "positive", "note": "Gold up 5d"}
        elif gold_5d < -2.0:
            mkt_sent = {"score": -0.5, "label": "negative", "note": "Gold down 5d"}

        return stock_sent, mkt_sent

    # ── Internal: Yahoo Finance OHLCV fetch ───────────────────────────────
    def _fetch_yahoo(self, ticker: str) -> dict:
        """Fetch daily OHLCV from Yahoo Finance v8 API."""
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept":     "application/json",
        }
        resp = requests.get(
            url,
            headers=headers,
            params={
                "interval": "1d",
                "range":    "3mo",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()

        result_obj = body.get("chart", {}).get("result", [{}])[0]
        timestamps = result_obj.get("timestamp", [])
        quote      = result_obj.get("indicators", {}).get("quote", [{}])[0]

        closes  = [c for c in quote.get("close",  []) if c is not None]
        volumes = [v for v in quote.get("volume", []) if v is not None]
        highs   = [h for h in quote.get("high",   []) if h is not None]
        lows    = [lo for lo in quote.get("low",  []) if lo is not None]
        opens   = [o for o in quote.get("open",   []) if o is not None]

        if len(closes) < 6:
            return {}

        avg_vol   = sum(volumes[:-1]) / max(len(volumes) - 1, 1) if volumes else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 and volumes else 1.0
        ret_1d    = (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else 0.0
        ret_5d    = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 5 else 0.0

        return {
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
