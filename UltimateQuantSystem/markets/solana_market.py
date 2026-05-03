"""
SolanaMarket — New memecoin detection on Solana via DexScreener + RugCheck.

Always open (24/7/365 — crypto never closes).
Data source: DexScreener public API (free, no key needed).
Rug detection: RugCheck.xyz API (free, no key needed).
Cache: 90s (fast cache — memecoins move in minutes, not hours).

Data format returned (per token):
  {
    "mint":             "...",       Solana contract address
    "symbol":           "POPCAT/SOL",
    "ltp":              0.000023,    price in USD
    "liquidity_usd":    25000,       pool liquidity
    "volume_5m":        45000,       5-min volume USD
    "volume_1h":        180000,      1h volume USD
    "price_change_5m":  234.5,       % price change last 5 min
    "price_change_1h":  180.0,       % price change last 1h
    "age_min":          12,          minutes since first trade
    "rug_risks":        1,           number of risk flags from RugCheck
    "rug_score_safe":   True,        True if rug_risks <= MAX_RUG_RISKS
    "pair_address":     "...",       DEX pair address
  }

Regime based on SOL market trend (SOL/USDT 1h SMA).
"""

import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from loguru import logger

from markets.base_market import BaseMarket


# ── Config ────────────────────────────────────────────────────────────────────
CACHE_TTL_S      = 90       # 90s cache — memecoins move fast
MAX_RUG_RISKS    = 2        # allow up to 2 minor risks (0 = paranoid, 3 = degenerate)
MIN_LIQUIDITY    = 10_000   # USD — skip ghost pools
MIN_VOLUME_5M    = 3_000    # USD — skip dead tokens
MAX_TOKEN_AGE_M  = 45       # minutes — only snipe new tokens
MAX_TOKENS_CHECK = 15       # max tokens to rug-check per cycle (API rate limit)

_DS_HEADERS = {"User-Agent": "Mozilla/5.0"}

_ALLOC_TABLE = {
    "BULL_SOL":  {"meme_sniper": 1.0},
    "BEAR_SOL":  {"meme_sniper": 0.5},   # reduced size in bear SOL
    "NEUTRAL":   {"meme_sniper": 0.8},
}


class SolanaMarket(BaseMarket):

    market_id = "SOLANA"

    def __init__(self):
        self._data_cache      = {}
        self._data_cache_time = 0
        self._regime          = "NEUTRAL"
        self._lock            = threading.Lock()

    # ── Always open ───────────────────────────────────────────────────────
    def is_open(self) -> bool:
        return True

    def is_safe(self) -> tuple[bool, str]:
        return True, "ok"

    # ── Market data (new Solana tokens, 90s cached) ───────────────────────
    def get_data(self) -> dict:
        with self._lock:
            if time.time() - self._data_cache_time < CACHE_TTL_S and self._data_cache:
                return dict(self._data_cache)

        tokens = self._fetch_new_tokens()
        result = {}

        checked = 0
        for t in tokens:
            if checked >= MAX_TOKENS_CHECK:
                break

            mint   = t.get("mint", "")
            symbol = t.get("symbol", mint[:8])

            if not mint:
                continue

            # Rug check (1 API call per token)
            rug_risks, rug_safe = self._check_rug(mint)
            checked += 1

            key = f"{symbol}/SOL_{mint[:6]}"
            result[key] = {
                "mint":            mint,
                "symbol":          symbol,
                "ltp":             t.get("priceUsd", 0.0),
                "liquidity_usd":   t.get("liquidity", 0),
                "volume_5m":       t.get("volume5m", 0),
                "volume_1h":       t.get("volume1h", 0),
                "price_change_5m": t.get("priceChange5m", 0),
                "price_change_1h": t.get("priceChange1h", 0),
                "age_min":         t.get("age_min", 999),
                "rug_risks":       rug_risks,
                "rug_score_safe":  rug_safe,
                "pair_address":    t.get("pairAddress", ""),
            }

            logger.debug(
                f"SolanaMarket | {symbol} | liq=${t.get('liquidity',0):,.0f} | "
                f"5m={t.get('priceChange5m',0):+.1f}% | age={t.get('age_min',0)}m | "
                f"rug_risks={rug_risks} safe={rug_safe}"
            )

        with self._lock:
            self._data_cache      = result
            self._data_cache_time = time.time()

        logger.info(f"SolanaMarket | fetched {len(result)} new Solana tokens")
        return dict(result)

    def _fetch_new_tokens(self) -> list[dict]:
        """
        Fetch recently boosted/trending Solana tokens from DexScreener.
        Falls back to token profiles if boosts endpoint fails.
        """
        candidates = []

        # Primary: token boosts (most recent launches with active buyers)
        try:
            resp = requests.get(
                "https://api.dexscreener.com/token-boosts/latest/v1",
                headers=_DS_HEADERS, timeout=10,
            )
            if resp.ok:
                items = resp.json() if isinstance(resp.json(), list) else []
                for item in items:
                    if item.get("chainId") != "solana":
                        continue
                    candidates.append({"mint": item.get("tokenAddress", ""), "source": "boost"})
        except Exception as e:
            logger.debug(f"SolanaMarket | boosts fetch failed: {e}")

        # Secondary: token profiles (latest token page submissions)
        if len(candidates) < 5:
            try:
                resp = requests.get(
                    "https://api.dexscreener.com/token-profiles/latest/v1",
                    headers=_DS_HEADERS, timeout=10,
                )
                if resp.ok:
                    items = resp.json() if isinstance(resp.json(), list) else []
                    for item in items:
                        if item.get("chainId") != "solana":
                            continue
                        candidates.append({"mint": item.get("tokenAddress", ""), "source": "profile"})
            except Exception as e:
                logger.debug(f"SolanaMarket | profiles fetch failed: {e}")

        # Deduplicate
        seen  = set()
        mints = []
        for c in candidates:
            m = c.get("mint", "")
            if m and m not in seen:
                seen.add(m)
                mints.append(m)

        if not mints:
            logger.warning("SolanaMarket | no new Solana tokens found from DexScreener")
            return []

        # Fetch detailed pair data for each mint (batch up to 30)
        enriched = []
        batch    = ",".join(mints[:30])
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{batch}",
                headers=_DS_HEADERS, timeout=15,
            )
            if resp.ok:
                pairs = resp.json().get("pairs", []) or []
                for pair in pairs:
                    if pair.get("chainId") != "solana":
                        continue
                    if pair.get("dexId") not in ("raydium", "orca", "meteora", "pumpswap"):
                        continue   # only major Solana DEXes

                    # Calculate token age
                    created_at = pair.get("pairCreatedAt", 0)
                    if created_at:
                        age_min = (time.time() - created_at / 1000) / 60
                    else:
                        age_min = 999

                    if age_min > MAX_TOKEN_AGE_M:
                        continue   # too old

                    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                    if liq < MIN_LIQUIDITY:
                        continue   # ghost pool

                    vol5m = float((pair.get("volume") or {}).get("m5", 0) or 0)
                    if vol5m < MIN_VOLUME_5M:
                        continue   # no activity

                    base = pair.get("baseToken", {})
                    enriched.append({
                        "mint":          base.get("address", ""),
                        "symbol":        base.get("symbol", "?"),
                        "priceUsd":      float(pair.get("priceUsd", 0) or 0),
                        "liquidity":     liq,
                        "volume5m":      vol5m,
                        "volume1h":      float((pair.get("volume") or {}).get("h1", 0) or 0),
                        "priceChange5m": float((pair.get("priceChange") or {}).get("m5", 0) or 0),
                        "priceChange1h": float((pair.get("priceChange") or {}).get("h1", 0) or 0),
                        "age_min":       round(age_min, 1),
                        "pairAddress":   pair.get("pairAddress", ""),
                    })
        except Exception as e:
            logger.warning(f"SolanaMarket | pair data fetch failed: {e}")

        logger.debug(f"SolanaMarket | {len(enriched)} tokens pass age/liquidity/volume filters")
        return enriched

    def _check_rug(self, mint: str) -> tuple[int, bool]:
        """
        Query RugCheck.xyz for the token's risk profile.
        Returns (risk_count, is_safe).
        is_safe = True if risk_count <= MAX_RUG_RISKS.
        """
        try:
            resp = requests.get(
                f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary",
                headers=_DS_HEADERS, timeout=8,
            )
            if not resp.ok:
                # Can't verify → treat as unsafe
                return 99, False

            data   = resp.json()
            risks  = data.get("risks", [])

            # Count HIGH severity risks only
            high_risks = [r for r in risks if r.get("level", "").lower() in ("danger", "high", "warn")]
            risk_count = len(high_risks)
            is_safe    = risk_count <= MAX_RUG_RISKS

            logger.debug(f"SolanaMarket | rug check {mint[:8]}… | risks={risk_count} safe={is_safe}")
            return risk_count, is_safe

        except Exception as e:
            logger.debug(f"SolanaMarket | rug check failed for {mint[:8]}: {e} — treating as unsafe")
            return 99, False

    # ── Regime: SOL trend via DexScreener SOL/USDT ────────────────────────
    def get_regime(self, market_data: dict = None) -> str:
        """
        Simple SOL regime: fetch SOL/USDT price change from DexScreener.
        BULL_SOL: SOL 1h > +2%
        BEAR_SOL: SOL 1h < -2%
        NEUTRAL:  otherwise
        """
        try:
            resp = requests.get(
                "https://api.dexscreener.com/latest/dex/tokens/"
                "So11111111111111111111111111111111111111112",
                headers=_DS_HEADERS, timeout=8,
            )
            if resp.ok:
                pairs = resp.json().get("pairs", []) or []
                sol_pairs = [p for p in pairs if p.get("quoteToken", {}).get("symbol") == "USDC"
                             or p.get("quoteToken", {}).get("symbol") == "USDT"]
                if sol_pairs:
                    change_1h = float((sol_pairs[0].get("priceChange") or {}).get("h1", 0) or 0)
                    if change_1h >= 2.0:
                        regime = "BULL_SOL"
                    elif change_1h <= -2.0:
                        regime = "BEAR_SOL"
                    else:
                        regime = "NEUTRAL"
                    with self._lock:
                        self._regime = regime
                    logger.debug(f"SolanaMarket | SOL 1h={change_1h:+.2f}% → regime={regime}")
                    return regime
        except Exception as e:
            logger.debug(f"SolanaMarket | SOL regime fetch failed: {e}")

        return self._regime

    # ── Allocation ────────────────────────────────────────────────────────
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        return _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

    # ── Fundamentals / Sentiment — N/A ────────────────────────────────────
    def get_fundamentals(self, symbols: list) -> dict:
        return {}

    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        stock_sent = {s: {"score": 0.0, "label": "neutral"} for s in symbols}
        mkt_sent   = {"score": 0.0, "label": "neutral"}
        return stock_sent, mkt_sent
