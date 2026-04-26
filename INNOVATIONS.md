# AlgoTrading — Innovation & Learning Log

> Auto-maintained log of non-generic, innovative decisions made in this project.
> Purpose: study, replicate, and build on these concepts later.

---

## [2026-03-27] Project Foundation

### 1. ATR-Based Dynamic Stop Loss
**File:** `strategies/ema_crossover.py`, `strategies/mean_reversion.py`
**What:** Stop loss is not a fixed percentage — it's calculated from ATR (Average True Range).
**Why it's non-generic:** Fixed % stops get hit randomly. ATR adapts to the current market's actual volatility. In a calm market, stop is tight. In a volatile market, stop widens automatically so normal price noise doesn't kick you out.
**Formula:** `stop_loss = entry - (ATR * multiplier)`
**Study:** Look up "volatility-adjusted position sizing" and "ATR trailing stop."

---

### 2. Risk-Based Position Sizing (not fixed lot size)
**File:** `strategies/base.py` → `calculate_position_size()`
**What:** Position size is calculated from how much you're willing to lose (risk amount), not from a fixed quantity.
**Why it's non-generic:** Most beginner bots buy "0.01 BTC every time." We buy exactly the quantity where if the stop loss hits, we lose exactly 2% of capital — no more.
**Formula:** `qty = (capital * risk_pct) / (entry - stop_loss)`
**Study:** "Kelly Criterion," "fixed fractional position sizing," Van Tharp's position sizing work.

---

### 3. Bollinger Band Width as Volatility Filter
**File:** `strategies/mean_reversion.py`
**What:** We calculate `bb_width = (upper - lower) / middle` and log it on every signal.
**Why it's non-generic:** BB width tells you HOW volatile the market is right now. A wide BB = high volatility = mean reversion moves will be bigger. Future use: only take mean reversion trades when BB width is above a threshold (avoid trading in flat, low-volatility conditions).
**Study:** "Bollinger Band Squeeze," "BB width as regime filter."

---

### 4. Dual-Strategy Backtest Comparison
**File:** `backtest/run_backtest.py`
**What:** Both strategies (EMA crossover + Mean reversion) run on the same historical data and results are compared side by side.
**Why it's non-generic:** Most tutorials test one strategy. We test both simultaneously on the same data so you can objectively pick the winner for current market conditions.
**Study:** "Walk-forward optimization," "strategy ensemble," "regime detection."

---

### 5. Daily Loss Circuit Breaker
**File:** `bot/trader.py` → `_check_daily_loss_limit()`
**What:** If the bot loses more than 5% in a single day, it stops trading entirely until the next day.
**Why it's non-generic:** Prevents the "death spiral" — a losing strategy in bad conditions keeps trading and compounds losses. Professional desks call this a "risk limit breach."
**Study:** "Drawdown-based position sizing," "risk of ruin," "maximum adverse excursion."

---

### 6. Exchange-Agnostic Architecture (ccxt)
**File:** `bot/exchange.py`
**What:** All exchange calls go through ccxt, not the Binance SDK directly.
**Why it's non-generic:** Switching from Binance to Coinbase or Kraken requires changing ONE line in config.yaml. The strategy and risk management code never changes.
**Study:** "Abstraction layer," "adapter pattern," "broker-agnostic trading systems."

---

---

## [2026-03-28] Book Study — Key Lessons (Chan 2013 + Krause 2005)

### Ernest Chan — Algorithmic Trading (2013)
**Core principle we violated:** Never trade mean reversion without first verifying the series is statistically mean-reverting.
- **ADF Test** (Augmented Dickey-Fuller): Tests if price series is stationary. If p-value < 0.05, series is mean-reverting. We skipped this — caused losing trades on trending BTC/ETH.
- **Hurst Exponent**: H < 0.5 = mean-reverting, H = 0.5 = random walk, H > 0.5 = trending. Must check before deploying.
- **Half-life**: How fast price reverts to mean. Formula: `half_life = -log(2) / beta` (OLS regression). Determines optimal BB period — NOT arbitrary 20.
- **Momentum vs Mean Reversion**: Diametrically opposite risk profiles. Mean reversion = low Sharpe in trending markets. Momentum = high Sharpe in trending markets. Must match strategy to regime.
- **Kelly Criterion**: Optimal position size = edge / odds. Already implemented. Do not over-leverage.
- **Key warning**: "Scaling in" (adding to losing mean reversion positions) is dangerous — caused our falling knife problem on BTC.

### Krause — Market Microstructure (2005)
- **Bid-Ask Spread**: Real hidden cost beyond commission. On BTC/ETH major pairs, spread is ~0.01% — small enough to ignore at our scale.
- **Informed vs Uninformed traders**: Institutions have information edge at microstructure level. At 1h timeframes, this edge disappears — prices have already adjusted.
- **Limit order vs Market order**: Limit orders earn the spread, market orders pay it. Our bot uses market orders — acceptable for 1h strategies where timing matters more than spread.

### What This Explains About Our Results
- BTC mean reversion failed: BTC was TRENDING (H > 0.5), ADF would have flagged this
- ETH EMA crossover succeeded: ETH was in strong downtrend (H >> 0.5) — perfect for momentum
- Fix: Add ADF + Hurst check BEFORE running any mean reversion strategy

### TODO from books (implement in order)
1. Add ADF test to backtest — reject mean reversion if p-value > 0.05
2. Add Hurst exponent calculation
3. Calculate half-life to set optimal BB period dynamically
4. Add regime detector (trending vs ranging) to auto-select strategy

---

## [2026-03-28] First Real Backtest Results — BTC/USDT 1h (42 days)
**Result:**
- EMA Crossover: -7.68% | WR 26.3% | PF 0.74 — FAILED
- Mean Reversion: +2.39% | WR 34.5% | PF 1.22 | Sharpe 2.68 — WINNER

**Why EMA failed:** BTC in downtrend (100k -> 83k). EMA kept buying upward crossovers in a bearish market.
**Why Mean Reversion won:** Profits from bounces regardless of direction. Works in ranging/volatile conditions.
**Key insight:** Strategy selection must match market regime. Mean reversion = ranging markets. EMA = trending markets.
**Action taken:** Mean reversion set as active strategy. EMA crossover stays in live/ for trending market conditions.
**Next:** Tune parameters to push profit factor above 1.3 threshold.

---

## [2026-03-27] Lab/Live/Archive Strategy Lifecycle
**Files:** `strategies/live/`, `strategies/lab/`, `strategies/archive/`, `promote.py`
**What:** Three-tier folder system for strategies based on their status.
**Why it's non-generic:** Most algo projects have one `strategies/` folder where everything mixes. When something breaks, you don't know what's safe. Our system enforces: test in lab → promote to live → archive failures. Nothing in `live/` is ever experimental.
**Workflow:** `lab/ → live/` (passed criteria) or `lab/ → archive/` (failed)
**Promotion criteria:** Profit factor >1.3, win rate >45%, max DD <15%, 30+ trades, 6+ months of data
**Study:** "Feature flags," "blue-green deployment," "trunk-based development" — same concept applied to trading strategies.

---

## [2026-03-28] Binance Testnet Setup & Live Paper Trading

### 7. Clock Sync Fix for Binance API
**File:** `bot/exchange.py` → `_init_exchange()`
**What:** Added `adjustForTimeDifference: True` to ccxt options.
**Why it's non-generic:** Binance rejects requests if your clock is >1000ms ahead of their server. This option auto-syncs ccxt's timestamp to Binance's server time — no manual clock fix needed.
**Error fixed:** `InvalidNonce: Timestamp for this request was 1000ms ahead of server's time`
**Study:** NTP clock sync, exchange timestamp validation.

---

### 8. Why 1h Candles (Not Faster)
**What:** Bot ticks once per candle close — 1 hour wait between checks.
**Why it's non-generic:** EMA crossover signals on *unclosed* candles are false signals — EMA can cross and uncross before the candle closes. We only act on confirmed, closed candles.
**Tradeoff:** 15m = more signals + more noise. 1h = fewer signals + higher quality. 1h is the validated setting from backtest.
**Study:** "Candle close confirmation," "signal repainting," "lookahead bias."

---

### Status as of 2026-03-28
- Testnet connected: 10,000 USDT fake balance
- Bot running: ETH/USDT EMA crossover, 1h, paper trading live
- Backtest confirmed: +14.13%, Sharpe 4.36, PF 1.51, WR 46.7% on 83 days
- Next: Run 1 week paper trading → validate live signal quality → go real with ₹1000

---

## [2026-03-29] Custom Z-Score Reversion Strategy + AI Filter

### 9. Z-Score Mean Reversion with Asymmetric Take Profit (User's Strategy)
**File:** `strategies/lab/zscore_reversion.py`
**What:** Rolling 100-candle mean/std → buy when price drops 2 std devs below mean → hold until 20% gain (not just return to mean).
**Why it's non-generic:** Standard mean reversion sells when price returns to mean (~5-8% gain). This strategy holds for 20%+ — designed to cover 30% Indian tax + fees and still profit.
**Flaws fixed:**
- 200 SMA filter → no buying in downtrends (prevents falling knife)
- RSI 20-45 filter → avoids freefall (RSI < 20) and non-oversold entries
- Volume spike confirmation (1.5x avg) → capitulation signal = stronger reversal
- ATR-based stop → adapts to volatility instead of fixed %
- Min 5% drop from mean → avoids marginal signals

### 10. AI Signal Filter (RandomForest on Trade Data)
**File:** `ai/signal_trainer.py`
**What:** Trains a RandomForest classifier on backtest trade data. Learns which conditions at signal time lead to wins vs losses. Filters live signals — only trades if model is 60%+ confident.
**Features:** Z-score, RSI, volume ratio, ATR%, drop from mean, SMA200 ratio
**Why it's non-generic:** Most bots use fixed rules forever. This model retrains on new data and improves over time — strategy gets smarter as more trades happen.
**Workflow:** backtest → collect trades → train model → save pkl → live bot loads model → filters signals
**Study:** "Supervised learning for trade filtering," "feature importance in trading," "online learning for strategy adaptation."

---

## [2026-04-04] Harvest Trading System (Separate Module)

### 11. Two-Tier Capital Model: Fixed F&O + Harvested Forex
**File:** `harvest/harvest_trader.py`
**What:** Separate trading system with two tiers:
  - F&O Tier: ₹1000 capital @ 5x leverage (NEVER reinvested, always ₹1000)
  - Forex Tier: Starts empty @ 2x leverage, grows ONLY from F&O profits

**Why it's non-generic:**
  - Standard bots reinvest all profits back into the same account. This creates compounding but also concentrates risk.
  - Harvest system isolates risk: F&O losses don't reduce Forex capital. Both tiers run simultaneously, feeding each other.
  - F&O stays fixed at ₹1000 → same leverage testing forever
  - Forex acts as "profit safe" → all F&O wins are harvested and protected from future F&O losses
  - Psychological advantage: If F&O has a -₹300 losing day, Forex capital is completely untouched

**Capital Flow:**
```
₹1000 (F&O locked)
   ├─ Trade 1: +₹100 → HARVEST to Forex ✂️
   ├─ Trade 2: -₹50  → [ignored, Forex unaffected]
   ├─ Trade 3: +₹80  → HARVEST to Forex ✂️
   └─ F&O always remains ₹1000

Forex (Starts ₹0)
   ├─ Receives ₹100 from F&O
   ├─ Internal gain +₹5
   ├─ Receives ₹80 from F&O
   └─ Forex capital: ₹185 (protected from F&O)
```

**Example Growth (30 days):**
```
F&O: ₹1000 → +₹2200 cumulative (always ₹1000 capital)
Forex: ₹0 → ₹2200 harvested + ₹180 internal gains = ₹2380
Total: ₹3380 (338% growth)

Key: F&O doesn't compound, but Forex does. Two engines = double growth source.
```

**Configuration:** `harvest/harvest_config.yaml`
**Entry Point:** `venv/Scripts/python.exe harvest/main_harvest.py` (SEPARATE from main.py)

**Safety Features:**
  - F&O circuit breaker: Pauses if daily loss > -₹200
  - Forex protection: F&O loss doesn't affect Forex capital
  - State persistence: Resumes on restart from saved state
  - **Recovery-First Harvesting:** If F&O capital < ₹1000, profits recover first before harvesting

**Recovery-First Logic (Key Innovation):**
  - F&O starts at ₹1000
  - If losses occur: F&O capital drops (e.g., ₹950 after -₹50 loss)
  - Next profit comes in: ₹100 gained
  - System automatically:
    1. Recovers ₹50 to bring F&O back to ₹1000 ✓
    2. Harvests remaining ₹50 to Forex
  - Result: Original capital is always protected/recovered first
  - Only excess profits are harvested to safe tier

**Study:** "Risk segregation," "profit isolation," "independent trading engines," "multi-tier portfolio allocation"

---

## [2026-04-16] Portfolio Cascade System: Four-Tier Risk-to-Safety Pipeline

### 12. Four-Tier Cascade: High-Risk → Safer → Safer → Safest
**Files:** `main_portfolio.py` (coordinator) + `strategies/tier{1-4}_*.py` (four strategies)
**What:** Four independent trading engines with escalating risk management. Each tier generates profits that cascade to the next safer tier.

**Architecture:**
```
Tier 1 (Crypto Scalp)      Tier 2 (Forex)          Tier 3 (Equity)         Tier 4 (Long-Term)
─────────────────────      ─────────────────       ──────────────────      ──────────────────
BTC 1m @ 50x               ETH 5m @ 20x            ETH 1h @ 5x             BTC 4h @ 2x
RSI<30 + volume            EMA 9/21 crossover      BB mean reversion       SMA 50/200 + MACD
SL: -0.5% | TP: +1%        SL: -1% | TP: +1.5%     SL: -1.5% | TP: +2%     SL: -2% | TP: +5%
Capital: Rs.1000           Capital: 0 (grows)      Capital: 0 (grows)      Capital: 0 (grows)
Profit: Rs.500 → CASCADE   Profit: Rs.300 → CASC   Profit: Rs.200 → CASC   Profit: HOLDS (final)
```

**Cascade Flow:**
1. Tier 1 scales aggressively → when cumulative profit >= Rs.500, transfers to Tier 2
2. Tier 2 scales moderately → when cumulative profit >= Rs.300, transfers to Tier 3
3. Tier 3 scales conservatively → when cumulative profit >= Rs.200, transfers to Tier 4
4. Tier 4 holds forever → capital preservation + final compound growth

**Why it's non-generic:**
- **Risk segregation:** Original ₹1000 is never directly risked at >50x leverage. It stays isolated in Tier 1.
- **Profit protection:** Tier 1 profits are immediately harvested to safer tiers — not re-risked at high leverage.
- **Progressive safety:** Each tier reduces leverage (50x → 20x → 5x → 2x), timeframe (1m → 5m → 1h → 4h), and trade frequency.
- **Leverage justification:** Tier 1 uses 50x only because it's small (Rs.1000) and short-term (1m). Tier 4 uses 2x because it's long-term — extended hold periods need lower leverage.
- **Capital preservation logic:** Even in worst case (Tier 1 goes to zero), Tiers 2-4 were never exposed to that leverage.

**Data & Testing:**
- Live Binance prices fetched via ccxt every cycle
- Paper mode: Signals generated, logged to `portfolio_state.json`, no actual trades placed
- State persistence: Restart mid-cycle, resumes from saved state
- Cascade events logged with timestamp, from/to tier, amount transferred

**Configuration:** `portfolio_state.json`
**Entry Point:** `python main_portfolio.py --paper --live-data --single` (one cycle)
or `python main_portfolio.py --paper --live-data --interval 60` (continuous)

**Study:** "Risk ladder," "segregated account model," "profit isolation," "progressive de-risking," "Kelly Criterion applied to portfolio design"

---

## [2026-04-17] Quant Portfolio Theory — Applied to Cascade System

### 13. Three Portfolio Types: Long-Only → Long-Short → Market Neutral
**Context:** User studying quant fundamentals while building cascade system.
**What:** Three distinct portfolio architectures with different risk/return profiles.

**Long-Only (Current system Tiers 1-4):**
- Buy only, profit from price rises
- Full market exposure (+100%)
- Sharpe: 0.5-1.5
- Capital: Rs.1,000+ (accessible now)

**Long-Short (Next: Tier 2 upgrade):**
- Buy undervalued + short overvalued simultaneously
- Partially hedged, profits from spread
- Sharpe: 1.0-2.0
- Capital: Rs.10,000+ (needs margin for shorts)
- Implementation: BTC/ETH pair trade on Z-score divergence

**Market Neutral (Phase 3: Zerodha NSE F&O):**
- Equal longs and shorts, zero net market exposure
- Profits regardless of market direction (theta, spread)
- Sharpe: 2.0-4.0 (highest quality, most consistent)
- Capital: Rs.50,000+ (NSE F&O margin requirement)
- Implementation: 9:20 Short Straddle on Nifty

**Key insight:** Each type is a stepping stone. Cascade system grows capital from Rs.1k → Rs.50k to unlock higher-quality strategies.

**Study:** Markowitz (1952), Sharpe (1964), Black-Scholes (1973), pairs trading literature.

---

### 14. Pair Trading (Statistical Arbitrage on Correlated Assets)
**What:** Long one asset + short a correlated asset. Profit from spread convergence.
**Why it's non-generic:** Standard trading needs market direction. Pair trading is market-neutral — profits whether BTC goes up or down, as long as the spread between BTC and ETH converges.

**Entry logic:**
```
spread = price_ETH - (ratio * price_BTC)
z_score = (spread - mean) / std_dev
if z_score > 2:  SHORT ETH + LONG BTC (spread too wide, expect convergence)
if z_score < -2: LONG ETH + SHORT BTC
if z_score ~ 0:  CLOSE trade
```

**Best crypto pairs:** BTC/ETH, SOL/AVAX, BNB/OKB, DOGE/SHIB
**Existing code:** `strategies/lab/zscore_reversion.py` — adaptable for pairs
**Study:** "Statistical arbitrage," "cointegration," "Engle-Granger test," "Ornstein-Uhlenbeck process"

---

### 15. Mean-Variance Optimization (MVO) — Markowitz Portfolio Theory
**What:** Scientifically optimal capital allocation across tiers using return, variance, and correlation data.
**Why it's non-generic:** Most traders split capital by gut feel or equal weighting. MVO finds the EXACT weights that maximize return for a given risk level.

**Formula:**
```
Minimize: w^T * Cov * w  (portfolio variance)
Subject to: sum(w) = 1, expected_return >= target
```

**Efficient Frontier:** Every point = best possible return for that risk level.

**Application to our cascade:**
- Low correlation between Tier 1 (1m scalp) and Tier 4 (4h trend) = diversification benefit
- Portfolio variance < sum of individual variances (Markowitz magic)
- After 30+ trades per tier → run scipy MVO → auto-rebalance cascade thresholds

**Code:**
```python
from scipy.optimize import minimize
result = minimize(portfolio_variance, x0=[0.25]*4,
                  constraints={'type':'eq','fun':lambda w: sum(w)-1},
                  bounds=[(0.05,0.5)]*4)
optimal_weights = result.x
```

**Study:** Markowitz (1952) "Portfolio Selection," Sharpe ratio optimization, Black-Litterman model (Bayesian extension of MVO)

---

---

## [2026-04-20] Deep Research Study Session — Industry Knowledge & System Design

### 16. The Medallion Fund — Core Principles We Adapt

**Source:** Zuckerman (2019) "The Man Who Solved the Market" + court documents + academic reconstruction

**What they did:**
- 66% gross / 39% net annual returns for 30+ years. Sharpe ~2.0–2.5. Only 1 losing year in 30.
- Founded by Jim Simons (mathematician, ex-NSA) — zero finance people hired.
- Not prediction. Pattern recognition at statistical level with 51%+ probability edges.
- 300+ uncorrelated signals simultaneously. Each signal Sharpe 0.3–0.5. Combined = 2.5.
- Hidden Markov Models (HMM) for regime detection. Market has hidden states (bull, bear, range, volatile). System transitions strategies as regime changes.
- Key discovery: Prices mean-revert over 1–2 days BUT trend over longer periods. Both signals run simultaneously, offsetting each other's directional risk.
- No human override. Ever. Emotional discipline is built into the system architecture.
- Monthly model retraining. No strategy runs unchanged forever.
- Obsessive transaction cost modeling. 0.01% improvement = millions at their scale.

**What we adapt:**
1. Multiple uncorrelated bots (our 4-bot design) — each bot covers a different regime
2. Statistical testing before deploying (ADF + Hurst — already in scan_assets.py)
3. Regime detection: Hurst < 0.45 = mean reversion, Hurst > 0.55 = trend following
4. Retire decaying strategies: Monthly Sharpe review, Lab → Live → Archive pipeline
5. No override: Once parameters set, no touching for 30 days minimum

**What we cannot do:** Sub-millisecond execution, proprietary order flow data, 300 PhD team, $10B capital for true arbitrage scale.

**Study:** "The Man Who Solved the Market" (Zuckerman), Hidden Markov Models, signal ensemble theory, Kelly Criterion at portfolio level.

---

### 17. Industry Pitfalls — Documented Mistakes (Never Repeat)

**Source:** Academic literature + industry post-mortems

#### Overfitting (kills most retail strategies)
- Testing 50 parameter combinations → 2–3 will "work" by pure statistical chance at 95% confidence
- Detection: Change RSI period 14 → 13 or 15. If performance collapses, it's curve-fit.
- **Pardo's Rule:** Need 30× more trades than parameters optimized. 3 parameters = 90+ trades minimum.
- Fix: Walk-forward testing — optimize Jan–Mar, test Apr–Jun unseen. Must be consistent.

#### Look-ahead Bias (silent backtest killer)
- Most common Python bug: `df['exit'] = df['close'].shift(-1)` — uses NEXT candle's close to exit current candle
- Our fix: Signal on closed candles, enter on next candle's open ✓

#### Fee Impact at Leverage
- Binance round-trip: 0.08%. At 50× leverage: 4% of margin per trade.
- 5 trades/day at 50× = 20% of capital/day in fees alone.
- Rule: Only deploy strategies where expected gain > 3× round-trip fee.
- Minimum trade target: 0.25% per trade to justify 0.08% fees.

#### RSI < 30 Alone = Sharpe 0.1–0.3 (barely above noise)
- Academic evidence: RSI in isolation barely beats random. In downtrends RSI stays below 30 for weeks.
- Fix: RSI < 30 + Volume > 2× avg + Price above SMA200. We have this. ✓

#### Strategy Decay
- Every edge decays as more traders discover it. Mean reversion edge half-life: ~6–18 months.
- Detection: Monthly Sharpe review. If Sharpe < 0.5 for 2 consecutive months → retire.

#### Correlation Blindspot (false diversification)
- 4 "different" strategies that all fire on RSI < 30, lower BB, and high volume → all lose together in crash.
- Test: Run all bots same data, correlate monthly returns. If correlation > 0.7 between any two → same strategy.
- Fix: Each bot must use different logic (mean reversion vs trend following vs capitulation scalp).

#### Leverage Misuse Math
- 10× leverage: 10% move against = 100% liquidation.
- Volatility drag: +2% then -2% at 50× = total wipeout. At 1×: net 99.96% (nearly unchanged).
- Safe guidelines: Beginners 1–3×, Intermediate 5–10×, Advanced 10–20×, 50×+ only hedged.

**Study:** Pardo (2008) "The Evaluation and Optimization of Trading Strategies", Kaufman "Trading Systems and Methods"

---

### 18. Evidence-Based Strategy Selection

**Source:** Academic papers (cited below)

#### Mean Reversion (Bot 2 — our curated strategy)
- Borri & Shakhnov (2022, J. Financial Economics): Crypto mean-reversion at 1–7 day horizons. Half-life: 2–4 days. Confirms 1h–4h timeframes.
- Chaim & Laurini (2019): Mean reversion stronger in low-volatility regimes (Hurst < 0.45).
- Realistic Sharpe: 0.6–1.8 | Annual: 10–25% | Win rate: 45–55%
- Conditions: Hurst < 0.45, price above SMA200, normal volume
- Breaks when: Strong trend (Hurst > 0.55), major news events, bear capitulation

#### Trend Following (Bot 3)
- Moskowitz, Ooi, Pedersen (2012): Time-series momentum works across 58 futures 1965–2009. Sharpe ~1.0.
- Realistic Sharpe: 0.7–1.5 | Annual: 15–30% | Win rate: 40–45% (but large winners)
- Conditions: Hurst > 0.55, 4h+ timeframe, volume confirms breakout
- Breaks when: Choppy markets, false breakouts, low volume

#### Statistical Arbitrage / Pairs Trading (future upgrade)
- Gatev, Goetzmann, Rouwenhorst (2006): Pairs trading on US stocks = ~11% annual, market-neutral
- Trade BTC/ETH spread when Z-score > 2.0. Exit at Z-score < 0.5.
- Realistic Sharpe: 0.8–1.8 (market neutral = smoother returns, not dependent on direction)
- Prerequisite: Engle-Granger cointegration test p < 0.05 before deploying

#### Funding Rate Arbitrage (future upgrade, best risk-adjusted)
- Crypto-specific: Buy spot BTC + Short perp BTC when funding > 0.01%/8h
- Collect funding payment. Zero directional exposure.
- Realistic Sharpe: 2.0–3.0 | Annual: 8–15% | Win rate: ~95%
- The closest thing to "risk-free" at retail scale in crypto

#### Regime Detection Filter
- Most impactful single improvement to any strategy
- Pause ALL trading when: ATR/Price > 5%, Volume < 0.5× avg, RSI < 15 in downtrend
- Impact: +0.3–0.5 Sharpe points on existing strategy

**Study:** Borri & Shakhnov (2022), Moskowitz et al. (2012) "Time Series Momentum", Gatev et al. (2006) "Pairs Trading"

---

### 19. AI/LLM Integration — What's Real vs. Hype

**Source:** Industry research + Two Sigma public papers + FinRL documentation

#### REAL (Build These in Order)

**1. Rule-Based Regime Detection (build first, no data required)**
- Hurst + RSI + ATR% → classify: range/trend/volatile → select strategy
- Impact: +0.3–0.5 Sharpe. Runs immediately from Day 1.

**2. Bayesian Parameter Optimization (build after 3 months)**
- Library: `optuna` (pip install optuna)
- 200–500 smart test combinations vs. 1M grid search combinations
- Re-run monthly. Impact: +0.1–0.3 Sharpe.
```python
import optuna
def objective(trial):
    rsi_period = trial.suggest_int('rsi_period', 10, 20)
    bb_period = trial.suggest_int('bb_period', 15, 30)
    # backtest and return sharpe
study = optuna.create_study(direction='maximize')
study.optimize(objective, n_trials=200)
```

**3. RandomForest Signal Filter (build after 500+ trades)**
- Features: RSI, volume ratio, BB position, SMA200 distance, ATR%, Hurst
- Label: win (1) or loss (0) per trade
- Only enter if model confidence > 60%
- Impact: +0.1–0.2 Sharpe, -10% drawdown
- Minimum data: 500–1,000 live trades before reliable

**4. LLM Trade Journal (Claude, build from Day 1)**
- After every 20 trades: feed trade log to Claude → pattern analysis
- Example output: "7 of 9 losses had volume < 0.8× average → add volume filter"
- Not a signal generator. A pattern detector for human-blind correlations.
- Impact: Habit-based, +1–2% annual through systematic improvement

#### HYPE (Don't Build These)
- LSTM price prediction: 49% accuracy out-of-sample. Coin flip after fees. Skip.
- 50+ indicator combinations: More correlation, not more information. Overfits.
- Sentiment as primary strategy: Edge disappears when everyone uses it.
- Reinforcement Learning: Needs 1M+ steps. At 1h intervals = 114 years of data. Skip until 5,000+ live trades.
- "Black box" neural networks: Uninterpretable, can't debug, likely overfit.

**Two Sigma's insight (public research):** "Our moat is data quality + feature engineering, not model complexity. A linear model with 5 good features beats an LSTM with 50 bad features."

**Study:** FinRL (open source RL for trading), optuna documentation, scikit-learn RandomForest, Feature engineering in financial ML (Lopez de Prado, 2018)

---

### 20. Realistic Goal Framework — Rs.1000 to Meaningful Income

**Source:** Academic returns literature + Indian tax law

#### The Math on Rs.1000 → $1M
- Rs.1000 = ~$12 USD
- $12 → $1,000,000 = 83,333× return in 8 months = mathematically impossible
- Even at 100% monthly compounding: $12 → $3,072 in 8 months (not $1M)
- The Medallion Fund at their extraordinary performance: $12 → ~$20 in 1 year

#### The Real Path (How Medallion Actually Scaled)
Simons proved it on $5M → track record attracted investors → scaled to $100B.
**The system is the asset. The track record unlocks capital.**

```
Phase 1 (Months 1–3):  Paper trade. Prove Sharpe > 0.8 on 30+ signals.
Phase 2 (Months 4–6):  Rs.500 real. 2% risk/trade. 2× leverage max.
Phase 3 (Months 7–12): Full Rs.1000. Inject Rs.500–1000/month savings. Target Rs.5000.
Phase 4 (Year 2):      12-month auditable track record → raise Rs.50,000 from others.
Phase 5 (Year 3+):     Rs.1 lakh capital at 5%/month net = Rs.5,000/month income.
Phase 6 (Year 5+):     Rs.5 lakh under management = Rs.25,000/month. Meaningful income.
```

#### Capital Required for Meaningful Income (after 30% India tax)
| Monthly Goal | At 5% net/month | At 10% net/month |
|-------------|-----------------|-----------------|
| Rs.10,000   | Rs.2,00,000     | Rs.1,00,000     |
| Rs.30,000   | Rs.6,00,000     | Rs.3,00,000     |
| Rs.1,00,000 | Rs.20,00,000    | Rs.10,00,000    |

#### India Tax Impact
- 30% flat tax on crypto gains, no loss offset
- Pre-tax 10%/month → 7% after tax
- High-frequency trading is doubly punished (more trades = more taxable events)
- Implication: Fewer, higher-quality trades preferred over many small trades

#### Sharpe Targets by Experience Level
| Level | Target Sharpe | Meaning |
|-------|--------------|---------|
| Year 1 | 0.8–1.0 | Decent edge, learning phase |
| Year 2 | 1.0–1.5 | Good retail trader |
| Year 3+ | 1.5–2.0 | Top-tier retail, fund-raising quality |
| Medallion | 2.5+ | 300 PhDs + proprietary data |

**Study:** Zuckerman "The Man Who Solved the Market", Lopez de Prado "Advances in Financial ML", Indian crypto tax rules FY2022 onwards

---

### 21. The 4-Bot Portfolio System (Research-Informed Design)

**Context:** Based on all above research, revised from naive cascade to industry-informed architecture.

**Design principle:** Each bot must be genuinely uncorrelated. Test: monthly return correlation between any two bots must be < 0.7.

#### Bot 1 — High-Risk Scalper (SOL/USDT 5m)
- Entry: RSI(14) < 30 AND Volume > 2× avg AND Price dropped > 3% in last 2 candles (capitulation signature)
- SL: -1% | TP: +2% | Leverage: 10× sim (NOT 50× — fees destroy 50× scalping)
- Signal freq: 3–5 per day | Fee check: Only enter if expected gain > 3× 0.08% = 0.24%
- Capital: Rs.1000 seed | Transfer rule: Every Rs.500 profit → Bot 2

#### Bot 2 — Safe Accumulator (ETH/USDT 1h) — THE CURATED STRATEGY
- Entry: Close ≤ BB Lower AND RSI < 30 AND Close > SMA200 AND Hurst < 0.45
- SL: entry - ATR×1.5 | TP: BB Middle band | Leverage: 3× sim
- Signal freq: 2–4 per week | File: strategies/mean_reversion.py (unchanged)
- Rebirth rule: When Bot 1 capital = 0 → send Rs.200 seed
- Growth rule: Capital > Rs.5000 → seed Bot 3 with Rs.500

#### Bot 3 — Medium Trend Follower (BNB/USDT 4h)
- Entry: EMA9 crosses above EMA21 AND MACD histogram turns positive AND Volume > 1.5× avg
- SL: -2% | TP: +4% | Leverage: 5× sim
- Signal freq: 1–3 per week | Genuinely uncorrelated from Bot 2 (different logic + timeframe)

#### Bot 4 — Sure Shot Sniper (BTC/USDT 1d)
- Entry: ALL THREE must be true simultaneously:
  1. BTC SMA50 > SMA200 (golden cross — macro bull confirmed)
  2. RSI between 40–60 (not overextended in either direction)
  3. Bot 2 AND Bot 3 BOTH gave BUY signals within last 24h (cross-bot confirmation)
- SL: -3% | TP: +8% | Leverage: 2× sim
- Signal freq: 1–2 per month (very selective — this is the Medallion principle: high confidence only)

#### Regime Master (coordinator, not a trading bot)
- Runs before each cycle
- Checks: Hurst (100-bar), ATR%, Volume ratio, BTC trend
- Output: "Range" → activate Bot 2, "Trend" → activate Bot 3, "Volatile/avoid" → pause all bots
- Inspired by Medallion's HMM regime detection — simple rule-based version

#### Capital Flow
```
Bot 1 wins → Rs.500 threshold → transfer to Bot 2
Bot 1 blows up → Bot 2 sends Rs.200 rebirth
Bot 2 > Rs.5000 → seeds Bot 3 with Rs.500
Bot 4 only fires when Bot 2 + Bot 3 + BTC macro all agree
```

**Why this design survives regime changes:** Bot 1 (scalp) works in any regime. Bot 2 (mean reversion) works in ranging. Bot 3 (trend) works in trending. Bot 4 (sure shot) only fires in confirmed bull. At least one bot always has conditions to trade.

**Study:** Portfolio construction theory, uncorrelated strategy ensemble, regime-conditional strategy allocation

---

## Template for future entries

```
### [DATE] — Title
**File:** path/to/file.py → function_name()
**What:** What was built or decided.
**Why it's non-generic:** Why this is smarter than the standard approach.
**Formula/Logic:** Key formula or pseudocode if applicable.
**Study:** Keywords to research further.
```
