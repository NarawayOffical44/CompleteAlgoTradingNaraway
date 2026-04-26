# Harvest System - Data Modes

**Policy: Always use LIVE or HISTORICAL data. NO MOCKS.**

## 1. HISTORICAL DATA (Recommended for Testing)

**Best for:** Reproducible testing, fast iteration, offline work

Edit `harvest/harvest_config.yaml`:
```yaml
data_mode: "historical"
historical_data_file: "data/harvest_backtest_data.csv"
```

### Generate Historical Data

Use existing backtest data:
```bash
# Run backtest to generate OHLCV data
venv/Scripts/python.exe backtest/run_backtest.py

# CSV will be in: data/harvest_backtest_data.csv
```

CSV format needed:
```
timestamp,open,high,low,close,volume
2026-01-01 00:00:00,35000,35500,34800,35200,1000
2026-01-01 01:00:00,35200,35600,35100,35400,1100
...
```

Run with historical:
```bash
venv/Scripts/python.exe harvest/main_harvest.py
```

---

## 2. LIVE DATA (Real-Time Testing)

**Best for:** Final validation, real market conditions, production prep

Edit `harvest/harvest_config.yaml`:
```yaml
data_mode: "live"
```

Requires:
- Internet connection
- Binance API access (no keys needed for public data)

Run:
```bash
venv/Scripts/python.exe harvest/main_harvest.py
```

---

## 3. How It Works

### Historical Mode
- Reads CSV line by line
- Simulates price movement
- **Deterministic:** Same results every run
- **Fast:** No network delays

### Live Mode
- Fetches real Binance prices
- **Real-time:** Current market conditions
- **Slow:** Network latency
- **Non-deterministic:** Results vary

---

## Recommended Workflow

```
Week 1-2:  Test with HISTORICAL data (fast, offline)
           └─ Validate logic, recovery, harvesting

Week 3:    Test with LIVE data (before deployment)
           └─ Verify real market conditions

Week 4+:   Deploy to VPS (live trading with real capital)
           └─ Start with ₹1000
```

---

## Quick Test

```bash
# 1. Generate historical data
venv/Scripts/python.exe backtest/run_backtest.py

# 2. Verify data created
ls -lh data/harvest_backtest_data.csv

# 3. Run harvest system
venv/Scripts/python.exe harvest/main_harvest.py

# 4. Check results
cat harvest/harvest_state.json
```
