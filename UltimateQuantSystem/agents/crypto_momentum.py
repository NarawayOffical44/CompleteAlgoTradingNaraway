"""
CryptoMomentumBot — 5-day breakout momentum on BTC/ETH/BNB.

Uses 4h candles (50 bars ≈ 8 days from CryptoMarket).
5-day high = max of last 30 × 4h bars (= 120h = 5 days).

Entry conditions:
  - Price breaks 5-day high (closes[-1] > max(highs[-31:-1]))
  - RSI 50–72 (not overextended)
  - Volume ratio >= 1.5x current 4h bar vs average
  - Regime is BULL_LOW_VOL or CHOPPY (not BEAR)
  - Market sentiment score > -0.3 (Fear & Greed not extreme fear)

Exit conditions:
  - 3-day max hold (= 18 × 4h bars)
  - 5% stop loss from entry
  - RSI > 78 (overbought exit)

Position sizing:
  - 1% max risk per trade (same as NSE bots via RiskEngine)
  - Stop is 5% below entry → quantity = risk_amount / (entry * 0.05)
"""

import os
from agents.base_agent import BaseAgent
from loguru import logger


DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT",   # large caps
    "SOL/USDT", "AVAX/USDT",               # high-beta L1s
    "FET/USDT", "RNDR/USDT", "TAO/USDT",   # AI narrative
    "INJ/USDT", "WIF/USDT",                # DeFi + Solana meme
]
SYMBOLS = [s.strip() for s in os.getenv("CRYPTO_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]
STOP_PCT    = 0.05      # 5% stop loss
MAX_HOLD_D  = 3         # max days to hold (= 18 × 4h bars)
MIN_VOL_RAT = 1.2       # minimum volume ratio for entry (lowered from 1.5 — CHOPPY markets)
RSI_MIN     = 50
RSI_MAX     = 72
RSI_EXIT    = 78
MIN_MKT_SENT= -0.3      # Fear & Greed floor
# 5 days × 6 (4h bars per day) = 30 bars; exclude current bar → [-31:-1]
FIVE_DAY_BARS = 30


class CryptoMomentumBot(BaseAgent):
    _exchange = "CRYPTO"

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

            # Need 30 bars for 5-day high + at least 14 for RSI + 1 current
            if len(closes) < FIVE_DAY_BARS + 2:
                continue

            # 5-day high: max of last 30 complete 4h bars (excludes current bar)
            five_day_high = max(highs[-(FIVE_DAY_BARS + 1):-1])
            if ltp <= five_day_high:
                continue

            # Volume confirmation on current 4h bar
            if vol_ratio < MIN_VOL_RAT:
                logger.info(f"{self.agent_id} | {symbol} | vol_ratio={vol_ratio:.1f}x < {MIN_VOL_RAT}x — skip")
                continue

            # RSI filter
            rsi = self._calc_rsi(closes, period=14)
            if not (RSI_MIN <= rsi <= RSI_MAX):
                logger.info(f"{self.agent_id} | {symbol} | RSI={rsi:.1f} outside [{RSI_MIN},{RSI_MAX}] — skip")
                continue

            # Already have open position in this symbol?
            open_trades = [t for t in self.journal.snapshot()
                           if t.agent_id == self.agent_id
                           and t.symbol == symbol
                           and t.status == "open"]
            if open_trades:
                continue

            stop_price  = ltp * (1 - STOP_PCT)
            risk_amount = self.risk.state.capital * 0.01

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

        trade = self.journal.get_trade(trade_id)
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
        from config import config
        stop_distance = entry_price * STOP_PCT
        quote_to_account = max(float(getattr(config, "quote_to_account_rate", 1.0)), 1e-9)
        return max(0.000001, risk_amount / (stop_distance * quote_to_account)) if stop_distance > 0 else 0.000001
