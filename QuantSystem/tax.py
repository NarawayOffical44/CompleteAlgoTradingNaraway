"""
Indian trading tax calculator (FY 2024-25, post Budget 2024).

Trade types:
  equity_intraday  → speculative business income → income slab rate
  equity_delivery  → STCG 20% (<1yr) | LTCG 12.5% (>1yr, ₹1.25L exemption ignored)
  fno              → non-speculative business income → income slab rate

Charges: STT, Dhan brokerage, NSE exchange, SEBI, GST on charges, stamp duty.
"""


# ── Charge rates ──────────────────────────────────────────────────────────────

# STT (% of trade value)
_STT = {
    "equity_delivery": {"buy": 0.1,   "sell": 0.1},
    "equity_intraday": {"buy": 0.0,   "sell": 0.025},
    "fno":             {"buy": 0.0,   "sell": 0.0625},   # options premium
}

# NSE exchange transaction charges (% of turnover)
_EXCHANGE = {
    "equity_delivery": 0.00297,
    "equity_intraday": 0.00297,
    "fno":             0.053,
}

# Stamp duty on buy side (%)
_STAMP = {
    "equity_delivery": 0.015,
    "equity_intraday": 0.003,
    "fno":             0.003,
}

_SEBI_PCT      = 0.0001    # % of turnover
_GST_PCT       = 18.0      # % on brokerage + exchange charges
_BROKERAGE_FEE = 20.0      # Dhan flat fee per order (equity intraday + F&O)
                            # Equity delivery = ₹0


class TaxCalculator:

    def __init__(self, income_slab_pct: float = 30.0):
        """
        income_slab_pct: your income tax slab rate.
        Default 30% (highest bracket — conservative).
        """
        self.slab = income_slab_pct

    # ── Single trade ──────────────────────────────────────────────────────
    def calculate(
        self,
        trade_type: str,        # 'equity_delivery' | 'equity_intraday' | 'fno'
        buy_value: float,       # qty × buy_price
        sell_value: float,      # qty × sell_price
        gross_pnl: float,       # pre-charge profit (+ or -)
        holding_days: int = 0,
    ) -> dict:
        """Full cost breakdown for one round-trip trade."""

        # STT
        stt = (buy_value  * _STT[trade_type]["buy"]  / 100 +
               sell_value * _STT[trade_type]["sell"] / 100)

        # Brokerage (per order × 2 sides)
        brokerage = 0.0 if trade_type == "equity_delivery" else _BROKERAGE_FEE * 2

        # Exchange charges
        turnover = buy_value + sell_value
        exchange = turnover * _EXCHANGE[trade_type] / 100

        # SEBI
        sebi = turnover * _SEBI_PCT / 100

        # Stamp duty (buy side only)
        stamp = buy_value * _STAMP[trade_type] / 100

        # GST on brokerage + exchange
        gst = (brokerage + exchange) * _GST_PCT / 100

        transaction_costs = stt + brokerage + exchange + sebi + stamp + gst

        # Capital gains / income tax (only on profit)
        tax_on_profit = 0.0
        if gross_pnl > 0:
            if trade_type == "equity_delivery":
                tax_on_profit = gross_pnl * (0.125 if holding_days >= 365 else 0.20)
            else:
                tax_on_profit = gross_pnl * (self.slab / 100)

        total_deductions = transaction_costs + tax_on_profit

        return {
            "stt":               round(stt, 2),
            "brokerage":         round(brokerage, 2),
            "exchange":          round(exchange, 2),
            "sebi":              round(sebi, 2),
            "stamp":             round(stamp, 2),
            "gst":               round(gst, 2),
            "transaction_costs": round(transaction_costs, 2),
            "tax_on_profit":     round(tax_on_profit, 2),
            "total_deductions":  round(total_deductions, 2),
            "net_pnl":           round(gross_pnl - total_deductions, 2),
        }

    # ── Batch (from journal trades) ───────────────────────────────────────
    def from_journal(self, trades: list) -> dict:
        """
        trades: list of TradeRecord objects (status == 'closed').
        Returns cumulative tax summary.
        """
        gross = costs = tax = 0.0

        for t in trades:
            if t.status != "closed":
                continue
            buy_val  = t.entry_price * t.quantity
            sell_val = t.exit_price  * t.quantity
            ttype    = "fno" if "options" in t.agent_id else "equity_intraday"
            result   = self.calculate(ttype, buy_val, sell_val, t.pnl)
            gross   += t.pnl
            costs   += result["transaction_costs"]
            tax     += result["tax_on_profit"]

        return {
            "gross_pnl":         round(gross, 2),
            "transaction_costs": round(costs, 2),
            "tax_on_profit":     round(tax, 2),
            "total_deductions":  round(costs + tax, 2),
            "net_pnl":           round(gross - costs - tax, 2),
            "effective_tax_pct": round((costs + tax) / gross * 100, 1) if gross > 0 else 0.0,
        }
