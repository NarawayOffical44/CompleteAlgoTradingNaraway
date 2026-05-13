"""
MemeSniper — Solana memecoin sniping agent.

Targets newly launched tokens on Solana DEXes (Raydium/Orca/Meteora).
Uses paper trading by default; live execution requires Solana wallet config.

Entry conditions (ALL must pass):
  1. Token age < 30 min (still in early momentum phase)
  2. Liquidity > $10,000 (real pool, not ghost)
  3. Volume 5m > $3,000 (active buyers)
  4. Price change 5m > 50% OR 1h > 100% (momentum breakout)
  5. rug_score_safe = True (RugCheck passed, <= 2 high risks)
  6. No existing position in this token
  7. SOL regime != BEAR (don't snipe in a falling SOL market)

Exit conditions (first to trigger):
  - Take profit:  price >= entry × 3.0  (3x, ~200% gain)
  - Stop loss:    price <= entry × 0.50 (-50%)
  - Liquidity rug: liquidity_usd < 4,000 (pool drained — rug pull signal)
  - Max hold:     24 hours

Position sizing:
  - Fixed per-snipe: Rs 5,000 (paper) = ~$60 at 1 USD = Rs 84
  - Max 3 concurrent meme positions (portfolio protection)
  - Expect 80-90% of positions to hit stop loss — one 3x covers ~6 losses

Live execution (future):
  - Set SOLANA_PRIVATE_KEY in .env
  - Bot will use Jupiter swap API to execute on-chain
  - For now: paper mode via DhanClient simulation
"""

import uuid
from datetime import datetime
from agents.base_agent import BaseAgent
from loguru import logger

# ── Strategy parameters ────────────────────────────────────────────────────────
TAKE_PROFIT_MULT  = 3.0     # exit at 3x entry price
STOP_LOSS_MULT    = 0.50    # stop at -50%
MIN_PRICE_CHG_5M  = 50.0    # % — must show 5-min momentum
MIN_PRICE_CHG_1H  = 100.0   # % — OR 1h breakout (either condition passes)
MAX_TOKEN_AGE_M   = 30      # minutes old max
MIN_LIQUIDITY     = 10_000  # USD
MIN_VOLUME_5M     = 3_000   # USD
RUG_LIQUIDITY     = 4_000   # USD — exit if liquidity drops below this
MAX_CONCURRENT    = 3       # max simultaneous meme positions
MAX_HOLD_HOURS    = 24
POSITION_SIZE_INR = 5_000   # Rs per snipe (paper)
MAX_RISK_PCT      = 0.005   # 0.5% capital cap for small-account consistency


class MemeSniper(BaseAgent):

    _exchange = "SOLANA"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track entry prices separately (DexScreener prices, not INR)
        self._entry_prices: dict[str, float] = {}

    # ── Override _execute_signal for SOLANA ───────────────────────────────
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

        # Paper: simulate order through DhanClient
        try:
            self.broker.place_order(
                symbol=symbol, exchange=self._exchange,
                order_type="BUY", quantity=1, price=entry_price,
                client_order_id=f"{self.agent_id}:{trade_id}:OPEN",
            )
        except Exception as e:
            self.risk.cancel_open(self.agent_id, trade_id, str(e))
            logger.error(f"{self.agent_id} | ORDER FAILED | {symbol} | {e}")
            return
        self._entry_prices[trade_id] = entry_price

        self.journal.open_trade(
            trade_id=trade_id, agent_id=self.agent_id, symbol=symbol,
            direction=direction, entry_price=entry_price,
            quantity=1, risk_amount=risk_amount,
            thesis=thesis, regime=regime,
        )
        logger.info(
            f"{self.agent_id} | SNIPED | {symbol} @ ${entry_price:.8f} | "
            f"risk=Rs{risk_amount:.0f} | {thesis[:60]}"
        )

    # ── Override _check_exits for SOLANA ──────────────────────────────────
    def _check_exits(self, market_data: dict):
        open_trades = self.journal.open_trades(agent_id=self.agent_id)

        for trade in open_trades:
            should_exit, reason = self.should_exit(trade.trade_id, market_data)
            if should_exit:
                # Get current price from market data (any matching key)
                ltp = self._find_ltp(trade.symbol, market_data, trade.entry_price)
                self.broker.place_order(
                    symbol=trade.symbol, exchange=self._exchange,
                    order_type="SELL", quantity=1, price=ltp,
                    client_order_id=f"{self.agent_id}:{trade.trade_id}:CLOSE",
                )
                closed = self.journal.close_trade(trade.trade_id, ltp, reason)
                self.risk.register_close(self.agent_id, trade.trade_id, closed.pnl)
                self._entry_prices.pop(trade.trade_id, None)
                logger.info(
                    f"{self.agent_id} | EXIT | {trade.symbol} @ ${ltp:.8f} | "
                    f"pnl=Rs{closed.pnl:.0f} | {reason}"
                )

    # ── Signal generation ─────────────────────────────────────────────────
    def generate_signals(self, market_data: dict) -> list[dict]:
        regime  = market_data.get("_regime", "NEUTRAL")
        signals = []

        # Don't snipe in falling SOL market
        if regime == "BEAR_SOL":
            logger.info(f"{self.agent_id} | BEAR_SOL regime — no snipes")
            return []

        # Count concurrent positions
        open_count = sum(
            1 for t in self.journal.snapshot()
            if t.agent_id == self.agent_id and t.status == "open"
        )
        if open_count >= MAX_CONCURRENT:
            logger.info(f"{self.agent_id} | max concurrent positions ({MAX_CONCURRENT}) reached")
            return []

        for key, data in market_data.items():
            if key.startswith("_"):
                continue

            # Already have position in this token?
            token_symbol = data.get("symbol", key)
            existing = [t for t in self.journal.snapshot()
                        if t.agent_id == self.agent_id
                        and t.symbol == key and t.status == "open"]
            if existing:
                continue

            age_min      = data.get("age_min", 999)
            liquidity    = data.get("liquidity_usd", 0)
            vol_5m       = data.get("volume_5m", 0)
            chg_5m       = data.get("price_change_5m", 0)
            chg_1h       = data.get("price_change_1h", 0)
            rug_safe     = data.get("rug_score_safe", False)
            rug_risks    = data.get("rug_risks", 99)
            ltp          = data.get("ltp", 0.0)

            if ltp <= 0:
                continue

            # ── Entry filters ─────────────────────────────────────────────
            if age_min > MAX_TOKEN_AGE_M:
                continue

            if liquidity < MIN_LIQUIDITY:
                logger.debug(f"{self.agent_id} | {token_symbol} | liq=${liquidity:,.0f} < ${MIN_LIQUIDITY:,.0f} — skip")
                continue

            if vol_5m < MIN_VOLUME_5M:
                logger.debug(f"{self.agent_id} | {token_symbol} | vol5m=${vol_5m:,.0f} < ${MIN_VOLUME_5M:,.0f} — skip")
                continue

            # Momentum: either 5m spike or sustained 1h breakout
            has_momentum = (chg_5m >= MIN_PRICE_CHG_5M) or (chg_1h >= MIN_PRICE_CHG_1H)
            if not has_momentum:
                logger.debug(f"{self.agent_id} | {token_symbol} | no momentum (5m={chg_5m:+.1f}% 1h={chg_1h:+.1f}%) — skip")
                continue

            if not rug_safe:
                logger.info(f"{self.agent_id} | {token_symbol} | rug check FAILED ({rug_risks} risks) — skip")
                continue

            signals.append({
                "symbol":      key,
                "direction":   "long",
                "entry_price": ltp,
                "risk_amount": min(POSITION_SIZE_INR, self.risk.state.capital * MAX_RISK_PCT),
                "quantity":    1,
                "thesis": (
                    f"age={age_min:.0f}m | liq=${liquidity:,.0f} | "
                    f"5m={chg_5m:+.1f}% 1h={chg_1h:+.1f}% | "
                    f"rug_risks={rug_risks} | vol5m=${vol_5m:,.0f}"
                ),
            })

            logger.info(
                f"{self.agent_id} | SIGNAL {token_symbol} | "
                f"age={age_min:.0f}m | 5m={chg_5m:+.1f}% | liq=${liquidity:,.0f} | "
                f"rug_risks={rug_risks}"
            )

            # Only take 1 snipe per cycle to avoid overexposure
            break

        return signals

    # ── Exit logic ────────────────────────────────────────────────────────
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade = self.journal.get_trade(trade_id)
        if not trade:
            return False, ""

        data      = market_data.get(trade.symbol, {})
        ltp       = data.get("ltp", 0.0)
        liquidity = data.get("liquidity_usd", MIN_LIQUIDITY)

        if ltp <= 0:
            return False, ""

        entry = trade.entry_price

        # Take profit: 3x
        if ltp >= entry * TAKE_PROFIT_MULT:
            return True, f"take_profit: {ltp:.8f} >= {entry * TAKE_PROFIT_MULT:.8f} (3x)"

        # Stop loss: -50%
        if ltp <= entry * STOP_LOSS_MULT:
            return True, f"stop_loss: {ltp:.8f} <= {entry * STOP_LOSS_MULT:.8f} (-50%)"

        # Rug pull: liquidity drained
        if liquidity < RUG_LIQUIDITY:
            return True, f"rug_detected: liquidity=${liquidity:,.0f} < ${RUG_LIQUIDITY:,.0f}"

        # Max hold: 24 hours
        try:
            entry_dt   = datetime.fromisoformat(trade.entry_time)
            hours_held = (datetime.now() - entry_dt).total_seconds() / 3600
            if hours_held >= MAX_HOLD_HOURS:
                return True, f"max_hold: {hours_held:.1f}h"
        except Exception:
            pass

        return False, ""

    # ── Helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _find_ltp(symbol: str, market_data: dict, fallback: float) -> float:
        data = market_data.get(symbol, {})
        return data.get("ltp", fallback) or fallback

    @staticmethod
    def _calc_quantity(risk_amount: float, entry_price: float) -> float:
        return 1   # meme sniper uses fixed 1 unit + INR risk_amount for sizing
