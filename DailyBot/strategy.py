"""
Signal generation using pure numpy — no pandas dependency.
Indicators: EMA 9/21, RSI 14, VWAP (UTC-day reset), Volume ratio.
"""
import numpy as np
from config import (
    EMA_FAST, EMA_SLOW, RSI_PERIOD, VOL_MA_PERIOD,
    RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX,
    TAKE_PROFIT_PCT, STOP_LOSS_PCT, BREAKEVEN_TRIGGER_PCT,
)


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = _ema(gain, period * 2 - 1)   # Wilder via EMA
    avg_loss = _ema(loss, period * 2 - 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, 100.0, avg_gain / avg_loss)
    return 100.0 - (100.0 / (1.0 + rs))


def _vwap(ts_ms: np.ndarray, high: np.ndarray, low: np.ndarray,
          close: np.ndarray, vol: np.ndarray) -> np.ndarray:
    """UTC-day reset VWAP."""
    days = ts_ms // 86_400_000   # ms → day bucket
    tp = (high + low + close) / 3.0
    vwap = np.empty_like(close)
    cum_tpv = cum_v = 0.0
    cur_day = days[0]
    for i in range(len(close)):
        if days[i] != cur_day:
            cum_tpv = cum_v = 0.0
            cur_day = days[i]
        cum_tpv += tp[i] * vol[i]
        cum_v   += vol[i]
        vwap[i] = cum_tpv / cum_v if cum_v > 0 else tp[i]
    return vwap


def compute_indicators(ohlcv: list) -> dict:
    """Returns a dict of numpy arrays, last index = most recent bar."""
    arr      = np.array(ohlcv, dtype=float)
    ts_ms    = arr[:, 0].astype(np.int64)
    close    = arr[:, 4]
    high     = arr[:, 2]
    low      = arr[:, 3]
    vol      = arr[:, 5]

    ema_fast = _ema(close, EMA_FAST)
    ema_slow = _ema(close, EMA_SLOW)
    rsi      = _rsi(close, RSI_PERIOD)
    vwap     = _vwap(ts_ms, high, low, close, vol)

    vol_ma    = np.convolve(vol, np.ones(VOL_MA_PERIOD) / VOL_MA_PERIOD, mode="full")[:len(vol)]
    vol_ratio = np.where(vol_ma > 0, vol / vol_ma, 1.0)

    return {
        "close":     close,
        "ema_fast":  ema_fast,
        "ema_slow":  ema_slow,
        "rsi":       rsi,
        "vwap":      vwap,
        "vol_ratio": vol_ratio,
    }


def get_signal(ind: dict) -> str | None:
    """Returns 'long', 'short', or None. Requires a fresh EMA crossover."""
    min_bars = max(EMA_SLOW, RSI_PERIOD, VOL_MA_PERIOD) + 5
    if len(ind["close"]) < min_bars:
        return None

    ef, es = ind["ema_fast"], ind["ema_slow"]
    rsi     = ind["rsi"]
    vwap    = ind["vwap"]
    close   = ind["close"]
    vr      = ind["vol_ratio"]

    # Fresh crossover on last bar only
    crossed_up   = (ef[-1] > es[-1]) and (ef[-2] <= es[-2])
    crossed_down = (ef[-1] < es[-1]) and (ef[-2] >= es[-2])

    vol_ok = vr[-1] >= 1.0

    if crossed_up and RSI_LONG_MIN <= rsi[-1] <= RSI_LONG_MAX and close[-1] > vwap[-1] and vol_ok:
        return "long"
    if crossed_down and RSI_SHORT_MIN <= rsi[-1] <= RSI_SHORT_MAX and close[-1] < vwap[-1] and vol_ok:
        return "short"
    return None


def check_exit(current_price: float, entry_price: float,
               side: str, sl_price: float) -> tuple[bool, float, str]:
    """Returns (should_exit, updated_sl, reason)."""
    if side == "long":
        pnl_pct = (current_price - entry_price) / entry_price
        if pnl_pct >= TAKE_PROFIT_PCT:
            return True, sl_price, "take_profit"
        new_sl = max(sl_price, entry_price) if pnl_pct >= BREAKEVEN_TRIGGER_PCT else sl_price
        if current_price <= new_sl:
            return True, new_sl, "stop_loss"
        return False, new_sl, ""
    else:
        pnl_pct = (entry_price - current_price) / entry_price
        if pnl_pct >= TAKE_PROFIT_PCT:
            return True, sl_price, "take_profit"
        new_sl = min(sl_price, entry_price) if pnl_pct >= BREAKEVEN_TRIGGER_PCT else sl_price
        if current_price >= new_sl:
            return True, new_sl, "stop_loss"
        return False, new_sl, ""
