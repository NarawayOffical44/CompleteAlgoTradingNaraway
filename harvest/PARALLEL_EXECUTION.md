# Harvest System - Parallel Execution

## Architecture: Both Tiers Run Simultaneously

```
asyncio.gather(*tasks)
    │
    ├─→ Task 1: F&O Trading Loop (every 5 sec)
    │   ├─ Check signal
    │   ├─ Enter trade if signal
    │   ├─ Exit trade
    │   ├─ Harvest to Forex
    │   └─ Save state
    │
    └─→ Task 2: Forex Trading Loop (every 10 sec)
        ├─ Check signal
        ├─ Enter trade if signal
        ├─ Exit trade
        ├─ Reinvest profits
        └─ Save state

RESULT: Both trading at the same time (concurrent)
```

## Timeline Example

```
Time:    Action:
0:00     [F&O] Loop started
0:00     [Forex] Loop started

0:05     [F&O] Check signal → BUY signal
0:05     [F&O] Enter trade @ 50500

0:07     [Forex] Check signal → No signal (waiting)

0:10     [F&O] Check signal → No signal (waiting)
0:10     [Forex] Check signal → BUY signal
0:10     [Forex] Enter trade @ 90.50

0:10     [F&O] Exit previous trade → +80 profit
0:10     [F&O] Harvest +80 → Forex

0:15     [F&O] Check signal → BUY signal (new)
0:15     [F&O] Enter trade @ 50600

0:20     [Forex] Exit previous trade → +12 profit
0:20     [Forex] Forex capital now 512
```

## Why Parallel?

### Benefits
✅ **Independence:** F&O doesn't block Forex
✅ **Efficiency:** Both trading simultaneously
✅ **Realistic:** Real-world concurrent trading
✅ **Non-blocking:** If one loop is slow, other continues

### Example: Without Parallel
```
F&O trade 1 (starts 0:00, ends 0:05)
  → blocks Forex
Forex trade 1 (starts 0:05, ends 0:20)
  → wastes 5 minutes waiting

Total time: 25 minutes for 2 trades
```

### Example: WITH Parallel (Current)
```
F&O trade 1: 0:00-0:05 ┐
Forex trade 1: 0:05-0:20 ├─ BOTH RUNNING AT SAME TIME
F&O trade 2: 0:05-0:10 ┘

Total time: 20 minutes for 3 trades (faster!)
```

## Current Output

When you run:
```bash
venv/Scripts/python.exe harvest/main_harvest.py
```

You'll see:
```
[F&O] Loop started (checking every 5 sec)
[Forex] Loop started (checking every 10 sec)    ← BOTH START IMMEDIATELY

F&O: Entry @ 50626.54
Forex: Entry @ 90.90                            ← Can happen at same time

F&O +100 -> Forex                               ← F&O profiting
Forex +15 (500 total)                           ← Forex also profiting
```

**Both tiers are ALWAYS trading in parallel.**

## Verification

Check `harvest_state.json`:
```json
{
  "f_and_o_pnl": 450,      ← F&O trades accumulating
  "forex_capital": 520,    ← Forex capital growing
  "forex_pnl": 20
}
```

Both numbers growing = Both tiers trading ✅
