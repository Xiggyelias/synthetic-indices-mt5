"""
Regime detection
================
  RegimeClassifier   volatility state (LOW / NORMAL / HIGH from training quantiles of
                     rv_300) crossed with trend state (TREND / RANGE from training
                     quantiles of the 15-minute efficiency ratio). Thresholds are
                     learned on training data only.
  transition_matrix  empirical regime transition probabilities
  cusum_change_points two-sided CUSUM on a standardized series (e.g. log realized
                     variance) for change-point detection
  GaussianHMM        small 1-D Gaussian hidden Markov model fitted by Baum-Welch.
                     Only filtered (forward) probabilities are exposed, which use
                     data up to t and never the future.

Deriv documents its Volatility Indices as constant-volatility processes, so on
real data these detectors are expected to find little; that is itself a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from scipy.special import logsumexp


@dataclass
class RegimeClassifier:
    vol_feature: str = "rv_300"
    trend_feature: str = "er_900"
    vol_cuts: Tuple[float, float] = (0.0, 0.0)
    trend_cut: float = 0.0
    fitted: bool = False

    def fit(self, features: pd.DataFrame) -> "RegimeClassifier":
        v = features[self.vol_feature].dropna().values
        e = features[self.trend_feature].dropna().values
        if len(v) < 30 or len(e) < 30:
            raise ValueError("Not enough rows to fit regime thresholds")
        self.vol_cuts = (float(np.quantile(v, 1 / 3)), float(np.quantile(v, 2 / 3)))
        self.trend_cut = float(np.quantile(e, 0.75))
        self.fitted = True
        return self

    def transform(self, features: pd.DataFrame) -> np.ndarray:
        if not self.fitted:
            raise RuntimeError("RegimeClassifier.fit must run on training data first")
        v = features[self.vol_feature].values
        e = features[self.trend_feature].values
        vol = np.where(v <= self.vol_cuts[0], "LOWVOL", np.where(v <= self.vol_cuts[1], "NORMALVOL", "HIGHVOL"))
        trend = np.where(e >= self.trend_cut, "TREND", "RANGE")
        labels = np.char.add(np.char.add(trend.astype(str), "_"), vol.astype(str))
        return np.where(np.isfinite(v) & np.isfinite(e), labels, "UNKNOWN")

    def as_dict(self) -> Dict[str, object]:
        return {"vol_feature": self.vol_feature, "trend_feature": self.trend_feature,
                "vol_cuts": list(self.vol_cuts), "trend_cut": self.trend_cut}


def transition_matrix(labels: np.ndarray) -> pd.DataFrame:
    labels = np.asarray(labels)
    states = sorted(set(labels.tolist()))
    counts = pd.DataFrame(0.0, index=states, columns=states)
    for a, b in zip(labels[:-1], labels[1:]):
        counts.loc[a, b] += 1
    return counts.div(counts.sum(axis=1).replace(0, 1), axis=0)


def cusum_change_points(x: np.ndarray, threshold: float = 8.0, drift: float = 0.5,
                        calibration: int = 100) -> List[int]:
    """
    Two-sided CUSUM. The series is standardized with the mean/std of the first
    `calibration` points after each detected change (or the start), then
    S+ = max(0, S+ + z - drift), S- = max(0, S- - z - drift); alarm when either
    exceeds `threshold`. Returns alarm indices.
    """
    x = np.asarray(x, float)
    changes: List[int] = []
    start = 0
    while start + calibration < len(x):
        ref = x[start:start + calibration]
        mu, sd = float(np.mean(ref)), float(np.std(ref)) or 1.0
        s_pos = s_neg = 0.0
        alarm = None
        for i in range(start + calibration, len(x)):
            z = (x[i] - mu) / sd
            s_pos = max(0.0, s_pos + z - drift)
            s_neg = max(0.0, s_neg - z - drift)
            if s_pos > threshold or s_neg > threshold:
                alarm = i
                break
        if alarm is None:
            break
        changes.append(alarm)
        start = alarm
    return changes


@dataclass
class GaussianHMM:
    n_states: int = 2
    n_iter: int = 50
    tol: float = 1e-4
    seed: int = 0
    means: np.ndarray = field(default_factory=lambda: np.zeros(0))
    variances: np.ndarray = field(default_factory=lambda: np.zeros(0))
    trans: np.ndarray = field(default_factory=lambda: np.zeros(0))
    start: np.ndarray = field(default_factory=lambda: np.zeros(0))
    log_likelihood: float = float("-inf")

    def _log_emission(self, x: np.ndarray) -> np.ndarray:
        var = np.maximum(self.variances, 1e-12)
        return -0.5 * (np.log(2 * np.pi * var)[None, :] + (x[:, None] - self.means[None, :]) ** 2 / var[None, :])

    def _forward(self, log_b: np.ndarray) -> Tuple[np.ndarray, float]:
        T, K = log_b.shape
        log_a = np.log(np.maximum(self.trans, 1e-300))
        alpha = np.empty((T, K))
        alpha[0] = np.log(np.maximum(self.start, 1e-300)) + log_b[0]
        for t in range(1, T):
            alpha[t] = logsumexp(alpha[t - 1][:, None] + log_a, axis=0) + log_b[t]
        return alpha, float(logsumexp(alpha[-1]))

    def fit(self, x: np.ndarray) -> "GaussianHMM":
        x = np.asarray(x, float)
        x = x[np.isfinite(x)]
        K, T = self.n_states, len(x)
        if T < 10 * K:
            raise ValueError("Too few observations for an HMM")
        qs = np.quantile(x, np.linspace(0.1, 0.9, K))
        self.means = qs.copy()
        self.variances = np.full(K, np.var(x))
        self.trans = np.full((K, K), 0.1 / max(K - 1, 1))
        np.fill_diagonal(self.trans, 0.9)
        self.start = np.full(K, 1.0 / K)
        log_a_prev = float("-inf")
        for _ in range(self.n_iter):
            log_b = self._log_emission(x)
            alpha, ll = self._forward(log_b)
            log_a = np.log(np.maximum(self.trans, 1e-300))
            beta = np.zeros((T, K))
            for t in range(T - 2, -1, -1):
                beta[t] = logsumexp(log_a + (log_b[t + 1] + beta[t + 1])[None, :], axis=1)
            gamma = np.exp(alpha + beta - ll)
            xi = np.zeros((K, K))
            for t in range(T - 1):
                m = alpha[t][:, None] + log_a + (log_b[t + 1] + beta[t + 1])[None, :] - ll
                xi += np.exp(m)
            self.start = gamma[0] / gamma[0].sum()
            self.trans = xi / np.maximum(xi.sum(axis=1, keepdims=True), 1e-300)
            w = gamma.sum(axis=0)
            self.means = (gamma * x[:, None]).sum(axis=0) / np.maximum(w, 1e-300)
            self.variances = (gamma * (x[:, None] - self.means[None, :]) ** 2).sum(axis=0) / np.maximum(w, 1e-300)
            self.log_likelihood = ll
            if abs(ll - log_a_prev) < self.tol * max(1.0, abs(ll)):
                break
            log_a_prev = ll
        order = np.argsort(self.variances)           # state 0 = calmest
        self.means, self.variances = self.means[order], self.variances[order]
        self.trans = self.trans[np.ix_(order, order)]
        self.start = self.start[order]
        return self

    def filter(self, x: np.ndarray) -> np.ndarray:
        """P(state_t | x_1..x_t) for each t: causal, safe to use as a live feature."""
        alpha, _ = self._forward(self._log_emission(np.asarray(x, float)))
        return np.exp(alpha - logsumexp(alpha, axis=1, keepdims=True))

    def bic(self, n_obs: int) -> float:
        k = self.n_states
        n_params = (k - 1) + k * (k - 1) + 2 * k
        return -2 * self.log_likelihood + n_params * np.log(max(n_obs, 1))


def hmm_state_count_by_bic(x: np.ndarray, max_states: int = 3, seed: int = 0) -> Dict[str, object]:
    """Fit 1..max_states HMMs and report BIC; a single state winning means no regime structure."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    out: Dict[str, object] = {"bic": {}}
    best_k, best_bic = 1, float("inf")
    for k in range(1, max_states + 1):
        try:
            m = GaussianHMM(n_states=k, seed=seed).fit(x)
        except ValueError:
            continue
        b = m.bic(len(x))
        out["bic"][k] = b
        if b < best_bic:
            best_k, best_bic = k, b
    out["best_states"] = best_k
    return out


def regime_performance(regimes: np.ndarray, returns: np.ndarray, min_trades: int = 30) -> List[Dict[str, object]]:
    """Mean trade return per regime (regimes and returns aligned per trade)."""
    rows = []
    for r in sorted(set(np.asarray(regimes).tolist())):
        mask = np.asarray(regimes) == r
        n = int(mask.sum())
        rows.append({"regime": r, "trades": n,
                     "mean_return": float(np.mean(returns[mask])) if n else 0.0,
                     "enough_data": n >= min_trades})
    return rows


def realized_variance_series(rv: np.ndarray) -> np.ndarray:
    """log realized variance: closer to Gaussian, better suited to CUSUM and HMM."""
    rv = np.asarray(rv, float)
    with np.errstate(divide="ignore"):
        return np.log(np.where(rv > 0, rv ** 2, np.nan))
