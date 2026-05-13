"""
Fundamentals Module — PE, PB, ROE, debt/equity, promoter holding, earnings growth.
Sources:
  - Screener.in  → PE, PB, ROE, D/E, promoter holding (free, scrape)
  - NSE filings  → Quarterly results, shareholding pattern

Data refreshes daily (fundamentals don't change intraday).
Cached to avoid repeated scraping.
"""

import json
import time
import requests
from pathlib import Path
from loguru import logger

CACHE_DIR = Path("data/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://www.screener.in/",
}


class FundamentalsData:

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(SCREENER_HEADERS)
        self._cache: dict = {}

    # ── Main method: get fundamentals for a symbol ─────────────────────
    def get(self, symbol: str) -> dict:
        """
        Returns:
        {
            pe_ratio:              float,   # Price/Earnings
            pb_ratio:              float,   # Price/Book
            roe:                   float,   # Return on Equity %
            debt_to_equity:        float,
            promoter_holding_pct:  float,
            revenue_growth_yoy:    float,   # % YoY revenue growth
            profit_growth_yoy:     float,
            current_ratio:         float,
            dividend_yield:        float,
            market_cap_cr:         float,
            fundamental_score:     float,   # 0-100 composite
        }
        """
        cache_key = f"fundamentals_{symbol}"
        cached    = self._load_cache(cache_key, max_age_hours=24)
        if cached:
            return cached

        data = self._fetch_screener(symbol)
        data["fundamental_score"] = self._score(data)
        self._save_cache(cache_key, data)
        return data

    # ── Batch fetch ───────────────────────────────────────────────────────
    def get_batch(self, symbols: list) -> dict[str, dict]:
        result = {}
        for i, sym in enumerate(symbols):
            try:
                result[sym] = self.get(sym)
                if i % 5 == 0 and i > 0:
                    time.sleep(2)   # polite scraping
            except Exception as e:
                logger.warning(f"Fundamentals failed for {sym}: {e}")
                result[sym] = self._empty()
        return result

    # ── Fundamental score: 0–100 composite ───────────────────────────────
    @staticmethod
    def _score(data: dict) -> float:
        score = 50.0

        # PE: lower is better (< 20 good, > 40 bad)
        pe = data.get("pe_ratio", 25)
        if 0 < pe < 15:      score += 15
        elif 15 <= pe < 25:  score += 8
        elif pe > 40:        score -= 10

        # ROE: higher is better
        roe = data.get("roe", 15)
        if roe > 25:         score += 15
        elif roe > 15:       score += 8
        elif roe < 5:        score -= 10

        # D/E: lower is better
        de = data.get("debt_to_equity", 1)
        if de < 0.3:         score += 10
        elif de < 0.7:       score += 5
        elif de > 2.0:       score -= 15

        # Promoter holding: higher = better alignment
        ph = data.get("promoter_holding_pct", 50)
        if ph > 60:          score += 8
        elif ph < 25:        score -= 8

        # Revenue growth
        rg = data.get("revenue_growth_yoy", 10)
        if rg > 20:          score += 10
        elif rg > 10:        score += 5
        elif rg < 0:         score -= 10

        return round(min(100, max(0, score)), 1)

    # ── Screener.in scraper ───────────────────────────────────────────────
    def _fetch_screener(self, symbol: str) -> dict:
        """
        Screener.in HTML (2026) structure:
          <span class="name">Stock P/E</span>
          <span class="nowrap value"><span class="number">23.8</span></span>
        Strategy: extract all (label → first number) pairs from top-ratios section,
        then fall back to meta description for ROE / promoter holding.
        """
        import re

        for url_type in ["consolidated", ""]:
            url = f"https://www.screener.in/company/{symbol}/{url_type}/".rstrip("/") + "/"
            try:
                r = self._session.get(url, timeout=12)
                if r.status_code == 200:
                    html = r.text
                    break
            except Exception:
                continue
        else:
            return self._empty()

        # ── Build label→value map from top-ratios ul ──────────────────
        ratios: dict[str, float] = {}
        try:
            # Extract the top-ratios block
            block_m = re.search(r'id="top-ratios"(.*?)</ul>', html, re.DOTALL)
            block = block_m.group(1) if block_m else html

            # Each <li> has one <span class="name"> and one or more <span class="number">
            li_chunks = re.findall(r'<li[^>]*>(.*?)</li>', block, re.DOTALL)
            for chunk in li_chunks:
                name_m = re.search(r'class="name"[^>]*>(.*?)</span>', chunk, re.DOTALL)
                num_m  = re.search(r'class="number"[^>]*>([\d.,\-]+)</span>', chunk)
                if name_m and num_m:
                    label = re.sub(r'\s+', ' ', name_m.group(1)).strip()
                    try:
                        ratios[label.lower()] = float(num_m.group(1).replace(",", ""))
                    except ValueError:
                        pass
        except Exception:
            pass

        # ── Map Screener labels to our field names ─────────────────────
        def get(*keys, default=0.0) -> float:
            for k in keys:
                v = ratios.get(k.lower())
                if v is not None:
                    return v
            return default

        data = {
            "pe_ratio":             get("stock p/e", "p/e"),
            "pb_ratio":             get("price to book", "p/b", "book value"),
            "roe":                  get("roe", "return on equity"),
            "debt_to_equity":       get("debt to equity", "d/e"),
            "promoter_holding_pct": get("promoter holding"),
            "revenue_growth_yoy":   get("sales growth", "revenue growth"),
            "profit_growth_yoy":    get("profit growth"),
            "current_ratio":        get("current ratio"),
            "dividend_yield":       get("dividend yield"),
            "market_cap_cr":        get("market cap"),
        }

        # ── Meta description fallback (always present, no JS needed) ───
        meta_m = re.search(r'name="description"\s+content="([^"]+)"', html)
        if meta_m:
            desc = meta_m.group(1)
            if data["roe"] == 0.0:
                m = re.search(r'return on equity of ([\d.]+)%', desc, re.IGNORECASE)
                if m: data["roe"] = float(m.group(1))
            if data["promoter_holding_pct"] == 0.0:
                m = re.search(r'Promoter Holding:\s*([\d.]+)%', desc, re.IGNORECASE)
                if m: data["promoter_holding_pct"] = float(m.group(1))

        logger.debug(f"Screener {symbol}: PE={data['pe_ratio']} ROE={data['roe']} D/E={data['debt_to_equity']}")
        return data

    # ── Cache helpers ─────────────────────────────────────────────────────
    def _load_cache(self, key: str, max_age_hours: int) -> dict:
        path = CACHE_DIR / f"{key}.json"
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            return None
        try:
            return json.loads(path.read_text())
        except:
            return None

    def _save_cache(self, key: str, data: dict):
        path = CACHE_DIR / f"{key}.json"
        tmp_path = CACHE_DIR / f"{key}.{time.time_ns()}.tmp"
        tmp_path.write_text(json.dumps(data))
        tmp_path.replace(path)

    @staticmethod
    def _empty() -> dict:
        return {
            "pe_ratio": 25.0, "pb_ratio": 3.0, "roe": 15.0,
            "debt_to_equity": 0.5, "promoter_holding_pct": 50.0,
            "revenue_growth_yoy": 10.0, "profit_growth_yoy": 10.0,
            "current_ratio": 1.5, "dividend_yield": 1.0,
            "market_cap_cr": 10000.0, "fundamental_score": 50.0,
        }
