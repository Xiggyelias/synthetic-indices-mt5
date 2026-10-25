"""
Edge Scanner: hunt for real deviations from fair randomness
===========================================================
Deriv publishes how each synthetic index is generated. Any statistically solid
departure from that in the real price history is a genuine, tradeable edge. This
module tests long history for the departures that map onto trades:

  drift         mean log return != 0          → directional bias (hold with the drift)
  variance ratio Lo-MacKinlay VR(q) != 1       → VR < 1 mean reversion, VR > 1 momentum
  runs          sign runs != chance            → too many runs: alternation, too few: streaks
  spike cluster spike counts over-dispersed    → Boom/Crash spikes arrive in bursts

Running many tests over many symbols guarantees some false alarms, so all p-values
from one scan are adjusted together with the Benjamini-Hochberg procedure.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List

import numpy as np
import pandas as pd
from scipy import stats

from quant_brain import detect_spikes


@dataclass
class EdgeTest:
    symbol: str
    test: str
    statistic: float
    p_value: float
    effect: float           # Size of the deviation in the test's natural units
    interpretation: str
    q_value: float = 1.0    # Benjamini-Hochberg adjusted p-value
    significant: bool = False

    def as_dict(self) -> Dict:
        return asdict(self)


def _two_sided(z: float) -> float:
    return float(2.0 * stats.norm.sf(abs(z)))


def drift_test(log_returns: np.ndarray) -> tuple[float, float, float]:
    """(t, p, mean return per bar) for H0: zero mean log return."""
    n = len(log_returns)
    sd = float(np.std(log_returns, ddof=1))
    mean = float(np.mean(log_returns))
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return t, _two_sided(t), mean


def variance_ratio_test(log_prices: np.ndarray, q: int) -> tuple[float, float, float]:
    """
    Lo-MacKinlay variance ratio with the heteroskedasticity-robust z*. Returns (z*, p, VR).
    Under a random walk the variance of q-bar returns is q times the 1-bar variance (VR = 1).
    """
    r = np.diff(log_prices)
    n = len(r)
    mu = float(np.mean(r))
    dev = r - mu
    sigma_a = float(np.sum(dev ** 2)) / (n - 1)
    q_returns = log_prices[q:] - log_prices[:-q] - q * mu
    m = q * (n - q + 1) * (1.0 - q / n)
    sigma_c = float(np.sum(q_returns ** 2)) / m
    vr = sigma_c / sigma_a if sigma_a > 0 else 1.0

    denom = float(np.sum(dev ** 2)) ** 2
    theta = 0.0
    for j in range(1, q):
        delta_j = n * float(np.sum(dev[j:] ** 2 * dev[:-j] ** 2)) / denom if denom > 0 else 0.0
        theta += (2.0 * (q - j) / q) ** 2 * delta_j
    # Var(VR) ≈ θ / n  (θ = 1 at q = 2 for i.i.d. returns, the textbook 1/n)
    z = (vr - 1.0) * math.sqrt(n / theta) if theta > 0 else 0.0
    return z, _two_sided(z), vr


def runs_test(signs: np.ndarray) -> tuple[float, float, float]:
    """Wald-Wolfowitz runs test on ±1 signs. Returns (z, p, observed/expected runs)."""
    signs = signs[signs != 0]
    n = len(signs)
    n1 = int(np.sum(signs > 0))
    n2 = n - n1
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0, 1.0
    runs = 1 + int(np.sum(signs[1:] != signs[:-1]))
    expected = 2.0 * n1 * n2 / n + 1.0
    var = 2.0 * n1 * n2 * (2.0 * n1 * n2 - n) / (n ** 2 * (n - 1))
    z = (runs - expected) / math.sqrt(var) if var > 0 else 0.0
    return z, _two_sided(z), runs / expected


def spike_dispersion_test(is_spike: np.ndarray, block: int = 20) -> tuple[float, float, float]:
    """
    Index of dispersion of spike counts per block. Memoryless spikes give D ≈ 1;
    bursts give D > 1. One-sided chi-square test. Returns (chi2, p, D).
    """
    k = len(is_spike) // block
    counts = is_spike[: k * block].reshape(k, block).sum(axis=1).astype(float)
    mean = float(np.mean(counts))
    if k < 10 or mean <= 0:
        return 0.0, 1.0, 1.0
    d = float(np.var(counts, ddof=1)) / mean
    chi2 = (k - 1) * d
    return chi2, float(stats.chi2.sf(chi2, k - 1)), d


def scan_symbol(df: pd.DataFrame, symbol: str, vr_lags: tuple = (2, 4, 8, 16)) -> List[EdgeTest]:
    """Run every applicable test on one symbol's bars (unadjusted p-values)."""
    closes = df["close"].astype(float).values
    log_p = np.log(closes)
    log_r = np.diff(log_p)
    sym = symbol.upper()
    results: List[EdgeTest] = []

    t, p, mean = drift_test(log_r)
    results.append(EdgeTest(symbol, "drift", t, p, mean,
                            f"{'upward' if mean > 0 else 'downward'} drift of {mean * 1e4:+.2f} bp/bar"))

    is_spiky = "BOOM" in sym or "CRASH" in sym
    if is_spiky:
        # Jumps dominate the variance of Boom/Crash returns; test the drift between spikes instead
        spikes = detect_spikes(df, "CRASH" in sym)
        chi2, p, d = spike_dispersion_test(spikes)
        results.append(EdgeTest(symbol, "spike_clustering", chi2, p, d,
                                f"dispersion {d:.2f} ({'bursts' if d > 1 else 'regular'}; memoryless = 1.00)"))
        calm = log_r[~spikes[1:]]
        z, p, ratio = runs_test(np.sign(calm))
    else:
        for q in vr_lags:
            if len(log_p) > 10 * q:
                z, p, vr = variance_ratio_test(log_p, q)
                results.append(EdgeTest(symbol, f"variance_ratio_{q}", z, p, vr,
                                        f"VR({q})={vr:.3f} ({'mean reversion' if vr < 1 else 'momentum'})"))
        z, p, ratio = runs_test(np.sign(log_r))

    results.append(EdgeTest(symbol, "runs", z, p, ratio,
                            f"runs/expected={ratio:.3f} ({'alternation' if ratio > 1 else 'streaks'})"))
    return results


def benjamini_hochberg(tests: List[EdgeTest], alpha: float = 0.05) -> List[EdgeTest]:
    """Adjust all p-values in place for the false discovery rate and flag discoveries."""
    m = len(tests)
    if m == 0:
        return tests
    order = sorted(range(m), key=lambda i: tests[i].p_value)
    q_running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        q_running = min(q_running, tests[i].p_value * m / rank)
        tests[i].q_value = q_running
        tests[i].significant = q_running <= alpha  # BH accepts p_(k) <= (k / m) * alpha
    return tests


def scan(frames: Dict[str, pd.DataFrame], alpha: float = 0.05) -> List[EdgeTest]:
    """Scan several symbols and apply one false-discovery correction across everything."""
    tests: List[EdgeTest] = []
    for symbol, df in frames.items():
        if df is not None and len(df) >= 200:
            tests.extend(scan_symbol(df, symbol))
    return benjamini_hochberg(tests, alpha)
