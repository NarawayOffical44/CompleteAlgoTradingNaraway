"""
Trade Journal — Thread-safe. All bots write to the same journal concurrently.
Auditable from Day 1. Required for future RA/AIF track record.
"""

import csv
import json
import threading
from datetime import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path
from loguru import logger


@dataclass
class TradeRecord:
    trade_id:    str
    agent_id:    str
    symbol:      str
    direction:   str            # long | short
    entry_price: float
    exit_price:  float = 0.0
    quantity:    float = 0.0
    risk_amount: float = 0.0
    pnl:         float = 0.0
    pnl_pct:     float = 0.0
    entry_time:  str   = ""
    exit_time:   str   = ""
    thesis:      str   = ""     # Why entered
    exit_reason: str   = ""     # Why exited
    regime:      str   = ""
    status:      str   = "open"
    tags:        list  = field(default_factory=list)


class TradeJournal:

    def __init__(self, journal_dir: str = "logs"):
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(exist_ok=True)
        self.trades: dict[str, TradeRecord] = {}
        self._lock = threading.Lock()
        self._load_existing()

    # ── Open a trade ──────────────────────────────────────────────────────
    def open_trade(
        self,
        trade_id:    str,
        agent_id:    str,
        symbol:      str,
        direction:   str,
        entry_price: float,
        quantity:    float,
        risk_amount: float,
        thesis:      str  = "",
        regime:      str  = "",
        tags:        list = None,
    ) -> TradeRecord:
        record = TradeRecord(
            trade_id=trade_id,
            agent_id=agent_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            quantity=quantity,
            risk_amount=risk_amount,
            entry_time=datetime.now().isoformat(),
            thesis=thesis,
            regime=regime,
            tags=tags or [],
            status="open",
        )
        with self._lock:
            self.trades[trade_id] = record
            self._append_csv(record)
        logger.info(f"JOURNAL OPEN | {trade_id} | {symbol} {direction} @ {entry_price}")
        return record

    # ── Close a trade ─────────────────────────────────────────────────────
    def close_trade(
        self,
        trade_id:    str,
        exit_price:  float,
        exit_reason: str = "",
    ) -> TradeRecord:
        with self._lock:
            if trade_id not in self.trades:
                raise ValueError(f"Trade {trade_id} not found in journal")

            r             = self.trades[trade_id]
            r.exit_price  = exit_price
            r.exit_time   = datetime.now().isoformat()
            r.exit_reason = exit_reason
            r.status      = "closed"

            if r.direction == "long":
                r.pnl = (exit_price - r.entry_price) * r.quantity
            else:
                r.pnl = (r.entry_price - exit_price) * r.quantity

            r.pnl_pct = (r.pnl / (r.entry_price * r.quantity)) * 100 if r.entry_price else 0
            self._append_csv(r)

        logger.info(f"JOURNAL CLOSE | {trade_id} | pnl={r.pnl:.2f} ({r.pnl_pct:.2f}%) | {exit_reason}")
        return r

    # ── Performance summary ───────────────────────────────────────────────
    def summary(self, agent_id: str = None) -> dict:
        with self._lock:
            closed = [t for t in self.trades.values() if t.status == "closed"]
        if agent_id:
            closed = [t for t in closed if t.agent_id == agent_id]
        if not closed:
            return {"trades": 0}

        pnls    = [t.pnl for t in closed]
        winners = [p for p in pnls if p > 0]
        losers  = [p for p in pnls if p <= 0]

        return {
            "trades":        len(closed),
            "win_rate":      round(len(winners) / len(closed) * 100, 1),
            "total_pnl":     round(sum(pnls), 2),
            "avg_win":       round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loss":      round(sum(losers)  / len(losers),  2) if losers  else 0,
            "profit_factor": round(abs(sum(winners) / sum(losers)), 2)
                             if losers and sum(losers) != 0 else 0,
            "best_trade":    round(max(pnls), 2),
            "worst_trade":   round(min(pnls), 2),
        }

    # ── Export ────────────────────────────────────────────────────────────
    def export_json(self) -> str:
        path = self.journal_dir / f"journal_{datetime.now().strftime('%Y%m%d')}.json"
        with self._lock:
            data = [asdict(t) for t in self.trades.values()]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return str(path)

    # ── Internal ──────────────────────────────────────────────────────────
    def _csv_path(self) -> Path:
        return self.journal_dir / "trades.csv"

    def _append_csv(self, record: TradeRecord):
        """Must be called with self._lock held."""
        path         = self._csv_path()
        write_header = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=asdict(record).keys())
            if write_header:
                writer.writeheader()
            writer.writerow(asdict(record))

    def _load_existing(self):
        path = self._csv_path()
        if not path.exists():
            return
        with open(path, "r") as f:
            for row in csv.DictReader(f):
                try:
                    row["tags"]        = json.loads(row.get("tags", "[]"))
                    row["pnl"]         = float(row["pnl"])
                    row["pnl_pct"]     = float(row["pnl_pct"])
                    row["entry_price"] = float(row["entry_price"])
                    row["exit_price"]  = float(row["exit_price"])
                    row["quantity"]    = float(row["quantity"])
                    row["risk_amount"] = float(row["risk_amount"])
                    self.trades[row["trade_id"]] = TradeRecord(**row)
                except Exception:
                    pass
        logger.info(f"Journal loaded | {len(self.trades)} trades")
