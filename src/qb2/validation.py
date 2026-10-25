"""
Anti-overfitting laboratory
===========================
  chronological_partition  development / final-test split with purging and embargo
  walk_forward_folds       expanding-window folds inside the development set; any
                           training row whose label ends at or after the test start
                           is purged (5-minute labels overlap 1-minute decisions)
  walk_forward_predict     out-of-sample predictions only; rows never predicted stay NaN
  probability_report       Brier, log loss, calibration table, ECE
  paired_brier_improvement bootstrap CI of the Brier improvement over a baseline
  contract_returns         non-overlapping Rise/Fall trades from OOS probabilities
  threshold_sensitivity    the same strategy across edge thresholds
  period_stability         is the result positive in most sub-periods, or one lucky one?
  HoldoutGuard             the final test set can be evaluated once per dataset; reuse
                           raises unless explicitly forced, and every use is logged
  acceptance               the gates a model must pass before a human may promote it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from qb2.stats_utils import (
    bootstrap_ci,
    brier,
    calibration_table,
    expected_calibration_error,
    log_loss,
    trade_metrics,
)


@dataclass
class Fold:
    train: np.ndarray
    test: np.ndarray


def _purge(candidates: np.ndarray, label_end: np.ndarray, boundary_time: float) -> np.ndarray:
    """Keep only rows whose label is fully known before `boundary_time`."""
    return candidates[label_end[candidates] < boundary_time]


def chronological_partition(
    times: np.ndarray,
    label_end: np.ndarray,
    development_fraction: float = 0.8,
    embargo_seconds: float = 0.0,
) -> Dict[str, np.ndarray]:
    """Development rows (purged) and a final test block that starts after an embargo."""
    times = np.asarray(times, float)
    n = len(times)
    cut = int(n * development_fraction)
    if cut <= 0 or cut >= n:
        raise ValueError("Partition leaves an empty segment")
    test_start_time = times[cut] + embargo_seconds
    test = np.flatnonzero(times >= test_start_time)
    dev = _purge(np.arange(cut), np.asarray(label_end, float), times[test[0]] if len(test) else np.inf)
    return {"development": dev, "test": test}


def walk_forward_folds(
    times: np.ndarray,
    label_end: np.ndarray,
    n_folds: int = 5,
    min_train_fraction: float = 0.4,
    embargo_seconds: float = 0.0,
) -> List[Fold]:
    times = np.asarray(times, float)
    label_end = np.asarray(label_end, float)
    n = len(times)
    start = int(n * min_train_fraction)
    if n_folds < 1 or start >= n:
        raise ValueError("Not enough rows for walk-forward folds")
    folds = []
    for block in np.array_split(np.arange(start, n), n_folds):
        if len(block) == 0:
            continue
        test_start = times[block[0]]
        test = block[times[block] >= test_start + embargo_seconds]
        train = _purge(np.arange(block[0]), label_end, test_start)
        if len(train) and len(test):
            folds.append(Fold(train=train, test=test))
    return folds


def walk_forward_predict(
    model_factory: Callable[[], Any],
    X: np.ndarray,
    y: np.ndarray,
    folds: Sequence[Fold],
) -> Tuple[np.ndarray, np.ndarray]:
    """(p, std) filled only on test rows; everything else stays NaN."""
    p = np.full(len(y), np.nan)
    s = np.full(len(y), np.nan)
    for fold in folds:
        model = model_factory().fit(X[fold.train], y[fold.train])
        p[fold.test], s[fold.test] = model.predict(X[fold.test])
    return p, s


def probability_report(y: np.ndarray, p: np.ndarray, bins: int = 10) -> Dict[str, Any]:
    ok = np.isfinite(p)
    y, p = np.asarray(y, float)[ok], np.asarray(p, float)[ok]
    return {
        "n": int(len(y)),
        "brier": brier(y, p),
        "log_loss": log_loss(y, p),
        "ece": expected_calibration_error(y, p, bins),
        "mean_predicted": float(np.mean(p)) if len(p) else float("nan"),
        "observed_rate": float(np.mean(y)) if len(y) else float("nan"),
        "calibration": calibration_table(y, p, bins),
    }


def paired_brier_improvement(
    y: np.ndarray, p_model: np.ndarray, p_base: np.ndarray,
    block: int = 50, n_boot: int = 500, seed: int = 0,
) -> Dict[str, float]:
    """Mean of (base Brier - model Brier) per row with a block-bootstrap 95% CI. > 0 = model better."""
    ok = np.isfinite(p_model) & np.isfinite(p_base)
    diff = (p_base[ok] - y[ok]) ** 2 - (p_model[ok] - y[ok]) ** 2
    est, lo, hi = bootstrap_ci(diff, np.mean, n_boot=n_boot, block=block, seed=seed)
    base = float(np.mean((p_base[ok] - y[ok]) ** 2)) if ok.any() else float("nan")
    return {"improvement": est, "ci_low": lo, "ci_high": hi,
            "brier_skill": est / base if base and np.isfinite(base) else float("nan"), "n": int(ok.sum())}


def contract_returns(
    times: np.ndarray,
    p_rise: np.ndarray,
    p_std: np.ndarray,
    outcome: np.ndarray,
    payout_ratio: float,
    p_tie: float = 0.0,
    min_edge: float = 0.0,
    z: float = 1.0,
    duration_seconds: int = 300,
) -> Dict[str, np.ndarray]:
    """
    One position at a time. Take CALL when the lowered P(Rise) beats break-even by
    `min_edge` of stake, PUT likewise for P(Fall). Returns per-trade return on stake.
    `payout_ratio` = payout / stake (e.g. 1.95).
    """
    breakeven = 1.0 / payout_ratio
    out_t, out_r, out_side = [], [], []
    busy_until = -np.inf
    for i in np.argsort(times):
        if times[i] < busy_until or not np.isfinite(p_rise[i]) or not np.isfinite(outcome[i]):
            continue
        sd = p_std[i] if np.isfinite(p_std[i]) else 0.0
        up_lo = p_rise[i] - z * sd
        down_lo = (1.0 - p_rise[i] - p_tie) - z * sd
        ev_up, ev_down = up_lo * payout_ratio - 1.0, down_lo * payout_ratio - 1.0
        if max(ev_up, ev_down) < min_edge or max(up_lo, down_lo) <= breakeven:
            continue
        side = 1 if ev_up >= ev_down else -1
        win = outcome[i] == side
        out_t.append(times[i])
        out_r.append(payout_ratio - 1.0 if win else -1.0)
        out_side.append(side)
        busy_until = times[i] + duration_seconds
    return {"times": np.asarray(out_t), "returns": np.asarray(out_r), "side": np.asarray(out_side)}


def threshold_sensitivity(
    times: np.ndarray, p_rise: np.ndarray, p_std: np.ndarray, outcome: np.ndarray,
    payout_ratio: float, p_tie: float = 0.0, thresholds: Sequence[float] = (0.0, 0.01, 0.02, 0.03, 0.05),
    duration_seconds: int = 300,
) -> List[Dict[str, float]]:
    rows = []
    for th in thresholds:
        r = contract_returns(times, p_rise, p_std, outcome, payout_ratio, p_tie, th, duration_seconds=duration_seconds)
        ret = r["returns"]
        est, lo, hi = bootstrap_ci(ret, np.mean, n_boot=500) if len(ret) >= 20 else (float(np.mean(ret)) if len(ret) else 0.0, float("nan"), float("nan"))
        rows.append({"min_edge": th, "trades": int(len(ret)), "mean_return": est, "ci_low": lo, "ci_high": hi})
    return rows


def period_stability(times: np.ndarray, returns: np.ndarray, n_periods: int = 5) -> Dict[str, Any]:
    if len(returns) < n_periods * 5:
        return {"periods": [], "positive_fraction": 0.0, "note": "too few trades for a stability check"}
    order = np.argsort(times)
    periods = []
    for chunk in np.array_split(order, n_periods):
        periods.append({"start": float(times[chunk[0]]), "end": float(times[chunk[-1]]),
                        "trades": int(len(chunk)), "mean_return": float(np.mean(returns[chunk]))})
    pos = float(np.mean([p["mean_return"] > 0 for p in periods]))
    return {"periods": periods, "positive_fraction": pos}


class HoldoutAlreadyUsed(RuntimeError):
    pass


class HoldoutGuard:
    """Evaluate the final test set at most once per dataset fingerprint."""

    def __init__(self, db: Any):
        self.db = db

    def evaluate(self, dataset_hash: str, model_version: Optional[str], fn: Callable[[], Any],
                 purpose: str = "final_test", allow_reuse: bool = False) -> Any:
        uses = self.db.holdout_uses(dataset_hash)
        if uses and not allow_reuse:
            raise HoldoutAlreadyUsed(
                f"Holdout {dataset_hash[:12]} was already evaluated {len(uses)} time(s). Reusing it for "
                "selection turns it into training data; collect new data or pass allow_reuse=True "
                "(the reuse is recorded)."
            )
        self.db.record_holdout_use(dataset_hash, model_version, purpose + (" (REUSED)" if uses else ""))
        return fn()


@dataclass
class AcceptanceGates:
    min_oos_samples: int = 2000
    min_trades: int = 200
    max_ece: float = 0.03
    min_positive_period_fraction: float = 0.6
    require_brier_ci_positive: bool = True
    require_return_ci_positive: bool = True


def acceptance(metrics: Dict[str, Any], gates: AcceptanceGates = AcceptanceGates()) -> Tuple[bool, List[str]]:
    """Hard gates on final-test metrics. All must pass for a model to become 'validated'."""
    failures: List[str] = []
    prob = metrics.get("probability", {})
    imp = metrics.get("brier_vs_base", {})
    trade = metrics.get("trading", {})
    stab = metrics.get("stability", {})
    if metrics.get("payout_source") not in ("historical_proposal", "deriv_proposal"):
        failures.append("payout not from real Deriv proposals (result INCOMPLETE)")
    if prob.get("n", 0) < gates.min_oos_samples:
        failures.append(f"only {prob.get('n', 0)} out-of-sample predictions (< {gates.min_oos_samples})")
    if not np.isfinite(prob.get("ece", np.nan)) or prob.get("ece", 1.0) > gates.max_ece:
        failures.append(f"calibration error {prob.get('ece')} > {gates.max_ece}")
    if gates.require_brier_ci_positive and not (imp.get("ci_low", -1.0) > 0):
        failures.append("Brier improvement over base rate not significant (CI includes 0)")
    if trade.get("trades", 0) < gates.min_trades:
        failures.append(f"only {trade.get('trades', 0)} trades (< {gates.min_trades})")
    if gates.require_return_ci_positive and not (trade.get("return_ci_low", -1.0) > 0):
        failures.append("mean return per stake not significantly positive after payout")
    if stab.get("positive_fraction", 0.0) < gates.min_positive_period_fraction:
        failures.append(f"positive in only {stab.get('positive_fraction', 0.0):.0%} of periods")
    return not failures, failures


def summarize_trades(times: np.ndarray, returns: np.ndarray) -> Dict[str, Any]:
    """Trade metrics for unit stakes plus a bootstrap CI on the mean return."""
    stakes = np.ones(len(returns))
    m = trade_metrics(returns, stakes, initial_equity=100.0)
    if len(returns) >= 20:
        _, lo, hi = bootstrap_ci(returns, np.mean, n_boot=1000, block=5)
    else:
        lo = hi = float("nan")
    m.update({"return_ci_low": lo, "return_ci_high": hi})
    return m
