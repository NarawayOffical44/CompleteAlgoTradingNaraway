"""
Agent 3: Momentum + Volume + Sentiment (FINAL — all upgrades included)

Filters (ALL must pass):
  1. Regime:           BULL_LOW_VOL or BULL_HIGH_VOL only
  2. Price breakout:   LTP > 20-day high (excluding today)
  3. Volume:           today vol >= 1.5x 20d average (confirmed breakout)
  4. RSI filter:       RSI(14) < 70 — avoid buying into overbought exhaustion
  5. Sentiment:        score >= 0 AND bias in (long_bias, neutral)
  6. Fundamentals:     revenue_growth_yoy >= 8% AND profit_growth_yoy >= 5%
  7. Relative strength: stock 5d return > Nifty 5d return (outperforming index)

Position sizing: volatility-adjusted (1% capital / ATR per share)
Exit: Chandelier trailing stop (max_high_since_entry - 3*ATR) — only moves up
"""

import numpy as np
from agents.base_agent import BaseAgent
from loguru import logger


SAFE_REGIMES        = {"BULL_LOW_VOL", "BULL_HIGH_VOL"}
BREAKOUT_PERIOD     = 20
MIN_VOLUME_RATIO    = 1.5       # breakout bar must be above-average vol
MAX_RSI_ENTRY       = 70.0      # don't buy overbought breakouts
MIN_SENTIMENT_SCORE = 0.0
ALLOWED_BIAS        = {"long_bias", "neutral"}
MIN_REVENUE_GROWTH  = 8.0
MIN_PROFIT_GROWTH   = 5.0
ATR_PERIOD          = 14
CHANDELIER_MULT     = 3.0       # tighter than 2x for chandelier
MAX_POSITIONS       = 4


class MomentumAgent(BaseAgent):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._chandelier_highs: dict[str, float] = {}   # trade_id → max_high since entry

    # ── Signal generation ─────────────────────────────────────────────────
    def generate_signals(self, market_data: dict) -> list[dict]:
        signals      = []
        regime       = market_data.get("_regime", "UNKNOWN")
        fundamentals = market_data.get("_fundamentals", {})
        sentiment    = market_data.get("_sentiment", {})
        nifty_5d     = market_data.get("NIFTY", {}).get("5d_return", 0.0)

        if regime not in SAFE_REGIMES:
            logger.info(f"Momentum | regime={regime} — needs bull, skip all")
            return signals

        open_count = sum(1 for t in self.journal.trades.values()
                         if t.agent_id == self.agent_id and t.status == "open")
        if open_count >= MAX_POSITIONS:
            return signals

        for symbol, data in market_data.items():
            if symbol.startswith("_") or symbol in ("NIFTY", "BANKNIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT"):
                continue

            closes  = data.get("closes", [])
            highs   = data.get("highs", [])
            lows    = data.get("lows", [])
            volumes = data.get("volumes", [])

            if len(closes) < BREAKOUT_PERIOD + 1 or len(highs) < BREAKOUT_PERIOD + 1:
                continue

            ltp = data.get("ltp", closes[-1])

            # ── 1. Price breakout above 20-day high ───────────────────────
            period_high = max(highs[-(BREAKOUT_PERIOD + 1):-1])   # exclude today
            if ltp <= period_high:
                continue

            # ── 2. Volume confirmation ────────────────────────────────────
            vol_ratio = data.get("volume_ratio", 1.0)
            if vol_ratio < MIN_VOLUME_RATIO:
                continue

            # ── 3. RSI filter — no overbought entries ─────────────────────
            rsi = self._rsi(closes)
            if rsi > MAX_RSI_ENTRY:
                logger.debug(f"Momentum {symbol} | RSI={rsi:.1f} overbought — skip")
                continue

            # ── 4. Sentiment filter ───────────────────────────────────────
            s = sentiment.get(symbol, {})
            if not (s.get("sentiment_score", 0) >= MIN_SENTIMENT_SCORE
                    and s.get("trade_bias", "neutral") in ALLOWED_BIAS):
                continue

            # ── 5. Fundamental filter (earnings growth) ───────────────────
            f = fundamentals.get(symbol, {})
            if (f.get("revenue_growth_yoy", MIN_REVENUE_GROWTH) < MIN_REVENUE_GROWTH or
                    f.get("profit_growth_yoy", MIN_PROFIT_GROWTH) < MIN_PROFIT_GROWTH):
                continue

            # ── 6. Relative strength vs Nifty ─────────────────────────────
            stock_5d = data.get("5d_return", 0.0)
            if stock_5d <= nifty_5d:
                logger.debug(f"Momentum {symbol} | 5d return {stock_5d:.2f}% <= Nifty {nifty_5d:.2f}% — skip")
                continue

            # ── Position sizing: volatility-adjusted ─────────────────────
            atr            = self._atr(highs, lows, closes)
            stop           = ltp - (CHANDELIER_MULT * atr)
            risk_per_share = ltp - stop
            capital        = self.risk.state.capital
            risk_amount    = capital * 0.01
            quantity       = risk_amount / risk_per_share if risk_per_share > 0 else 1

            signals.append({
                "symbol":       symbol,
                "direction":    "long",
                "entry_price":  ltp,
                "quantity":     quantity,
                "risk_amount":  risk_amount,
                "thesis":       (f"Breakout {BREAKOUT_PERIOD}d high={period_high:.0f} | "
                                 f"vol={vol_ratio:.2f}x | RSI={rsi:.0f} | "
                                 f"sent={s.get('sentiment_score', 0):.2f} | "
                                 f"RS={stock_5d:.1f}% vs Nifty {nifty_5d:.1f}% | "
                                 f"rev_g={f.get('revenue_growth_yoy', 0):.1f}%"),
            })

        return signals

    # ── Exit logic: Chandelier trailing stop ──────────────────────────────
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade = self.journal.trades.get(trade_id)
        if not trade:
            return False, ""

        # Sentiment emergency exit
        s = market_data.get("_sentiment", {}).get(trade.symbol, {})
        if s.get("trade_bias") == "avoid" or s.get("sentiment_score", 0) < -0.5:
            self._chandelier_highs.pop(trade_id, None)
            return True, f"Sentiment bearish | score={s.get('sentiment_score', 0):.2f}"

        data   = market_data.get(trade.symbol, {})
        ltp    = data.get("ltp", trade.entry_price)
        highs  = data.get("highs", [])
        lows   = data.get("lows", [])
        closes = data.get("closes", [])
        atr    = self._atr(highs, lows, closes)

        # Track the highest price seen since entry (chandelier logic)
        max_high = self._chandelier_highs.get(trade_id, trade.entry_price)
        if ltp > max_high:
            self._chandelier_highs[trade_id] = ltp
            max_high = ltp

        # Chandelier stop = max_high - 3*ATR (only moves up with price)
        stop = max_high - (CHANDELIER_MULT * atr)

        if ltp < stop:
            self._chandelier_highs.pop(trade_id, None)
            return True, f"Chandelier stop | ltp={ltp:.2f} max_high={max_high:.2f} stop={stop:.2f}"

        return False, ""

    # ── Indicators ────────────────────────────────────────────────────────
    @staticmethod
    def _rsi(closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        c    = np.array(closes[-(period + 1):], dtype=float)
        diff = np.diff(c)
        gain = np.where(diff > 0, diff, 0.0)
        loss = np.where(diff < 0, -diff, 0.0)
        avg_gain = np.mean(gain)
        avg_loss = np.mean(loss)
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 2)

    @staticmethod
    def _atr(highs: list, lows: list, closes: list, period: int = ATR_PERIOD) -> float:
        if not highs or len(highs) < 2:
            return closes[-1] * 0.015 if closes else 1.0
        n   = min(period, len(highs) - 1)
        trs = [max(highs[-i] - lows[-i],
                   abs(highs[-i] - closes[-i - 1]),
                   abs(lows[-i]  - closes[-i - 1]))
               for i in range(1, n + 1)]
        return sum(trs) / len(trs) if trs else closes[-1] * 0.015
