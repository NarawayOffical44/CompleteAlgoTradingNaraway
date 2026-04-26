# Harvest Trading System

**SEPARATE SYSTEM** from main AlgoTrading bot. Runs independently.

## Architecture

### Two-Tier Capital Model

```
START: ₹1000
   │
   ├─→ F&O Tier (₹1000, FIXED)
   │   ├─ Leverage: 5x
   │   ├─ Strategy: Mean Reversion (1h)
   │   ├─ Capital: Never changes (always ₹1000)
   │   ├─ Trades: Continuously
   │   └─ Profit: HARVESTED to Forex ✂️
   │
   └─→ Forex Tier (Grows from harvests)
       ├─ Leverage: 2x
       ├─ Strategy: EMA Crossover (4h)
       ├─ Capital: Starts ₹0, grows from F&O profits
       ├─ Source: F&O profits ONLY
       ├─ Protection: F&O losses don't affect it
       └─ Reinvestment: All gains compound
```

## Key Features

✅ **F&O Locked at ₹1000**
- Never reinvests profits
- Always trading with same capital
- Enables consistent leverage testing

✅ **Profit Harvesting**
- Every F&O profit → Exits to Forex tier
- Automatic transfer, no manual action
- Forex grows safely from harvests

✅ **Risk Segregation**
- F&O loss doesn't reduce Forex capital
- Forex loss doesn't stop F&O trading
- Both tiers run simultaneously

✅ **Stateful Persistence**
- Saves state every trade close
- Resumes from previous state on restart
- Track all trades in JSON

## Running

### Start Harvest System (Separate)

```bash
venv/Scripts/python.exe harvest/main_harvest.py
```

### Monitor Status

Dashboard prints every 60 seconds:
```
============================================================
🤖 HARVEST SYSTEM STATUS
============================================================
F&O Capital:        ₹1,000 (FIXED)
F&O Cumulative P&L: ₹450

Forex Capital:      ₹450 (HARVEST)
Forex Cumulative P&L: ₹20

TOTAL CAPITAL:      ₹1,470
TOTAL P&L:          ₹470

F&O Trades:         24
Forex Trades:       8
Total Harvested:    ₹450

F&O Status:         🟢 RUNNING
Harvest Growth:     45.0% of F&O capital
============================================================
```

## Configuration

Edit `harvest/harvest_config.yaml` to customize:

```yaml
f_and_o_tier:
  capital: 1000              # LOCKED
  leverage: 5
  strategy: mean_reversion_1h
  symbol: ETH/USDT

forex_tier:
  leverage: 2
  strategy: ema_crossover_4h
  symbol: EUR/INR
  min_capital: 50            # Min to start trading
```

## Data Files

- **harvest_state.json** - Current capital & P&L (updated every trade)
- **harvest_trades.json** - All trade records
- **harvest_config.yaml** - System configuration
- **harvest_history.json** - Harvest event log

## Example Growth Timeline

```
DAY 1:
  F&O: ₹1000 → +₹145 profit
  Forex: ₹0 → ₹145 (harvested)
  Total: ₹1145

DAY 3:
  F&O: ₹1000 → +₹280 cumulative
  Forex: ₹145 → ₹280 (harvested) + ₹8 (internal gain) = ₹288
  Total: ₹1288

DAY 7:
  F&O: ₹1000 → +₹450 cumulative
  Forex: ₹288 → ₹450 (harvested) + ₹35 (internal gain) = ₹485
  Total: ₹1485

DAY 30:
  F&O: ₹1000 → +₹2200 cumulative
  Forex: ₹485 → ₹2200 (harvested) + ₹180 (internal gain) = ₹2380
  Total: ₹3380 (338% growth)

KEY:
  - F&O always remains ₹1000 (unchanged)
  - Forex is protected from F&O losses
  - Both growing simultaneously
  - Total grows from both sources
```

## Circuit Breakers

### F&O Pause
- Pauses F&O if daily loss > -₹200
- Resumes after 1 hour cooldown
- Forex continues trading (protected)

### Forex Protection
- No minimum capital requirement
- Can trade even if capital low
- Never stops trading Forex

## Integration with Main System

This system runs **completely separately** from `main.py`:

```bash
# Terminal 1: Main trading bot
venv/Scripts/python.exe main.py backtest

# Terminal 2: Harvest system (separate)
venv/Scripts/python.exe harvest/main_harvest.py
```

They don't share state, capital, or configuration.

## Comparison: Main vs Harvest

| Feature | Main System | Harvest System |
|---------|----------|----------|
| Entry | `main.py` | `harvest/main_harvest.py` |
| Config | `config/config.yaml` | `harvest/harvest_config.yaml` |
| State | Multiple accounts | Separate capitals |
| Purpose | General trading | F&O profit harvesting |
| F&O Reinvestment | Yes (pyramid) | No (fixed) |
| Forex Source | Profits + manual | F&O harvests only |

## Next Steps

1. ✅ Configure symbols and leverage in `harvest_config.yaml`
2. ⏭️ Integrate real exchange APIs (currently mock)
3. ⏭️ Add mean_reversion and EMA strategies
4. ⏭️ Deploy on VPS for 24/7 trading
5. ⏭️ Monitor first week, adjust parameters

## Troubleshooting

### State not loading
```bash
# Check if state file exists
ls harvest/harvest_state.json

# If corrupted, delete and restart (fresh)
rm harvest/harvest_state.json
venv/Scripts/python.exe harvest/main_harvest.py
```

### F&O keeps pausing
- Check loss limit: `-₹200` in config
- Adjust `daily_loss_limit` if needed
- Or reduce leverage if losses continue

### Forex not trading
- Check minimum capital: `min_capital: 50` in config
- F&O needs to make at least ₹50 profit first
- Monitor `Total Harvested` to see progress

## Support

Questions? Check:
- `INNOVATIONS.md` for implementation notes
- Main `README.md` for project overview
- Logs at `logs/harvest_*.log`
