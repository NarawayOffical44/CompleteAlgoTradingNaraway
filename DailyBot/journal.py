"""
Append-only CSV trade journal.
"""
import csv
import os
from datetime import datetime

JOURNAL_FILE = "trades.csv"
HEADERS = [
    "date", "time", "side", "entry_price", "exit_price", "reason",
    "hold_minutes", "gross_inr", "tax_inr", "net_inr",
]


def log_trade(trade: dict) -> None:
    write_header = not os.path.exists(JOURNAL_FILE)
    trade.setdefault("time", datetime.now().strftime("%H:%M:%S"))
    with open(JOURNAL_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow({k: trade.get(k, "") for k in HEADERS})
