"""
Contract-aware Rise/Fall settlement
===================================
Labels are built to match how a Deriv Rise/Fall contract settles, not "price
300 ticks later":

  start time   the moment the purchase is accepted (decision time + latency)
  expiry       start time + duration in seconds (5 minutes = 300 s, which is ~150
               ticks on a 2-second index and ~300 on a 1-second index)
  entry spot   the first tick strictly after the start time
  exit spot    the last tick at or before expiry
  outcome      +1 exit > entry (CALL wins), -1 exit < entry (PUT wins),
                0 tie (both lose)

This mirrors Deriv's published description of the contract, but it is an
assumption for simulation. Paper results use it; demo results always use the
entry/exit spots reported by proposal_open_contract.

A label is void (NaN) when the entry tick arrives too late, the exit tick is
stale (a data gap spans expiry), or the data ends before expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np


@dataclass(frozen=True)
class SettlementRule:
    duration_seconds: int = 300
    max_entry_wait_seconds: float = 10.0
    max_exit_staleness_seconds: float = 10.0


def settle(
    epochs: np.ndarray,
    quotes: np.ndarray,
    start_times: np.ndarray,
    rule: SettlementRule = SettlementRule(),
) -> Dict[str, np.ndarray]:
    epochs = np.asarray(epochs, dtype=np.int64)
    quotes = np.asarray(quotes, dtype=float)
    start = np.asarray(start_times, dtype=float)
    expiry = start + rule.duration_seconds
    n = len(start)

    entry_idx = np.searchsorted(epochs, start, side="right")           # first tick > start
    exit_idx = np.searchsorted(epochs, expiry, side="right") - 1       # last tick <= expiry

    valid = (entry_idx < len(epochs)) & (exit_idx >= 0)
    ei = np.clip(entry_idx, 0, max(len(epochs) - 1, 0))
    xi = np.clip(exit_idx, 0, max(len(epochs) - 1, 0))
    if len(epochs):
        valid &= (epochs[ei] - start) <= rule.max_entry_wait_seconds
        valid &= (expiry - epochs[xi]) <= rule.max_exit_staleness_seconds
        valid &= xi > ei
        valid &= epochs[-1] >= expiry          # no later tick could still land before expiry
    else:
        valid[:] = False

    entry_q = np.where(valid, quotes[ei] if len(quotes) else np.nan, np.nan)
    exit_q = np.where(valid, quotes[xi] if len(quotes) else np.nan, np.nan)
    outcome = np.full(n, np.nan)
    outcome[valid] = np.sign(exit_q[valid] - entry_q[valid])
    return {
        "entry_epoch": np.where(valid, epochs[ei] if len(epochs) else 0, 0),
        "exit_epoch": np.where(valid, epochs[xi] if len(epochs) else 0, 0),
        "entry_quote": entry_q,
        "exit_quote": exit_q,
        "expiry": expiry,
        "outcome": outcome,
        "valid": valid,
    }
