"""
Causal multi-horizon features
=============================
Every feature at decision time t is computed only from ticks with epoch <= t
(see ticks.asof_index). Windows are measured in seconds, not ticks, so 1-second
and 2-second indices are treated alike.

  ret_{h}         log return over the last h seconds
  ret_{h}_z       that return in units of recent realized volatility
  rv_{w}          realized volatility (root sum of squared tick returns) over w seconds
  volvol          coefficient of variation of rv_60 sampled each minute over 15 minutes
  accel_60        ret_60 now minus ret_60 one minute ago, in rv units (acceleration / deceleration)
  disp_900        log distance from the 15-minute mean price, in rv_900 units
  er_{w}          efficiency ratio |net move| / path length: ~1 trending, ~0 choppy
  streak          signed length of the current run of same-direction ticks
  rev_freq_300    share of tick-direction reversals over 5 minutes
  intensity_300   ticks per second over 5 minutes (arrival irregularity)
  staleness       seconds since the last tick
  range_pos_900   position inside the 15-minute high-low range (0 = low, 1 = high)
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, Tuple

import numpy as np
import pandas as pd

from qb2.ticks import asof_index

FEATURE_VERSION = "qb2-features-v1"
RETURN_HORIZONS = (10, 30, 60, 120, 300, 900)
RV_WINDOWS = (60, 300, 900)
WARMUP_SECONDS = 1800          # history needed before the first decision (volvol uses 15 min + 15 min)


def make_decision_times(epochs: np.ndarray, interval: int, horizon_seconds: int,
                        warmup_seconds: int = WARMUP_SECONDS) -> np.ndarray:
    """Regular decision grid with enough history before and enough data after each point."""
    if len(epochs) == 0:
        return np.array([], dtype=np.int64)
    first = int(math.ceil((int(epochs[0]) + warmup_seconds) / interval) * interval)
    last = int(epochs[-1]) - horizon_seconds
    if last < first:
        return np.array([], dtype=np.int64)
    return np.arange(first, last + 1, interval, dtype=np.int64)


def _padded_cumsum(x: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(x)])


def _run_lengths(signs: np.ndarray) -> np.ndarray:
    """Signed length of the run of equal non-zero signs ending at each position."""
    n = len(signs)
    if n == 0:
        return np.zeros(0)
    breaks = np.ones(n, dtype=bool)
    breaks[1:] = (signs[1:] != signs[:-1]) | (signs[1:] == 0)
    run_start = np.maximum.accumulate(np.where(breaks, np.arange(n), 0))
    return (np.arange(n) - run_start + 1) * signs


def compute_features(epochs: np.ndarray, quotes: np.ndarray, times: Iterable[float]) -> pd.DataFrame:
    epochs = np.asarray(epochs, dtype=np.int64)
    quotes = np.asarray(quotes, dtype=float)
    t = np.asarray(list(times) if not isinstance(times, np.ndarray) else times, dtype=float)

    logq = np.log(quotes)
    r = np.concatenate([[0.0], np.diff(logq)])
    s = np.sign(r)
    cs2 = _padded_cumsum(r ** 2)
    csabs = _padded_cumsum(np.abs(r))
    csq = _padded_cumsum(quotes)
    rev = np.zeros(len(r))
    rev[1:] = (s[1:] * s[:-1]) < 0
    csrev = _padded_cumsum(rev)
    runs = _run_lengths(s)

    i = asof_index(epochs, t)
    have = i >= 0
    ii = np.clip(i, 0, None)

    def window_index(w: float) -> np.ndarray:
        return asof_index(epochs, t - w)

    def windowed(cs: np.ndarray, j: np.ndarray) -> np.ndarray:
        """Sum over ticks j+1..i; NaN when the window start predates the data."""
        out = cs[ii + 1] - cs[np.clip(j, -1, None) + 1]
        return np.where(have & (j >= 0), out, np.nan)

    out: Dict[str, np.ndarray] = {}
    for h in RETURN_HORIZONS:
        j = window_index(h)
        out[f"ret_{h}"] = np.where(have & (j >= 0), logq[ii] - logq[np.clip(j, 0, None)], np.nan)

    for w in RV_WINDOWS:
        out[f"rv_{w}"] = np.sqrt(windowed(cs2, window_index(w)))

    rv900 = out["rv_900"]
    with np.errstate(divide="ignore", invalid="ignore"):
        for h in (60, 300):
            out[f"ret_{h}_z"] = out[f"ret_{h}"] / (rv900 * math.sqrt(h / 900.0))

        # Volatility of volatility: rv_60 at t, t-60, ..., t-840
        samples = []
        for k in range(15):
            tk = t - 60 * k
            ik, jk = asof_index(epochs, tk), asof_index(epochs, tk - 60)
            v = cs2[np.clip(ik, -1, None) + 1] - cs2[np.clip(jk, -1, None) + 1]
            samples.append(np.where((ik >= 0) & (jk >= 0), np.sqrt(v), np.nan))
        stack = np.vstack(samples)
        out["volvol"] = np.nanstd(stack, axis=0) / np.nanmean(stack, axis=0)
        out["volvol"][np.isnan(stack).any(axis=0)] = np.nan

        j60, j120 = window_index(60), window_index(120)
        prev_ret = np.where(j120 >= 0, logq[np.clip(j60, 0, None)] - logq[np.clip(j120, 0, None)], np.nan)
        out["accel_60"] = (out["ret_60"] - prev_ret) / (rv900 * math.sqrt(60 / 900.0))

        j900 = window_index(900)
        count900 = (ii - j900).astype(float)
        mean900 = windowed(csq, j900) / count900
        out["disp_900"] = (logq[ii] - np.log(mean900)) / rv900

        for w in (300, 900):
            j = window_index(w)
            path = windowed(csabs, j)
            out[f"er_{w}"] = np.abs(out[f"ret_{w}"]) / path

        j300 = window_index(300)
        n300 = (ii - j300).astype(float)
        out["rev_freq_300"] = windowed(csrev, j300) / n300
        out["intensity_300"] = np.where(j300 >= 0, n300 / 300.0, np.nan)

    out["streak"] = np.where(have, runs[ii], np.nan)
    out["staleness"] = np.where(have, t - epochs[ii], np.nan)

    series = pd.Series(quotes, index=pd.to_datetime(epochs, unit="s"))
    hi = series.rolling("900s").max().values
    lo = series.rolling("900s").min().values
    rng = hi[ii] - lo[ii]
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = np.where(rng > 0, (quotes[ii] - lo[ii]) / rng, 0.5)
    out["range_pos_900"] = np.where(have & (window_index(900) >= 0), pos, np.nan)

    df = pd.DataFrame(out, index=pd.Index(t.astype(np.int64), name="decision_time"))
    return df.replace([np.inf, -np.inf], np.nan)


FEATURE_NAMES: Tuple[str, ...] = tuple(
    [f"ret_{h}" for h in RETURN_HORIZONS]
    + [f"rv_{w}" for w in RV_WINDOWS]
    + ["ret_60_z", "ret_300_z", "volvol", "accel_60", "disp_900", "er_300", "er_900",
       "rev_freq_300", "intensity_300", "streak", "staleness", "range_pos_900"]
)

# Scale-free subset used by the models, so a fitted model does not depend on the price level
MODEL_FEATURES: Tuple[str, ...] = (
    "ret_60_z", "ret_300_z", "volvol", "accel_60", "disp_900", "er_300", "er_900",
    "rev_freq_300", "intensity_300", "streak", "range_pos_900",
)


def dataset_hash(epochs: np.ndarray, quotes: np.ndarray, params: Dict[str, Any]) -> str:
    """Fingerprint of the exact raw data and parameters a dataset was built from."""
    h = hashlib.sha256()
    h.update(np.asarray(epochs, dtype=np.int64).tobytes())
    h.update(np.round(np.asarray(quotes, dtype=float), 8).tobytes())
    h.update(json.dumps({"feature_version": FEATURE_VERSION, **params}, sort_keys=True, default=str).encode())
    return h.hexdigest()
