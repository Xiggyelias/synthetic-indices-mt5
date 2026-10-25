"""
Drift monitoring
================
Compares recent data with the reference sample stored inside the model artifact:
  - feature drift      population stability index (PSI) per feature
  - prediction drift   PSI of the predicted probabilities
  - calibration        Brier score on recently settled outcomes vs the reference Brier,
                       and a binomial test of realized vs predicted win rate

Responses (applied by the caller, never by retraining):
  WARN -> widen uncertainty (confidence_penalty) so fewer opportunities pass
  HALT -> halt the strategy for that symbol and demote its champion
Diagnostics are written to the drift_events table either way.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from qb2.stats_utils import binomial_p_value, brier


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    ref = np.asarray(reference, float)
    cur = np.asarray(current, float)
    ref, cur = ref[np.isfinite(ref)], cur[np.isfinite(cur)]
    if len(ref) < 20 or len(cur) < 20:
        return 0.0
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    r = np.histogram(ref, edges)[0] / len(ref)
    c = np.histogram(cur, edges)[0] / len(cur)
    r, c = np.clip(r, 1e-4, None), np.clip(c, 1e-4, None)
    return float(np.sum((c - r) * np.log(c / r)))


@dataclass
class DriftThresholds:
    psi_warn: float = 0.10
    psi_halt: float = 0.25
    brier_warn_increase: float = 0.005
    brier_halt_increase: float = 0.015
    calibration_p_halt: float = 0.01
    min_outcomes: int = 100


@dataclass
class DriftReport:
    status: str                              # OK, WARN, HALT
    feature_psi: Dict[str, float] = field(default_factory=dict)
    prediction_psi: float = 0.0
    brier_reference: Optional[float] = None
    brier_recent: Optional[float] = None
    realized_rate: Optional[float] = None
    predicted_rate: Optional[float] = None
    calibration_p_value: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    confidence_penalty: float = 1.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DriftMonitor:
    def __init__(self, thresholds: DriftThresholds = DriftThresholds()):
        self.th = thresholds

    def evaluate(
        self,
        reference_features: Dict[str, List[float]],
        recent_features: Dict[str, np.ndarray],
        reference_predictions: Optional[List[float]] = None,
        recent_predictions: Optional[np.ndarray] = None,
        reference_brier: Optional[float] = None,
        recent_outcomes: Optional[np.ndarray] = None,
        recent_outcome_predictions: Optional[np.ndarray] = None,
    ) -> DriftReport:
        th = self.th
        rep = DriftReport(status="OK")
        level = 0

        for name, ref in (reference_features or {}).items():
            if name in recent_features:
                rep.feature_psi[name] = round(psi(np.asarray(ref), recent_features[name]), 4)
        worst = max(rep.feature_psi.values(), default=0.0)
        if worst >= th.psi_halt:
            level = max(level, 2)
            rep.reasons.append(f"feature drift PSI {worst:.3f} >= {th.psi_halt}")
        elif worst >= th.psi_warn:
            level = max(level, 1)
            rep.reasons.append(f"feature drift PSI {worst:.3f} >= {th.psi_warn}")

        if reference_predictions is not None and recent_predictions is not None:
            rep.prediction_psi = round(psi(np.asarray(reference_predictions), recent_predictions), 4)
            if rep.prediction_psi >= th.psi_halt:
                level = max(level, 2)
                rep.reasons.append(f"prediction drift PSI {rep.prediction_psi:.3f}")
            elif rep.prediction_psi >= th.psi_warn:
                level = max(level, 1)
                rep.reasons.append(f"prediction drift PSI {rep.prediction_psi:.3f}")

        if recent_outcomes is not None and recent_outcome_predictions is not None:
            y = np.asarray(recent_outcomes, float)
            p = np.asarray(recent_outcome_predictions, float)
            ok = np.isfinite(y) & np.isfinite(p)
            y, p = y[ok], p[ok]
            if len(y) >= th.min_outcomes:
                rep.brier_recent = round(brier(y, p), 5)
                rep.realized_rate = round(float(np.mean(y)), 4)
                rep.predicted_rate = round(float(np.mean(p)), 4)
                rep.calibration_p_value = binomial_p_value(int(y.sum()), len(y), float(np.clip(np.mean(p), 1e-3, 1 - 1e-3)))
                if reference_brier is not None:
                    rep.brier_reference = round(float(reference_brier), 5)
                    inc = rep.brier_recent - reference_brier
                    if inc >= th.brier_halt_increase:
                        level = max(level, 2)
                        rep.reasons.append(f"Brier rose by {inc:.4f}")
                    elif inc >= th.brier_warn_increase:
                        level = max(level, 1)
                        rep.reasons.append(f"Brier rose by {inc:.4f}")
                if rep.calibration_p_value < th.calibration_p_halt:
                    level = max(level, 2)
                    rep.reasons.append(f"realized rate {rep.realized_rate:.3f} vs predicted {rep.predicted_rate:.3f} "
                                       f"(p={rep.calibration_p_value:.4f})")

        rep.status = ("OK", "WARN", "HALT")[level]
        rep.confidence_penalty = (1.0, 2.0, float("inf"))[level]
        return rep
