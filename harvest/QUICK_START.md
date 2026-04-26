# Harvest System - Quick Start

## Start
```bash
venv/Scripts/python.exe harvest/main_harvest.py
```

Output: Logs only trades, saves state to `harvest_state.json`

## Monitor
```bash
cat harvest/harvest_state.json
```

Current capital and P&L:
```json
{
  "f_and_o_pnl": 450,
  "forex_capital": 450,
  "forex_pnl": 20,
  "total_harvested": 450,
  "timestamp": "2026-04-04T..."
}
```

## Check All Trades
```bash
cat harvest/harvest_trades.json
```

## Configure
Edit `harvest/harvest_config.yaml`:
- F&O symbol, leverage
- Forex symbol, min capital

## Stop
`Ctrl+C` - saves state automatically

---

## What It Does

- **F&O:** Trades ₹1000 continuously
- **Forex:** Grows from F&O profits
- **Both run simultaneously**
- **Auto-saves state every trade**

Done!
