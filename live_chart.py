"""
LIVE CHART — ETH/USDT 1h | BB Mean Reversion Strategy
======================================================
Fetches live data, shows candles + BB bands + RSI + signal markers.
Saves chart to charts/eth_1h_signal.png and opens it automatically.

Run: venv/Scripts/python.exe live_chart.py
"""
import os
import sys
import ccxt
import pandas as pd
import pandas_ta as ta
import mplfinance as mpf
from datetime import datetime

# --- Config ---
SYMBOL     = "ETH/USDT"
TIMEFRAME  = "1h"
CANDLES    = 250    # fetch 250 candles (need 200 for SMA200)
DISPLAY    = 80     # show last 80 candles in chart
BB_PERIOD  = 20
BB_STD     = 2.0
RSI_PERIOD = 14
RSI_OB     = 70     # overbought
RSI_OS     = 30     # oversold
ATR_PERIOD = 14
ATR_MULT   = 1.5
CAPITAL    = 1000   # paper capital (Rs)
RISK_PCT   = 0.02   # 2% risk per trade


def fetch_data() -> pd.DataFrame:
    print(f"Fetching {CANDLES} candles of {SYMBOL} {TIMEFRAME} from Binance...")
    exchange = ccxt.binance({"enableRateLimit": True})
    candles = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=CANDLES)
    df = pd.DataFrame(candles, columns=["Open time", "Open", "High", "Low", "Close", "Volume"])
    df["Open time"] = pd.to_datetime(df["Open time"], unit="ms")
    df = df.set_index("Open time").astype(float)
    print(f"  Got {len(df)} candles | Latest: {df.index[-1].strftime('%Y-%m-%d %H:%M')} | Price: {df['Close'].iloc[-1]:.2f}")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    bb = ta.bbands(df["Close"], length=BB_PERIOD, std=BB_STD)
    df["BBU"] = bb[[c for c in bb.columns if c.startswith("BBU")][0]]
    df["BBM"] = bb[[c for c in bb.columns if c.startswith("BBM")][0]]
    df["BBL"] = bb[[c for c in bb.columns if c.startswith("BBL")][0]]
    df["RSI"]    = ta.rsi(df["Close"], length=RSI_PERIOD)
    df["ATR"]    = ta.atr(df["High"], df["Low"], df["Close"], length=ATR_PERIOD)
    df["SMA200"] = ta.sma(df["Close"], length=200)
    return df


def detect_signals(df: pd.DataFrame) -> tuple[list, list, dict]:
    """Returns (buy_signal_indices, sell_signal_indices, current_signal_info)."""
    buys, sells = [], []

    for i in range(len(df)):
        row = df.iloc[i]
        if pd.isna(row["BBL"]) or pd.isna(row["RSI"]) or pd.isna(row["SMA200"]):
            continue
        in_uptrend   = row["Close"] > row["SMA200"]
        in_downtrend = row["Close"] < row["SMA200"]

        if row["Close"] <= row["BBL"] and row["RSI"] < RSI_OS and in_uptrend:
            buys.append(i)
        elif row["Close"] >= row["BBU"] and row["RSI"] > RSI_OB and in_downtrend:
            sells.append(i)

    # Current signal (latest candle)
    last = df.iloc[-1]
    if pd.isna(last["BBL"]) or pd.isna(last["RSI"]) or pd.isna(last["SMA200"]):
        current = {"signal": "HOLD", "reason": "Not enough data", "color": "yellow"}
    elif last["Close"] <= last["BBL"] and last["RSI"] < RSI_OS and last["Close"] > last["SMA200"]:
        sl  = last["Close"] - (last["ATR"] * ATR_MULT)
        tp  = last["BBM"]
        risk = last["Close"] - sl
        reward = tp - last["Close"]
        qty = (CAPITAL * RISK_PCT) / risk if risk > 0 else 0
        current = {
            "signal": "BUY",
            "color": "lime",
            "entry": last["Close"],
            "sl": sl,
            "tp": tp,
            "rr": round(reward / risk, 2) if risk > 0 else 0,
            "qty": round(qty, 6),
            "reason": f"Lower BB touch | RSI={last['RSI']:.1f} | UPTREND",
        }
    elif last["Close"] >= last["BBU"] and last["RSI"] > RSI_OB and last["Close"] < last["SMA200"]:
        sl  = last["Close"] + (last["ATR"] * ATR_MULT)
        tp  = last["BBM"]
        risk = sl - last["Close"]
        reward = last["Close"] - tp
        current = {
            "signal": "SELL",
            "color": "red",
            "entry": last["Close"],
            "sl": sl,
            "tp": tp,
            "rr": round(reward / risk, 2) if risk > 0 else 0,
            "reason": f"Upper BB touch | RSI={last['RSI']:.1f} | DOWNTREND",
        }
    else:
        uptrend = last["Close"] > last["SMA200"] if pd.notna(last["SMA200"]) else False
        pct_from_lower = ((last["Close"] - last["BBL"]) / last["BBL"] * 100) if pd.notna(last["BBL"]) else 0
        current = {
            "signal": "HOLD",
            "color": "yellow",
            "reason": f"{'UPTREND' if uptrend else 'DOWNTREND'} | RSI={last['RSI']:.1f} | {pct_from_lower:.1f}% above lower BB",
        }

    return buys, sells, current


def print_signal_summary(df: pd.DataFrame, current: dict):
    last = df.iloc[-1]
    print(f"\n{'='*55}")
    print(f"  ETH/USDT 1h  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")
    print(f"  Price    : {last['Close']:.2f}")
    print(f"  BB Lower : {last['BBL']:.2f}  |  Middle: {last['BBM']:.2f}  |  Upper: {last['BBU']:.2f}")
    print(f"  RSI      : {last['RSI']:.1f}  (OS<{RSI_OS} | OB>{RSI_OB})")
    print(f"  SMA200   : {last['SMA200']:.2f}  ({'UPTREND' if last['Close'] > last['SMA200'] else 'DOWNTREND'})")
    print(f"  ATR      : {last['ATR']:.2f}")
    print(f"{'─'*55}")
    print(f"  SIGNAL   : {current['signal']}")
    print(f"  Reason   : {current['reason']}")
    if current["signal"] in ("BUY", "SELL"):
        print(f"  Entry    : {current['entry']:.2f}")
        print(f"  SL       : {current['sl']:.2f}")
        print(f"  TP       : {current['tp']:.2f}")
        print(f"  R:R      : 1:{current['rr']}")
        if "qty" in current:
            print(f"  Qty (2% risk of Rs.{CAPITAL}): {current['qty']} ETH")
    print(f"{'='*55}\n")


def build_chart(view_df: pd.DataFrame, buys: list, sells: list, current: dict):
    os.makedirs("charts", exist_ok=True)
    save_path = "charts/eth_1h_signal.png"

    # RSI panel data (fill oversold/overbought zones)
    rsi_series = view_df["RSI"].copy()
    ob_line = pd.Series(RSI_OB, index=view_df.index)
    os_line = pd.Series(RSI_OS, index=view_df.index)

    # Signal markers (NaN where no signal)
    buy_markers  = pd.Series(float("nan"), index=view_df.index)
    sell_markers = pd.Series(float("nan"), index=view_df.index)
    for i in buys:
        buy_markers.iloc[i] = view_df["Low"].iloc[i] * 0.9975
    for i in sells:
        sell_markers.iloc[i] = view_df["High"].iloc[i] * 1.0025

    # Mark latest candle if signal
    if current["signal"] == "BUY":
        buy_markers.iloc[-1] = view_df["Low"].iloc[-1] * 0.9975
    elif current["signal"] == "SELL":
        sell_markers.iloc[-1] = view_df["High"].iloc[-1] * 1.0025

    addplots = [
        # BB bands
        mpf.make_addplot(view_df["BBU"],    color="#f4a261", linewidth=1.2, panel=0, label="BB Upper"),
        mpf.make_addplot(view_df["BBM"],    color="#e9c46a", linewidth=0.8, linestyle="--", panel=0, label="BB Mid"),
        mpf.make_addplot(view_df["BBL"],    color="#f4a261", linewidth=1.2, panel=0, label="BB Lower"),
        # SMA200
        mpf.make_addplot(view_df["SMA200"], color="#9b5de5", linewidth=1.5, panel=0, label="SMA200"),
        # RSI
        mpf.make_addplot(rsi_series, panel=1, color="#2ec4b6", linewidth=1.2, ylabel="RSI"),
        mpf.make_addplot(ob_line,    panel=1, color="#ef476f", linewidth=0.8, linestyle="--"),
        mpf.make_addplot(os_line,    panel=1, color="#06d6a0", linewidth=0.8, linestyle="--"),
        # Signal markers
        mpf.make_addplot(buy_markers,  type="scatter", markersize=150, marker="^", color="#00ff88", panel=0),
        mpf.make_addplot(sell_markers, type="scatter", markersize=150, marker="v", color="#ff3366", panel=0),
    ]

    mc = mpf.make_marketcolors(
        up="#2ec4b6", down="#ef476f",
        edge="inherit", wick="inherit",
        volume="#555555"
    )
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mc,
        gridstyle=":",
        gridcolor="#333355",
    )

    signal_str = current["signal"]
    if current["signal"] in ("BUY", "SELL"):
        signal_str += f"  Entry={current['entry']:.2f}  SL={current['sl']:.2f}  TP={current['tp']:.2f}  R:R 1:{current['rr']}"

    title = (
        f"ETH/USDT 1h  |  BB Mean Reversion + RSI + SMA200 filter\n"
        f"Signal: {signal_str}  |  {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
    )

    fig, _ = mpf.plot(
        view_df,
        type="candle",
        style=style,
        title=title,
        addplot=addplots,
        volume=False,
        panel_ratios=(3, 1),
        figsize=(20, 11),
        tight_layout=True,
        returnfig=True,
    )

    fig.savefig(save_path, dpi=150, bbox_inches="tight", facecolor="#1a1a2e")
    print(f"Chart saved -> {save_path}")
    return save_path


def open_chart(path: str):
    try:
        os.startfile(os.path.abspath(path))
        print("Chart opened in default image viewer")
    except Exception as e:
        print(f"Open manually: {os.path.abspath(path)}")


def main():
    df = fetch_data()
    df = add_indicators(df)

    # Use last DISPLAY candles for chart (but detect signals across all)
    view_df = df.tail(DISPLAY).copy()

    # Detect signals in view window (re-index to 0-based for markers)
    buys, sells, current = detect_signals(view_df)

    print_signal_summary(df, current)
    chart_path = build_chart(view_df, buys, sells, current)
    open_chart(chart_path)


if __name__ == "__main__":
    main()
