"""
Agent 2: Mean Reversion + Regime + Fundamentals
Core:    Bollinger Bands Z-score + LightGBM filter (19 features, matches lgbm_trainer)
Added:   Fundamental filter + Sentiment check + HMM regime gate

Filters (all must pass):
  - Regime: only in BULL_LOW_VOL or CHOPPY (passed via regime param)
  - Fundamentals: ROE > 10%, Debt/Equity < 1.2, fundamental_score > 45
  - Sentiment: trade_bias != 'avoid' AND score > -0.4
  - Volume: today's volume >= 60% of 20d avg (stock must be active)
  - LightGBM: 19-feature model probability >= 0.60 (auto-loads; passthrough if not trained)
"""

import numpy as np
from agents.base_agent import BaseAgent
from loguru import logger


SAFE_REGIMES       = {"BULL_LOW_VOL", "CHOPPY", "UNKNOWN"}
MIN_ROE            = 10.0
MAX_DE             = 1.2
MIN_FUND_SCORE     = 45.0
MIN_VOLUME_RATIO   = 0.6
MAX_BEAR_SENTIMENT = -0.4


class MeanReversionAgent(BaseAgent):

    def __init__(self, *args, bb_period=20, bb_std=2.0, max_positions=5, **kwargs):
        super().__init__(*args, **kwargs)
        self.bb_period    = bb_period
        self.bb_std       = bb_std
        self.max_positions = max_positions
        # Auto-load LightGBM model; falls back to passthrough if model file missing
        from data.lgbm_trainer import MeanReversionSignalModel
        self.model = MeanReversionSignalModel.load()

    def generate_signals(self, market_data: dict) -> list[dict]:
        signals      = []
        regime       = market_data.get("_regime", "UNKNOWN")
        fundamentals = market_data.get("_fundamentals", {})
        sentiment    = market_data.get("_sentiment", {})

        # ── Regime gate ───────────────────────────────────────────────────
        if regime not in SAFE_REGIMES:
            logger.info(f"MeanRev | regime={regime} not safe — skip all")
            return signals

        open_count = sum(1 for t in self.journal.trades.values()
                         if t.agent_id == self.agent_id and t.status == "open")
        if open_count >= self.max_positions:
            return signals

        for symbol, data in market_data.items():
            if symbol.startswith("_") or symbol in (
                    "NIFTY", "BANKNIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT"):
                continue

            closes = data.get("closes", [])
            if len(closes) < self.bb_period + 5:
                continue

            # ── Fundamental filter ────────────────────────────────────────
            f = fundamentals.get(symbol, {})
            if not self._fundamentals_ok(f):
                logger.debug(f"MeanRev {symbol} | fundamentals fail — skip")
                continue

            # ── Sentiment filter ──────────────────────────────────────────
            s = sentiment.get(symbol, {})
            if not self._sentiment_ok(s):
                logger.debug(f"MeanRev {symbol} | sentiment avoid — skip")
                continue

            # ── Volume filter ─────────────────────────────────────────────
            if data.get("volume_ratio", 1.0) < MIN_VOLUME_RATIO:
                logger.debug(f"MeanRev {symbol} | low volume — skip")
                continue

            # ── Bollinger Band Z-score entry ──────────────────────────────
            z = self._bb_zscore(closes)
            if z >= -1.8:   # Not oversold enough
                continue

            # ── LightGBM filter (19-feature, matches lgbm_trainer) ────────
            feats = self._build_features(data, market_data, f)
            if feats is None or not self.model.is_valid_signal(feats):
                logger.debug(f"MeanRev {symbol} | LightGBM filter failed — skip")
                continue

            ltp  = data.get("ltp", closes[-1])
            atr  = self._atr(data.get("highs", []), data.get("lows", []), closes)
            stop = ltp - (2 * atr)
            risk_per_share = ltp - stop
            capital        = self.risk.state.capital
            risk_amount    = capital * 0.01
            quantity       = risk_amount / risk_per_share if risk_per_share > 0 else 1

            fund_score = f.get("fundamental_score", 50)
            sent_score = s.get("sentiment_score", 0)
            signals.append({
                "symbol":      symbol,
                "direction":   "long",
                "entry_price": ltp,
                "quantity":    quantity,
                "risk_amount": risk_amount,
                "thesis":      (f"BB z={z:.2f} | regime={regime} | "
                                f"fund={fund_score} | sent={sent_score:.2f} | "
                                f"vol_ratio={data.get('volume_ratio', 1):.2f} | "
                                f"lgbm_prob={self.model.predict(feats):.2f}"),
            })

        return signals

    def reload_model(self):
        """Hot-reload LightGBM from disk. Called by AutoTrainer after successful retrain."""
        from data.lgbm_trainer import MeanReversionSignalModel
        self.model = MeanReversionSignalModel.load()
        logger.info(f"MeanReversionAgent | LightGBM model reloaded")

    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        trade = self.journal.trades.get(trade_id)
        if not trade:
            return False, ""

        data   = market_data.get(trade.symbol, {})
        closes = data.get("closes", [])
        ltp    = data.get("ltp", trade.entry_price)

        # Sentiment deterioration → exit early
        s = market_data.get("_sentiment", {}).get(trade.symbol, {})
        if s.get("trade_bias") == "avoid":
            return True, "Sentiment deteriorated to avoid"

        # Bollinger Band mean reversion target (z back to 0)
        if len(closes) >= self.bb_period:
            z = self._bb_zscore(closes)
            if z > 0:
                return True, f"BB mean reversion complete | z={z:.2f}"

        # ATR stop
        atr  = self._atr(data.get("highs", []), data.get("lows", []), closes)
        stop = trade.entry_price - (2 * atr)
        if ltp < stop:
            return True, f"ATR stop | ltp={ltp:.2f} stop={stop:.2f}"

        return False, ""

    # ── Filters ───────────────────────────────────────────────────────────
    @staticmethod
    def _fundamentals_ok(f: dict) -> bool:
        return (f.get("roe", MIN_ROE) >= MIN_ROE and
                f.get("debt_to_equity", 0.5) <= MAX_DE and
                f.get("fundamental_score", MIN_FUND_SCORE) >= MIN_FUND_SCORE)

    @staticmethod
    def _sentiment_ok(s: dict) -> bool:
        return (s.get("trade_bias") != "avoid" and
                s.get("sentiment_score", 0) > MAX_BEAR_SENTIMENT)

    # ── 19-feature builder (matches lgbm_trainer.FEATURE_COLS exactly) ────
    @staticmethod
    def _build_features(data: dict, market_data: dict, fund: dict) -> dict | None:
        from data.lgbm_trainer import build_features
        return build_features(
            closes      = data.get("closes", []),
            highs       = data.get("highs", []),
            lows        = data.get("lows", []),
            volumes     = data.get("volumes", []),
            vix         = market_data.get("VIX", {}).get("ltp", 15.0),
            nifty_5d    = market_data.get("NIFTY", {}).get("5d_return", 0.0),
            pe          = fund.get("pe_ratio", 25.0),
            roe         = fund.get("roe", 15.0),
            de          = fund.get("debt_to_equity", 0.5),
            promoter    = fund.get("promoter_holding_pct", 50.0),
            fund_score  = fund.get("fundamental_score", 50.0),
            delivery_pct = data.get("delivery_pct", 50.0),
        )

    # ── Indicators ────────────────────────────────────────────────────────
    def _bb_zscore(self, closes: list) -> float:
        c    = np.array(closes[-self.bb_period:], dtype=float)
        mean = float(np.mean(c))
        std  = float(np.std(c))
        return (closes[-1] - mean) / std if std != 0 else 0.0

    @staticmethod
    def _atr(highs, lows, closes, period=14):
        if not highs or len(highs) < period:
            return closes[-1] * 0.02 if closes else 1.0
        trs = []
        for i in range(1, min(period + 1, len(highs))):
            tr = max(highs[-i] - lows[-i],
                     abs(highs[-i] - closes[-i-1]) if len(closes) > i else 0,
                     abs(lows[-i]  - closes[-i-1]) if len(closes) > i else 0)
            trs.append(tr)
        return sum(trs) / len(trs) if trs else closes[-1] * 0.02
