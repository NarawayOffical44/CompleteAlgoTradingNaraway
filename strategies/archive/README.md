# Archive — Strategies That Didn't Work

Strategies here are kept for **learning purposes only**.
Never run these in live trading.

## How to read an archived strategy

Each archived strategy has a header comment explaining:
- What it was trying to do
- Why it failed (bad win rate, excessive drawdown, overfit, etc.)
- What we learned from it

## Promotion / Demotion Rules

| From | To | Condition |
|---|---|---|
| `lab/` | `live/` | Backtest profit factor > 1.3, win rate > 45%, max DD < 15% |
| `lab/` | `archive/` | Failed above criteria after fair testing |
| `live/` | `archive/` | Stopped working in live conditions (forward test failure) |

## Archive Log

| Date | Strategy | Why Archived |
|---|---|---|
| (none yet) | | |
