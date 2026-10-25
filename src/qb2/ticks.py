"""
Tick helpers and a clearly labelled simulator
=============================================
`asof_index` is the single place where "information available at time t" is
defined: the last tick with epoch <= t. Features and labels both use it.

The simulator produces Deriv-style Volatility Index ticks (GBM, constant
volatility, one tick per 2 s or 1 s). It exists for tests and offline dry runs
and is always recorded with source="simulator". It must never be used as
evidence that a strategy works on the real market.
"""

from __future__ import annotations

import math
import re
from typing import Optional

import numpy as np
import pandas as pd

SECONDS_PER_YEAR = 365 * 24 * 3600


def asof_index(epochs: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Index of the last tick with epoch <= t for each t (-1 when none exists)."""
    return np.searchsorted(epochs, np.asarray(times), side="right") - 1


def asof_prices(epochs: np.ndarray, quotes: np.ndarray, times: np.ndarray) -> np.ndarray:
    idx = asof_index(epochs, times)
    out = np.full(len(idx), np.nan)
    ok = idx >= 0
    out[ok] = quotes[idx[ok]]
    return out


def volatility_from_symbol(symbol: str) -> tuple[float, int]:
    """(annualized vol, seconds per tick) for R_75 / 1HZ75V / 'Volatility 75 (1s) Index'."""
    s = symbol.upper()
    one_second = "1HZ" in s or "(1S)" in s
    m = re.search(r"(\d+)", s.replace("1HZ", "").replace("(1S)", ""))
    vol = int(m.group(1)) / 100.0 if m else 1.0
    return vol, 1 if one_second else 2


def simulate_volatility_ticks(
    symbol: str = "R_100",
    seconds: int = 86400,
    seed: Optional[int] = 0,
    start_epoch: int = 1_700_000_000,
    start_price: float = 1000.0,
    ar1: float = 0.0,
    ou_ratio: float = 0.0,
    ou_half_life_seconds: float = 180.0,
) -> pd.DataFrame:
    """
    Simulated ticks: log returns ~ N(-σ²dt/2, σ²dt).
    Artificial edges, used only by tests to prove the pipeline can find real structure:
      ar1       serial correlation in tick returns (too short-lived to matter at 5 minutes)
      ou_ratio  adds an Ornstein-Uhlenbeck component whose stationary std is
                ou_ratio x the 5-minute random-walk std, so displacement predicts reversal
    """
    rng = np.random.default_rng(seed)
    vol, step = volatility_from_symbol(symbol)
    n = seconds // step
    dt = step / SECONDS_PER_YEAR
    shocks = rng.normal(0.0, vol * math.sqrt(dt), n)
    if ar1:
        from scipy.signal import lfilter
        shocks = lfilter([1.0], [1.0, -ar1], shocks) * math.sqrt(1 - ar1 ** 2)
    log_p = np.cumsum(shocks - 0.5 * vol ** 2 * dt)
    if ou_ratio:
        from scipy.signal import lfilter
        a = math.exp(-math.log(2) * step / ou_half_life_seconds)
        stat_sd = ou_ratio * vol * math.sqrt(300 / SECONDS_PER_YEAR)
        ou = lfilter([1.0], [1.0, -a], rng.normal(0.0, stat_sd * math.sqrt(1 - a ** 2), n))
        log_p = log_p + ou
    quotes = start_price * np.exp(log_p)
    epochs = start_epoch + np.arange(n, dtype=np.int64) * step
    return pd.DataFrame({"epoch": epochs, "quote": np.round(quotes, 4)})
