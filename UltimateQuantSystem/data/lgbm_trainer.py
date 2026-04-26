"""
LightGBM Signal Trainer — Mean Reversion filter model.

Features (19):
  Price/vol:    zscore_20, zscore_50, bb_width, atr_pct, roc_5, roc_10, adx_14
  Volume:       vol_ratio, delivery_pct
  Momentum:     rsi_14, macd_hist
  Regime ctx:   vix_level, vix_change, nifty_5d_return
  Fundamentals: pe_ratio, roe, debt_to_equity, promoter_holding, fund_score

Label: 1 if price hits +0.5% within next 10 bars, else 0
CV:    TimeSeriesSplit (no lookahead leakage), 5 folds, early stopping
"""

import pickle
import warnings
import numpy as np
from pathlib import Path
from loguru import logger

warnings.filterwarnings("ignore")

MODEL_DIR      = Path("model")
MODEL_PATH     = MODEL_DIR / "lgbm_mean_rev.pkl"
LABEL_PROFIT   = 0.005
LABEL_BARS     = 10
PRED_THRESHOLD = 0.55

FEATURE_COLS = [
    "zscore_20", "zscore_50", "bb_width", "atr_pct", "roc_5", "roc_10", "adx_14",
    "vol_ratio", "delivery_pct",
    "rsi_14", "macd_hist",
    "vix_level", "vix_change", "nifty_5d_return",
    "pe_ratio", "roe", "debt_to_equity", "promoter_holding", "fund_score",
]


def build_features(closes: list, highs: list, lows: list, volumes: list,
                   vix: float = 15.0, nifty_5d: float = 0.0,
                   pe: float = 25.0, roe: float = 15.0, de: float = 0.5,
                   promoter: float = 50.0, fund_score: float = 50.0,
                   delivery_pct: float = 50.0) -> dict | None:
    if len(closes) < 52:
        return None

    c = np.array(closes, dtype=float)
    h = np.array(highs,   dtype=float) if highs   else c.copy()
    l = np.array(lows,    dtype=float) if lows    else c.copy()
    v = np.array(volumes, dtype=float) if volumes else np.ones(len(c))

    def zscore(n):
        w = c[-n:]
        sd = np.std(w)
        return float((c[-1] - np.mean(w)) / sd) if sd > 0 else 0.0

    bb_std   = np.std(c[-20:])
    bb_mean  = np.mean(c[-20:])
    bb_width = float(4 * bb_std / bb_mean) if bb_mean > 0 else 0.0

    n_atr = min(14, len(h) - 1)
    trs   = [max(h[-i] - l[-i], abs(h[-i] - c[-i-1]), abs(l[-i] - c[-i-1]))
             for i in range(1, n_atr + 1)]
    atr_pct = float(np.mean(trs) / c[-1]) if trs and c[-1] > 0 else 0.01

    diff     = np.diff(c[-15:])
    avg_g    = np.mean(np.where(diff > 0, diff, 0.0))
    avg_l    = np.mean(np.where(diff < 0, -diff, 0.0))
    rsi      = float(100 - 100 / (1 + avg_g / avg_l)) if avg_l > 0 else 50.0

    def ema_val(arr, n):
        k, e = 2 / (n + 1), arr[0]
        for x in arr[1:]: e = x * k + e * (1 - k)
        return e
    arr26     = c[-26:].tolist()
    macd_hist = float(ema_val(arr26, 12) - ema_val(arr26, 26)) * 0.3

    roc_5  = float((c[-1] / c[-6]  - 1) * 100) if len(c) > 5  else 0.0
    roc_10 = float((c[-1] / c[-11] - 1) * 100) if len(c) > 10 else 0.0
    vol_ratio = float(v[-1] / np.mean(v[-20:])) if len(v) >= 20 and np.mean(v[-20:]) > 0 else 1.0

    if len(h) > 14:
        p_dm  = [max(h[-i] - h[-i-1], 0) if h[-i] > h[-i-1] else 0 for i in range(1, 14)]
        m_dm  = [max(l[-i-1] - l[-i], 0) if l[-i] < l[-i-1] else 0 for i in range(1, 14)]
        adx   = float(abs(np.mean(p_dm) - np.mean(m_dm)) / (np.mean(trs[:13]) + 1e-9) * 100)
    else:
        adx = 25.0

    return {
        "zscore_20": zscore(20), "zscore_50": zscore(50),
        "bb_width": bb_width, "atr_pct": atr_pct,
        "roc_5": roc_5, "roc_10": roc_10, "adx_14": adx,
        "vol_ratio": vol_ratio, "delivery_pct": float(delivery_pct),
        "rsi_14": rsi, "macd_hist": macd_hist,
        "vix_level": float(vix), "vix_change": 0.0,
        "nifty_5d_return": float(nifty_5d),
        "pe_ratio": float(pe), "roe": float(roe),
        "debt_to_equity": float(de), "promoter_holding": float(promoter),
        "fund_score": float(fund_score),
    }


class LGBMTrainer:
    """
    market_data_history: list of per-day market_data dicts (same format as orchestrator produces).
    Each day must contain per-stock OHLCV + _fundamentals for labels.
    """

    def __init__(self, market_data_history: list, model_path: str = str(MODEL_PATH)):
        self.history    = market_data_history
        self.model_path = Path(model_path)

    def train(self) -> dict:
        try:
            import lightgbm as lgb
            from sklearn.model_selection import TimeSeriesSplit
            from sklearn.metrics import roc_auc_score
        except ImportError as e:
            logger.error(f"Missing dep: {e}")
            return {}

        X_rows, y_rows = self._build_dataset()
        if len(X_rows) < 50:
            logger.error(f"Only {len(X_rows)} samples — need ≥50")
            return {}

        X = np.array([[r[f] for f in FEATURE_COLS] for r in X_rows])
        y = np.array(y_rows)

        params = {
            "objective": "binary", "metric": "auc",
            "learning_rate": 0.05, "num_leaves": 31,
            "min_child_samples": 20, "subsample": 0.8,
            "colsample_bytree": 0.8, "reg_alpha": 0.1,
            "reg_lambda": 0.1, "verbose": -1, "n_jobs": -1,
        }

        aucs = []
        for fold, (tr_i, va_i) in enumerate(TimeSeriesSplit(n_splits=5).split(X)):
            m = lgb.train(
                params, lgb.Dataset(X[tr_i], y[tr_i]),
                num_boost_round=500,
                valid_sets=[lgb.Dataset(X[va_i], y[va_i])],
                callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
            )
            auc = roc_auc_score(y[va_i], m.predict(X[va_i]))
            aucs.append(auc)
            logger.info(f"Fold {fold+1} AUC: {auc:.4f}")

        mean_auc = float(np.mean(aucs))
        final    = lgb.train(params, lgb.Dataset(X, y), num_boost_round=300)

        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump({"model": final, "feature_cols": FEATURE_COLS,
                         "threshold": PRED_THRESHOLD, "oof_auc": mean_auc}, f)

        logger.info(f"Model saved → {self.model_path} | OOF AUC={mean_auc:.4f}")
        return {"oof_auc": mean_auc, "n_samples": len(y), "positive_rate": float(y.mean())}

    def _build_dataset(self) -> tuple[list, list]:
        from collections import defaultdict
        by_sym: dict = defaultdict(lambda: {
            "closes": [], "highs": [], "lows": [], "volumes": [],
            "vix": [], "nifty_5d": [], "fund": {},
        })

        for day in self.history:
            vix_ltp  = day.get("VIX", {}).get("ltp", 15.0)
            nifty_5d = day.get("NIFTY", {}).get("5d_return", 0.0)
            funds    = day.get("_fundamentals", {})
            for sym, data in day.items():
                if sym.startswith("_") or not isinstance(data, dict) or "ltp" not in data:
                    continue
                s = by_sym[sym]
                s["closes"].append(data["ltp"])
                s["highs"].append(data.get("highs", [data["ltp"]])[-1] if data.get("highs") else data["ltp"])
                s["lows"].append(data.get("lows",  [data["ltp"]])[-1] if data.get("lows")  else data["ltp"])
                s["volumes"].append(data.get("volumes", [1])[-1] if data.get("volumes") else 1)
                s["vix"].append(vix_ltp)
                s["nifty_5d"].append(nifty_5d)
                if not s["fund"]:
                    s["fund"] = funds.get(sym, {})

        X_rows, y_rows = [], []
        for sym_data in by_sym.values():
            c = sym_data["closes"]
            if len(c) < 62:
                continue
            f = sym_data["fund"]
            for i in range(52, len(c) - LABEL_BARS):
                feats = build_features(
                    closes=c[:i], highs=sym_data["highs"][:i],
                    lows=sym_data["lows"][:i], volumes=sym_data["volumes"][:i],
                    vix=sym_data["vix"][i-1], nifty_5d=sym_data["nifty_5d"][i-1],
                    pe=f.get("pe_ratio", 25), roe=f.get("roe", 15),
                    de=f.get("debt_to_equity", 0.5),
                    promoter=f.get("promoter_holding_pct", 50),
                    fund_score=f.get("fundamental_score", 50),
                )
                if feats is None:
                    continue
                entry  = c[i]
                label  = 1 if any(p >= entry * (1 + LABEL_PROFIT) for p in c[i+1:i+1+LABEL_BARS]) else 0
                X_rows.append(feats)
                y_rows.append(label)

        return X_rows, y_rows


class _PassthroughModel:
    threshold = PRED_THRESHOLD
    def predict(self, _): return 0.6


class MeanReversionSignalModel:

    def __init__(self, model=None, threshold: float = PRED_THRESHOLD):
        self._model    = model or _PassthroughModel()
        self.threshold = threshold

    @classmethod
    def load(cls, path: str = str(MODEL_PATH)) -> "MeanReversionSignalModel":
        p = Path(path)
        if not p.exists():
            logger.warning(f"No model at {p} — passthrough mode (all signals pass)")
            return cls()
        try:
            with open(p, "rb") as f:
                b = pickle.load(f)
            logger.info(f"LightGBM loaded | OOF AUC={b.get('oof_auc', '?'):.4f}")
            return cls(model=b["model"], threshold=b.get("threshold", PRED_THRESHOLD))
        except Exception as e:
            logger.warning(f"Model load failed ({e}) — passthrough mode")
            return cls()

    def predict(self, features_dict: dict) -> float:
        if isinstance(self._model, _PassthroughModel):
            return self._model.predict(features_dict)
        try:
            row = np.array([[features_dict.get(f, 0.0) for f in FEATURE_COLS]])
            return float(self._model.predict(row)[0])
        except Exception as e:
            logger.debug(f"predict error: {e}")
            return 0.6

    def is_valid_signal(self, features_dict: dict) -> bool:
        return self.predict(features_dict) >= self.threshold
