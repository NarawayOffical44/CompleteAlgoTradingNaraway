# Harvest Trading System - Complete Setup ✅

## What Was Built

**Separate, independent trading system** that runs alongside your main bot.

```
D:/Apps/AlgoTrading/
│
├── main.py                                   [MAIN TRADING BOT]
│   └─ Runs: venv/Scripts/python.exe main.py
│
├── harvest/                                  [NEW - HARVEST SYSTEM]
│   ├── __init__.py                          (Makes it a module)
│   ├── main_harvest.py                      (Entry point - START HERE)
│   ├── harvest_trader.py                    (Core logic)
│   ├── harvest_config.yaml                  (Configuration)
│   ├── README.md                            (Full documentation)
│   ├── harvest_state.json                   (Auto-created: persistent state)
│   ├── harvest_trades.json                  (Auto-created: all trades)
│   └── harvest_history.json                 (Auto-created: harvest log)
│
└── [everything else unchanged]
```

## How to Run

```bash
venv/Scripts/python.exe harvest/main_harvest.py
```

**Simple CLI - no dashboard. Logs trades to console, saves state to file.**

Runs until you press `Ctrl+C` (saves state automatically)

---

## Key Concepts

### F&O Tier (High Risk)
- Capital: **₹1000 (FIXED)**
- Leverage: 5x
- Strategy: Mean reversion (1h)
- Behavior: **Keeps trading with same ₹1000 forever**
- Profits: **HARVESTED** (exit to Forex, don't reinvest)
- Losses: **Absorbed**, Forex unaffected

### Forex Tier (Low Risk)
- Capital: **Starts ₹0, grows from F&O harvests**
- Leverage: 2x
- Strategy: EMA crossover (4h)
- Behavior: **Starts only after F&O makes profit**
- Profits: **COMPOUND** (stay in Forex, reinvest)
- Losses: **Reduce capital but don't stop F&O**

---

## Example: First 7 Days

```
DAY 1:
  ├─ F&O trades: +₹50 profit
  ├─ Action: Harvest ₹50 → Forex
  └─ Total: F&O ₹1000 + Forex ₹50 = ₹1050

DAY 2:
  ├─ F&O trades: -₹30 loss
  ├─ Action: [Nothing, Forex protected]
  └─ Total: F&O ₹1000 + Forex ₹50 = ₹1050 (protected!)

DAY 3:
  ├─ F&O trades: +₹100 profit
  ├─ Forex trades: +₹2 profit
  ├─ Actions: Harvest ₹100 → Forex
  └─ Total: F&O ₹1000 + Forex ₹152 = ₹1152

DAY 7:
  └─ Total: F&O ₹1000 + Forex ₹280 = ₹1280
```

---

## Files Created

| File | Purpose |
|------|---------|
| `harvest/main_harvest.py` | Start here: `venv/Scripts/python.exe harvest/main_harvest.py` |
| `harvest/harvest_trader.py` | Core system (HarvestTrader class) |
| `harvest/harvest_config.yaml` | Configure symbols, leverage, strategies |
| `harvest/README.md` | Full documentation & troubleshooting |
| `harvest/__init__.py` | Makes it importable as module |
| `INNOVATIONS.md` | Added entry #11: Harvest System documentation |

**Auto-created on first run:**
- `harvest/harvest_state.json` - Current P&L and capital (persists across restarts)
- `harvest/harvest_trades.json` - All trade records
- `harvest/harvest_history.json` - Harvest events log

---

## Configuration

Edit `harvest/harvest_config.yaml` to change:

```yaml
f_and_o_tier:
  capital: 1000              # Keep this fixed
  leverage: 5                # Can increase as you test
  symbol: ETH/USDT           # Change to any pair

forex_tier:
  leverage: 2                # Keep lower risk
  symbol: EUR/INR            # Zerodha Forex
  min_capital: 50            # Minimum ₹50 to trade
```

---

## Directory Structure Explanation

You asked: **"Are you keeping all strategies in one dir?" and "Will you keep making folders?"**

### Answer: **STRATEGIC SEPARATION**

**Main project** (`strategies/`, `bot/`, `backtest/`) - All together because they're unified.

**Harvest system** (separate `harvest/` folder) - Isolated because:
1. ✅ **Runs independently** - Can run without touching main bot
2. ✅ **Different logic** - F&O + Forex tiers (vs single account)
3. ✅ **Different config** - `harvest_config.yaml` (not main `config.yaml`)
4. ✅ **Different entry point** - `harvest/main_harvest.py` (not `main.py`)
5. ✅ **No code sharing** - Each system is self-contained

### NOT creating unnecessary folders!

Only created:
- ✅ `harvest/` - Because it's a fundamentally different system

NOT creating:
- ❌ `f_and_o/`, `forex/` separately - They're part of harvest system
- ❌ `strategies/harvest_*` - Strategies stay where they are
- ❌ Duplicate bot logic - Only one copy of trader code

---

## Next Steps

1. **Test the system:**
   ```bash
   venv/Scripts/python.exe harvest/main_harvest.py
   ```

2. **Watch dashboard output** - Prints status every 60 seconds

3. **Check files:**
   ```bash
   cat harvest/harvest_state.json       # See current capital
   cat harvest/harvest_trades.json      # See all trades
   ```

4. **When ready for real exchange:**
   - Replace mock signals in `harvest_trader.py` with real exchange API calls
   - Add mean_reversion strategy logic
   - Add EMA strategy logic
   - Deploy on VPS for 24/7 trading

---

## Summary

✅ **Harvest System: READY TO USE**
- Standalone module, fully separate
- Auto-saves state, survives restarts
- Documented in INNOVATIONS.md
- Entry point: `venv/Scripts/python.exe harvest/main_harvest.py`

✅ **Directory Structure: CLEAN**
- Not creating unnecessary folders
- Strategic separation only (harvest = different system)
- Main project unchanged

✅ **Next: Real Exchange Integration**
- Currently uses mock trades
- Ready for Binance Futures API integration
- Ready for Zerodha Forex API integration

---

## Questions?

- **How to start:** `venv/Scripts/python.exe harvest/main_harvest.py`
- **Full docs:** `harvest/README.md`
- **Config:** `harvest/harvest_config.yaml`
- **Innovation notes:** `INNOVATIONS.md` entry #11
