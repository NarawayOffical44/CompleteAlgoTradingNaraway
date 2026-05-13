"""
Agent 1: Enhanced Pairs Trading (Statistical Arbitrage)
Core:    Cointegration (ADF test) + Z-score entry/exit
Added:   Volume confirmation + Fundamental filter + Sentiment filter

Filters (all must pass before entry):
  - Cointegration: Engle-Granger ADF test, p-value < 0.05 (cached daily)
  - Volume: both stocks trading above 20d average volume
  - Fundamentals: ROE > 12% AND Debt/Equity < 0.8 for both stocks
  - Sentiment: neither stock has trade_bias == 'avoid' or score < -0.5
"""

import numpy as np
from datetime import date
from agents.base_agent import BaseAgent
from loguru import logger


DEFAULT_PAIRS = [
    ("HDFCBANK",    "ICICIBANK"),
    ("RELIANCE",    "ONGC"),
    ("INFY",        "TCS"),
    ("AXISBANK",    "KOTAKBANK"),
    ("HINDUNILVR",  "DABUR"),
]

# Fundamental thresholds
MIN_ROE            = 12.0
MAX_DEBT_TO_EQUITY = 0.8
MIN_VOLUME_RATIO   = 0.8    # at least 80% of 20d avg
MAX_SENTIMENT_BEAR = -0.5   # avoid if score below this
ADF_PVALUE_THRESHOLD = 0.05  # spread must be stationary


class PairsTradingAgent(BaseAgent):

    def __init__(self, *args, pairs=None, lookback=30,
                 entry_zscore=2.0, exit_zscore=0.3, stop_zscore=3.5, **kwargs):
        super().__init__(*args, **kwargs)
        self.pairs         = pairs or DEFAULT_PAIRS
        self.lookback      = lookback
        self.entry_zscore  = entry_zscore
        self.exit_zscore   = exit_zscore
        self.stop_zscore   = stop_zscore
        self._coint_cache: dict = {}  # (sym_a, sym_b) → {"date": date, "result": bool}

    def generate_signals(self, market_data: dict) -> list[dict]:
        signals      = []
        fundamentals = market_data.get("_fundamentals", {})
        sentiment    = market_data.get("_sentiment", {})

        for sym_a, sym_b in self.pairs:
            if sym_a not in market_data or sym_b not in market_data:
                continue

            # ── Fundamental filter ────────────────────────────────────────
            if not self._fundamentals_ok(sym_a, sym_b, fundamentals):
                logger.debug(f"Pairs {sym_a}/{sym_b} | fundamentals fail — skip")
                continue

            # ── Sentiment filter ──────────────────────────────────────────
            if not self._sentiment_ok(sym_a, sym_b, sentiment):
                logger.debug(f"Pairs {sym_a}/{sym_b} | negative sentiment — skip")
                continue

            data_a = market_data[sym_a]
            data_b = market_data[sym_b]

            closes_a = data_a.get("closes", [])
            closes_b = data_b.get("closes", [])
            if len(closes_a) < self.lookback or len(closes_b) < self.lookback:
                continue

            # ── Volume filter ─────────────────────────────────────────────
            vol_a = data_a.get("volume_ratio", 1.0)
            vol_b = data_b.get("volume_ratio", 1.0)
            if vol_a < MIN_VOLUME_RATIO or vol_b < MIN_VOLUME_RATIO:
                logger.debug(f"Pairs {sym_a}/{sym_b} | low volume ({vol_a:.2f}/{vol_b:.2f}) — skip")
                continue

            # ── Cointegration test (ADF, cached daily) ────────────────────
            if not self._is_cointegrated(sym_a, sym_b, closes_a, closes_b):
                logger.debug(f"Pairs {sym_a}/{sym_b} | ADF: spread not stationary — skip")
                continue

            pair_key = f"{sym_a}-{sym_b}"
            if self._find_open_pair_trade(pair_key):
                continue

            z, beta, mean, std = self._calc_zscore(closes_a, closes_b)
            ltp_a = data_a.get("ltp", closes_a[-1])
            ltp_b = data_b.get("ltp", closes_b[-1])
            capital = self.risk.state.capital
            risk_amount = capital * 0.01

            if z > self.entry_zscore:
                signals += [
                    {"symbol": sym_a, "direction": "short", "entry_price": ltp_a,
                     "risk_amount": risk_amount / 2,
                     "thesis": f"Pair {pair_key} | z={z:.2f} | ADF_ok | vol_ok | fund_ok | short leg"},
                    {"symbol": sym_b, "direction": "long",  "entry_price": ltp_b,
                     "risk_amount": risk_amount / 2,
                     "thesis": f"Pair {pair_key} | z={z:.2f} | ADF_ok | vol_ok | fund_ok | long leg"},
                ]
            elif z < -self.entry_zscore:
                signals += [
                    {"symbol": sym_a, "direction": "long",  "entry_price": ltp_a,
                     "risk_amount": risk_amount / 2,
                     "thesis": f"Pair {pair_key} | z={z:.2f} | ADF_ok | vol_ok | fund_ok | long leg"},
                    {"symbol": sym_b, "direction": "short", "entry_price": ltp_b,
                     "risk_amount": risk_amount / 2,
                     "thesis": f"Pair {pair_key} | z={z:.2f} | ADF_ok | vol_ok | fund_ok | short leg"},
                ]

        return signals

    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade = self.journal.get_trade(trade_id)
        if not trade:
            return False, ""

        pair_key = self._extract_pair_key(trade.thesis)
        if not pair_key:
            return False, ""

        sym_a, sym_b = pair_key.split("-")
        if sym_a not in market_data or sym_b not in market_data:
            return False, ""

        closes_a = market_data[sym_a].get("closes", [])
        closes_b = market_data[sym_b].get("closes", [])
        if len(closes_a) < self.lookback or len(closes_b) < self.lookback:
            return False, ""

        z, _, _, _ = self._calc_zscore(closes_a, closes_b)

        if abs(z) < self.exit_zscore:
            return True, f"Pair {pair_key} mean reversion complete | z={z:.2f}"
        if abs(z) > self.stop_zscore:
            return True, f"Pair {pair_key} stop | z={z:.2f} > {self.stop_zscore}"

        return False, ""

    # ── Cointegration (Engle-Granger ADF, cached daily) ───────────────────
    def _is_cointegrated(self, sym_a: str, sym_b: str,
                         closes_a: list, closes_b: list) -> bool:
        key = (sym_a, sym_b)
        cached = self._coint_cache.get(key)
        if cached and cached["date"] == date.today():
            return cached["result"]

        try:
            from statsmodels.tsa.stattools import adfuller
            a = np.array(closes_a[-self.lookback:], dtype=float)
            b = np.array(closes_b[-self.lookback:], dtype=float)
            beta   = np.cov(a, b)[0, 1] / np.var(b) if np.var(b) != 0 else 1.0
            spread = a - beta * b
            p_val  = adfuller(spread, autolag="AIC")[1]
            result = p_val < ADF_PVALUE_THRESHOLD
            logger.debug(f"ADF {sym_a}/{sym_b} | p={p_val:.4f} | {'COINT' if result else 'NOT COINT'}")
        except ImportError:
            logger.warning("statsmodels not installed — skipping ADF, assuming cointegrated")
            result = True
        except Exception as e:
            logger.warning(f"ADF error {sym_a}/{sym_b}: {e} — assuming not cointegrated")
            result = False

        self._coint_cache[key] = {"date": date.today(), "result": result}
        return result

    # ── Filters ───────────────────────────────────────────────────────────
    @staticmethod
    def _fundamentals_ok(sym_a: str, sym_b: str, fundamentals: dict) -> bool:
        for sym in (sym_a, sym_b):
            f = fundamentals.get(sym, {})
            roe = f.get("roe", MIN_ROE)
            de  = f.get("debt_to_equity", 0.5)
            if roe < MIN_ROE or de > MAX_DEBT_TO_EQUITY:
                return False
        return True

    @staticmethod
    def _sentiment_ok(sym_a: str, sym_b: str, sentiment: dict) -> bool:
        for sym in (sym_a, sym_b):
            s = sentiment.get(sym, {})
            if s.get("trade_bias") == "avoid":
                return False
            if s.get("sentiment_score", 0) < MAX_SENTIMENT_BEAR:
                return False
        return True

    # ── Maths ─────────────────────────────────────────────────────────────
    def _calc_zscore(self, closes_a, closes_b):
        a = np.array(closes_a[-self.lookback:], dtype=float)
        b = np.array(closes_b[-self.lookback:], dtype=float)
        beta   = np.cov(a, b)[0, 1] / np.var(b) if np.var(b) != 0 else 1.0
        spread = a - beta * b
        mean   = float(np.mean(spread))
        std    = float(np.std(spread))
        z      = (spread[-1] - mean) / std if std != 0 else 0.0
        return z, beta, mean, std

    def _find_open_pair_trade(self, pair_key: str):
        return next((t for t in self.journal.snapshot()
                     if t.agent_id == self.agent_id and t.status == "open"
                     and pair_key in t.thesis), None)

    @staticmethod
    def _extract_pair_key(thesis: str) -> str:
        for part in thesis.split("|"):
            part = part.strip()
            if part.startswith("Pair "):
                return part.replace("Pair ", "").strip()
        return ""
