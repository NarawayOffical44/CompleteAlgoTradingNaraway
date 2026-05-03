import os
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ──────────────────────────────────────────────────────────────────
BYBIT_API_KEY      = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET   = os.getenv("BYBIT_API_SECRET", "")
TESTNET            = os.getenv("TESTNET", "false").lower() == "true"
SIM_MODE           = os.getenv("SIM_MODE", "false").lower() == "true"

# ── Capital ───────────────────────────────────────────────────────────────────
CAPITAL_USDT = float(os.getenv("CAPITAL_USDT", "12.0"))   # ~₹1000
INR_PER_USD  = float(os.getenv("INR_PER_USD", "83.5"))

# ── Market ────────────────────────────────────────────────────────────────────
SYMBOL     = "BTC/USDT:USDT"   # perpetual futures (works on both Bybit and Binance)
TIMEFRAME  = "5m"
LEVERAGE   = 10

# ── Signal parameters ─────────────────────────────────────────────────────────
EMA_FAST       = 9
EMA_SLOW       = 21
RSI_PERIOD     = 14
VOL_MA_PERIOD  = 20

RSI_LONG_MIN   = 45   # LONG entry: RSI must be in [45, 62]
RSI_LONG_MAX   = 62
RSI_SHORT_MIN  = 38   # SHORT entry: RSI must be in [38, 55]
RSI_SHORT_MAX  = 55

# ── Trade management ──────────────────────────────────────────────────────────
TAKE_PROFIT_PCT       = 0.006   # +0.6%
STOP_LOSS_PCT         = 0.002   # −0.2%
BREAKEVEN_TRIGGER_PCT = 0.002   # trail SL to entry once +0.2% in profit
MAX_HOLD_HOURS        = 3
FEES_PCT_ROUNDTRIP    = 0.001   # Binance taker 0.05% × 2 sides

# ── Daily limits ──────────────────────────────────────────────────────────────
MAX_TRADES_PER_DAY   = 2
INDIA_TAX_RATE       = 0.30
DAILY_NET_TARGET_INR = 20.0
# Gross target auto-calculated: ₹20 / 0.70 = ₹28.57
DAILY_GROSS_TARGET_INR = DAILY_NET_TARGET_INR / (1 - INDIA_TAX_RATE)
MAX_DAILY_LOSS_INR   = 166.0   # ~16.6% of ₹1000 capital

# ── Scheduler ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS  = 300   # every 5 minutes
TRADING_HOURS_UTC_START = 0     # 24/7 — no window restriction
TRADING_HOURS_UTC_END   = 24
