"""
HMM Regime Detector — Hidden Markov Model for market regime classification.
Uses price returns + volatility features to detect:
  State 0: Bull / Low Volatility   → all agents active
  State 1: Choppy / Mean-Reverting → options + mean reversion only
  State 2: Bear / High Volatility  → pairs trading only, no options

Complements the Claude AI regime detector:
  - HMM: fast, data-driven, no API cost (runs on every bar)
  - Claude: deep reasoning, event-aware, runs once per day
"""

import numpy as np
from enum import Enum
from loguru import logger


class HMMState(Enum):
    BULL_LOW_VOL  = 0
    CHOPPY        = 1
    BEAR_HIGH_VOL = 2


# Maps HMM state → agent allocation multipliers
# mean_reversion reduced in BULL_LOW_VOL to avoid over-concentration in mean-rev strategies
# momentum_scalper only in BULL_LOW_VOL (needs calm trending market)
HMM_ALLOCATIONS = {
    HMMState.BULL_LOW_VOL:  {"pairs_trading": 1.0, "mean_reversion": 0.8, "momentum": 1.0, "momentum_scalper": 1.0, "options_bot": 1.0},
    HMMState.CHOPPY:        {"pairs_trading": 1.0, "mean_reversion": 1.0, "momentum": 0.3, "momentum_scalper": 0.0, "options_bot": 0.8},
    HMMState.BEAR_HIGH_VOL: {"pairs_trading": 0.7, "mean_reversion": 0.0, "momentum": 0.0, "momentum_scalper": 0.0, "options_bot": 0.0},
}


class HMMRegimeDetector:
    """
    Gaussian HMM implemented from scratch (no hmmlearn dependency).
    Trained on: daily returns + 5-day realized volatility.
    """

    def __init__(self, n_states: int = 3, n_iter: int = 100):
        self.n_states   = n_states
        self.n_iter     = n_iter
        self.is_trained = False

        # Model parameters (initialized, updated via Baum-Welch)
        self.pi    = np.array([0.6, 0.3, 0.1])                          # Initial state probs
        self.A     = np.array([[0.9, 0.08, 0.02],                        # Transition matrix
                                [0.1, 0.85, 0.05],
                                [0.05, 0.1, 0.85]])
        self.mu    = np.array([[0.05, 0.008],                            # Emission means [return, vol]
                                [0.0,  0.015],
                                [-0.04, 0.03]])
        self.sigma = np.array([[0.005, 0.003],                           # Emission std
                                [0.008, 0.005],
                                [0.015, 0.01]])
        self.current_state = HMMState.BULL_LOW_VOL

    # ── Train on historical data ──────────────────────────────────────────
    def fit(self, closes: list) -> None:
        """
        Train on daily close prices.
        Minimum 60 days recommended.
        """
        if len(closes) < 30:
            logger.warning("HMM: insufficient data for training")
            return

        obs = self._extract_features(closes)
        self._baum_welch(obs)
        self.is_trained = True
        logger.info(f"HMM trained | {len(closes)} days | states={self.n_states}")

    # ── Predict current regime ────────────────────────────────────────────
    def predict(self, closes: list) -> tuple[HMMState, float]:
        """
        Returns (current_state, confidence).
        Confidence = probability of the most likely state.
        """
        if len(closes) < 10:
            return HMMState.BULL_LOW_VOL, 0.5

        obs = self._extract_features(closes)
        state_probs = self._viterbi_last_state(obs)
        best_state_idx = int(np.argmax(state_probs))
        confidence     = float(state_probs[best_state_idx])

        state_map = {0: HMMState.BULL_LOW_VOL, 1: HMMState.CHOPPY, 2: HMMState.BEAR_HIGH_VOL}
        self.current_state = state_map[best_state_idx]

        logger.debug(f"HMM regime: {self.current_state.name} | confidence={confidence:.2f}")
        return self.current_state, confidence

    def get_allocations(self) -> dict:
        return HMM_ALLOCATIONS[self.current_state]

    # ── Features ─────────────────────────────────────────────────────────
    @staticmethod
    def _extract_features(closes: list) -> np.ndarray:
        c = np.array(closes, dtype=float)
        returns = np.diff(c) / c[:-1]
        vol     = np.array([np.std(returns[max(0, i-5):i+1]) for i in range(len(returns))])
        return np.column_stack([returns, vol])

    # ── Gaussian emission probability ─────────────────────────────────────
    def _emission_prob(self, obs: np.ndarray, state: int) -> np.ndarray:
        mu    = self.mu[state]
        sigma = self.sigma[state]
        diff  = obs - mu
        exponent = -0.5 * np.sum((diff / sigma) ** 2, axis=1)
        norm     = np.prod(np.sqrt(2 * np.pi) * sigma)
        return np.exp(exponent) / norm + 1e-300   # avoid zero

    # ── Viterbi: last state probabilities ────────────────────────────────
    def _viterbi_last_state(self, obs: np.ndarray) -> np.ndarray:
        T = len(obs)
        delta = np.zeros((T, self.n_states))
        delta[0] = self.pi * self._emission_prob(obs[:1], 0)  # placeholder

        for s in range(self.n_states):
            delta[0, s] = self.pi[s] * self._emission_prob(obs[0:1], s)[0]

        for t in range(1, T):
            for s in range(self.n_states):
                ep = self._emission_prob(obs[t:t+1], s)[0]
                delta[t, s] = np.max(delta[t-1] * self.A[:, s]) * ep

        # Normalize
        last = delta[-1]
        total = last.sum()
        return last / total if total > 0 else np.ones(self.n_states) / self.n_states

    # ── Baum-Welch (simplified EM) ────────────────────────────────────────
    def _baum_welch(self, obs: np.ndarray):
        T, D = obs.shape
        for iteration in range(self.n_iter):
            # Forward pass
            alpha = np.zeros((T, self.n_states))
            for s in range(self.n_states):
                alpha[0, s] = self.pi[s] * self._emission_prob(obs[0:1], s)[0]
            for t in range(1, T):
                for s in range(self.n_states):
                    alpha[t, s] = np.dot(alpha[t-1], self.A[:, s]) * self._emission_prob(obs[t:t+1], s)[0]

            # Backward pass
            beta = np.ones((T, self.n_states))
            for t in range(T - 2, -1, -1):
                for s in range(self.n_states):
                    beta[t, s] = np.sum(self.A[s] * self._emission_prob(obs[t+1:t+2], np.arange(self.n_states)).flatten() * beta[t+1])

            # Gamma (state occupancy)
            gamma = alpha * beta
            row_sums = gamma.sum(axis=1, keepdims=True) + 1e-300
            gamma /= row_sums

            # Update means and stds
            for s in range(self.n_states):
                g = gamma[:, s:s+1]
                self.mu[s]    = (g * obs).sum(axis=0) / (g.sum() + 1e-300)
                diff          = obs - self.mu[s]
                self.sigma[s] = np.sqrt((g * diff**2).sum(axis=0) / (g.sum() + 1e-300)) + 1e-6

            # Update transition matrix
            for i in range(self.n_states):
                for j in range(self.n_states):
                    xi_num = sum(
                        alpha[t, i] * self.A[i, j] *
                        self._emission_prob(obs[t+1:t+2], j)[0] * beta[t+1, j]
                        for t in range(T - 1)
                    )
                    self.A[i, j] = xi_num + 1e-300
                row_sum = self.A[i].sum()
                self.A[i] /= row_sum
