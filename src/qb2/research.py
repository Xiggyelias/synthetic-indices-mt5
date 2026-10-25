"""
Research pipeline
=================
  build_dataset       clean ticks -> decision grid -> causal features -> settlement labels
  select_model        walk-forward on the development segment; a model is preferred
                      over the base rate only if its paired Brier improvement has a
                      95% CI above zero; the ensemble only if it beats both members
  final_test          the chosen model, refit on the full development segment, scored
                      once on the untouched final segment (HoldoutGuard)
  train_candidate     final_test + registration; acceptance gates decide
                      validated / rejected; promotion stays a human step
  research_instrument data quality, distribution and dependence statistics, patterns,
                      regimes, predictive evidence, stability and break-even
  rank_instruments    evidence-first ranking. Volatility, recent wins and short
                      profitable stretches play no part.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats

from edge_scanner import runs_test, variance_ratio_test
from qb2.config import Settings
from qb2.data_quality import DataQualityReport, clean_ticks
from qb2.features import FEATURE_VERSION, MODEL_FEATURES, compute_features, dataset_hash, make_decision_times
from qb2.labels import SettlementRule, settle
from qb2.models import TrainedModel, make_model
from qb2.patterns import discover_patterns, event_study, ljung_box
from qb2.regime import (
    RegimeClassifier,
    hmm_state_count_by_bic,
    realized_variance_series,
    regime_performance,
    transition_matrix,
)
from qb2.validation import (
    HoldoutGuard,
    chronological_partition,
    contract_returns,
    paired_brier_improvement,
    period_stability,
    probability_report,
    summarize_trades,
    threshold_sensitivity,
    walk_forward_folds,
    walk_forward_predict,
)

log = logging.getLogger("qb2.research")


@dataclass
class Dataset:
    symbol: str
    features: pd.DataFrame          # all features, indexed by decision time
    outcome: np.ndarray             # +1 rise, -1 fall, 0 tie
    times: np.ndarray
    label_end: np.ndarray           # contract expiry per row (for purging)
    epochs: np.ndarray
    quotes: np.ndarray
    hash: str
    params: Dict[str, Any]
    quality: DataQualityReport

    @property
    def X(self) -> np.ndarray:
        return self.features[list(MODEL_FEATURES)].values

    @property
    def y(self) -> np.ndarray:
        return (self.outcome == 1).astype(int)

    @property
    def p_tie(self) -> float:
        return float(np.mean(self.outcome == 0)) if len(self.outcome) else 0.0

    def __len__(self) -> int:
        return len(self.times)


def build_dataset(ticks: pd.DataFrame, symbol: str, settings: Settings,
                  interval: Optional[int] = None) -> Dataset:
    interval = interval or settings.decision_interval_seconds
    clean, quality = clean_ticks(ticks, symbol)
    e, q = clean["epoch"].values, clean["quote"].values
    latency = settings.execution_latency_seconds
    horizon = int(settings.contract_seconds + latency + 15)
    times = make_decision_times(e, interval, horizon)
    feats = compute_features(e, q, times) if len(times) else pd.DataFrame(columns=list(MODEL_FEATURES))
    lab = settle(e, q, times + latency, SettlementRule(duration_seconds=settings.contract_seconds)) if len(times) else None
    if lab is None:
        keep = np.zeros(0, dtype=bool)
    else:
        keep = lab["valid"] & feats[list(MODEL_FEATURES)].notna().all(axis=1).values
    params = {"symbol": symbol, "interval": interval, "duration": settings.contract_seconds, "latency": latency}
    return Dataset(
        symbol=symbol,
        features=feats[keep] if len(times) else feats,
        outcome=lab["outcome"][keep] if lab is not None else np.zeros(0),
        times=times[keep].astype(float),
        label_end=lab["expiry"][keep] if lab is not None else np.zeros(0),
        epochs=e, quotes=q,
        hash=dataset_hash(e, q, params),
        params=params,
        quality=quality,
    )


def _purge_rows(settings: Settings, interval: int) -> int:
    return int(np.ceil((settings.contract_seconds + settings.execution_latency_seconds) / interval)) + 1


def select_model(
    ds: Dataset,
    idx: np.ndarray,
    settings: Settings,
    candidates: Sequence[str] = ("logistic", "gbm"),
    n_folds: int = 5,
    seed: int = 0,
) -> Dict[str, Any]:
    """Walk-forward comparison on rows `idx` (the development segment)."""
    X, y, times, label_end = ds.X[idx], ds.y[idx], ds.times[idx], ds.label_end[idx]
    folds = walk_forward_folds(times, label_end, n_folds=n_folds)
    purge = _purge_rows(settings, ds.params["interval"])
    p_base, s_base = walk_forward_predict(lambda: make_model("base_rate"), X, y, folds)
    results: Dict[str, Any] = {"base_rate": {"probability": probability_report(y, p_base)}}
    preds = {"base_rate": (p_base, s_base)}
    for name in candidates:
        p, s = walk_forward_predict(lambda n=name: make_model(n, purge_rows=purge, seed=seed), X, y, folds)
        preds[name] = (p, s)
        results[name] = {"probability": probability_report(y, p),
                         "brier_vs_base": paired_brier_improvement(y, p, p_base, seed=seed)}

    significant = [n for n in candidates if results[n]["brier_vs_base"]["ci_low"] > 0]
    chosen = min(significant, key=lambda n: results[n]["probability"]["brier"]) if significant else "base_rate"

    if {"logistic", "gbm"} <= set(significant):
        p_ens = (preds["logistic"][0] + preds["gbm"][0]) / 2
        ens = {"probability": probability_report(y, p_ens),
               "brier_vs_base": paired_brier_improvement(y, p_ens, p_base, seed=seed)}
        beats = all(paired_brier_improvement(y, p_ens, preds[m][0], seed=seed)["ci_low"] > 0 for m in ("logistic", "gbm"))
        ens["beats_both_members"] = beats
        results["ensemble"] = ens
        if beats:
            chosen = "ensemble"

    return {"chosen": chosen, "folds": len(folds), "models": results,
            "oos_predictions": {k: v[0] for k, v in preds.items()},
            "oos_std": {k: v[1] for k, v in preds.items()},
            "oos_times": times, "oos_outcome": ds.outcome[idx]}


def payout_ratio_from_quotes(quotes_df: Optional[pd.DataFrame], duration_seconds: int) -> Optional[float]:
    """Median payout/stake of recorded proposals for this duration (None when none recorded)."""
    if quotes_df is None or len(quotes_df) == 0:
        return None
    q = quotes_df[(quotes_df["duration_seconds"] == duration_seconds) & (quotes_df["stake"] > 0)]
    if len(q) == 0:
        return None
    return float(np.median(q["payout"].values / q["stake"].values))


def final_test(
    ds: Dataset,
    model_name: str,
    settings: Settings,
    guard: Optional[HoldoutGuard] = None,
    payout_ratio: Optional[float] = None,
    payout_source: str = "none",
    allow_holdout_reuse: bool = False,
    seed: int = 0,
) -> Tuple[TrainedModel, Dict[str, Any]]:
    part = chronological_partition(ds.times, ds.label_end, development_fraction=0.8,
                                   embargo_seconds=settings.contract_seconds)
    dev, test = part["development"], part["test"]
    purge = _purge_rows(settings, ds.params["interval"])
    model = make_model(model_name, purge_rows=purge, seed=seed).fit(ds.X[dev], ds.y[dev])
    base = make_model("base_rate").fit(ds.X[dev], ds.y[dev])
    p_tie = float(np.mean(ds.outcome[dev] == 0))

    def evaluate() -> Dict[str, Any]:
        p, s = model.predict(ds.X[test])
        pb, _ = base.predict(ds.X[test])
        y = ds.y[test]
        m: Dict[str, Any] = {
            "segment": "final_test",
            "test_rows": int(len(test)),
            "test_range": [float(ds.times[test[0]]), float(ds.times[test[-1]])],
            "probability": probability_report(y, p),
            "brier_vs_base": paired_brier_improvement(y, p, pb, seed=seed),
            "payout_ratio": payout_ratio,
            "payout_source": payout_source,
        }
        if payout_ratio:
            r = contract_returns(ds.times[test], p, s, ds.outcome[test], payout_ratio, p_tie,
                                 min_edge=settings.min_edge_fraction, z=settings.uncertainty_z,
                                 duration_seconds=settings.contract_seconds)
            m["trading"] = summarize_trades(r["times"], r["returns"])
            m["stability"] = period_stability(r["times"], r["returns"])
            m["threshold_sensitivity"] = threshold_sensitivity(ds.times[test], p, s, ds.outcome[test], payout_ratio,
                                                               p_tie, duration_seconds=settings.contract_seconds)
        else:
            m["trading"] = {"trades": 0, "note": "no payout data: expected value cannot be computed"}
            m["stability"] = {"positive_fraction": 0.0}
        return m

    metrics = guard.evaluate(ds.hash, None, evaluate, allow_reuse=allow_holdout_reuse) if guard else evaluate()

    rng = np.random.default_rng(seed)
    ref_rows = dev[-min(len(dev), 2000):]
    sample = rng.choice(ref_rows, size=min(len(ref_rows), 1000), replace=False)
    ref_p, _ = model.predict(ds.X[sample])
    trained = TrainedModel(
        model=model, model_type=model_name, symbol=ds.symbol, feature_names=MODEL_FEATURES,
        feature_version=FEATURE_VERSION, p_tie=p_tie, duration_seconds=settings.contract_seconds,
        decision_interval=ds.params["interval"], dataset_hash=ds.hash,
        train_range=(int(ds.times[dev[0]]), int(ds.times[dev[-1]])),
        reference_features={f: ds.features[f].values[sample].tolist() for f in MODEL_FEATURES},
        reference_predictions=ref_p.tolist(),
        reference_brier=metrics["probability"]["brier"],
    )
    return trained, metrics


def describe_returns(ds: Dataset) -> Dict[str, Any]:
    """Distribution, clustering and dependence statistics of 5-minute and tick returns."""
    lq = np.log(ds.quotes)
    tick_r = np.diff(lq)
    out: Dict[str, Any] = {"ticks": int(len(ds.quotes))}
    if len(tick_r) < 100:
        return out
    grid = np.arange(ds.epochs[0], ds.epochs[-1], 300)
    idx = np.searchsorted(ds.epochs, grid, side="right") - 1
    r5 = np.diff(lq[idx])
    sd = float(np.std(r5)) or 1e-12
    out.update({
        "ret_5m_std": sd,
        "ret_5m_skew": float(stats.skew(r5)),
        "ret_5m_excess_kurtosis": float(stats.kurtosis(r5)),
        "ret_5m_cvar_5pct_sigma": float(np.mean(np.sort(r5)[: max(1, len(r5) // 20)]) / sd),
        "tick_autocorr": ljung_box(tick_r, 10),
        "volatility_clustering": ljung_box(tick_r ** 2, 10),
    })
    vr = {}
    for qlag in (2, 5, 15, 60):
        if len(lq) > 20 * qlag:
            z, p, v = variance_ratio_test(lq, qlag)
            vr[str(qlag)] = {"vr": v, "z": z, "p_value": p}
    out["variance_ratio"] = vr
    z, p, ratio = runs_test(np.sign(tick_r))
    out["runs"] = {"z": z, "p_value": p, "runs_over_expected": ratio}
    return out


def research_instrument(
    ds: Dataset,
    settings: Settings,
    quotes_df: Optional[pd.DataFrame] = None,
    candidates: Sequence[str] = ("logistic", "gbm"),
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Research one instrument using only the development segment (first 80%), so the
    final test segment stays untouched for train_candidate.
    """
    report: Dict[str, Any] = {"symbol": ds.symbol, "dataset_hash": ds.hash, "rows": len(ds),
                              "data_quality": ds.quality.as_dict(), "returns": describe_returns(ds)}
    ratio = payout_ratio_from_quotes(quotes_df, settings.contract_seconds)
    report["payout"] = {"payout_ratio": ratio, "breakeven_probability": 1 / ratio if ratio else None,
                        "source": "historical_proposal" if ratio else "unavailable"}
    if len(ds) < 1000:
        report["verdict"] = "INSUFFICIENT_DATA"
        report["merits_investigation"] = False
        return report

    part = chronological_partition(ds.times, ds.label_end, 0.8, embargo_seconds=settings.contract_seconds)
    dev = part["development"]
    fdev = ds.features.iloc[dev]

    report["patterns"] = discover_patterns(fdev, ds.outcome[dev], settings.contract_seconds)
    report["event_study"] = event_study(ds.epochs[ds.epochs <= ds.times[dev[-1]]],
                                        ds.quotes[ds.epochs <= ds.times[dev[-1]]])

    half = len(dev) // 2
    reg = RegimeClassifier().fit(fdev.iloc[:half])
    labels = reg.transform(fdev)
    report["regimes"] = {
        "classifier": reg.as_dict(),
        "share": {k: float(v) for k, v in pd.Series(labels).value_counts(normalize=True).items()},
        "transitions": transition_matrix(labels).round(4).to_dict(),
        "hmm_states_by_bic": hmm_state_count_by_bic(realized_variance_series(fdev["rv_300"].values)[::5]),
    }

    sel = select_model(ds, dev, settings, candidates=candidates, seed=seed)
    report["model_selection"] = {k: v for k, v in sel.items() if not k.startswith("oos_")}
    chosen = sel["chosen"]
    p, s = sel["oos_predictions"][chosen], sel["oos_std"][chosen]
    t_oos, o_oos = sel["oos_times"], sel["oos_outcome"]

    ok = np.isfinite(p)
    first_half = ok & (t_oos <= np.median(t_oos[ok])) if ok.any() else ok
    second_half = ok & ~first_half
    stab = {}
    if chosen != "base_rate":
        pb = sel["oos_predictions"]["base_rate"]
        y = (o_oos == 1).astype(int)
        stab = {"first_half": paired_brier_improvement(y[first_half], p[first_half], pb[first_half], seed=seed),
                "second_half": paired_brier_improvement(y[second_half], p[second_half], pb[second_half], seed=seed)}
    report["stability"] = stab

    if ratio and chosen != "base_rate":
        p_tie = float(np.mean(ds.outcome[dev] == 0))
        r = contract_returns(t_oos, p, s, o_oos, ratio, p_tie, settings.min_edge_fraction, settings.uncertainty_z,
                             settings.contract_seconds)
        report["walk_forward_trading"] = summarize_trades(r["times"], r["returns"])
        trade_regimes = labels[np.searchsorted(fdev.index.values.astype(float), r["times"])] if len(r["times"]) else []
        report["regime_performance"] = regime_performance(np.asarray(trade_regimes), r["returns"]) if len(r["times"]) else []

    best = report["model_selection"]["models"].get(chosen, {})
    ci_low = best.get("brier_vs_base", {}).get("ci_low", 0.0) if chosen != "base_rate" else 0.0
    stable = bool(stab) and all(v.get("improvement", 0) > 0 for v in stab.values())
    discoveries = report["patterns"]["discoveries_fdr"]
    report["evidence"] = {"chosen_model": chosen, "brier_improvement_ci_low": ci_low,
                          "stable_across_halves": stable, "pattern_discoveries_fdr": discoveries}
    report["merits_investigation"] = bool(ds.quality.passed and ((ci_low > 0 and stable) or discoveries))
    report["verdict"] = ("EVIDENCE_WORTH_INVESTIGATING" if report["merits_investigation"]
                         else "NO_EVIDENCE_OF_EDGE" if ds.quality.passed else "DATA_QUALITY_FAILED")
    return report


def rank_instruments(reports: List[Dict[str, Any]], availability: Optional[Dict[str, Optional[bool]]] = None) -> List[Dict[str, Any]]:
    """
    Evidence-first ranking:
      1. eligible (data quality passed, 5-minute Rise/Fall offered or not yet checked)
      2. merits_investigation
      3. Brier improvement CI lower bound, then FDR pattern discoveries, then data coverage
    """
    availability = availability or {}
    rows = []
    for r in reports:
        ev = r.get("evidence", {})
        avail = availability.get(r["symbol"])
        eligible = bool(r.get("data_quality", {}).get("passed")) and avail is not False
        rows.append({
            "symbol": r["symbol"],
            "eligible": eligible,
            "rise_fall_5m": avail,
            "merits_investigation": bool(r.get("merits_investigation")) and eligible,
            "verdict": r.get("verdict"),
            "brier_improvement_ci_low": float(ev.get("brier_improvement_ci_low", 0.0) or 0.0),
            "pattern_discoveries": len(ev.get("pattern_discoveries_fdr", []) or []),
            "chosen_model": ev.get("chosen_model"),
            "coverage": float(r.get("data_quality", {}).get("coverage_ratio", 0.0)),
            "breakeven_probability": r.get("payout", {}).get("breakeven_probability"),
            "rows": r.get("rows", 0),
        })
    rows.sort(key=lambda x: (x["eligible"], x["merits_investigation"], x["brier_improvement_ci_low"],
                             x["pattern_discoveries"], x["coverage"]), reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows
