"""
PerpFuturesBot — Leveraged perpetual futures on Binance (USDT-margined).

High-risk, high-reward strategy. Targets 15-25% price moves, amplified by leverage.

Leverage: 3x default (configurable). At 3x:
  - A 15% price move = +45% gain on capital
  - A 7% adverse move = -21% loss on capital
  - Liquidation at ~33% adverse move (not 7% — leverage cushioned by stop)

Strategy: 4h EMA momentum breakout (long + short)
  LONG:  EMA20 > EMA50, price > EMA20, RSI 45–75, vol 0.7x, funding < 0.08%
  SHORT: EMA20 < EMA50, price < EMA20, RSI 25–58, vol 0.7x, funding > -0.01%

Exit:
  - Take profit: +15% price move (= +45% at 3x)
  - Stop loss:   -7%  price move (= -21% at 3x)  ← strict, never widen
  - Max hold:    5 days
  - Funding cost: if cumulative funding > 0.3% → exit (carry cost eating profit)
  - Regime flip: if regime changes to opposite → exit immediately

Position sizing:
  - Rs 10,000 per trade capital at risk
  - Notional = Rs 30,000 at 3x (paper simulated)
  - Max 4 concurrent positions

Funding rate filter:
  - LONG: skip if funding > 0.08%/8h (longs already crowded, mean-reversion risk)
  - SHORT: skip if funding < -0.05%/8h (shorts already crowded)

Paper vs Live:
  - Paper: DhanClient simulates entry/exit, tracks P&L by price change × leverage
  - Live:  Requires BINANCE_API_KEY + BINANCE_SECRET in .env, futures permissions
"""

import uuid
import math
from datetime import datetime
from agents.base_agent import BaseAgent
from loguru import logger


LEVERAGE          = 3           # 3x leverage — aggressive but survivable
TAKE_PROFIT_PCT   = 0.15        # 15% price move = +45% at 3x
STOP_LOSS_PCT     = 0.07        # -7% price move = -21% at 3x
MAX_HOLD_DAYS     = 5
MAX_CONCURRENT    = 4
POSITION_SIZE_INR = 10_000      # Rs per trade (capital, not notional)
MAX_RISK_PCT      = 0.005       # 0.5% capital cap for small-account consistency

RSI_LONG_MIN      = 45          # widened: catch momentum earlier
RSI_LONG_MAX      = 75
RSI_SHORT_MIN     = 25          # widened: short when RSI retreats from overbought
RSI_SHORT_MAX     = 58
MIN_VOL_RATIO     = 0.7         # lowered: perp markets have lower vol spikes than spot
MAX_FUNDING_LONG  = 0.08        # % per 8h — skip longs if longs are crowded
MIN_FUNDING_SHORT = -0.05       # % per 8h — skip shorts if shorts are crowded
MAX_CUMUL_FUNDING = 0.30        # % — exit if carry cost exceeds this


class PerpFuturesBot(BaseAgent):

    _exchange = "PERP"

    def __init__(self, *args, leverage: int = LEVERAGE, **kwargs):
        super().__init__(*args, **kwargs)
        self.leverage        = leverage
        self._entry_regimes: dict[str, str] = {}  # trade_id → regime at entry

    # ── Override _execute_signal for PERP ────────────────────────────────
    def _execute_signal(self, signal: dict, regime: str):
        symbol      = signal["symbol"]
        direction   = signal["direction"]
        entry_price = signal["entry_price"]
        risk_amount = signal["risk_amount"]
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
                order_type=order_type, quantity=1, price=entry_price,
                client_order_id=f"{self.agent_id}:{trade_id}:OPEN",
            )
        except Exception as e:
            self.risk.cancel_open(self.agent_id, trade_id, str(e))
            logger.error(f"{self.agent_id} | ORDER FAILED | {symbol} | {e}")
            return
        self._entry_regimes[trade_id] = regime

        self.journal.open_trade(
            trade_id=trade_id, agent_id=self.agent_id, symbol=symbol,
            direction=direction, entry_price=entry_price,
            quantity=self.leverage,   # quantity = leverage multiplier for P&L tracking
            risk_amount=risk_amount,
            thesis=thesis, regime=regime,
        )

        notional_inr = (risk_amount / STOP_LOSS_PCT) * self.leverage
        logger.info(
            f"{self.agent_id} | ENTERED {direction.upper()} | {symbol} @ {entry_price:.4f} | "
            f"{self.leverage}x | notional=Rs{notional_inr:,.0f} | {thesis[:60]}"
        )

    # ── Override _check_exits for PERP ───────────────────────────────────
    def _check_exits(self, market_data: dict):
        open_trades = self.journal.open_trades(agent_id=self.agent_id)
        regime = market_data.get("_regime", "BULL")

        for trade in open_trades:
            should_exit, reason = self.should_exit(trade.trade_id, market_data)
            if should_exit:
                ltp        = market_data.get(trade.symbol, {}).get("ltp", trade.entry_price)
                order_type = "SELL" if trade.direction == "long" else "BUY"
                self.broker.place_order(
                    symbol=trade.symbol, exchange=self._exchange,
                    order_type=order_type, quantity=1, price=ltp,
                    client_order_id=f"{self.agent_id}:{trade.trade_id}:CLOSE",
                )
                # P&L = capital × leverage × price_change_pct
                pnl_pct = (ltp - trade.entry_price) / trade.entry_price
                if trade.direction == "short":
                    pnl_pct = -pnl_pct
                capital_used = trade.risk_amount / STOP_LOSS_PCT
                pnl_inr = capital_used * self.leverage * pnl_pct

                closed = self.journal.close_trade(trade.trade_id, ltp, reason)
                self.risk.register_close(self.agent_id, trade.trade_id, pnl_inr)
                self._entry_regimes.pop(trade.trade_id, None)

                logger.info(
                    f"{self.agent_id} | EXIT {trade.direction.upper()} | {trade.symbol} | "
                    f"entry={trade.entry_price:.4f} exit={ltp:.4f} | "
                    f"pnl=Rs{pnl_inr:+,.0f} ({pnl_pct*self.leverage*100:+.1f}%) | {reason}"
                )

    # ── Signal generation ─────────────────────────────────────────────────
    def generate_signals(self, market_data: dict) -> list[dict]:
        regime   = market_data.get("_regime", "BULL")
        mkt_sent = market_data.get("_market_sentiment", {})
        funding  = mkt_sent.get("funding", 0.0) if isinstance(mkt_sent, dict) else 0.0
        signals  = []

        # Count concurrent positions
        open_count = sum(
            1 for t in self.journal.snapshot()
            if t.agent_id == self.agent_id and t.status == "open"
        )
        if open_count >= MAX_CONCURRENT:
            return []

        for symbol, data in market_data.items():
            if symbol.startswith("_"):
                continue

            closes     = data.get("closes", [])
            highs      = data.get("highs", [])
            lows       = data.get("lows", [])
            vol_ratio  = data.get("volume_ratio", 0.0)
            ltp        = data.get("ltp", 0.0)
            sym_fund   = data.get("funding_rate", funding)  # per-symbol funding

            if len(closes) < 52 or ltp <= 0:
                continue

            # Already have position in this symbol?
            existing = [t for t in self.journal.snapshot()
                        if t.agent_id == self.agent_id
                        and t.symbol == symbol and t.status == "open"]
            if existing:
                continue

            if vol_ratio < MIN_VOL_RATIO:
                continue

            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)
            rsi   = self._rsi(closes, 14)

            direction = None

            # ── LONG signal ───────────────────────────────────────────────
            if (regime in ("BULL", "CHOPPY") and
                    ema20 > ema50 and ltp > ema20 and
                    RSI_LONG_MIN <= rsi <= RSI_LONG_MAX and
                    sym_fund < MAX_FUNDING_LONG):
                direction = "long"

            # ── SHORT signal ──────────────────────────────────────────────
            elif (regime in ("BEAR", "CHOPPY") and
                    ema20 < ema50 and ltp < ema20 and
                    RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX and
                    sym_fund > MIN_FUNDING_SHORT):
                direction = "short"

            if not direction:
                continue

            # Risk = capital × stop_pct (stop before liquidation)
            risk_amount = min(POSITION_SIZE_INR * STOP_LOSS_PCT, self.risk.state.capital * MAX_RISK_PCT)

            signals.append({
                "symbol":      symbol,
                "direction":   direction,
                "entry_price": ltp,
                "risk_amount": risk_amount,
                "quantity":    1,
                "thesis": (
                    f"{self.leverage}x {direction} | EMA20={ema20:.4f} EMA50={ema50:.4f} | "
                    f"RSI={rsi:.0f} | vol={vol_ratio:.2f}x | fund={sym_fund:.4f}% | "
                    f"regime={regime}"
                ),
            })

            logger.info(
                f"{self.agent_id} | SIGNAL {direction.upper()} {symbol} @ {ltp:.4f} | "
                f"RSI={rsi:.0f} | vol={vol_ratio:.2f}x | fund={sym_fund:.4f}%"
            )

        return signals

    # ── Exit logic ────────────────────────────────────────────────────────
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade  = self.journal.get_trade(trade_id)
        if not trade:
            return False, ""

        data   = market_data.get(trade.symbol, {})
        ltp    = data.get("ltp", trade.entry_price)
        regime = market_data.get("_regime", "BULL")

        if not ltp:
            return False, ""

        entry = trade.entry_price
        pnl_pct = (ltp - entry) / entry
        if trade.direction == "short":
            pnl_pct = -pnl_pct

        # Take profit: +15% price move (= +45% at 3x)
        if pnl_pct >= TAKE_PROFIT_PCT:
            return True, f"take_profit: {pnl_pct*100:+.1f}% (={pnl_pct*self.leverage*100:.1f}% leveraged)"

        # Stop loss: -7% price move (= -21% at 3x)
        if pnl_pct <= -STOP_LOSS_PCT:
            return True, f"stop_loss: {pnl_pct*100:+.1f}% (={pnl_pct*self.leverage*100:.1f}% leveraged)"

        # Regime flip: exit if market turns against direction
        entry_regime = self._entry_regimes.get(trade_id, "BULL")
        if trade.direction == "long" and regime == "BEAR":
            return True, f"regime_flip: {entry_regime}→{regime}"
        if trade.direction == "short" and regime == "BULL":
            return True, f"regime_flip: {entry_regime}→{regime}"

        # Funding cost: cumulative carry eating profit
        sym_fund = data.get("funding_rate", 0.0)
        if sym_fund > 0 and trade.direction == "long":
            try:
                entry_dt    = datetime.fromisoformat(trade.entry_time)
                hours_held  = (datetime.now() - entry_dt).total_seconds() / 3600
                periods_8h  = hours_held / 8
                cumul_fund  = sym_fund * periods_8h
                if cumul_fund > MAX_CUMUL_FUNDING:
                    return True, f"funding_cost: cumul={cumul_fund:.3f}%"
            except Exception:
                pass

        # Max hold: 5 days
        try:
            entry_dt  = datetime.fromisoformat(trade.entry_time)
            days_held = (datetime.now() - entry_dt).days
            if days_held >= MAX_HOLD_DAYS:
                return True, f"max_hold: {days_held}d"
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
        return round(100 - 100 / (1 + avg_g / avg_l), 1)

    @staticmethod
    def _calc_quantity(risk_amount: float, entry_price: float) -> float:
        return 1
