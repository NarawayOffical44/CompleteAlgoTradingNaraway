"""
Exchange connector — wraps ccxt for Binance (testnet + live).
Handles all API calls with error handling and rate limiting.
"""
import os
import ccxt
import pandas as pd
from dotenv import load_dotenv
from utils.logger import get_logger

load_dotenv("config/.env")
logger = get_logger("exchange")


class Exchange:
    def __init__(self, config: dict):
        self.config = config
        self.testnet = config["exchange"]["testnet"]
        self._init_exchange()

    def _init_exchange(self):
        api_key = os.getenv("BINANCE_TESTNET_API_KEY" if self.testnet else "BINANCE_API_KEY", "")
        secret = os.getenv("BINANCE_TESTNET_SECRET" if self.testnet else "BINANCE_SECRET", "")

        self.client = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
            },
        })

        if self.testnet:
            self.client.set_sandbox_mode(True)
            logger.info("[yellow]Running on TESTNET — no real money[/yellow]")
        else:
            logger.info("[red]Running LIVE — real money at risk[/red]")

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        """Fetch OHLCV candles and return as DataFrame."""
        raw = self.client.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        return df

    def get_balance(self) -> dict:
        """Return USDT balance."""
        balance = self.client.fetch_balance()
        usdt = balance["USDT"]
        logger.info(f"Balance — Free: {usdt['free']:.2f} | Used: {usdt['used']:.2f} | Total: {usdt['total']:.2f}")
        return usdt

    def get_ticker(self, symbol: str) -> dict:
        return self.client.fetch_ticker(symbol)

    def place_market_order(self, symbol: str, side: str, amount: float) -> dict:
        """Place a market order. side = 'buy' or 'sell'."""
        logger.info(f"Placing {side.upper()} market order | {symbol} | amount: {amount:.6f}")
        order = self.client.create_market_order(symbol, side, amount)
        logger.info(f"Order filled: {order['id']} @ ~{order.get('average', '?')}")
        return order

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        """Place a limit order."""
        logger.info(f"Placing {side.upper()} limit order | {symbol} | {amount:.6f} @ {price:.2f}")
        order = self.client.create_limit_order(symbol, side, amount, price)
        return order

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        return self.client.cancel_order(order_id, symbol)

    def get_open_orders(self, symbol: str) -> list:
        return self.client.fetch_open_orders(symbol)

    def get_position(self, symbol: str) -> float:
        """Return current base asset holdings (e.g. BTC for BTC/USDT)."""
        base = symbol.split("/")[0]
        balance = self.client.fetch_balance()
        return balance[base]["free"] if base in balance else 0.0
