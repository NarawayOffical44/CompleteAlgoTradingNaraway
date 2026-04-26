# Reference Library

Study materials for this algo trading project.

---

## 1. Algorithmic Trading — Winning Strategies and Their Rationale (2013)
**Author:** Ernest P. Chan
**File:** `Algorithmic Trading - Winning Strategies and Their Rationale 2013.pdf`

### Key Strategies Covered
| Strategy | Applicable to Us | Priority |
|---|---|---|
| Mean Reversion (Bollinger Bands) | Yes — already built | Done |
| ADF Test (verify mean reversion statistically) | Yes — add to backtest | Next |
| Kalman Filter for dynamic hedging | Yes — future upgrade | Later |
| Pairs Trading / Stat Arb | Yes — next strategy in lab/ | Soon |
| Kelly Criterion position sizing | Yes — already implemented | Done |
| Momentum / Time Series | Yes — EMA crossover variant | Done |

### Most Important Lessons for Solo Trader
1. **Always run ADF test** before trading a mean-reversion strategy — verify it's statistically mean-reverting
2. **Sharpe Ratio > 1.0** annualized is the minimum bar (we're at 2.68 — good)
3. **Profit Factor > 1.5** is the real target for robust strategies
4. **Beware overfitting** — test on out-of-sample data, not just the period you tuned on
5. **Transaction costs kill** small-account strategies — always include fees in backtest

### Directly Applicable Formulas
```
# Half-life of mean reversion (how fast price returns to mean)
# Shorter half-life = faster reversion = better for short timeframes
half_life = -log(2) / log(1 + beta)  # beta from OLS regression

# Z-score for entry/exit signals (more precise than raw Bollinger Bands)
z_score = (price - rolling_mean) / rolling_std
# Enter when z > 2.0, exit when z < 0.5
```

---

## 2. Market Microstructure
**File:** `Microstructure.pdf`

### Key Concepts Applicable to Us
| Concept | Why It Matters | How We Use It |
|---|---|---|
| Bid-Ask Spread | Hidden cost on every trade | Use limit orders to avoid paying spread |
| Market Impact | Large orders move price against you | On ₹1000, our orders are too small to matter |
| Order Book Depth | Liquidity available at each price | Stick to BTC/USDT — deepest crypto market |
| Slippage | Difference between expected and actual fill | Built into backtest as commission buffer |
| Price Discovery | How information gets into prices | 1h candles avoid noise from microstructure |

### Solo Trader Takeaway
> "Retail traders cannot compete on microstructure. Trade on timeframes (1h+) where microstructure noise is irrelevant."

---

## Reading Priority

1. **Read first:** Chan Ch.1-3 (mean reversion fundamentals)
2. **Read second:** Chan Ch.7 (risk management)
3. **Read third:** Microstructure Ch.1-2 (understand what you're trading against)
4. **Reference as needed:** Everything else

---

## Key Concepts to Implement Next (from these books)

- [ ] ADF stationarity test before deploying mean reversion
- [ ] Z-score based entry/exit (more precise than raw BB touch)
- [ ] Half-life calculation to optimize BB period
- [ ] Pairs trading on BTC/ETH spread (from Chan Ch.5)
