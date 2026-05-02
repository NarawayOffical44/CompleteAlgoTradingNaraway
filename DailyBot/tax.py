"""
India crypto tax tracker — 30% flat on gains, no loss offset.
Persists to tax_ledger.json in the run directory.
"""
import json
import os
from datetime import date

from config import INDIA_TAX_RATE, INR_PER_USD

TAX_FILE = "tax_ledger.json"


def _load() -> dict:
    if os.path.exists(TAX_FILE):
        with open(TAX_FILE) as f:
            return json.load(f)
    return {"total_gains_usdt": 0.0, "total_tax_usdt": 0.0, "trades": []}


def _save(ledger: dict) -> None:
    with open(TAX_FILE, "w") as f:
        json.dump(ledger, f, indent=2, default=str)


def record_trade(gross_pnl_usdt: float, inr_per_usd: float = INR_PER_USD) -> dict:
    """
    Persist one closed trade. Returns a dict with INR breakdown.
    Tax is only charged on gains (not losses).
    """
    ledger = _load()
    tax_usdt = max(0.0, gross_pnl_usdt * INDIA_TAX_RATE)
    net_usdt = gross_pnl_usdt - tax_usdt

    entry = {
        "date":       str(date.today()),
        "gross_usdt": round(gross_pnl_usdt, 4),
        "tax_usdt":   round(tax_usdt, 4),
        "net_usdt":   round(net_usdt, 4),
        "gross_inr":  round(gross_pnl_usdt * inr_per_usd, 2),
        "tax_inr":    round(tax_usdt       * inr_per_usd, 2),
        "net_inr":    round(net_usdt        * inr_per_usd, 2),
    }
    ledger["trades"].append(entry)

    if gross_pnl_usdt > 0:
        ledger["total_gains_usdt"] += gross_pnl_usdt
        ledger["total_tax_usdt"]   += tax_usdt

    _save(ledger)
    return entry


def daily_net_inr() -> float:
    """Sum of net_inr for today's trades (after tax)."""
    ledger = _load()
    today  = str(date.today())
    return sum(t["net_inr"] for t in ledger["trades"] if t["date"] == today)


def daily_summary(inr_per_usd: float = INR_PER_USD) -> dict:
    ledger = _load()
    today  = str(date.today())
    rows   = [t for t in ledger["trades"] if t["date"] == today]

    gross = sum(t["gross_inr"] for t in rows)
    tax   = sum(t["tax_inr"]   for t in rows)
    net   = sum(t["net_inr"]   for t in rows)

    return {
        "date":        today,
        "trades":      len(rows),
        "gross_inr":   round(gross, 2),
        "tax_inr":     round(tax,   2),
        "net_inr":     round(net,   2),
        "target_met":  net >= 20.0,
    }
