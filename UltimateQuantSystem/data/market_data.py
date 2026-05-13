"""
Market Data Module — Fetches OHLCV + volume + delivery data.
Sources:
  - Dhan API     → live quotes, OHLCV
  - NSE website  → delivery %, FII/DII flows, advance/decline
  - VIX          → India VIX (NSE)

All methods return a standardized dict that agents and orchestrator expect.
Paper mode: returns cached/simulated data when API unavailable.
"""

import json
import time
import requests
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from config import config


CACHE_DIR = Path("data/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

# Nifty 50 universe
NIFTY50_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "KOTAKBANK", "SBIN", "BHARTIARTL", "ITC",
    "BAJFINANCE", "LT", "AXISBANK", "ASIANPAINT", "MARUTI",
    "WIPRO", "ONGC", "HCLTECH", "NTPC", "POWERGRID",
    "ULTRACEMCO", "TITAN", "NESTLEIND", "JSWSTEEL", "TATASTEEL",
    "SUNPHARMA", "DRREDDY", "DIVISLAB", "CIPLA", "APOLLOHOSP",
    "ADANIENT", "ADANIPORTS", "BAJAJFINSV", "BAJAJ-AUTO", "EICHERMOT",
    "HEROMOTOCO", "BRITANNIA", "TATACONSUM", "DABUR", "MARICO",
    "BPCL", "COALINDIA", "HINDALCO", "VEDL", "TATAMOTORS",
    "M&M", "TECHM", "LTIM", "INDUSINDBK", "GRASIM",
]


class MarketDataFetcher:

    def __init__(self, broker=None):
        self.broker   = broker
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        self._warm_nse_session()

    # ── Main method: fetch full enriched data for all symbols ─────────────
    def get_market_data(self, symbols: list = None) -> dict:
        """
        Returns enriched market_data dict consumed by all agents.
        {
          symbol: {
            ltp, closes, highs, lows, volumes,
            volume_ratio,        # today vol / 20d avg
            delivery_pct,        # % of traded vol that is delivery
            1d_return, 5d_return,
            vs_200dma,
          },
          "NIFTY":  { ltp, closes, 1d_return, 5d_return, vs_200dma, iv_rank },
          "VIX":    { ltp, 7d_change },
          "PCR":    float,
          "FII_FLOW": float,
          "ADR":    float,
          "DAYS_TO_EVENT": int,
        }
        """
        symbols = symbols or NIFTY50_SYMBOLS
        data    = {}

        # Index data
        data["NIFTY"]  = self._get_index_data("NIFTY 50")
        data["VIX"]    = self._get_vix()
        data["PCR"]    = self._get_put_call_ratio()
        data["FII_FLOW"]     = self._get_fii_flow()
        data["ADR"]          = self._get_advance_decline()
        data["DAYS_TO_EVENT"] = self._days_to_next_event()

        # Per-stock data (batched to avoid rate limits)
        for i, sym in enumerate(symbols):
            try:
                data[sym] = self._get_stock_data(sym)
                if i > 0 and i % 10 == 0:
                    time.sleep(1)   # polite rate limiting
            except Exception as e:
                logger.warning(f"Data fetch failed for {sym}: {e}")
                data[sym] = self._empty_stock()

        return data

    # ── Index OHLCV ───────────────────────────────────────────────────────
    def _get_index_data(self, index_name: str) -> dict:
        cache_key = f"index_{index_name.replace(' ', '_')}"
        cached    = self._load_cache(cache_key, max_age_mins=5)
        if cached:
            return cached

        try:
            url = "https://www.nseindia.com/api/allIndices"
            r   = self._session.get(url, timeout=10)
            all_indices = r.json().get("data", [])
            nifty = next((x for x in all_indices if x["index"] == index_name), {})

            ltp    = float(nifty.get("last", 0))
            prev   = float(nifty.get("previousClose", ltp))
            hist   = self._get_index_history(index_name)

            result = {
                "ltp":        ltp,
                "closes":     hist["closes"],
                "highs":      hist["highs"],
                "lows":       hist["lows"],
                "volumes":    hist["volumes"],
                "1d_return":  round((ltp / prev - 1) * 100, 3) if prev else 0,
                "5d_return":  self._period_return(hist["closes"], 5),
                "vs_200dma":  self._vs_dma(hist["closes"], 200),
                "iv_rank":    self._calc_iv_rank(),
            }
            self._save_cache(cache_key, result)
            return result
        except Exception as e:
            logger.warning(f"Index data failed: {e}")
            return self._empty_stock()

    # ── Stock OHLCV + Volume + Delivery ──────────────────────────────────
    def _get_stock_data(self, symbol: str) -> dict:
        cache_key = f"stock_{symbol}"
        cached    = self._load_cache(cache_key, max_age_mins=5)
        if cached:
            return cached

        # ── Try NSE live quote first ───────────────────────────────────────
        ltp = None
        prev = None
        vol_today = None
        delivery_pct = 50.0
        try:
            url  = f"https://www.nseindia.com/api/quote-equity?symbol={requests.utils.quote(symbol)}"
            r    = self._session.get(url, timeout=10)
            q    = r.json()
            ltp  = float(q["priceInfo"]["lastPrice"])
            prev = float(q["priceInfo"]["previousClose"])
            trade_info = q.get("marketDeptOrderBook", {}).get("tradeInfo", {})
            vol_today  = float(trade_info.get("totalTradedVolume") or 0)
            delivery_pct = float((q.get("securityWiseDP") or {}).get("deliveryToTradedQuantity") or 50)
        except Exception as e:
            logger.debug(f"NSE quote failed for {symbol}: {e}")

        # ── Always fetch historical OHLCV ─────────────────────────────────
        hist = self._get_stock_history(symbol)
        if not hist["closes"]:
            return self._empty_stock()

        # ── If NSE quote failed, use Yahoo Finance last close as LTP ───────
        if ltp is None:
            ltp  = hist["closes"][-1]
            prev = hist["closes"][-2] if len(hist["closes"]) >= 2 else ltp
            vol_today = hist["volumes"][-1] if hist["volumes"] else 0
            logger.debug(f"NSE quote unavailable for {symbol} — using YF last close ltp={ltp:.2f}")

        vol_20d_avg = np.mean(hist["volumes"][-20:]) if len(hist["volumes"]) >= 20 else (vol_today or 1)

        result = {
            "ltp":          ltp,
            "closes":       hist["closes"],
            "highs":        hist["highs"],
            "lows":         hist["lows"],
            "volumes":      hist["volumes"],
            "volume_ratio": round(vol_today / vol_20d_avg, 2) if vol_20d_avg else 1.0,
            "delivery_pct": delivery_pct,
            "1d_return":    round((ltp / prev - 1) * 100, 3) if prev else 0,
            "5d_return":    self._period_return(hist["closes"], 5),
            "vs_200dma":    self._vs_dma(hist["closes"], 200),
        }
        self._save_cache(cache_key, result)
        return result

    # ── Historical OHLCV (NSE) ────────────────────────────────────────────
    def _get_stock_history(self, symbol: str, days: int = 250) -> dict:
        cache_key = f"hist_{symbol}"
        cached    = self._load_cache(cache_key, max_age_mins=60)
        if cached:
            return cached

        result = None

        # ── Try NSE API first ──────────────────────────────────────────────
        try:
            end   = datetime.now()
            start = end - timedelta(days=days + 50)
            url   = (f"https://www.nseindia.com/api/historical/cm/equity"
                     f"?symbol={requests.utils.quote(symbol)}"
                     f"&series=[%22EQ%22]"
                     f"&from={start.strftime('%d-%m-%Y')}"
                     f"&to={end.strftime('%d-%m-%Y')}")
            r = self._session.get(url, timeout=5)
            rows = r.json().get("data", []) if r.text.strip() else []
            if rows:
                result = {
                    "closes":  [float(d["CH_CLOSING_PRICE"])    for d in rows],
                    "highs":   [float(d["CH_TRADE_HIGH_PRICE"]) for d in rows],
                    "lows":    [float(d["CH_TRADE_LOW_PRICE"])  for d in rows],
                    "volumes": [float(d["CH_TOT_TRADED_QTY"])   for d in rows],
                }
        except Exception as e:
            logger.debug(f"NSE history failed for {symbol}: {e}")

        # ── Fallback: Yahoo Finance (.NS then .BO) ────────────────────────
        if not result:
            result = self._yfinance_stock(f"{symbol}.NS", label=symbol)
        if not result:
            result = self._yfinance_stock(f"{symbol}.BO", label=f"{symbol}(BSE)")

        if result:
            self._save_cache(cache_key, result)
            return result
        return {"closes": [], "highs": [], "lows": [], "volumes": []}

    def _get_index_history(self, index_name: str, days: int = 250) -> dict:
        cache_key = f"hist_idx_{index_name.replace(' ', '_')}"
        cached    = self._load_cache(cache_key, max_age_mins=60)
        if cached:
            return cached

        result = None

        # ── Try NSE API first ──────────────────────────────────────────────
        try:
            end   = datetime.now()
            start = end - timedelta(days=days + 50)
            url   = (f"https://www.nseindia.com/api/historical/indicesHistory"
                     f"?indexType={requests.utils.quote(index_name)}"
                     f"&from={start.strftime('%d-%m-%Y')}"
                     f"&to={end.strftime('%d-%m-%Y')}")
            r = self._session.get(url, timeout=5)
            rows = r.json().get("data", {}).get("indexCloseOnlineRecords", []) if r.text.strip() else []
            if rows:
                result = {
                    "closes":  [float(d["EOD_CLOSE_INDEX_VAL"])           for d in rows],
                    "highs":   [float(d["EOD_HIGH_INDEX_VAL"])             for d in rows],
                    "lows":    [float(d["EOD_LOW_INDEX_VAL"])              for d in rows],
                    "volumes": [float(d.get("EOD_TRADED_VALUE", 0))        for d in rows],
                }
        except Exception as e:
            logger.debug(f"NSE index history failed: {e}")

        # ── Fallback: yfinance (thread-guarded, 20s timeout) ──────────────
        if not result:
            yf_map = {"NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK", "INDIA VIX": "^INDIAVIX"}
            yf_sym = yf_map.get(index_name)
            if yf_sym:
                result = self._yfinance_stock(yf_sym, label=index_name)

        if result:
            self._save_cache(cache_key, result)
            return result
        return {"closes": [], "highs": [], "lows": [], "volumes": []}

    # ── Market breadth ────────────────────────────────────────────────────
    def _get_vix(self) -> dict:
        try:
            url = "https://www.nseindia.com/api/allIndices"
            r   = self._session.get(url, timeout=10)
            all_idx = r.json().get("data", [])
            vix_row = next((x for x in all_idx if "VIX" in x.get("index", "")), {})
            ltp  = float(vix_row.get("last", 15))
            prev = float(vix_row.get("previousClose", ltp))
            return {"ltp": ltp, "7d_change": round(ltp - prev, 2)}
        except:
            return {"ltp": 15.0, "7d_change": 0}

    def _get_put_call_ratio(self) -> float:
        try:
            url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
            r   = self._session.get(url, timeout=10)
            data = r.json()
            total_ce_oi = sum(x.get("CE", {}).get("openInterest", 0)
                              for x in data.get("filtered", {}).get("data", []))
            total_pe_oi = sum(x.get("PE", {}).get("openInterest", 0)
                              for x in data.get("filtered", {}).get("data", []))
            return round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 1.0
        except:
            return 1.0

    def _get_fii_flow(self) -> float:
        """FII net flow in Crores (positive = buying)."""
        try:
            url = "https://www.nseindia.com/api/fiidiiTradeReact"
            r   = self._session.get(url, timeout=10)
            rows = r.json()
            fii = next((x for x in rows if x.get("category") == "FII/FPI"), {})
            return float(fii.get("netVal", 0))
        except:
            return 0.0

    def _get_advance_decline(self) -> float:
        try:
            url = "https://www.nseindia.com/api/allIndices"
            r   = self._session.get(url, timeout=10)
            data = r.json()
            advances = data.get("advances", 1)
            declines = data.get("declines", 1)
            return round(advances / declines, 2) if declines else 1.0
        except:
            return 1.0

    def _calc_iv_rank(self, lookback: int = 252) -> float:
        """IV Rank = (current VIX - 52w low) / (52w high - 52w low) * 100"""
        try:
            cache = self._load_cache("vix_history", max_age_mins=240)
            if cache:
                vix_hist = cache["values"]
            else:
                vix_hist = [15.0] * lookback   # fallback
            current_vix = self._get_vix()["ltp"]
            low  = min(vix_hist[-lookback:])
            high = max(vix_hist[-lookback:])
            return round((current_vix - low) / (high - low) * 100, 1) if high != low else 50.0
        except:
            return 50.0

    # ── Yahoo Finance fallback (direct requests, no library dependency) ──
    _YF_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/json",
    }

    def _yfinance_stock(self, symbol: str, label: str = None) -> dict | None:
        """
        Fetch 1yr OHLCV from Yahoo Finance v8 chart API.
        symbol: Yahoo Finance ticker (e.g. 'RELIANCE.NS', '^NSEI')
        Returns dict or None on failure.
        """
        try:
            url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
                   f"?interval=1d&range=1y")
            r = requests.get(url, headers=self._YF_HEADERS, timeout=15)
            data = r.json()
            chart = data.get("chart", {}).get("result", [])
            if not chart:
                return None
            q = chart[0]["indicators"]["quote"][0]
            closes  = [x for x in q.get("close",  []) if x is not None]
            highs   = [x for x in q.get("high",   []) if x is not None]
            lows    = [x for x in q.get("low",    []) if x is not None]
            volumes = [x for x in q.get("volume", []) if x is not None]
            if not closes:
                return None
            logger.debug(f"Yahoo Finance OK for {label or symbol} ({len(closes)} bars)")
            return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}
        except Exception as e:
            logger.debug(f"Yahoo Finance fallback failed for {label or symbol}: {e}")
            return None

    # ── Utilities ─────────────────────────────────────────────────────────
    @staticmethod
    def _period_return(closes: list, period: int) -> float:
        if len(closes) <= period:
            return 0.0
        return round((closes[-1] / closes[-period - 1] - 1) * 100, 3)

    @staticmethod
    def _vs_dma(closes: list, period: int) -> float:
        if len(closes) < period:
            return 0.0
        dma = np.mean(closes[-period:])
        return round((closes[-1] / dma - 1) * 100, 2)

    @staticmethod
    def _days_to_next_event() -> int:
        """Hard-coded upcoming events — update manually or wire to calendar API."""
        known_events = [
            datetime(2026, 6, 6),    # RBI policy
            datetime(2026, 7, 23),   # Union Budget
        ]
        today = datetime.now()
        future = [e for e in known_events if e > today]
        return (min(future) - today).days if future else 30

    def _warm_nse_session(self):
        """Warm NSE session cookies. Retries with cookie clear if first attempt fails."""
        try:
            self._session.get("https://www.nseindia.com", timeout=10)
            r = self._session.get("https://www.nseindia.com/api/allIndices", timeout=10)
            if r.status_code != 200 or not r.text.strip():
                logger.warning("NSE session stale — clearing cookies and retrying")
                self._session.cookies.clear()
                self._session.get("https://www.nseindia.com", timeout=10)
        except Exception as e:
            logger.warning(f"NSE session warm-up failed: {e}")

    def _load_cache(self, key: str, max_age_mins: int) -> dict:
        path = CACHE_DIR / f"{key}.json"
        if not path.exists():
            return None
        age = (time.time() - path.stat().st_mtime) / 60
        if age > max_age_mins:
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
    def _empty_stock() -> dict:
        return {
            "ltp": 0, "closes": [], "highs": [], "lows": [], "volumes": [],
            "volume_ratio": 1.0, "delivery_pct": 50.0,
            "1d_return": 0, "5d_return": 0, "vs_200dma": 0,
        }
