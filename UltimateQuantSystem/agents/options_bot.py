"""
Options Agent: Iron Condor on Nifty/BankNifty
Only activates when ALL conditions pass:
  1. HMM + Claude regime = BULL_LOW_VOL or CHOPPY
  2. IVR > 30 (sufficient premium to sell)
  3. Market sentiment score > -0.2 (not significantly bearish)
  4. Overall fundamentals healthy (ADR > 1.0, FII not strongly negative)
  5. Days to next major event >= 5

Lot sizes (SEBI revised 2024):
  NIFTY     = 25
  BANKNIFTY = 15
"""

import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger
from risk import RiskEngine, RiskMode
from journal import TradeJournal
from broker import DhanClient


SAFE_REGIMES         = {"BULL_LOW_VOL", "CHOPPY"}
MIN_IVR              = 30
MIN_MARKET_SENTIMENT = -0.2
MIN_ADR              = 1.0
MAX_FII_OUTFLOW      = -2000   # Crores
MIN_DTE_TO_EVENT     = 5

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://www.nseindia.com/",
}

# SEBI-revised lot sizes (updated Oct 2024)
LOT_SIZES = {
    "NIFTY":     25,
    "BANKNIFTY": 15,
}


@dataclass
class IronCondorLeg:
    symbol:      str
    strike:      float
    option_type: str    # CE | PE
    action:      str    # BUY | SELL
    premium:     float
    quantity:    int


@dataclass
class IronCondorPosition:
    position_id:         str
    underlying:          str
    expiry:              str
    legs:                list = field(default_factory=list)
    max_profit:          float = 0
    max_loss:            float = 0
    entry_premium:       float = 0
    target_exit_premium: float = 0
    stop_exit_premium:   float = 0
    entry_time:          str = ""
    status:              str = "open"


class OptionsBot:

    def __init__(self, risk_engine: RiskEngine, journal: TradeJournal,
                 broker: DhanClient, underlying: str = "NIFTY", wing_width: int = 100):
        self.risk       = risk_engine
        self.journal    = journal
        self.broker     = broker
        self.underlying = underlying
        self.wing_width = wing_width
        self.positions: dict[str, IronCondorPosition] = {}
        self._nse_session = self._make_nse_session()

    def run(self, regime: str, market_data: dict):
        if self.risk.state.mode == RiskMode.STOPPED:
            logger.warning("OptionsBot | kill switch — skip")
            return

        block, reason = self._pre_checks(regime, market_data)
        if block:
            logger.info(f"OptionsBot | BLOCKED | {reason}")
            return

        if self._has_open_position():
            self._manage_open_positions(market_data)
        else:
            self._enter_iron_condor(market_data)

    # ── Pre-checks (all must pass) ────────────────────────────────────────
    def _pre_checks(self, regime: str, market_data: dict) -> tuple[bool, str]:
        if regime not in SAFE_REGIMES:
            return True, f"regime={regime} not safe for options selling"

        ivr = market_data.get(self.underlying, {}).get("iv_rank", 0)
        if ivr < MIN_IVR:
            return True, f"IVR={ivr} < {MIN_IVR} — insufficient premium"

        mkt_sent = market_data.get("_market_sentiment", {})
        if mkt_sent.get("score", 0) < MIN_MARKET_SENTIMENT:
            return True, f"Market sentiment={mkt_sent.get('score', 0):.2f} — too bearish"

        adr = market_data.get("ADR", 1.0)
        if adr < MIN_ADR:
            return True, f"ADR={adr:.2f} < {MIN_ADR} — weak breadth"

        fii = market_data.get("FII_FLOW", 0)
        if fii < MAX_FII_OUTFLOW:
            return True, f"FII outflow={fii}Cr — institutional selling"

        dte_event = market_data.get("DAYS_TO_EVENT", 30)
        if dte_event < MIN_DTE_TO_EVENT:
            return True, f"Major event in {dte_event} days — avoid selling"

        return False, ""

    # ── Iron Condor entry ─────────────────────────────────────────────────
    def _enter_iron_condor(self, market_data: dict):
        spot   = market_data.get(self.underlying, {}).get("ltp", 0)
        expiry = self._nearest_weekly_expiry()
        dte    = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days

        if dte < 5 or spot == 0:
            return

        std_move  = spot * 0.01 * (dte ** 0.5)
        sc_strike = round((spot + std_move) / 50) * 50
        lc_strike = sc_strike + self.wing_width
        sp_strike = round((spot - std_move) / 50) * 50
        lp_strike = sp_strike - self.wing_width

        # Fetch live option chain premiums from NSE
        chain = self._fetch_option_chain()
        sc_prem = self._get_strike_premium(chain, sc_strike, "CE", spot * 0.005)
        lc_prem = self._get_strike_premium(chain, lc_strike, "CE", spot * 0.002)
        sp_prem = self._get_strike_premium(chain, sp_strike, "PE", spot * 0.005)
        lp_prem = self._get_strike_premium(chain, lp_strike, "PE", spot * 0.002)

        net_premium = (sc_prem - lc_prem) + (sp_prem - lp_prem)
        lot_size    = LOT_SIZES.get(self.underlying, 25)
        max_loss    = (self.wing_width - net_premium) * lot_size

        approved, reason = self.risk.approve_trade("options_bot", max_loss)
        if not approved:
            logger.info(f"OptionsBot | risk blocked: {reason}")
            return

        ivr      = market_data.get(self.underlying, {}).get("iv_rank", 50)
        mkt_sent = market_data.get("_market_sentiment", {}).get("score", 0)
        pos_id   = f"IC_{self.underlying}_{expiry}_{datetime.now().strftime('%H%M')}"

        legs = [
            IronCondorLeg(f"{self.underlying}{expiry}C{sc_strike}", sc_strike, "CE", "SELL", sc_prem, lot_size),
            IronCondorLeg(f"{self.underlying}{expiry}C{lc_strike}", lc_strike, "CE", "BUY",  lc_prem, lot_size),
            IronCondorLeg(f"{self.underlying}{expiry}P{sp_strike}", sp_strike, "PE", "SELL", sp_prem, lot_size),
            IronCondorLeg(f"{self.underlying}{expiry}P{lp_strike}", lp_strike, "PE", "BUY",  lp_prem, lot_size),
        ]

        for leg in legs:
            self.broker.place_order(leg.symbol, "NFO", leg.action, leg.quantity, leg.premium)

        pos = IronCondorPosition(
            position_id=pos_id, underlying=self.underlying, expiry=expiry, legs=legs,
            max_profit=net_premium * lot_size, max_loss=max_loss,
            entry_premium=net_premium,
            target_exit_premium=net_premium * 0.50,
            stop_exit_premium=net_premium * 2.0,
            entry_time=datetime.now().isoformat(),
        )
        self.positions[pos_id] = pos
        self.risk.register_open("options_bot", pos_id, max_loss)
        self.journal.open_trade(
            trade_id=pos_id, agent_id="options_bot", symbol=self.underlying,
            direction="short_vol", entry_price=net_premium, quantity=lot_size,
            risk_amount=max_loss,
            thesis=(f"Iron Condor | {sp_strike}P/{sc_strike}C | expiry={expiry} | "
                    f"IVR={ivr} | market_sent={mkt_sent:.2f} | DTE={dte} | "
                    f"lot={lot_size} | net_prem={net_premium:.2f}"),
        )
        logger.info(f"OptionsBot | ENTERED {pos_id} | net_prem={net_premium:.2f} | max_loss={max_loss:.0f}")

    # ── Position management ───────────────────────────────────────────────
    def _manage_open_positions(self, market_data: dict):
        chain = self._fetch_option_chain()

        for pos_id, pos in list(self.positions.items()):
            if pos.status != "open":
                continue

            dte             = (datetime.strptime(pos.expiry, "%Y-%m-%d") - datetime.now()).days
            current_premium = self._calc_current_close_cost(pos, chain)
            pnl             = (pos.entry_premium - current_premium) * LOT_SIZES.get(pos.underlying, 25)

            should_close, reason = False, ""

            if dte <= 3:
                should_close, reason = True, f"DTE={dte} mandatory exit"
            elif current_premium <= pos.target_exit_premium:
                should_close, reason = True, f"50% profit target | pnl={pnl:.0f}"
            elif current_premium >= pos.stop_exit_premium:
                should_close, reason = True, f"2x loss limit | pnl={pnl:.0f}"

            # Sentiment emergency exit
            mkt_sent = market_data.get("_market_sentiment", {}).get("score", 0)
            if mkt_sent < -0.6:
                should_close, reason = True, f"Market sentiment collapsed to {mkt_sent:.2f}"

            if should_close:
                self._close_position(pos_id, pnl, reason)

    def _close_position(self, pos_id: str, pnl: float, reason: str):
        pos = self.positions[pos_id]
        for leg in pos.legs:
            close_action = "BUY" if leg.action == "SELL" else "SELL"
            self.broker.place_order(leg.symbol, "NFO", close_action, leg.quantity, 0)
        pos.status = "closed"
        self.risk.register_close("options_bot", pos_id, pnl)
        self.journal.close_trade(pos_id, pos.entry_premium, reason)
        logger.info(f"OptionsBot | CLOSED {pos_id} | pnl={pnl:.0f} | {reason}")

    def _has_open_position(self) -> bool:
        return any(p.status == "open" for p in self.positions.values())

    # ── Live NSE option chain ─────────────────────────────────────────────
    def _fetch_option_chain(self) -> dict:
        """
        Fetch live option chain from NSE.
        Returns {strike: {"CE": price, "PE": price}} lookup.
        """
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
            logger.debug(f"OptionsBot | chain fetched | {len(chain)} strikes")
            return chain
        except Exception as e:
            logger.warning(f"OptionsBot | chain fetch failed: {e} — using fallback premiums")
            return {}

    @staticmethod
    def _get_strike_premium(chain: dict, strike: float, opt_type: str,
                            fallback: float) -> float:
        """Look up premium for a given strike from the chain dict."""
        if not chain:
            return fallback
        nearest = min(chain.keys(), key=lambda x: abs(x - strike))
        prem = chain.get(nearest, {}).get(opt_type, 0)
        return prem if prem > 0 else fallback

    def _calc_current_close_cost(self, pos: IronCondorPosition, chain: dict) -> float:
        """
        Current net debit to close all 4 legs.
        SELL legs: we BUY to close (costs money).
        BUY legs: we SELL to close (receives money).
        """
        if not chain:
            # Fallback: linear time-decay estimate
            dte_remaining = max((datetime.strptime(pos.expiry, "%Y-%m-%d") - datetime.now()).days, 0)
            decay = max(0.10, dte_remaining / max(dte_remaining + 7, 1))
            return pos.entry_premium * decay

        cost = 0.0
        for leg in pos.legs:
            nearest  = min(chain.keys(), key=lambda x: abs(x - leg.strike)) if chain else leg.strike
            curr_prem = chain.get(nearest, {}).get(leg.option_type, leg.premium * 0.5)
            cost += curr_prem if leg.action == "SELL" else -curr_prem

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
