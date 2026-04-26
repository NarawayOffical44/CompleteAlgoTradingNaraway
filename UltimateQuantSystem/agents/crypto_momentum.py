"""
CryptoMomentumBot — 5-day breakout momentum on BTC/ETH/BNB.

Entry conditions:
  - Price breaks 5-day high (momentum confirmation)
  - RSI 50–72 (not overextended)
  - Volume ratio >= 1.5x (breakout has volume)
  - Regime is BULL_LOW_VOL or CHOPPY (not BEAR)
  - Market sentiment score > -0.3 (Fear & Greed not extreme fear)

Exit conditions:
  - 3-day max hold
  - 5% stop loss from entry
  - RSI > 78 (overbought exit)

Position sizing:
  - 1% max risk per trade (same as NSE bots via RiskEngine)
  - Stop is 5% below entry → quantity = risk_amount / (entry * 0.05)
"""

from agents.base_agent import BaseAgent
from loguru import logger


SYMBOLS     = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
STOP_PCT    = 0.05      # 5% stop loss
MAX_HOLD_D  = 3         # max days to hold
MIN_VOL_RAT = 1.5       # minimum volume ratio for entry
RSI_MIN     = 50
RSI_MAX     = 72
RSI_EXIT    = 78
MIN_MKT_SENT= -0.3      # Fear & Greed floor


class CryptoMomentumBot(BaseAgent):

    # ── Signal generation ─────────────────────────────────────────────────
    def generate_signals(self, market_data: dict) -> list[dict]:
        regime   = market_data.get("_regime", "UNKNOWN")
        mkt_sent = market_data.get("_market_sentiment", {})
        sent_score = mkt_sent.get("score", 0.0) if isinstance(mkt_sent, dict) else 0.0

        if regime == "BEAR_HIGH_VOL":
            logger.info(f"{self.agent_id} | BEAR regime — skip all longs")
            return []

        if sent_score < MIN_MKT_SENT:
            logger.info(f"{self.agent_id} | Fear&Greed score={sent_score:.2f} < {MIN_MKT_SENT} — skip")
            return []

        signals = []

        for symbol in SYMBOLS:
            data = market_data.get(symbol, {})
            closes     = data.get("closes",       [])
            highs      = data.get("highs",        [])
            vol_ratio  = data.get("volume_ratio", 0.0)
            ltp        = data.get("ltp",          0.0)

            if len(closes) < 6:
                continue

            # 5-day high breakout
            five_day_high = max(highs[-6:-1])
            if ltp <= five_day_high:
                continue

            # Volume confirmation
            if vol_ratio < MIN_VOL_RAT:
                logger.info(f"{self.agent_id} | {symbol} | vol_ratio={vol_ratio:.1f}x < {MIN_VOL_RAT}x — skip")
                continue

            # RSI filter
            rsi = self._calc_rsi(closes, period=14)
            if not (RSI_MIN <= rsi <= RSI_MAX):
                logger.info(f"{self.agent_id} | {symbol} | RSI={rsi:.1f} outside [{RSI_MIN},{RSI_MAX}] — skip")
                continue

            # Already have open position in this symbol?
            open_trades = [t for t in self.journal.trades.values()
                           if t.agent_id == self.agent_id
                           and t.symbol == symbol
                           and t.status == "open"]
            if open_trades:
                continue

            # Risk amount: stop is STOP_PCT below entry
            # quantity = risk_amount / (entry * STOP_PCT)  → already handled in _calc_quantity
            stop_price  = ltp * (1 - STOP_PCT)
            risk_amount = ltp * STOP_PCT       # risk per unit × 1 unit (quantity scaled by RiskEngine)

            signals.append({
                "symbol":      symbol,
                "direction":   "long",
                "entry_price": ltp,
                "risk_amount": risk_amount,
                "thesis": (f"5d breakout {five_day_high:,.2f}→{ltp:,.2f} | "
                           f"RSI={rsi:.0f} | vol={vol_ratio:.1f}x | "
                           f"regime={regime} | F&G={sent_score:.2f}"),
            })

            logger.info(f"{self.agent_id} | SIGNAL {symbol} LONG @ {ltp:,.2f} | "
                        f"stop={stop_price:,.2f} | RSI={rsi:.0f} | vol={vol_ratio:.1f}x")

        return signals

    # ── Exit logic ────────────────────────────────────────────────────────
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        from datetime import datetime

        trade = self.journal.trades.get(trade_id)
        if not trade:
            return False, ""

        data   = market_data.get(trade.symbol, {})
        closes = data.get("closes", [])
        ltp    = data.get("ltp", trade.entry_price)

        if not ltp:
            return False, ""

        # Stop loss: 5% below entry
        stop = trade.entry_price * (1 - STOP_PCT)
        if ltp <= stop:
            return True, f"stop_loss: {ltp:,.2f} <= {stop:,.2f}"

        # RSI overbought exit
        if len(closes) >= 14:
            rsi = self._calc_rsi(closes, period=14)
            if rsi > RSI_EXIT:
                return True, f"RSI_overbought: {rsi:.1f} > {RSI_EXIT}"

        # Max hold: 3 days
        try:
            entry_dt = datetime.fromisoformat(trade.entry_time)
            days_held = (datetime.now() - entry_dt).days
            if days_held >= MAX_HOLD_D:
                return True, f"max_hold: {days_held}d >= {MAX_HOLD_D}d"
        except Exception:
            pass

        return False, ""

    # ── Helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _calc_rsi(closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [d for d in deltas[-period:] if d > 0]
        losses = [-d for d in deltas[-period:] if d < 0]
        avg_gain = sum(gains)  / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs  = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)

    @staticmethod
    def _calc_quantity(risk_amount: float, entry_price: float) -> float:
        """Override: crypto quantity = risk_amount / (entry * STOP_PCT)."""
        stop_distance = entry_price * STOP_PCT
        return max(0.001, risk_amount / stop_distance) if stop_distance > 0 else 0.001
