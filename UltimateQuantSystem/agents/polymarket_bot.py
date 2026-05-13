"""
PolymarketBot — Momentum + value trader on Polymarket prediction markets.

Two strategies running simultaneously:

1. MOMENTUM: price moving fast in one direction → follow it
   - Entry: price_change_1h > 15% AND liquidity > $10k AND spread < 5c
   - Buy YES if price surging up, Buy NO if collapsing
   - Exit: momentum stalls (change < 2% over 2 cycles) OR target hit

2. VALUE: price far from estimated fair value → buy the cheap side
   - Entry: YES price < 0.15 but days_to_end > 30 (underpriced longshot)
   - OR:    YES price > 0.85 but event not certain (overpriced favorite fade)
   - Exit:  Price reaches fair value OR event resolves

Position sizing: Rs 2,000 per market (small — binary risk, each goes to 0 or 1)
Max concurrent: 6 positions
Take profit: 40% gain on position value
Stop loss: -30% on position value (prediction markets rarely bounce)

Live execution requires:
  POLYMARKET_API_KEY and POLYMARKET_PRIVATE_KEY in .env
  Paper mode works without keys (simulates trades).
"""

import uuid
import os
from datetime import datetime
from agents.base_agent import BaseAgent
from loguru import logger


POSITION_SIZE_INR  = 2_000    # Rs per prediction market position
MAX_RISK_PCT       = 0.005    # 0.5% capital cap for small-account consistency
MAX_CONCURRENT     = 6
TAKE_PROFIT_PCT    = 0.40     # +40% gain on position
STOP_LOSS_PCT      = 0.30     # -30% loss on position
MAX_HOLD_DAYS      = 14

# Momentum entry
MOM_CHANGE_1H_MIN  = 15.0     # % price move in last 1h
MOM_MIN_LIQUIDITY  = 10_000   # USDC
MOM_MAX_SPREAD     = 0.05     # max bid-ask spread

# Value entry
VAL_LONGSHOT_MAX   = 0.15     # buy YES if price < 0.15 (underpriced)
VAL_FADE_MIN       = 0.85     # buy NO if price > 0.85 (overpriced favorite)
VAL_MIN_DAYS       = 30       # value plays need time to resolve
VAL_MIN_LIQUIDITY  = 8_000

# Exit
STALL_CHANGE_PCT   = 2.0      # exit momentum if change drops below this


class PolymarketBot(BaseAgent):

    _exchange = "POLY"

    def _execute_signal(self, signal: dict, regime: str):
        symbol      = signal["symbol"]
        direction   = signal["direction"]
        entry_price = signal["entry_price"]
        risk_amount = signal["risk_amount"]
        thesis      = signal.get("thesis", "")

        trade_id = str(uuid.uuid4())[:8]
        approved, reason = self.risk.approve_and_open(self.agent_id, trade_id, risk_amount)
        if not approved:
            logger.info(f"{self.agent_id} | BLOCKED | {symbol[:40]} | {reason}")
            return

        try:
            self.broker.place_order(
                symbol=symbol, exchange=self._exchange,
                order_type="BUY", quantity=1, price=entry_price,
                client_order_id=f"{self.agent_id}:{trade_id}:OPEN",
            )
        except Exception as e:
            self.risk.cancel_open(self.agent_id, trade_id, str(e))
            logger.error(f"{self.agent_id} | ORDER FAILED | {symbol[:40]} | {e}")
            return
        self.journal.open_trade(
            trade_id=trade_id, agent_id=self.agent_id, symbol=symbol,
            direction=direction, entry_price=entry_price,
            quantity=1, risk_amount=risk_amount,
            thesis=thesis, regime=regime,
        )
        logger.info(
            f"{self.agent_id} | ENTERED | {symbol[:50]} @ {entry_price:.3f} | "
            f"risk=Rs{risk_amount:.0f} | {thesis[:60]}"
        )

    def _check_exits(self, market_data: dict):
        open_trades = self.journal.open_trades(agent_id=self.agent_id)
        for trade in open_trades:
            should_exit, reason = self.should_exit(trade.trade_id, market_data)
            if should_exit:
                ltp = market_data.get(trade.symbol, {}).get("ltp", trade.entry_price)
                self.broker.place_order(
                    symbol=trade.symbol, exchange=self._exchange,
                    order_type="SELL", quantity=1, price=ltp,
                    client_order_id=f"{self.agent_id}:{trade.trade_id}:CLOSE",
                )
                closed = self.journal.close_trade(trade.trade_id, ltp, reason)
                self.risk.register_close(self.agent_id, trade.trade_id, closed.pnl)
                logger.info(
                    f"{self.agent_id} | EXIT | {trade.symbol[:40]} @ {ltp:.3f} | "
                    f"pnl=Rs{closed.pnl:+.0f} | {reason}"
                )

    # ── Signal generation ─────────────────────────────────────────────────
    def generate_signals(self, market_data: dict) -> list[dict]:
        regime  = market_data.get("_regime", "NORMAL")
        signals = []

        open_count = sum(
            1 for t in self.journal.snapshot()
            if t.agent_id == self.agent_id and t.status == "open"
        )
        if open_count >= MAX_CONCURRENT:
            return []

        for key, data in market_data.items():
            if key.startswith("_"):
                continue
            if not isinstance(data, dict):
                continue

            # Skip if already in this market
            existing = [t for t in self.journal.snapshot()
                        if t.agent_id == self.agent_id
                        and t.symbol == key and t.status == "open"]
            if existing:
                continue

            ltp            = data.get("ltp", 0.5)
            chg_1h         = data.get("price_change_1h", 0.0)
            liquidity      = data.get("liquidity", 0)
            spread         = data.get("spread", 0.05)
            days_to_end    = data.get("days_to_end", 999)
            question       = data.get("question", key)
            outcome        = data.get("outcome", "Yes")

            if ltp <= 0 or ltp >= 1:
                continue

            signal = None

            # ── Strategy 1: MOMENTUM ──────────────────────────────────────
            if (abs(chg_1h) >= MOM_CHANGE_1H_MIN and
                    liquidity >= MOM_MIN_LIQUIDITY and
                    spread <= MOM_MAX_SPREAD and
                    days_to_end >= MIN_DAYS_TO_END_MOM):

                direction = "long"   # always buy (YES or NO token) in direction of move
                signal = {
                    "symbol":      key,
                    "direction":   direction,
                    "entry_price": ltp,
                    "risk_amount": min(POSITION_SIZE_INR, self.risk.state.capital * MAX_RISK_PCT),
                    "strategy":    "momentum",
                    "thesis": (
                        f"MOMENTUM | {question[:40]} | {outcome} @ {ltp:.3f} | "
                        f"1h={chg_1h:+.1f}% | liq=${liquidity:,.0f}"
                    ),
                }

            # ── Strategy 2: VALUE LONGSHOT ────────────────────────────────
            elif (ltp <= VAL_LONGSHOT_MAX and
                    liquidity >= VAL_MIN_LIQUIDITY and
                    days_to_end >= VAL_MIN_DAYS and
                    outcome.lower() == "yes"):

                signal = {
                    "symbol":      key,
                    "direction":   "long",
                    "entry_price": ltp,
                    "risk_amount": min(POSITION_SIZE_INR * 0.5, self.risk.state.capital * MAX_RISK_PCT),
                    "strategy":    "value_longshot",
                    "thesis": (
                        f"VALUE_LONGSHOT | {question[:40]} | YES @ {ltp:.3f} | "
                        f"liq=${liquidity:,.0f} | {days_to_end}d to end"
                    ),
                }

            # ── Strategy 3: FADE OVERPRICED FAVORITE ─────────────────────
            elif (ltp >= VAL_FADE_MIN and
                    liquidity >= VAL_MIN_LIQUIDITY and
                    days_to_end >= VAL_MIN_DAYS and
                    outcome.lower() == "yes"):

                # Buy the NO token (fade the overpriced YES)
                no_price = round(1.0 - ltp, 4)
                signal = {
                    "symbol":      key.replace("_YES", "_NO"),
                    "direction":   "long",
                    "entry_price": no_price,
                    "risk_amount": min(POSITION_SIZE_INR * 0.5, self.risk.state.capital * MAX_RISK_PCT),
                    "strategy":    "fade_favorite",
                    "thesis": (
                        f"FADE | {question[:40]} | NO @ {no_price:.3f} "
                        f"(YES={ltp:.3f} overpriced) | {days_to_end}d"
                    ),
                }

            if signal:
                signals.append(signal)
                logger.info(
                    f"{self.agent_id} | SIGNAL [{signal['strategy']}] "
                    f"{signal['thesis'][:80]}"
                )
                if len(signals) + open_count >= MAX_CONCURRENT:
                    break

        return signals

    # ── Exit logic ────────────────────────────────────────────────────────
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade = self.journal.get_trade(trade_id)
        if not trade:
            return False, ""

        data    = market_data.get(trade.symbol, {})
        ltp     = data.get("ltp", trade.entry_price)
        chg_1h  = data.get("price_change_1h", 0.0)
        dte     = data.get("days_to_end", 999)

        if not ltp:
            return False, ""

        pnl_pct = (ltp - trade.entry_price) / trade.entry_price

        # Take profit: +40%
        if pnl_pct >= TAKE_PROFIT_PCT:
            return True, f"take_profit: {pnl_pct*100:+.1f}%"

        # Stop loss: -30%
        if pnl_pct <= -STOP_LOSS_PCT:
            return True, f"stop_loss: {pnl_pct*100:+.1f}%"

        # Near resolution: exit 2 days before (binary risk)
        if dte <= 2:
            return True, f"near_resolution: {dte}d remaining"

        # Momentum stall: if we entered on momentum and it's faded
        if abs(chg_1h) < STALL_CHANGE_PCT and trade.entry_price > 0.15:
            try:
                entry_dt   = datetime.fromisoformat(trade.entry_time)
                hours_held = (datetime.now() - entry_dt).total_seconds() / 3600
                if hours_held > 4:   # give it 4h to develop
                    return True, f"momentum_stall: chg_1h={chg_1h:+.1f}%"
            except Exception:
                pass

        # Max hold: 14 days
        try:
            entry_dt  = datetime.fromisoformat(trade.entry_time)
            days_held = (datetime.now() - entry_dt).days
            if days_held >= MAX_HOLD_DAYS:
                return True, f"max_hold: {days_held}d"
        except Exception:
            pass

        return False, ""

    @staticmethod
    def _calc_quantity(risk_amount: float, entry_price: float) -> float:
        return 1


# ── Missing constant (referenced above) ──────────────────────────────────────
MIN_DAYS_TO_END_MOM = 5   # momentum plays can be short-dated
