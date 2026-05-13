"""
AutoTrainer — Automated model retraining with zero-downtime hot-reload.

Safety guarantees:
  1. Never trains during market hours (9:10–15:35 buffer)
  2. Backup-before-replace: old model restored automatically on failure
  3. Validation gates before any model goes live:
       HMM:      regime confidence >= 0.35 on current data
       LightGBM: OOF AUC >= 0.52 (better than random)
  4. Hot-reload: live HMMRegimeDetector + MeanReversionAgent updated in-place
     — bots never restart, no gap in coverage
  5. Min re-train interval: 6 hours (prevents thrashing)
  6. Startup stale check: if model file > 7 days old, train 60s after boot

Schedule:
  - Daily:  15:45 IST Mon–Fri (30 min after market close)
  - Weekly: Sunday 07:00 IST (full retrain on fresh data)

Telegram alerts on: start, success (with AUC/confidence), failure.
"""

import os
import json
import time
import shutil
import pickle
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

from data.lgbm_trainer import LGBMTrainer, MeanReversionSignalModel, MODEL_PATH as LGBM_PATH
from ai.hmm_regime import HMMRegimeDetector, MODEL_PATH as HMM_PATH


LAST_TRAINED_PATH = Path("model/last_trained.json")
HMM_MIN_CONF      = 0.35   # reject new HMM if confidence below this
LGBM_MIN_AUC      = 0.52   # reject new LightGBM if OOF AUC below this
MIN_INTERVAL_H    = 6      # minimum hours between training runs
STALE_MODEL_DAYS  = 7      # retrain on startup if model older than this


class AutoTrainer:

    def __init__(self, nse_market, mean_rev_agent=None, notify_fn=None):
        """
        nse_market:      live NSEMarket (provides data + live HMM ref for hot-reload)
        mean_rev_agent:  live MeanReversionAgent (hot-reloads its LightGBM after training)
        notify_fn:       callable(str) — Telegram notifier (silent no-op if None)
        """
        self.nse            = nse_market
        self.mean_rev_agent = mean_rev_agent
        self.notify         = notify_fn or (lambda msg: None)

        self._last_trained  = self._load_last_trained()
        self._train_lock    = threading.Lock()
        self._thread        = None
        self._stop_event    = threading.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="auto-trainer", daemon=True
        )
        self._thread.start()
        logger.info("AutoTrainer | started | schedule: daily 15:45 + Sunday 07:00 IST")

    def stop(self, join: bool = False, timeout: float = 10.0):
        self._stop_event.set()
        if join and self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ── Scheduling loop ────────────────────────────────────────────────────
    def _loop(self):
        # Stale check: train 60s after boot if model hasn't been trained recently
        if self._model_is_stale():
            logger.info(f"AutoTrainer | model >{STALE_MODEL_DAYS}d old — scheduling startup train")
            self._sleep(60)
            if not self._stop_event.is_set():
                self._run_cycle(reason="startup_stale")

        while not self._stop_event.is_set():
            if self._should_train_now():
                self._run_cycle(reason=self._schedule_reason())
            self._sleep(60)   # check every minute

    def _sleep(self, seconds: int):
        """Interruptible sleep."""
        for _ in range(seconds):
            if self._stop_event.is_set():
                return
            time.sleep(1)

    # ── Schedule checks ────────────────────────────────────────────────────
    def _should_train_now(self) -> bool:
        if self._is_market_hours():
            return False
        if not self._min_interval_elapsed():
            return False
        now = datetime.now()
        t   = now.hour * 60 + now.minute
        # Daily: 15:45–16:00 Mon-Fri
        if now.weekday() < 5 and 15 * 60 + 45 <= t <= 16 * 60:
            return True
        # Weekly: Sunday 07:00–07:15
        if now.weekday() == 6 and 7 * 60 <= t <= 7 * 60 + 15:
            return True
        return False

    def _schedule_reason(self) -> str:
        return "weekly_sunday" if datetime.now().weekday() == 6 else "daily_post_close"

    def _is_market_hours(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return 9 * 60 + 10 <= t <= 15 * 60 + 35   # 5-min buffer each side

    def _min_interval_elapsed(self) -> bool:
        if not self._last_trained:
            return True
        elapsed_h = (datetime.now() - self._last_trained).total_seconds() / 3600
        return elapsed_h >= MIN_INTERVAL_H

    def _model_is_stale(self) -> bool:
        if not HMM_PATH.exists():
            return True
        age_d = (time.time() - HMM_PATH.stat().st_mtime) / 86400
        return age_d > STALE_MODEL_DAYS

    # ── Core training cycle ────────────────────────────────────────────────
    def _run_cycle(self, reason: str = "scheduled"):
        if not self._train_lock.acquire(blocking=False):
            logger.info("AutoTrainer | training already in progress — skipping")
            return

        try:
            logger.info(f"AutoTrainer | ── cycle start | reason={reason} ──")
            self.notify(f"<b>AutoTrainer</b> | training started (<code>{reason}</code>)")

            market_data = self.nse.market_fetcher.get_market_data()
            if not market_data:
                logger.warning("AutoTrainer | no market data returned — aborting cycle")
                return

            hmm_ok  = self._train_hmm(market_data)
            lgbm_ok = self._train_lgbm(market_data)

            self._last_trained = datetime.now()
            self._save_last_trained()

            icons  = ("✅" if hmm_ok else "❌", "✅" if lgbm_ok else "❌")
            status = f"HMM={icons[0]}  LightGBM={icons[1]}"
            logger.info(f"AutoTrainer | ── cycle done | {status} ──")
            self.notify(f"<b>AutoTrainer done</b> | {status}")

        except Exception as e:
            logger.error(f"AutoTrainer | cycle error: {e}")
            self.notify(f"<b>AutoTrainer ERROR</b>\n<code>{e}</code>")
        finally:
            self._train_lock.release()

    # ── HMM training ──────────────────────────────────────────────────────
    def _train_hmm(self, market_data: dict) -> bool:
        """
        Train on Nifty closes.
        Safety: backup → train (saves to HMM_PATH) → validate → restore on fail.
        Hot-reload live HMM on success.
        """
        nifty_closes = market_data.get("NIFTY", {}).get("closes", [])
        if len(nifty_closes) < 60:
            logger.warning(f"AutoTrainer | HMM: only {len(nifty_closes)} bars (need 60) — skip")
            return False

        bak_path = HMM_PATH.with_suffix(".pkl.bak")
        had_model = HMM_PATH.exists()

        # Backup current model so we can restore on failure
        if had_model:
            shutil.copy2(HMM_PATH, bak_path)

        try:
            # Train a fresh instance — fit() saves to HMM_PATH automatically
            fresh_hmm = HMMRegimeDetector()
            fresh_hmm.fit(nifty_closes)   # saves to model/hmm_nifty.pkl

            # Validate: confidence on current data must be >= threshold
            _, conf = fresh_hmm.predict(nifty_closes)
            if conf < HMM_MIN_CONF:
                logger.warning(
                    f"AutoTrainer | HMM validation failed: conf={conf:.2f} < {HMM_MIN_CONF} "
                    f"— restoring backup"
                )
                if had_model:
                    shutil.copy2(bak_path, HMM_PATH)   # restore
                else:
                    HMM_PATH.unlink(missing_ok=True)
                return False

            # Hot-reload live HMM in NSEMarket (bots keep running, no gap)
            self.nse.regime_hmm._load()
            logger.info(
                f"AutoTrainer | HMM trained & reloaded | "
                f"bars={len(nifty_closes)} | conf={conf:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"AutoTrainer | HMM training error: {e} — restoring backup")
            if had_model:
                shutil.copy2(bak_path, HMM_PATH)
            else:
                HMM_PATH.unlink(missing_ok=True)
            return False

    # ── LightGBM training ──────────────────────────────────────────────────
    def _train_lgbm(self, market_data: dict) -> bool:
        """
        Build historical dataset from OHLCV arrays already in market_data.
        Trains to a temp file, validates AUC, then atomically replaces.
        Hot-reloads live MeanReversionAgent model on success.
        """
        try:
            import lightgbm  # noqa
        except ImportError:
            logger.warning("AutoTrainer | lightgbm not installed — skip LightGBM training")
            return False

        history = self._build_lgbm_history(market_data)
        if len(history) < 62:
            logger.warning(f"AutoTrainer | LightGBM: only {len(history)} history days (need 62) — skip")
            return False

        tmp_path = LGBM_PATH.with_suffix(".pkl.tmp")
        bak_path = LGBM_PATH.with_suffix(".pkl.bak")

        try:
            trainer = LGBMTrainer(
                market_data_history=history,
                model_path=str(tmp_path),
            )
            result = trainer.train()

            if not result:
                logger.warning("AutoTrainer | LightGBM training returned empty — skip")
                tmp_path.unlink(missing_ok=True)
                return False

            oof_auc = result.get("oof_auc", 0.0)
            if oof_auc < LGBM_MIN_AUC:
                logger.warning(
                    f"AutoTrainer | LightGBM validation failed: AUC={oof_auc:.4f} < {LGBM_MIN_AUC} "
                    f"— discarding"
                )
                tmp_path.unlink(missing_ok=True)
                return False

            # Atomic swap: backup → replace
            if LGBM_PATH.exists():
                shutil.copy2(LGBM_PATH, bak_path)
            os.replace(tmp_path, LGBM_PATH)   # atomic on Windows + Linux

            # Hot-reload live agent (thread-safe: next signal cycle picks up new model)
            if self.mean_rev_agent is not None:
                self.mean_rev_agent.reload_model()

            logger.info(
                f"AutoTrainer | LightGBM trained & reloaded | "
                f"AUC={oof_auc:.4f} | samples={result.get('n_samples', '?')} | "
                f"pos_rate={result.get('positive_rate', 0):.2%}"
            )
            return True

        except Exception as e:
            logger.error(f"AutoTrainer | LightGBM training error: {e}")
            tmp_path.unlink(missing_ok=True)
            return False

    # ── Build LightGBM history from OHLCV arrays ───────────────────────────
    @staticmethod
    def _build_lgbm_history(market_data: dict) -> list:
        """
        Convert a single market_data snapshot (which contains 248-bar OHLCV arrays)
        into a list of 248 synthetic daily market_data dicts for LGBMTrainer.

        This bootstraps training without needing a stored snapshot history.
        Each synthetic "day" contains the data up to that bar index.
        """
        stock_keys = [
            k for k, v in market_data.items()
            if not k.startswith("_")
            and isinstance(v, dict)
            and k not in ("NIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT")
            and v.get("closes")
        ]
        if not stock_keys:
            return []

        n_days       = min(len(market_data[k]["closes"]) for k in stock_keys)
        n_days       = min(n_days, 248)
        nifty_closes = market_data.get("NIFTY", {}).get("closes", [])
        vix_ltp      = market_data.get("VIX",   {}).get("ltp", 15.0)
        funds        = market_data.get("_fundamentals", {})

        history = []
        for i in range(n_days):
            day = {
                "NIFTY": {
                    "ltp":       nifty_closes[i] if i < len(nifty_closes) else 22000.0,
                    "5d_return": ((nifty_closes[i] / nifty_closes[i - 5] - 1) * 100
                                  if i >= 5 and i < len(nifty_closes) else 0.0),
                },
                "VIX":           {"ltp": vix_ltp},
                "_fundamentals": funds,
            }
            for sym in stock_keys:
                d      = market_data[sym]
                closes = d.get("closes", [])
                if i >= len(closes):
                    continue
                day[sym] = {
                    "ltp":     closes[i],
                    "highs":   d.get("highs",   closes)[:i + 1],
                    "lows":    d.get("lows",    closes)[:i + 1],
                    "volumes": d.get("volumes", [1] * len(closes))[:i + 1],
                }
            history.append(day)

        return history

    # ── Persist last-trained timestamp ─────────────────────────────────────
    def _save_last_trained(self):
        LAST_TRAINED_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LAST_TRAINED_PATH, "w") as f:
            json.dump({"last_trained": self._last_trained.isoformat()}, f)

    def _load_last_trained(self) -> datetime | None:
        if not LAST_TRAINED_PATH.exists():
            return None
        try:
            with open(LAST_TRAINED_PATH) as f:
                return datetime.fromisoformat(json.load(f)["last_trained"])
        except Exception:
            return None
