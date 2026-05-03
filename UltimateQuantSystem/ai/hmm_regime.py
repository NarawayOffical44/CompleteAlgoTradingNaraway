"""
HMM Regime Detector — Hidden Markov Model for market regime classification.
Uses price returns + volatility features to detect:
  State 0: Bull / Low Volatility   → all agents active
  State 1: Choppy / Mean-Reverting → options + mean reversion only
  State 2: Bear / High Volatility  → pairs trading only, no options

Complements the Claude AI regime detector:
  - HMM: fast, data-driven, no API cost (runs on every bar)
  - Claude: deep reasoning, event-aware, runs once per day

Fixes:
  - Log-space Viterbi: prevents NaN/underflow over long sequences
  - Scaled forward-backward: stable Baum-Welch training
  - Save/load to model/hmm_nifty.pkl: persists trained params across runs
"""

import pickle
import numpy as np
from enum import Enum
from pathlib import Path
from loguru import logger


MODEL_PATH = Path("model/hmm_nifty.pkl")


class HMMState(Enum):
    BULL_LOW_VOL  = 0
    CHOPPY        = 1
    BEAR_HIGH_VOL = 2


HMM_ALLOCATIONS = {
    HMMState.BULL_LOW_VOL:  {"pairs_trading": 1.0, "mean_reversion": 0.8, "momentum": 1.0, "momentum_scalper": 1.0, "options_bot": 1.0},
    HMMState.CHOPPY:        {"pairs_trading": 1.0, "mean_reversion": 1.0, "momentum": 0.3, "momentum_scalper": 0.0, "options_bot": 0.8},
    HMMState.BEAR_HIGH_VOL: {"pairs_trading": 0.7, "mean_reversion": 0.0, "momentum": 0.0, "momentum_scalper": 0.0, "options_bot": 0.0},
}


class HMMRegimeDetector:
    """
    Gaussian HMM — pure NumPy, no hmmlearn.
    Features: daily return + 5-day realized volatility.
    Numerically stable via log-space Viterbi + scaled forward-backward.
    Auto-loads saved model on init; saves after every fit().
    """

    def __init__(self, n_states: int = 3, n_iter: int = 100):
        self.n_states   = n_states
        self.n_iter     = n_iter
        self.is_trained = False

        # Default priors (used if no saved model)
        self.pi    = np.array([0.6, 0.3, 0.1])
        self.A     = np.array([[0.9,  0.08, 0.02],
                                [0.1,  0.85, 0.05],
                                [0.05, 0.1,  0.85]])
        self.mu    = np.array([[ 0.05,  0.008],
                                [ 0.0,   0.015],
                                [-0.04,  0.03]])
        self.sigma = np.array([[0.005, 0.003],
                                [0.008, 0.005],
                                [0.015, 0.01]])
        self.current_state = HMMState.BULL_LOW_VOL

        # Auto-load persisted model if it exists
        if MODEL_PATH.exists():
            self._load()

    # ── Train ─────────────────────────────────────────────────────────────
    def fit(self, closes: list) -> None:
        if len(closes) < 30:
            logger.warning("HMM: insufficient data for training")
            return

        obs = self._extract_features(closes)
        self._baum_welch_scaled(obs)
        self.is_trained = True
        self._save()
        logger.info(f"HMM trained | {len(closes)} days | states={self.n_states}")

    # ── Predict ───────────────────────────────────────────────────────────
    def predict(self, closes: list) -> tuple[HMMState, float]:
        if len(closes) < 10:
            return HMMState.BULL_LOW_VOL, 0.5

        obs = self._extract_features(closes)
        state_probs = self._viterbi_log(obs)

        if np.any(np.isnan(state_probs)):
            logger.warning("HMM: NaN in state_probs — defaulting to BULL_LOW_VOL")
            return HMMState.BULL_LOW_VOL, 0.5

        best_state_idx = int(np.argmax(state_probs))
        confidence     = float(state_probs[best_state_idx])

        state_map = {0: HMMState.BULL_LOW_VOL, 1: HMMState.CHOPPY, 2: HMMState.BEAR_HIGH_VOL}
        self.current_state = state_map[best_state_idx]

        logger.debug(f"HMM regime: {self.current_state.name} | confidence={confidence:.2f}")
        return self.current_state, confidence

    def get_allocations(self) -> dict:
        return HMM_ALLOCATIONS[self.current_state]

    # ── Save / Load ───────────────────────────────────────────────────────
    def _save(self):
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump({
                "pi": self.pi, "A": self.A,
                "mu": self.mu, "sigma": self.sigma,
                "is_trained": self.is_trained,
            }, f)
        logger.info(f"HMM saved → {MODEL_PATH}")

    def _load(self):
        try:
            with open(MODEL_PATH, "rb") as f:
                p = pickle.load(f)
            self.pi         = p["pi"]
            self.A          = p["A"]
            self.mu         = p["mu"]
            self.sigma      = p["sigma"]
            self.is_trained = p.get("is_trained", True)
            logger.info(f"HMM loaded ← {MODEL_PATH} | trained={self.is_trained}")
        except Exception as e:
            logger.warning(f"HMM load failed: {e} — using defaults")

    # ── Features ─────────────────────────────────────────────────────────
    @staticmethod
    def _extract_features(closes: list) -> np.ndarray:
        c       = np.array(closes, dtype=float)
        returns = np.diff(c) / c[:-1]
        vol     = np.array([np.std(returns[max(0, i - 5):i + 1]) for i in range(len(returns))])
        return np.column_stack([returns, vol])

    # ── Log emission (single observation) ────────────────────────────────
    def _log_emission(self, obs_t: np.ndarray, state: int) -> float:
        mu    = self.mu[state]
        sigma = np.maximum(self.sigma[state], 1e-8)
        diff  = obs_t - mu
        return float(
            -0.5 * np.sum((diff / sigma) ** 2)
            - np.sum(np.log(sigma))
            - 0.5 * len(mu) * np.log(2 * np.pi)
        )

    # ── Log-space Viterbi (NaN-safe over any sequence length) ────────────
    def _viterbi_log(self, obs: np.ndarray) -> np.ndarray:
        T      = len(obs)
        log_A  = np.log(np.clip(self.A, 1e-300, None))

        log_delta = np.full((T, self.n_states), -np.inf)
        for s in range(self.n_states):
            log_delta[0, s] = np.log(max(self.pi[s], 1e-300)) + self._log_emission(obs[0], s)

        for t in range(1, T):
            for s in range(self.n_states):
                log_delta[t, s] = (
                    np.max(log_delta[t - 1] + log_A[:, s])
                    + self._log_emission(obs[t], s)
                )

        # Softmax of last row → state probabilities
        last = log_delta[-1]
        last = last - last.max()          # shift for numerical stability
        probs = np.exp(last)
        total = probs.sum()
        return probs / total if total > 0 else np.ones(self.n_states) / self.n_states

    # ── Scaled forward-backward Baum-Welch ───────────────────────────────
    def _baum_welch_scaled(self, obs: np.ndarray):
        T, D = obs.shape

        for _ in range(self.n_iter):
            # ── Forward with scaling ──────────────────────────────────────
            alpha  = np.zeros((T, self.n_states))
            scales = np.zeros(T)

            for s in range(self.n_states):
                alpha[0, s] = self.pi[s] * np.exp(self._log_emission(obs[0], s))
            scales[0] = alpha[0].sum()
            if scales[0] > 0:
                alpha[0] /= scales[0]

            for t in range(1, T):
                for s in range(self.n_states):
                    alpha[t, s] = (
                        np.dot(alpha[t - 1], self.A[:, s])
                        * np.exp(self._log_emission(obs[t], s))
                    )
                scales[t] = alpha[t].sum()
                if scales[t] > 0:
                    alpha[t] /= scales[t]

            # ── Backward with scaling ─────────────────────────────────────
            beta = np.ones((T, self.n_states))
            for t in range(T - 2, -1, -1):
                for s in range(self.n_states):
                    beta[t, s] = np.sum(
                        self.A[s]
                        * np.array([np.exp(self._log_emission(obs[t + 1], j)) for j in range(self.n_states)])
                        * beta[t + 1]
                    )
                if scales[t + 1] > 0:
                    beta[t] /= scales[t + 1]

            # ── Gamma (state occupancy) ───────────────────────────────────
            gamma    = alpha * beta
            row_sums = gamma.sum(axis=1, keepdims=True) + 1e-300
            gamma   /= row_sums

            # ── Update emission params ────────────────────────────────────
            for s in range(self.n_states):
                g            = gamma[:, s:s + 1]
                g_sum        = g.sum() + 1e-300
                self.mu[s]   = (g * obs).sum(axis=0) / g_sum
                diff         = obs - self.mu[s]
                self.sigma[s] = np.sqrt((g * diff ** 2).sum(axis=0) / g_sum) + 1e-6

            # ── Update transition matrix ──────────────────────────────────
            for i in range(self.n_states):
                for j in range(self.n_states):
                    xi_num = 0.0
                    for t in range(T - 1):
                        xi_num += (
                            alpha[t, i]
                            * self.A[i, j]
                            * np.exp(self._log_emission(obs[t + 1], j))
                            * beta[t + 1, j]
                        )
                    self.A[i, j] = xi_num + 1e-300
                row_sum = self.A[i].sum()
                self.A[i] /= row_sum
