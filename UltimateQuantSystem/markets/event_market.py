"""
EventMarket — Event-driven market that is only "open" on predefined event days.

Use this for bots that trade around specific market events:
  - Union Budget (January/February)
  - RBI Monetary Policy announcements (6x per year)
  - Nifty/BankNifty expiry days (last Thursday of month)
  - Q1/Q2/Q3/Q4 result seasons
  - Custom one-off events (elections, policy changes, etc.)

How it works:
  - is_open() returns True only during window around a registered event
  - Each event has: date, name, window_days_before, window_days_after
  - Bot only activates on event days (and surrounding window)
  - Market data comes from NSEMarket (same underlying data)
  - Regime is NSE regime (events are NSE equity events)

Usage:
  from markets import EventMarket
  budget_market = EventMarket(name="Budget2026")
  budget_market.add_event("2026-02-01", "Union Budget 2026", before_days=1, after_days=2)
  rbi_bot = RBIStraddleBot(agent_id="rbi_straddle", ...)
  registry.register(BotRunner(agent=rbi_bot, market=budget_market, risk_engine=risk))

The bot will only run from Jan 31 (1 day before) to Feb 3 (2 days after).
All other days: is_open() returns False → BotRunner skips → bot sleeps.

To add recurring events programmatically:
  market.add_rbi_events_2026()    # fills entire year's RBI dates
  market.add_expiry_thursdays()   # adds all monthly expiry days
"""

from datetime import datetime, date, timedelta
from loguru import logger

from markets.base_market import BaseMarket


class EventWindow:
    """A single event with its active trading window."""
    def __init__(self, event_date: date, name: str, before_days: int = 1, after_days: int = 1):
        self.event_date  = event_date
        self.name        = name
        self.start_date  = event_date - timedelta(days=before_days)
        self.end_date    = event_date + timedelta(days=after_days)

    def is_active(self) -> bool:
        today = date.today()
        return self.start_date <= today <= self.end_date

    def __repr__(self):
        return f"EventWindow({self.name}, {self.event_date}, [{self.start_date}→{self.end_date}])"


class EventMarket(BaseMarket):
    """
    Market that only opens during registered event windows.
    Wraps NSEMarket for data (events are NSE equity/options events).
    """

    def __init__(self, name: str = "EventMarket", nse_market=None):
        """
        name       : human-readable name for this event market
        nse_market : optional NSEMarket instance to share (avoids duplicate data fetch)
                     If None, creates its own NSEMarket
        """
        self._name   = name
        self._events: list[EventWindow] = []
        self._nse    = nse_market

        # Lazy-load NSE market to avoid circular import at module load
        self._nse_loaded = False

    @property
    def market_id(self) -> str:
        return f"EVENT:{self._name}"

    def _get_nse(self):
        if self._nse is None and not self._nse_loaded:
            from markets.nse_market import NSEMarket
            self._nse = NSEMarket()
            self._nse_loaded = True
        return self._nse

    # ── Event registration ────────────────────────────────────────────────
    def add_event(self, date_str: str, name: str,
                  before_days: int = 1, after_days: int = 1) -> "EventMarket":
        """
        Add a trading event.
        date_str: 'YYYY-MM-DD'
        Returns self for chaining.

        Example:
          market.add_event("2026-02-01", "Union Budget", before_days=1, after_days=2)
                 .add_event("2026-06-06", "RBI Policy June", before_days=0, after_days=1)
        """
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        event = EventWindow(d, name, before_days, after_days)
        self._events.append(event)
        logger.info(f"EventMarket | {self._name} | added event: {event}")
        return self

    def add_expiry_thursdays(self, year: int = None) -> "EventMarket":
        """Add all monthly Nifty expiry days (last Thursday of each month) for a year."""
        if year is None:
            year = date.today().year
        for month in range(1, 13):
            # Find last Thursday of month
            last_day = date(year, month, 28)
            if month == 12:
                last_day = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = date(year, month + 1, 1) - timedelta(days=1)
            # Walk back to last Thursday (weekday=3)
            while last_day.weekday() != 3:
                last_day -= timedelta(days=1)
            self.add_event(
                last_day.strftime("%Y-%m-%d"),
                f"Nifty Expiry {last_day.strftime('%b-%Y')}",
                before_days=1,
                after_days=0,
            )
        return self

    def add_rbi_events_2026(self) -> "EventMarket":
        """RBI MPC meeting dates for 2026 (approximate — verify official calendar)."""
        dates = [
            ("2026-02-07", "RBI MPC Feb 2026"),
            ("2026-04-09", "RBI MPC Apr 2026"),
            ("2026-06-06", "RBI MPC Jun 2026"),
            ("2026-08-06", "RBI MPC Aug 2026"),
            ("2026-10-08", "RBI MPC Oct 2026"),
            ("2026-12-05", "RBI MPC Dec 2026"),
        ]
        for d, name in dates:
            self.add_event(d, name, before_days=1, after_days=1)
        return self

    # ── Market hours — only active during event windows ───────────────────
    def is_open(self) -> bool:
        # Must be a weekday
        if date.today().weekday() >= 5:
            return False
        # Must be within NSE trading hours
        now = datetime.now()
        t = now.hour * 60 + now.minute
        if not (9 * 60 + 15 <= t <= 15 * 60 + 30):
            return False
        # Must be within at least one event window
        active_events = [e for e in self._events if e.is_active()]
        if active_events:
            logger.debug(f"EventMarket | {self._name} | active events: {[e.name for e in active_events]}")
            return True
        return False

    def active_event_names(self) -> list[str]:
        """Return names of currently active events (for dashboard display)."""
        return [e.name for e in self._events if e.is_active()]

    def next_event(self) -> EventWindow | None:
        """Return the next upcoming event (future only)."""
        today = date.today()
        future = [e for e in self._events if e.event_date >= today]
        return min(future, key=lambda e: e.event_date) if future else None

    # ── Safety check ──────────────────────────────────────────────────────
    def is_safe(self) -> tuple[bool, str]:
        nse = self._get_nse()
        if nse:
            return nse.is_safe()
        return True, "ok"

    # ── Delegate data/regime/etc to NSEMarket ────────────────────────────
    def get_data(self) -> dict:
        return self._get_nse().get_data()

    def get_regime(self, market_data: dict = None) -> str:
        return self._get_nse().get_regime(market_data)

    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        # Event bots are always fully allocated during their window
        # (HeadAI can reduce via head_ai_mult)
        return 1.0

    def get_fundamentals(self, symbols: list) -> dict:
        return self._get_nse().get_fundamentals(symbols)

    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        return self._get_nse().get_sentiment(symbols, market_data)

    # ── Status summary ────────────────────────────────────────────────────
    def status_str(self) -> str:
        active = self.active_event_names()
        if active:
            return f"ACTIVE: {', '.join(active)}"
        nxt = self.next_event()
        if nxt:
            days = (nxt.event_date - date.today()).days
            return f"Waiting ({nxt.name} in {days}d)"
        return "No events scheduled"
