"""
BankNiftyStraddleBot — Short straddle on BANKNIFTY weekly options.

Sells ATM Call + ATM Put when IV is elevated (IVR > 40).
Premium collection strategy — profits when BANKNIFTY stays near ATM.

Entry conditions:
  - Regime BULL_LOW_VOL or CHOPPY
  - IVR > 40 (higher than NIFTY condor — straddle needs rich premium)
  - Market sentiment > -0.2
  - Days to major event >= 5

Exit conditions:
  - 50% premium decay (profit target)
  - 150% premium expansion (stop loss)
  - DTE <= 2 (mandatory exit before pin risk)
  - Market sentiment collapses < -0.6

Lot size: BANKNIFTY = 15 (SEBI Oct 2024)
"""

import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger
from risk import RiskEngine
from journal import TradeJournal
from broker import DhanClient


SAFE_REGIMES         = {"BULL_LOW_VOL", "CHOPPY"}
MIN_IVR              = 40          # higher than condor — straddle needs more premium
MIN_MARKET_SENTIMENT = -0.2
MAX_FII_OUTFLOW      = -2000
MIN_DTE_TO_EVENT     = 5
BNK_LOT_SIZE         = 15

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://www.nseindia.com/",
}


@dataclass
class StraddleLeg:
    symbol:      str
    strike:      float
    option_type: str    # CE | PE
    action:      str    # SELL
    premium:     float
    quantity:    int


@dataclass
class StraddlePosition:
    position_id:         str
    underlying:          str = "BANKNIFTY"
    expiry:              str = ""
    legs:                list = field(default_factory=list)
    entry_premium:       float = 0
    target_exit_premium: float = 0
    stop_exit_premium:   float = 0
    entry_time:          str = ""
    status:              str = "open"


class BankNiftyStraddleBot:
    """Short straddle on BANKNIFTY weekly options (paper + live)."""

    def __init__(self, risk_engine: RiskEngine, journal: TradeJournal, broker: DhanClient):
        self.agent_id   = "banknifty_straddle"
        self.risk       = risk_engine
        self.journal    = journal
        self.broker     = broker
        self.underlying = "BANKNIFTY"
        self.positions: dict[str, StraddlePosition] = {}
        self._nse_session = self._make_nse_session()

    def run(self, regime: str, market_data: dict):
        from risk import RiskMode
        if self.risk.state.mode == RiskMode.STOPPED:
            logger.warning("BNKStraddle | kill switch — skip")
            return

        block, reason = self._pre_checks(regime, market_data)
        if block:
            logger.info(f"BNKStraddle | BLOCKED | {reason}")
            return

        if self._has_open_position():
            self._manage_positions(market_data)
        else:
            self._enter_straddle(market_data)

    # ── Pre-checks ────────────────────────────────────────────────────────
    def _pre_checks(self, regime: str, market_data: dict) -> tuple[bool, str]:
        if regime not in SAFE_REGIMES:
            return True, f"regime={regime}"

        ivr = market_data.get(self.underlying, {}).get("iv_rank", 0)
        if ivr < MIN_IVR:
            return True, f"IVR={ivr} < {MIN_IVR}"

        mkt_sent = market_data.get("_market_sentiment", {})
        if mkt_sent.get("score", 0) < MIN_MARKET_SENTIMENT:
            return True, f"mkt_sent={mkt_sent.get('score',0):.2f}"

        if market_data.get("FII_FLOW", 0) < MAX_FII_OUTFLOW:
            return True, f"FII outflow={market_data.get('FII_FLOW',0)}Cr"

        if market_data.get("DAYS_TO_EVENT", 30) < MIN_DTE_TO_EVENT:
            return True, f"event in {market_data.get('DAYS_TO_EVENT',30)}d"

        return False, ""

    # ── Straddle entry ────────────────────────────────────────────────────
    def _enter_straddle(self, market_data: dict):
        spot   = market_data.get(self.underlying, {}).get("ltp", 0)
        expiry = self._nearest_weekly_expiry()
        dte    = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days

        if dte < 5 or spot == 0:
            return

        # ATM strike rounded to nearest 100 for BANKNIFTY
        atm_strike = round(spot / 100) * 100

        chain    = self._fetch_option_chain()
        c_prem   = self._get_premium(chain, atm_strike, "CE", spot * 0.008)
        p_prem   = self._get_premium(chain, atm_strike, "PE", spot * 0.008)
        net_prem = c_prem + p_prem

        if net_prem <= 0:
            logger.warning(f"BNKStraddle | invalid premium={net_prem} — skip")
            return

        # Risk = 2× net_premium × lot (straddle can lose 2x if big move)
        max_loss = net_prem * 2 * BNK_LOT_SIZE

        pos_id = f"STR_{self.underlying}_{expiry}_{datetime.now().strftime('%H%M')}"
        approved, reason = self.risk.approve_and_open(self.agent_id, pos_id, max_loss)
        if not approved:
            logger.info(f"BNKStraddle | risk blocked: {reason}")
            return

        ivr    = market_data.get(self.underlying, {}).get("iv_rank", 0)

        legs = [
            StraddleLeg(f"{self.underlying}{expiry}C{atm_strike}", atm_strike, "CE", "SELL", c_prem, BNK_LOT_SIZE),
            StraddleLeg(f"{self.underlying}{expiry}P{atm_strike}", atm_strike, "PE", "SELL", p_prem, BNK_LOT_SIZE),
        ]

        try:
            for idx, leg in enumerate(legs):
                self.broker.place_order(
                    leg.symbol, "NFO", "SELL", leg.quantity, leg.premium,
                    client_order_id=f"{self.agent_id}:{pos_id}:OPEN:{idx}",
                )
        except Exception as e:
            self.risk.cancel_open(self.agent_id, pos_id, str(e))
            logger.error(f"BNKStraddle | ORDER FAILED | {pos_id} | {e}")
            return

        pos = StraddlePosition(
            position_id=pos_id,
            expiry=expiry,
            legs=legs,
            entry_premium=net_prem,
            target_exit_premium=net_prem * 0.50,   # buy back at 50% of credit received
            stop_exit_premium=net_prem * 1.50,      # cut if premium doubled (net debit 150%)
            entry_time=datetime.now().isoformat(),
        )
        self.positions[pos_id] = pos
        self.journal.open_trade(
            trade_id=pos_id, agent_id=self.agent_id, symbol=self.underlying,
            direction="short_vol", entry_price=net_prem, quantity=BNK_LOT_SIZE,
            risk_amount=max_loss,
            thesis=(f"Short Straddle | ATM={atm_strike} | expiry={expiry} | "
                    f"IVR={ivr} | C={c_prem:.1f} P={p_prem:.1f} net={net_prem:.1f} | DTE={dte}"),
        )
        logger.info(f"BNKStraddle | ENTERED {pos_id} | net={net_prem:.1f} | max_loss={max_loss:.0f}")

    # ── Position management ───────────────────────────────────────────────
    def _manage_positions(self, market_data: dict):
        chain = self._fetch_option_chain()

        for pos_id, pos in list(self.positions.items()):
            if pos.status != "open":
                continue

            dte          = (datetime.strptime(pos.expiry, "%Y-%m-%d") - datetime.now()).days
            current_cost = self._current_close_cost(pos, chain)
            pnl          = (pos.entry_premium - current_cost) * BNK_LOT_SIZE

            should_close, reason = False, ""

            if dte <= 2:
                should_close, reason = True, f"DTE={dte} mandatory exit"
            elif current_cost <= pos.target_exit_premium:
                should_close, reason = True, f"50% profit | pnl={pnl:.0f}"
            elif current_cost >= pos.stop_exit_premium:
                should_close, reason = True, f"150% loss | pnl={pnl:.0f}"

            mkt_sent = market_data.get("_market_sentiment", {}).get("score", 0)
            if mkt_sent < -0.6:
                should_close, reason = True, f"sentiment_collapse: {mkt_sent:.2f}"

            if should_close:
                self._close_position(pos_id, pnl, reason)

    def _close_position(self, pos_id: str, pnl: float, reason: str):
        pos = self.positions[pos_id]
        for idx, leg in enumerate(pos.legs):
            self.broker.place_order(
                leg.symbol, "NFO", "BUY", leg.quantity, 0,
                client_order_id=f"{self.agent_id}:{pos_id}:CLOSE:{idx}",
            )
        pos.status = "closed"
        self.risk.register_close(self.agent_id, pos_id, pnl)
        self.journal.close_trade(pos_id, pos.entry_premium, reason)
        logger.info(f"BNKStraddle | CLOSED {pos_id} | pnl={pnl:.0f} | {reason}")

    def _has_open_position(self) -> bool:
        return any(p.status == "open" for p in self.positions.values())

    # ── NSE option chain ──────────────────────────────────────────────────
    def _fetch_option_chain(self) -> dict:
        try:
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={self.underlying}"
            r   = self._nse_session.get(url, timeout=10)
            rows = r.json().get("filtered", {}).get("data", [])
            chain = {}
            for row in rows:
                strike = row.get("strikePrice", 0)
                chain[strike] = {
                    "CE": float(row.get("CE", {}).get("lastPrice", 0) or 0),
                    "PE": float(row.get("PE", {}).get("lastPrice", 0) or 0),
                }
            return chain
        except Exception as e:
            logger.warning(f"BNKStraddle | chain fetch failed: {e}")
            return {}

    @staticmethod
    def _get_premium(chain: dict, strike: float, opt_type: str, fallback: float) -> float:
        if not chain:
            return fallback
        nearest = min(chain.keys(), key=lambda x: abs(x - strike))
        prem = chain.get(nearest, {}).get(opt_type, 0)
        return prem if prem > 0 else fallback

    @staticmethod
    def _current_close_cost(pos: StraddlePosition, chain: dict) -> float:
        """Net debit to buy back both short legs."""
        if not chain:
            dte_rem = max((datetime.strptime(pos.expiry, "%Y-%m-%d") - datetime.now()).days, 0)
            decay   = max(0.10, dte_rem / max(dte_rem + 7, 1))
            return pos.entry_premium * decay

        cost = 0.0
        for leg in pos.legs:
            nearest  = min(chain.keys(), key=lambda x: abs(x - leg.strike)) if chain else leg.strike
            curr_prem = chain.get(nearest, {}).get(leg.option_type, leg.premium * 0.5)
            cost += curr_prem   # both legs are SELL → both cost money to close

        return max(cost, 0.0)

    def _make_nse_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(NSE_HEADERS)
        try:
            session.get("https://www.nseindia.com", timeout=8)
        except Exception:
            pass
        return session

    @staticmethod
    def _nearest_weekly_expiry() -> str:
        today = datetime.now()
        days  = (3 - today.weekday()) % 7
        if days == 0:
            days = 7
        return (today + timedelta(days=days)).strftime("%Y-%m-%d")
