"""
ForexMeanReversionBot — Bollinger Band z-score reversion on major FX pairs.

Entry (LONG):  z-score < -2.0 (pair oversold vs 20h mean)
Entry (SHORT): z-score >  2.0 (pair overbought vs 20h mean)
Exit:  z-score returns to ±0.5, OR 1% stop loss, OR 48h max hold
"""

from agents.base_agent import BaseAgent
from loguru import logger
import math

FOREX_LOT  = 10_000
STOP_PCT   = 0.01
Z_ENTRY    = 2.0
Z_EXIT     = 0.5
BB_PERIOD  = 20
MAX_HOLD_H = 48     # 2 days
MIN_STD_PCT = 0.001  # filter out flat, illiquid periods (0.1% min std)


class ForexMeanReversionBot(BaseAgent):

    _exchange = "FOREX"

    def _execute_signal(self, signal: dict, regime: str):
        import uuid
        symbol      = signal["symbol"]
        direction   = signal["direction"]
        entry_price = signal["entry_price"]
        risk_amount = signal["risk_amount"]
        quantity    = signal.get("quantity", FOREX_LOT)
        thesis      = signal.get("thesis", "")

        trade_id = str(uuid.uuid4())[:8]
        approved, reason = self.risk.approve_and_open(self.agent_id, trade_id, risk_amount)
        if not approved:
            logger.info(f"{self.agent_id} | BLOCKED | {symbol} | {reason}")
            return

        order_type = "BUY" if direction == "long" else "SELL"
        try:
            self.broker.place_order(
                symbol=symbol, exchange=self._exchange,
                order_type=order_type, quantity=int(quantity), price=entry_price,
                client_order_id=f"{self.agent_id}:{trade_id}:OPEN",
            )
        except Exception as e:
            self.risk.cancel_open(self.agent_id, trade_id, str(e))
            logger.error(f"{self.agent_id} | ORDER FAILED | {symbol} | {e}")
            return
        self.journal.open_trade(
            trade_id=trade_id, agent_id=self.agent_id, symbol=symbol,
            direction=direction, entry_price=entry_price, quantity=quantity,
            risk_amount=risk_amount, thesis=thesis, regime=regime,
        )
        logger.info(f"{self.agent_id} | ENTERED | {symbol} {direction} @ {entry_price:.5f}")

    def _check_exits(self, market_data: dict):
        open_trades = self.journal.open_trades(agent_id=self.agent_id)
        for trade in open_trades:
            should_exit, reason = self.should_exit(trade.trade_id, market_data)
            if should_exit:
                exit_price = market_data.get(trade.symbol, {}).get("ltp", trade.entry_price)
                order_type = "SELL" if trade.direction == "long" else "BUY"
                self.broker.place_order(
                    symbol=trade.symbol, exchange=self._exchange,
                    order_type=order_type, quantity=int(trade.quantity), price=exit_price,
                    client_order_id=f"{self.agent_id}:{trade.trade_id}:CLOSE",
                )
                closed = self.journal.close_trade(trade.trade_id, exit_price, reason)
                self.risk.register_close(self.agent_id, trade.trade_id, closed.pnl)

    # ── Signal generation ─────────────────────────────────────────────────
    def generate_signals(self, market_data: dict) -> list[dict]:
        regime  = market_data.get("_regime", "NEUTRAL")
        signals = []

        for symbol, data in market_data.items():
            if symbol.startswith("_"):
                continue
            closes = data.get("closes", [])

            if len(closes) < BB_PERIOD + 1:
                continue

            mean = sum(closes[-BB_PERIOD:]) / BB_PERIOD
            variance = sum((c - mean) ** 2 for c in closes[-BB_PERIOD:]) / BB_PERIOD
            std  = math.sqrt(variance) if variance > 0 else 0.0

            # Skip flat/illiquid periods
            if std / mean < MIN_STD_PCT:
                continue

            ltp    = closes[-1]
            zscore = (ltp - mean) / std

            # Already have open position?
            open_pos = [t for t in self.journal.snapshot()
                        if t.agent_id == self.agent_id
                        and t.symbol == symbol and t.status == "open"]
            if open_pos:
                continue

            direction = None
            if zscore < -Z_ENTRY:
                direction = "long"
            elif zscore > Z_ENTRY:
                direction = "short"

            if not direction:
                continue

            from config import config
            quote_to_account = max(float(getattr(config, "quote_to_account_rate", 1.0)), 1e-9)
            risk_amount = self.risk.state.capital * 0.005
            quantity = max(1.0, risk_amount / (ltp * STOP_PCT * quote_to_account))

            signals.append({
                "symbol":      symbol,
                "direction":   direction,
                "entry_price": ltp,
                "risk_amount": risk_amount,
                "quantity":    quantity,
                "thesis": (f"BB z={zscore:+.2f} | mean={mean:.5f} ±{std:.5f} | "
                           f"regime={regime}"),
            })
            logger.info(f"{self.agent_id} | SIGNAL {symbol} {direction.upper()} @ {ltp:.5f} | z={zscore:+.2f}")

        return signals

    # ── Exit logic ────────────────────────────────────────────────────────
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        from datetime import datetime
        trade  = self.journal.get_trade(trade_id)
        if not trade:
            return False, ""

        data   = market_data.get(trade.symbol, {})
        closes = data.get("closes", [])
        ltp    = data.get("ltp", trade.entry_price)

        if not ltp or len(closes) < BB_PERIOD:
            return False, ""

        # Stop loss
        if trade.direction == "long":
            stop = trade.entry_price * (1 - STOP_PCT)
            if ltp <= stop:
                return True, f"stop_loss: {ltp:.5f}"
        else:
            stop = trade.entry_price * (1 + STOP_PCT)
            if ltp >= stop:
                return True, f"stop_loss: {ltp:.5f}"

        # Z-score mean revert exit
        mean = sum(closes[-BB_PERIOD:]) / BB_PERIOD
        variance = sum((c - mean) ** 2 for c in closes[-BB_PERIOD:]) / BB_PERIOD
        import math
        std = math.sqrt(variance) if variance > 0 else 1e-8
        zscore = (ltp - mean) / std

        if trade.direction == "long" and zscore >= -Z_EXIT:
            return True, f"mean_revert: z={zscore:+.2f}"
        if trade.direction == "short" and zscore <= Z_EXIT:
            return True, f"mean_revert: z={zscore:+.2f}"

        # Max hold
        try:
            entry_dt   = datetime.fromisoformat(trade.entry_time)
            hours_held = (datetime.now() - entry_dt).total_seconds() / 3600
            if hours_held >= MAX_HOLD_H:
                return True, f"max_hold: {hours_held:.0f}h"
        except Exception:
            pass

        return False, ""
