"""
Statistical pattern discovery
=============================
Each pattern is a hypothesis "P(Rise | condition) differs from P(Rise)":

  - streaks of k same-direction ticks (k = 3..8, both directions)
  - extreme recent moves (event study: 1- and 5-minute returns beyond the
    training 5% / 95% quantiles)
  - displacement from the 15-minute mean, range position, trending vs choppy
  - volatility states
plus serial-dependence tests (Ljung-Box on tick returns and on squared returns
for volatility clustering).

Rules that keep this honest:
  - quantile thresholds come from the training part only; tests run on the rest
  - only non-overlapping decisions are counted, so the binomial test's independence
    assumption holds (5-minute labels overlap 1-minute decisions)
  - conditions with fewer than `min_samples` cases are reported, never tested
  - every p-value in one run is corrected together (Holm for family-wise error,
    Benjamini-Hochberg for false discovery rate)
"""

from __future__ import annotations

from typing import Callable, Dict, List

import numpy as np
import pandas as pd
from scipy import stats

from qb2.stats_utils import benjamini_hochberg, binomial_p_value, holm


def ljung_box(x: np.ndarray, lags: int = 10) -> Dict[str, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)] - np.nanmean(x)
    n = len(x)
    if n <= lags + 1:
        return {"q": 0.0, "p_value": 1.0, "lags": lags}
    denom = float(np.sum(x ** 2))
    acf = np.array([float(np.sum(x[k:] * x[:-k])) / denom for k in range(1, lags + 1)])
    q = n * (n + 2) * float(np.sum(acf ** 2 / (n - np.arange(1, lags + 1))))
    return {"q": q, "p_value": float(stats.chi2.sf(q, lags)), "lags": lags,
            "acf": [round(float(a), 5) for a in acf]}


def nonoverlap_mask(times: np.ndarray, duration_seconds: int) -> np.ndarray:
    """Greedy subset of decision times at least one contract duration apart."""
    keep = np.zeros(len(times), dtype=bool)
    next_ok = -np.inf
    for i in np.argsort(times):
        if times[i] >= next_ok:
            keep[i] = True
            next_ok = times[i] + duration_seconds
    return keep


def default_conditions(train: pd.DataFrame) -> Dict[str, Callable[[pd.DataFrame], np.ndarray]]:
    q = lambda col, p: float(np.nanquantile(train[col].values, p))  # noqa: E731
    conds: Dict[str, Callable[[pd.DataFrame], np.ndarray]] = {}
    for k in range(3, 9):
        conds[f"streak_up>={k}"] = lambda f, k=k: f["streak"].values >= k
        conds[f"streak_down>={k}"] = lambda f, k=k: f["streak"].values <= -k
    for col in ("ret_60_z", "ret_300_z", "disp_900", "accel_60"):
        lo, hi = q(col, 0.05), q(col, 0.95)
        conds[f"{col}<p5"] = lambda f, c=col, v=lo: f[c].values < v
        conds[f"{col}>p95"] = lambda f, c=col, v=hi: f[c].values > v
    rp_lo, rp_hi = 0.1, 0.9
    conds["range_pos<0.1"] = lambda f: f["range_pos_900"].values < rp_lo
    conds["range_pos>0.9"] = lambda f: f["range_pos_900"].values > rp_hi
    er_hi, er_lo = q("er_900", 0.9), q("er_900", 0.1)
    conds["trending(er_900>p90)"] = lambda f, v=er_hi: f["er_900"].values > v
    conds["choppy(er_900<p10)"] = lambda f, v=er_lo: f["er_900"].values < v
    vol_hi, vol_lo = q("rv_300", 0.9), q("rv_300", 0.1)
    conds["highvol(rv_300>p90)"] = lambda f, v=vol_hi: f["rv_300"].values > v
    conds["lowvol(rv_300<p10)"] = lambda f, v=vol_lo: f["rv_300"].values < v
    return conds


def discover_patterns(
    features: pd.DataFrame,
    outcome: np.ndarray,
    duration_seconds: int = 300,
    train_fraction: float = 0.5,
    min_samples: int = 200,
    alpha: float = 0.05,
) -> Dict[str, object]:
    """Condition thresholds from the first `train_fraction`; tests on the rest."""
    outcome = np.asarray(outcome, float)
    times = features.index.values.astype(float)
    valid = np.isfinite(outcome)
    cut = int(len(features) * train_fraction)
    train, test = features.iloc[:cut], features.iloc[cut:]
    y_test = outcome[cut:]
    keep = nonoverlap_mask(times[cut:], duration_seconds) & valid[cut:]
    rise = (y_test == 1)

    base_n = int(keep.sum())
    base_rate = float(np.mean(rise[keep])) if base_n else 0.5
    rows: List[Dict[str, object]] = []
    for name, cond in default_conditions(train).items():
        mask = np.asarray(cond(test), dtype=bool) & keep
        n = int(mask.sum())
        row: Dict[str, object] = {"pattern": name, "n": n, "tested": n >= min_samples}
        if n:
            wins = int(rise[mask].sum())
            row["p_rise"] = wins / n
            row["effect"] = wins / n - base_rate
            row["p_value"] = binomial_p_value(wins, n, base_rate) if n >= min_samples else None
        rows.append(row)

    tested = [r for r in rows if r["tested"]]
    if tested:
        pv = [float(r["p_value"]) for r in tested]
        for r, h, q in zip(tested, holm(pv), benjamini_hochberg(pv)):
            r["p_holm"], r["q_bh"] = float(h), float(q)
            r["significant_fwer"] = h <= alpha
            r["significant_fdr"] = q <= alpha
    return {
        "base_rate": base_rate,
        "nonoverlapping_test_samples": base_n,
        "tests_run": len(tested),
        "patterns": sorted(rows, key=lambda r: (r.get("p_value") is None, r.get("p_value") or 1.0)),
        "discoveries_fdr": [r["pattern"] for r in tested if r.get("significant_fdr")],
        "discoveries_fwer": [r["pattern"] for r in tested if r.get("significant_fwer")],
    }


def event_study(epochs: np.ndarray, quotes: np.ndarray, threshold_sigma: float = 4.0,
                horizons: tuple = (10, 30, 60, 300)) -> List[Dict[str, float]]:
    """
    Forward log returns after extreme single-tick moves, in units of tick sigma.
    A tradeable reaction would show a significant mean (t-test) in the reversal or
    continuation direction.
    """
    epochs = np.asarray(epochs, np.int64)
    lq = np.log(np.asarray(quotes, float))
    r = np.diff(lq)
    sd = float(np.std(r)) or 1e-12
    events = np.flatnonzero(np.abs(r) > threshold_sigma * sd) + 1
    rows = []
    for h in horizons:
        j = np.searchsorted(epochs, epochs[events] + h, side="right") - 1
        ok = j > events
        fwd = (lq[j[ok]] - lq[events[ok]]) * np.sign(r[events[ok] - 1])  # + = continuation
        n = len(fwd)
        t = float(np.mean(fwd) / (np.std(fwd, ddof=1) / np.sqrt(n))) if n > 2 and np.std(fwd) > 0 else 0.0
        rows.append({"horizon_seconds": h, "events": n, "mean_continuation_sigma": float(np.mean(fwd) / sd) if n else 0.0,
                     "t_stat": t, "p_value": float(2 * stats.t.sf(abs(t), max(n - 1, 1))) if n > 2 else 1.0})
    return rows
