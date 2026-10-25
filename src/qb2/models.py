"""
Probability models for P(Rise)
==============================
  base_rate   training frequency of Rise: the zero-skill benchmark every model must beat
  logistic    standardized L2 logistic regression: the interpretable baseline
  gbm         shallow histogram gradient boosting: the nonlinear baseline
  ensemble    average of logistic and gbm, used only when it beats both on
              out-of-sample data (decided in research.select_model)

Every model is calibrated with Platt scaling on the chronologically last part of
its own training window (after a purge gap), and reports an uncertainty from the
spread of block-bootstrap bags. predict() returns (p_rise, std).

No deep learning or reinforcement learning: nothing here has shown out-of-sample
value that would justify the extra variance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from qb2.stats_utils import block_bootstrap_indices

EPS = 1e-6


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


@dataclass
class PlattCalibrator:
    a: float = 1.0
    b: float = 0.0

    def fit(self, p_raw: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        from sklearn.linear_model import LogisticRegression
        if len(np.unique(y)) < 2 or len(y) < 50:
            return self
        lr = LogisticRegression(C=1.0)
        lr.fit(_logit(p_raw).reshape(-1, 1), y)
        self.a, self.b = float(lr.coef_[0, 0]), float(lr.intercept_[0])
        return self

    def apply(self, p_raw: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(self.a * _logit(p_raw) + self.b)))


class BaseRateModel:
    name = "base_rate"

    def __init__(self, **_: Any):
        self.p = 0.5
        self.n = 0

    def fit(self, X: np.ndarray, y: np.ndarray) -> "BaseRateModel":
        self.n = len(y)
        self.p = float((np.sum(y) + 1) / (len(y) + 2))
        return self

    def predict(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        n = len(X)
        std = np.sqrt(self.p * (1 - self.p) / (self.n + 2))
        return np.full(n, self.p), np.full(n, std)

    def describe(self) -> Dict[str, Any]:
        return {"model": self.name, "p_rise": self.p, "n_train": self.n}


class _BaggedCalibrated:
    """Shared fit/predict: bootstrap bags on the fit window, Platt on the calibration window."""
    name = "abstract"

    def __init__(self, n_bags: int = 8, block: int = 50, calib_fraction: float = 0.2,
                 purge_rows: int = 5, seed: int = 0):
        self.n_bags = n_bags
        self.block = block
        self.calib_fraction = calib_fraction
        self.purge_rows = purge_rows
        self.seed = seed
        self.bags: List[Any] = []
        self.calibrator = PlattCalibrator()
        self.feature_count = 0

    def _new_estimator(self, seed: int) -> Any:
        raise NotImplementedError

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_BaggedCalibrated":
        X, y = np.asarray(X, float), np.asarray(y, int)
        self.feature_count = X.shape[1]
        n = len(y)
        n_cal = int(n * self.calib_fraction)
        fit_end = n - n_cal - self.purge_rows   # purge: fit labels must end before calibration starts
        if fit_end < 200 or n_cal < 50:
            fit_end, n_cal = n, 0
        Xf, yf = X[:fit_end], y[:fit_end]
        rng = np.random.default_rng(self.seed)
        self.bags = []
        for b in range(self.n_bags):
            idx = block_bootstrap_indices(len(yf), self.block, rng) if self.n_bags > 1 else np.arange(len(yf))
            if len(np.unique(yf[idx])) < 2:
                continue
            est = self._new_estimator(self.seed + b)
            est.fit(Xf[idx], yf[idx])
            self.bags.append(est)
        if not self.bags:
            raise ValueError("No usable bootstrap bag (single-class training data)")
        if n_cal:
            raw = self._raw_bag_predictions(X[n - n_cal:]).mean(axis=0)
            self.calibrator.fit(raw, y[n - n_cal:])
        return self

    def _raw_bag_predictions(self, X: np.ndarray) -> np.ndarray:
        return np.vstack([est.predict_proba(X)[:, 1] for est in self.bags])

    def predict(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        bags = self.calibrator.apply(self._raw_bag_predictions(np.asarray(X, float)))
        return bags.mean(axis=0), bags.std(axis=0)

    def describe(self) -> Dict[str, Any]:
        return {"model": self.name, "bags": len(self.bags), "platt_a": self.calibrator.a,
                "platt_b": self.calibrator.b, "features": self.feature_count}


class LogisticModel(_BaggedCalibrated):
    name = "logistic"

    def __init__(self, C: float = 0.1, **kw: Any):
        super().__init__(**kw)
        self.C = C

    def _new_estimator(self, seed: int) -> Any:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        return make_pipeline(StandardScaler(), LogisticRegression(C=self.C, max_iter=500))

    def coefficients(self, feature_names: List[str]) -> Dict[str, float]:
        """Mean standardized coefficient per feature across bags (for interpretation)."""
        coefs = np.mean([est[-1].coef_[0] for est in self.bags], axis=0)
        return {f: float(c) for f, c in zip(feature_names, coefs)}


class GBMModel(_BaggedCalibrated):
    name = "gbm"

    def __init__(self, n_bags: int = 4, **kw: Any):
        super().__init__(n_bags=n_bags, **kw)

    def _new_estimator(self, seed: int) -> Any:
        from sklearn.ensemble import HistGradientBoostingClassifier
        return HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=100,
            l2_regularization=1.0, random_state=seed,
        )


class EnsembleModel:
    name = "ensemble"

    def __init__(self, **kw: Any):
        self.members = [LogisticModel(**kw), GBMModel(**{k: v for k, v in kw.items() if k != "n_bags"})]

    def fit(self, X: np.ndarray, y: np.ndarray) -> "EnsembleModel":
        for m in self.members:
            m.fit(X, y)
        return self

    def predict(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        preds = [m.predict(X) for m in self.members]
        p = np.mean([p for p, _ in preds], axis=0)
        # Disagreement between members adds to each member's own uncertainty
        within = np.sqrt(np.mean([s ** 2 for _, s in preds], axis=0))
        between = np.std([p for p, _ in preds], axis=0)
        return p, np.sqrt(within ** 2 + between ** 2)

    def describe(self) -> Dict[str, Any]:
        return {"model": self.name, "members": [m.describe() for m in self.members]}


MODEL_TYPES = {
    "base_rate": BaseRateModel,
    "logistic": LogisticModel,
    "gbm": GBMModel,
    "ensemble": EnsembleModel,
}


def make_model(name: str, purge_rows: int = 5, seed: int = 0, **kw: Any) -> Any:
    if name not in MODEL_TYPES:
        raise ValueError(f"Unknown model {name!r}; choose from {sorted(MODEL_TYPES)}")
    if name == "base_rate":
        return BaseRateModel()
    return MODEL_TYPES[name](purge_rows=purge_rows, seed=seed, **kw)


@dataclass
class TrainedModel:
    """What the registry stores: the fitted model plus everything needed to use it correctly."""
    model: Any
    model_type: str
    symbol: str
    feature_names: Tuple[str, ...]
    feature_version: str
    p_tie: float
    duration_seconds: int
    decision_interval: int
    dataset_hash: str
    train_range: Tuple[int, int]
    reference_features: Optional[Dict[str, List[float]]] = None   # sample for drift monitoring
    reference_predictions: Optional[List[float]] = None
    reference_brier: Optional[float] = None

    def predict(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        return self.model.predict(X)
