"""
Agent 5: AggressiveMomentumScalper

Completely different from the base MomentumAgent:
  - Shorter breakout: 5-day high (vs 20-day) → more signals, faster entries
  - Higher volume threshold: >= 2x avg (vs 1.5x) → only confirmed moves
  - RSI 55–72: buying into confirmed momentum (vs RSI < 70)
  - Fixed exit targets: +3.5% profit OR 1.5x ATR stop (vs chandelier trailing)
  - Max hold: 3 days (scalper — no overnight bag holding)
  - Regime: BULL_LOW_VOL only (needs calm trending conditions)
  - Risk: 1.5% per trade (vs base 1%)

Why this reduces correlation with MeanReversion:
  - MeanReversion buys weakness (z < -1.8), expects reversion
  - MomentumScalper buys strength (RSI 55-72 + new high), expects continuation
  - They profit in opposite market conditions
"""

import numpy as np
from agents.base_agent import BaseAgent
from loguru import logger


SAFE_REGIMES        = {"BULL_LOW_VOL"}          # Only in calm bull — aggressive strategy
BREAKOUT_PERIOD     = 5                          # 5-day high breakout (fast)
MIN_VOLUME_RATIO    = 2.0                        # Must be 2x avg volume
MIN_RSI             = 55.0                       # Buying into confirmed momentum
MAX_RSI             = 72.0                       # Not overbought yet
MIN_SENTIMENT_SCORE = 0.1                        # Slight positive bias
PROFIT_TARGET_PCT   = 3.5                        # 3.5% profit target
ATR_STOP_MULT       = 1.5                        # 1.5x ATR stop (tighter than base)
MAX_HOLD_DAYS       = 3                          # Exit after 3 days regardless
MAX_POSITIONS       = 3                          # Small, concentrated
RISK_PCT            = 0.015                      # 1.5% risk per trade
MIN_ROE             = 8.0                        # Lighter fundamental bar (momentum stocks)
MIN_FUND_SCORE      = 40.0
RSI_PERIOD          = 14


class MomentumScalper(BaseAgent):

    def generate_signals(self, market_data: dict) -> list[dict]:
        signals      = []
        regime       = market_data.get("_regime", "UNKNOWN")
        fundamentals = market_data.get("_fundamentals", {})
        sentiment    = market_data.get("_sentiment", {})

        # ── Regime gate — BULL only ───────────────────────────────────────
        if regime not in SAFE_REGIMES:
            logger.info(f"MomentumScalper | regime={regime} — needs BULL_LOW_VOL, skip all")
            return signals

        # ── Market trend gate — needs positive Nifty momentum ─────────────
        nifty_5d = market_data.get("NIFTY", {}).get("5d_return", 0.0)
        if nifty_5d < 0:
            logger.info(f"MomentumScalper | Nifty 5d={nifty_5d:.1f}% negative — skip all")
            return signals

        open_count = sum(1 for t in self.journal.trades.values()
                         if t.agent_id == self.agent_id and t.status == "open")
        if open_count >= MAX_POSITIONS:
            return signals

        for symbol, data in market_data.items():
            if symbol.startswith("_") or symbol in (
                    "NIFTY", "BANKNIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT"):
                continue

            closes  = data.get("closes", [])
            highs   = data.get("highs", [])
            volumes = data.get("volumes", [])
            if len(closes) < RSI_PERIOD + BREAKOUT_PERIOD + 5:
                continue

            # ── Fundamental check (lighter than base momentum) ────────────
            f = fundamentals.get(symbol, {})
            if f.get("roe", MIN_ROE) < MIN_ROE or f.get("fundamental_score", MIN_FUND_SCORE) < MIN_FUND_SCORE:
                continue

            # ── Sentiment: needs positive bias ────────────────────────────
            s = sentiment.get(symbol, {})
            if (s.get("trade_bias") == "avoid" or
                    s.get("sentiment_score", 0) < MIN_SENTIMENT_SCORE):
                continue

            # ── Volume: 2x avg (strong confirmation) ─────────────────────
            if data.get("volume_ratio", 1.0) < MIN_VOLUME_RATIO:
                continue

            # ── 5-day breakout ────────────────────────────────────────────
            ltp = data.get("ltp", closes[-1])
            recent_high = max(closes[-BREAKOUT_PERIOD-1:-1]) if len(closes) > BREAKOUT_PERIOD else closes[-1]
            if ltp <= recent_high:
                continue   # Not a new 5-day high

            # ── RSI filter: 55–72 momentum zone ───────────────────────────
            rsi = self._rsi(closes)
            if not (MIN_RSI <= rsi <= MAX_RSI):
                logger.debug(f"MomentumScalper {symbol} | RSI={rsi:.1f} outside 55-72 — skip")
                continue

            # ── Relative strength vs Nifty ─────────────────────────────────
            stock_5d  = data.get("5d_return", 0.0)
            if stock_5d <= nifty_5d:
                logger.debug(f"MomentumScalper {symbol} | stock 5d={stock_5d:.1f}% <= nifty {nifty_5d:.1f}% — skip")
                continue

            # ── Sizing: tighter stop, fixed profit target ─────────────────
            atr            = self._atr(highs, data.get("lows", []), closes)
            stop           = ltp - (ATR_STOP_MULT * atr)
            target         = ltp * (1 + PROFIT_TARGET_PCT / 100)
            risk_per_share = ltp - stop
            if risk_per_share <= 0:
                continue

            capital    = self.risk.state.capital
            risk_amt   = capital * RISK_PCT
            quantity   = risk_amt / risk_per_share

            signals.append({
                "symbol":       symbol,
                "direction":    "long",
                "entry_price":  ltp,
                "quantity":     quantity,
                "risk_amount":  risk_amt,
                "target_price": target,
                "stop_price":   stop,
                "thesis":       (f"5d-breakout={ltp:.2f}>{recent_high:.2f} | RSI={rsi:.1f} | "
                                 f"vol_ratio={data.get('volume_ratio', 1):.1f}x | "
                                 f"nifty_5d={nifty_5d:.1f}% stock_5d={stock_5d:.1f}% | "
                                 f"regime={regime} | sent={s.get('sentiment_score', 0):.2f}"),
            })

        return signals

    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade = self.journal.trades.get(trade_id)
        if not trade:
            return False, ""

        data   = market_data.get(trade.symbol, {})
        closes = data.get("closes", [])
        ltp    = data.get("ltp", trade.entry_price)

        # ── Max hold: 3 days ──────────────────────────────────────────────
        from datetime import date
        today      = date.today()
        entry_date = getattr(trade, "entry_date", today)
        hold_days  = (today - entry_date).days if isinstance(entry_date, type(today)) else 0
        if hold_days >= MAX_HOLD_DAYS:
            return True, f"Max hold {MAX_HOLD_DAYS}d reached | pnl={ltp - trade.entry_price:.2f}"

        # ── Profit target ─────────────────────────────────────────────────
        target = trade.entry_price * (1 + PROFIT_TARGET_PCT / 100)
        if ltp >= target:
            return True, f"Profit target {PROFIT_TARGET_PCT}% | ltp={ltp:.2f} target={target:.2f}"

        # ── ATR stop ──────────────────────────────────────────────────────
        atr  = self._atr(data.get("highs", []), data.get("lows", []), closes)
        stop = trade.entry_price - (ATR_STOP_MULT * atr)
        if ltp <= stop:
            return True, f"ATR stop | ltp={ltp:.2f} stop={stop:.2f}"

        # ── Sentiment flip ────────────────────────────────────────────────
        s = market_data.get("_sentiment", {}).get(trade.symbol, {})
        if s.get("trade_bias") == "avoid":
            return True, "Sentiment flipped to avoid"

        return False, ""

    # ── Indicators ─────────────────────────────────────────────────────────
    @staticmethod
    def _rsi(closes: list, period: int = RSI_PERIOD) -> float:
        c = np.array(closes[-period-1:], dtype=float)
        if len(c) < 2:
            return 50.0
        deltas = np.diff(c)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = gains.mean()
        avg_loss = losses.mean()
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _atr(highs, lows, closes, period: int = 14) -> float:
        if not highs or len(highs) < period:
            return closes[-1] * 0.02 if closes else 1.0
        trs = []
        for i in range(1, min(period + 1, len(highs))):
            tr = max(highs[-i] - lows[-i],
                     abs(highs[-i] - closes[-i-1]) if len(closes) > i else 0,
                     abs(lows[-i]  - closes[-i-1]) if len(closes) > i else 0)
            trs.append(tr)
        return sum(trs) / len(trs) if trs else closes[-1] * 0.02
