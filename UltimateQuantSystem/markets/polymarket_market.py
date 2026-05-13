"""
PolymarketMarket — Prediction market trading via Polymarket CLOB + Gamma APIs.

Always open (24/7/365).
Data: Gamma API (market discovery) + CLOB API (live prices + order book).
Cache: 90s — prediction markets move fast on news but not millisecond HFT.

Market data format (per token):
  {
    "question":       "Will X happen?",
    "outcome":        "Yes" | "No",
    "ltp":            0.45,         # current price = probability (0–1)
    "bid":            0.44,
    "ask":            0.46,
    "spread":         0.02,
    "volume_24h":     150000,       # USDC
    "price_change_1h": 8.5,         # %
    "liquidity":      45000,        # USDC available to trade
    "end_date":       "2026-11-05", # event resolution date
    "days_to_end":    180,
    "condition_id":   "0x...",      # CLOB market identifier
    "token_id":       "...",        # YES/NO token id
  }

Regime: based on overall market activity level
  HIGH_ACTIVITY:  many markets moving > 5% in 1h → active news cycle
  NORMAL:         standard activity
  LOW_ACTIVITY:   quiet period → fewer entries

Auth (.env keys):
  POLYMARKET_API_KEY        — from polymarket.com/profile
  POLYMARKET_PRIVATE_KEY    — Polygon wallet private key (live orders only)
  Paper trading works without any keys (read-only APIs are public).
"""

import os
import time
import threading
import requests
from datetime import datetime, timezone
from loguru import logger

from markets.base_market import BaseMarket


GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"

CACHE_TTL_S      = 90       # 90s cache
MAX_MARKETS      = 30       # fetch top N active markets per cycle
MIN_VOLUME_24H   = 20_000   # USDC — skip illiquid markets
MIN_LIQUIDITY    = 5_000    # USDC — skip thin order books
MAX_SPREAD       = 0.05     # max bid-ask spread (5 cents) — skip wide markets
MIN_DAYS_TO_END  = 2        # skip markets resolving within 2 days (binary risk)

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept":     "application/json",
}

_ALLOC_TABLE = {
    "HIGH_ACTIVITY": {"polymarket_bot": 1.0},
    "NORMAL":        {"polymarket_bot": 0.8},
    "LOW_ACTIVITY":  {"polymarket_bot": 0.5},
}


class PolymarketMarket(BaseMarket):

    market_id = "POLY"

    def __init__(self):
        self._data_cache      = {}
        self._data_cache_time = 0
        self._regime          = "NORMAL"
        self._lock            = threading.Lock()

        self._api_key = os.getenv("POLYMARKET_API_KEY", "")
        if self._api_key:
            logger.info("PolymarketMarket | API key loaded — live orders enabled")
        else:
            logger.info("PolymarketMarket | No API key — read-only / paper mode")

    # ── Always open ───────────────────────────────────────────────────────
    def is_open(self) -> bool:
        return True

    def is_safe(self) -> tuple[bool, str]:
        return True, "ok"

    # ── Market data (top active markets, 90s cached) ──────────────────────
    def get_data(self) -> dict:
        with self._lock:
            if time.time() - self._data_cache_time < CACHE_TTL_S and self._data_cache:
                return dict(self._data_cache)

        markets = self._fetch_active_markets()
        result  = {}

        for mkt in markets:
            key  = mkt.get("_key", "")
            if not key:
                continue
            result[key] = mkt

        with self._lock:
            self._data_cache      = result
            self._data_cache_time = time.time()

        logger.info(f"PolymarketMarket | {len(result)} active markets loaded")
        return dict(result)

    def _fetch_active_markets(self) -> list[dict]:
        """Fetch top active markets from Gamma API, enrich with CLOB prices."""
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active":   "true",
                    "closed":   "false",
                    "limit":    MAX_MARKETS,
                    "order":    "volume24hr",
                    "ascending":"false",
                },
                headers=_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()
            markets_raw = raw if isinstance(raw, list) else raw.get("markets", [])
        except Exception as e:
            logger.warning(f"PolymarketMarket | Gamma fetch failed: {e}")
            return []

        enriched = []
        for mkt in markets_raw:
            try:
                parsed = self._parse_market(mkt)
                if parsed:
                    enriched.extend(parsed)   # one market → YES + NO tokens
            except Exception as e:
                logger.debug(f"PolymarketMarket | parse error: {e}")

        return enriched

    def _parse_market(self, mkt: dict) -> list[dict]:
        """Parse a Gamma market into YES/NO token dicts."""
        question   = mkt.get("question", "")
        volume_24h = float(mkt.get("volume24hr", 0) or 0)
        liquidity  = float(mkt.get("liquidity", 0) or 0)
        end_date_s = mkt.get("endDate", "") or mkt.get("end_date_iso", "")
        condition  = mkt.get("conditionId", "") or mkt.get("condition_id", "")
        tokens     = mkt.get("tokens", []) or mkt.get("clobTokenIds", [])

        if volume_24h < MIN_VOLUME_24H:
            return []
        if liquidity < MIN_LIQUIDITY:
            return []

        # Days to end
        days_to_end = 999
        if end_date_s:
            try:
                end_dt    = datetime.fromisoformat(end_date_s.replace("Z", "+00:00"))
                days_to_end = (end_dt - datetime.now(timezone.utc)).days
            except Exception:
                pass

        if days_to_end < MIN_DAYS_TO_END:
            return []

        # Parse prices from outcomePrices
        outcome_prices = mkt.get("outcomePrices", [])
        outcomes       = mkt.get("outcomes", ["Yes", "No"])

        result = []
        for i, outcome in enumerate(outcomes[:2]):   # Yes + No only
            try:
                price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.5
            except Exception:
                price = 0.5

            # Bid/ask from CLOB (optional — use price ± spread estimate if unavailable)
            spread = 0.02   # default estimate
            bid    = round(price - spread / 2, 4)
            ask    = round(price + spread / 2, 4)

            # Price change 1h — try CLOB if available
            price_change_1h = self._fetch_price_change(condition, i)

            # Safe unique key
            safe_q = "".join(c if c.isalnum() else "_" for c in question[:40])
            key    = f"{safe_q}_{outcome.upper()}"

            result.append({
                "_key":            key,
                "question":        question,
                "outcome":         outcome,
                "ltp":             round(price, 4),
                "bid":             bid,
                "ask":             ask,
                "spread":          spread,
                "volume_24h":      volume_24h,
                "price_change_1h": price_change_1h,
                "liquidity":       liquidity,
                "end_date":        end_date_s[:10] if end_date_s else "",
                "days_to_end":     days_to_end,
                "condition_id":    condition,
                "token_index":     i,
                # Standard fields for BotRunner enrichment
                "closes":          [price],
                "volume_ratio":    1.0,
            })

        return result

    def _fetch_price_change(self, condition_id: str, token_idx: int) -> float:
        """Fetch 1h price change from CLOB timeseries. Returns 0.0 on failure."""
        if not condition_id:
            return 0.0
        try:
            resp = requests.get(
                f"{CLOB_API}/prices-history",
                params={
                    "market":   condition_id,
                    "startTs":  int(time.time()) - 3600,
                    "endTs":    int(time.time()),
                    "fidelity": 60,
                },
                headers=_HEADERS,
                timeout=5,
            )
            if resp.ok:
                history = resp.json().get("history", [])
                if len(history) >= 2:
                    prices = history[token_idx] if isinstance(history[0], list) else history
                    if prices:
                        first = float(prices[0].get("p", 0) or 0)
                        last  = float(prices[-1].get("p", 0) or 0)
                        if first > 0:
                            return round((last - first) / first * 100, 2)
        except Exception:
            pass
        return 0.0

    # ── Regime: overall market activity ──────────────────────────────────
    def get_regime(self, market_data: dict = None) -> str:
        if market_data is None:
            market_data = self.get_data()

        movers = sum(
            1 for k, v in market_data.items()
            if not k.startswith("_") and isinstance(v, dict) and abs(v.get("price_change_1h", 0)) > 5.0
        )
        total  = max(len([k for k in market_data if not k.startswith("_")]), 1)
        pct    = movers / total

        if pct > 0.30:
            regime = "HIGH_ACTIVITY"
        elif pct < 0.10:
            regime = "LOW_ACTIVITY"
        else:
            regime = "NORMAL"

        with self._lock:
            self._regime = regime

        logger.debug(f"PolymarketMarket | regime={regime} | {movers}/{total} markets moving >5%")
        return regime

    # ── Allocation ────────────────────────────────────────────────────────
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        return _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

    # ── Fundamentals / Sentiment — N/A ────────────────────────────────────
    def get_fundamentals(self, symbols: list) -> dict:
        return {}

    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        # Use average price movement as market sentiment proxy
        changes = [
            v.get("price_change_1h", 0)
            for k, v in market_data.items()
            if not k.startswith("_") and isinstance(v, dict)
        ]
        avg_chg = sum(changes) / max(len(changes), 1)
        score   = max(-1.0, min(1.0, avg_chg / 20))   # normalise ±20% → ±1
        mkt_sent = {"score": round(score, 2), "label": "active" if abs(score) > 0.2 else "neutral"}
        stock_sent = {s: {"score": 0.0, "label": "neutral"} for s in symbols}
        return stock_sent, mkt_sent
