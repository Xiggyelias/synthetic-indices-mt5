"""
Statistics shared by research, validation and backtesting
=========================================================
Multiple-testing corrections, block bootstrap, probability scoring and
trade-sequence risk metrics.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats


# ── Multiple testing ─────────────────────────────────────────────────────────

def holm(p_values: Sequence[float]) -> np.ndarray:
    """Holm-Bonferroni adjusted p-values (family-wise error control)."""
    p = np.asarray(p_values, dtype=float)
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adj[i] = running
    return adj


def benjamini_hochberg(p_values: Sequence[float]) -> np.ndarray:
    """Benjamini-Hochberg q-values (false discovery rate control)."""
    p = np.asarray(p_values, dtype=float)
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p)
    q = np.empty(m)
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p[i] * m / rank)
        q[i] = running
    return q


# ── Bootstrap ────────────────────────────────────────────────────────────────

def block_bootstrap_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Moving-block bootstrap resample of 0..n-1 (keeps short-range dependence)."""
    block = max(1, min(block, n))
    starts = rng.integers(0, n - block + 1, size=int(math.ceil(n / block)))
    return (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]


def bootstrap_ci(
    values: Sequence[float] | np.ndarray,
    stat: Callable[[np.ndarray], float] = np.mean,
    n_boot: int = 1000,
    alpha: float = 0.05,
    block: int = 1,
    seed: int = 0,
    paired: Optional[np.ndarray] = None,
) -> Tuple[float, float, float]:
    """(estimate, lower, upper) percentile CI. With `paired`, stat receives (values, paired)."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)

    def f(idx: np.ndarray) -> float:
        return float(stat(x[idx], paired[idx])) if paired is not None else float(stat(x[idx]))

    est = f(np.arange(n))
    boots = np.array([f(block_bootstrap_indices(n, block, rng)) for _ in range(n_boot)])
    boots = boots[np.isfinite(boots)]
    if len(boots) == 0:
        return est, float("nan"), float("nan")
    return est, float(np.quantile(boots, alpha / 2)), float(np.quantile(boots, 1 - alpha / 2))


# ── Probability scoring ──────────────────────────────────────────────────────

def brier(y: np.ndarray, p: np.ndarray) -> float:
    y, p = np.asarray(y, float), np.asarray(p, float)
    return float(np.mean((p - y) ** 2)) if len(y) else float("nan")


def log_loss(y: np.ndarray, p: np.ndarray, eps: float = 1e-6) -> float:
    y, p = np.asarray(y, float), np.clip(np.asarray(p, float), eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(y) else float("nan")


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 10) -> List[Dict[str, float]]:
    """Reliability table over equal-count bins of predicted probability."""
    y, p = np.asarray(y, float), np.asarray(p, float)
    if len(y) == 0:
        return []
    order = np.argsort(p)
    rows = []
    for chunk in np.array_split(order, min(bins, len(y))):
        if len(chunk) == 0:
            continue
        rows.append({
            "n": int(len(chunk)),
            "mean_predicted": float(np.mean(p[chunk])),
            "observed_rate": float(np.mean(y[chunk])),
        })
    return rows


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    table = calibration_table(y, p, bins)
    n = sum(r["n"] for r in table)
    return float(sum(r["n"] * abs(r["mean_predicted"] - r["observed_rate"]) for r in table) / n) if n else float("nan")


def binomial_p_value(successes: int, n: int, p0: float) -> float:
    if n <= 0:
        return 1.0
    return float(stats.binomtest(int(successes), int(n), p0).pvalue)


# ── Trade-sequence metrics ───────────────────────────────────────────────────

def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a fraction of the peak."""
    eq = np.asarray(equity, dtype=float)
    if len(eq) == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
    return float(np.max(dd))


def longest_losing_streak(pnl: Sequence[float]) -> int:
    best = cur = 0
    for v in pnl:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def trade_metrics(pnl: Sequence[float], stakes: Sequence[float], initial_equity: float) -> Dict[str, float]:
    """Expectancy, profit factor, drawdown, streaks and risk-adjusted return of settled trades."""
    pnl = np.asarray(pnl, dtype=float)
    stakes = np.asarray(stakes, dtype=float)
    n = len(pnl)
    if n == 0:
        return {"trades": 0, "net_profit": 0.0, "expectancy": 0.0, "expectancy_per_stake": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0, "longest_losing_streak": 0,
                "downside_deviation": 0.0, "sharpe_per_trade": 0.0, "sortino_per_trade": 0.0}
    ret = pnl / np.where(stakes > 0, stakes, 1.0)
    gains, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    downside = ret[ret < 0]
    dd_dev = float(np.sqrt(np.mean(np.minimum(ret, 0.0) ** 2)))
    sd = float(np.std(ret, ddof=1)) if n > 1 else 0.0
    equity = initial_equity + np.concatenate([[0.0], np.cumsum(pnl)])
    return {
        "trades": int(n),
        "net_profit": float(pnl.sum()),
        "expectancy": float(pnl.mean()),
        "expectancy_per_stake": float(ret.mean()),
        "win_rate": float(np.mean(pnl > 0)),
        "profit_factor": float(gains / losses) if losses > 0 else (float("inf") if gains > 0 else 0.0),
        "max_drawdown": max_drawdown(equity),
        "longest_losing_streak": longest_losing_streak(pnl),
        "downside_deviation": dd_dev,
        "sharpe_per_trade": float(ret.mean() / sd) if sd > 0 else 0.0,
        "sortino_per_trade": float(ret.mean() / dd_dev) if dd_dev > 0 and len(downside) else 0.0,
    }
