# Modular Bot Fleet

The system is structured as a trading-company style fleet:

- `BotRunner` runs every bot independently and in parallel.
- `RiskEngine` owns portfolio-level risk and per-bot allocation limits.
- `TradeJournal` owns the audit trail and per-bot PnL history.
- `SharedResourceHub` owns common news and sentiment resources.
- `HeadAI` monitors all bots and can reduce, suspend, resume, or boost allocation.
- `BrokerRouter` routes orders by `exchange` to the correct execution adapter.

## Adding a bot

1. Create an agent in `agents/`.
2. Inherit `BaseAgent` unless the strategy needs a custom multi-leg lifecycle.
3. Set `_exchange` to the execution route, for example `NSE`, `NFO`, `CRYPTO`, `PERP`, `FOREX`, `SOLANA`, or `POLY`.
4. Implement `generate_signals()` and `should_exit()`.
5. Register it in `main.py` with a `market` and the shared `broker`, `risk`, and `journal`.

The bot should not create its own broker, risk engine, journal, LLM, or news client.

## Adding a market

1. Create a `BaseMarket` subclass in `markets/`.
2. Implement market hours, data fetch, regime, allocation, fundamentals, and sentiment.
3. Use env variables for symbols/API choices where possible.
4. Register the market and bot runner in `main.py`.

## Adding live execution

Execution is plugged into `BrokerRouter`.

- Indian exchanges route to `DhanClient`.
- `CRYPTO` and `PERP` can route to `CcxtBroker` when live keys are present.
- Unconfigured routes fall back to `PaperBroker`.

To add a new live venue, implement a broker adapter with `place_order()`, `cancel_order()`,
`get_positions()`, and `get_order_history()`, then call:

```python
router.register("YOUR_EXCHANGE", YourBrokerAdapter(...))
```

Do not add live trading logic inside a strategy bot.

## Telegram

Hosted Telegram reporting sends one portfolio report with capital, drawdown, open risk,
ranking, PnL, and open trades.

Two-way control is optional:

```env
TELEGRAM_CONTROL_ENABLED=true
TELEGRAM_CONTROL_ALLOW_LLM_CHAT=true
TELEGRAM_CONTROL_ALLOW_ACTIONS=false
TELEGRAM_CONTROL_ALLOW_RUNBOOK=false
```

Readonly commands work with control enabled. Normal chat is also supported when
`TELEGRAM_CONTROL_ALLOW_LLM_CHAT=true`, so you can ask questions such as:

- `why are we not making money?`
- `which bot is working best?`
- `what should I check before going live?`
- `what is the current risk?`

The chat layer answers from live telemetry. It does not trade or change bot
state from free-form text. Actions such as `/pause`, `/resume`, `/reduce`, and
`/boost` require `TELEGRAM_CONTROL_ALLOW_ACTIONS=true`.

Owner runbook commands such as `/run test`, `/run compile`, `/run git_status`, and
`/run last_log` require `TELEGRAM_CONTROL_ALLOW_RUNBOOK=true`. These are fixed
diagnostics, not an arbitrary remote shell.
