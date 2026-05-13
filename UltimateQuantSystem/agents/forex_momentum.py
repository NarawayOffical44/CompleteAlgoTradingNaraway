"""
ForexMomentumBot — EMA crossover trend following on major FX pairs.

Entry (LONG):  EMA20 > EMA50, price > EMA20, RSI 40-65, vol_ratio > 0.5
Entry (SHORT): EMA20 < EMA50, price < EMA20, RSI 35-60, vol_ratio > 0.5
Exit:  EMA crossover reverses, OR 1% stop loss, OR RSI extreme (>75 / <25)
Max hold: 5 days (120 hourly bars)
"""

from agents.base_agent import BaseAgent
from loguru import logger

FOREX_LOT   = 10_000    # micro-lot — 10,000 units of base currency
STOP_PCT    = 0.01      # 1% stop loss
MAX_HOLD_H  = 120       # 5 days in hours
RSI_LONG_MIN, RSI_LONG_MAX   = 40, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 60
RSI_EXIT_LONG  = 75     # overbought → exit long
RSI_EXIT_SHORT = 25     # oversold → exit short
MIN_VOL_RATIO  = 0.5


class ForexMomentumBot(BaseAgent):

    _exchange = "FOREX"   # used in _execute_signal override

    # ── Override _execute_signal to use FOREX exchange ────────────────────
    def _execute_signal(self, signal: dict, regime: str):
        import uuid
        from risk import RiskMode
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
        logger.info(f"{self.agent_id} | ENTERED | {symbol} {direction} @ {entry_price:.5f} | risk={risk_amount:.2f}")

    # ── Override _check_exits for FOREX exchange ──────────────────────────
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
        regime    = market_data.get("_regime", "NEUTRAL")
        signals   = []

        for symbol, data in market_data.items():
            if symbol.startswith("_"):
                continue
            closes    = data.get("closes", [])
            vol_ratio = data.get("volume_ratio", 0.0)

            if len(closes) < 52:
                continue
            if vol_ratio < MIN_VOL_RATIO:
                logger.debug(f"{self.agent_id} | {symbol} | vol_ratio={vol_ratio:.2f} < {MIN_VOL_RATIO} — skip")
                continue

            # Already have open position?
            open_pos = [t for t in self.journal.snapshot()
                        if t.agent_id == self.agent_id
                        and t.symbol == symbol and t.status == "open"]
            if open_pos:
                continue

            ema20  = self._ema(closes, 20)
            ema50  = self._ema(closes, 50)
            ltp    = closes[-1]
            rsi    = self._rsi(closes, 14)

            direction = None
            if ema20 > ema50 and ltp > ema20 and RSI_LONG_MIN <= rsi <= RSI_LONG_MAX:
                direction = "long"
            elif ema20 < ema50 and ltp < ema20 and RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX:
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
                "thesis": (f"EMA20={ema20:.5f} EMA50={ema50:.5f} | "
                           f"RSI={rsi:.1f} | vol={vol_ratio:.2f}x | "
                           f"regime={regime}"),
            })
            logger.info(f"{self.agent_id} | SIGNAL {symbol} {direction.upper()} @ {ltp:.5f} | RSI={rsi:.1f}")

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

        if not ltp or len(closes) < 20:
            return False, ""

        # Stop loss
        if trade.direction == "long":
            stop = trade.entry_price * (1 - STOP_PCT)
            if ltp <= stop:
                return True, f"stop_loss: {ltp:.5f} <= {stop:.5f}"
        else:
            stop = trade.entry_price * (1 + STOP_PCT)
            if ltp >= stop:
                return True, f"stop_loss: {ltp:.5f} >= {stop:.5f}"

        # RSI extreme exit
        if len(closes) >= 15:
            rsi = self._rsi(closes, 14)
            if trade.direction == "long" and rsi > RSI_EXIT_LONG:
                return True, f"RSI_overbought: {rsi:.1f}"
            if trade.direction == "short" and rsi < RSI_EXIT_SHORT:
                return True, f"RSI_oversold: {rsi:.1f}"

        # EMA crossover reversal
        if len(closes) >= 52:
            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)
            if trade.direction == "long" and ema20 < ema50:
                return True, f"EMA_crossdown: {ema20:.5f} < {ema50:.5f}"
            if trade.direction == "short" and ema20 > ema50:
                return True, f"EMA_crossup: {ema20:.5f} > {ema50:.5f}"

        # Max hold (120 hours = 5 days)
        try:
            entry_dt   = datetime.fromisoformat(trade.entry_time)
            hours_held = (datetime.now() - entry_dt).total_seconds() / 3600
            if hours_held >= MAX_HOLD_H:
                return True, f"max_hold: {hours_held:.0f}h >= {MAX_HOLD_H}h"
        except Exception:
            pass

        return False, ""

    # ── Indicators ────────────────────────────────────────────────────────
    @staticmethod
    def _ema(closes: list, period: int) -> float:
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        k = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for price in closes[period:]:
            ema = price * k + ema * (1 - k)
        return ema

    @staticmethod
    def _rsi(closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [d for d in deltas[-period:] if d > 0]
        losses = [-d for d in deltas[-period:] if d < 0]
        avg_g  = sum(gains) / period
        avg_l  = sum(losses) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return round(100 - 100 / (1 + rs), 1)
