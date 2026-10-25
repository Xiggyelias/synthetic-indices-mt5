"""
Orchestration used by the CLI, the dashboard and tests
======================================================
Each function is one reproducible research command: inputs come from the
database, outputs are written back as reports with the dataset fingerprint.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Sequence

import numpy as np

from qb2.backtest import BacktestConfig, run_contract_backtest
from qb2.config import Settings
from qb2.data_quality import clean_ticks
from qb2.drift import DriftMonitor
from qb2.features import compute_features, make_decision_times
from qb2.labels import SettlementRule, settle
from qb2.models import make_model
from qb2.regime import RegimeClassifier
from qb2.registry import ModelRegistry
from qb2.research import (
    _purge_rows,
    build_dataset,
    final_test,
    payout_ratio_from_quotes,
    rank_instruments,
    research_instrument,
    select_model,
)
from qb2.storage import Database
from qb2.trader import CONFIDENCE_KEY, STRATEGY_HALT_KEY
from qb2.validation import HoldoutGuard, walk_forward_folds, walk_forward_predict

log = logging.getLogger("qb2.pipeline")


def research_symbols(db: Database, settings: Settings, symbols: Sequence[str]) -> Dict[str, Any]:
    reports = []
    for sym in symbols:
        ds = build_dataset(db.load_ticks(sym), sym, settings)
        db.save_quality_report(sym, ds.quality.passed, ds.quality.as_dict())
        rep = research_instrument(ds, settings, db.load_quotes(sym, settings.contract_seconds))
        db.save_report(f"instrument:{sym}", rep)
        reports.append(rep)
        log.info("%s: %s", sym, rep.get("verdict"))
    availability = {i["symbol"]: (None if i["rise_fall_5m"] is None else bool(i["rise_fall_5m"]))
                    for i in db.instruments()}
    ranking = rank_instruments(reports, availability)
    payload = {"created_at": time.time(), "ranking": ranking,
               "note": "Ranked by out-of-sample evidence. merits_investigation=false everywhere means no edge was found."}
    db.save_report("instrument_ranking", payload)
    return payload


def train_symbol(db: Database, settings: Settings, symbol: str, registry: ModelRegistry,
                 allow_holdout_reuse: bool = False) -> Dict[str, Any]:
    ds = build_dataset(db.load_ticks(symbol), symbol, settings)
    if len(ds) < 1000:
        return {"symbol": symbol, "status": "insufficient_data", "rows": len(ds)}
    quotes = db.load_quotes(symbol, settings.contract_seconds)
    ratio = payout_ratio_from_quotes(quotes, settings.contract_seconds)
    from qb2.validation import chronological_partition
    dev = chronological_partition(ds.times, ds.label_end, 0.8, settings.contract_seconds)["development"]
    selection = select_model(ds, dev, settings)
    chosen = selection["chosen"]
    trained, metrics = final_test(ds, chosen, settings, guard=HoldoutGuard(db), payout_ratio=ratio,
                                  payout_source="historical_proposal" if ratio else "none",
                                  allow_holdout_reuse=allow_holdout_reuse)
    metrics["selection"] = {k: v for k, v in selection.items() if not k.startswith("oos_")}
    version, status = registry.register_candidate(trained, metrics, notes=f"selected {chosen} by walk-forward")
    return {"symbol": symbol, "version": version, "status": status, "chosen": chosen,
            "acceptance": registry.db.model(version)["metrics"]["acceptance"]}


def backtest_symbol(db: Database, settings: Settings, symbol: str, model_name: str = "logistic",
                    payout_ratio: Optional[float] = None, n_folds: int = 8) -> Dict[str, Any]:
    """Walk-forward out-of-sample probabilities over the whole history, replayed as contracts."""
    ds = build_dataset(db.load_ticks(symbol), symbol, settings)
    if len(ds) < 1000:
        return {"symbol": symbol, "status": "insufficient_data", "rows": len(ds)}
    folds = walk_forward_folds(ds.times, ds.label_end, n_folds=n_folds, min_train_fraction=0.3)
    purge = _purge_rows(settings, ds.params["interval"])
    p, s = walk_forward_predict(lambda: make_model(model_name, purge_rows=purge), ds.X, ds.y, folds)
    first_test = folds[0].test[0]
    regimes = RegimeClassifier().fit(ds.features.iloc[:first_test]).transform(ds.features)
    result = run_contract_backtest(
        symbol, ds.epochs, ds.quotes, ds.times, p, s, ds.p_tie, settings,
        BacktestConfig(payout_ratio=payout_ratio), regimes=regimes,
        historical_quotes=db.load_quotes(symbol, settings.contract_seconds),
    )
    summary = dict(result.summary(), model=model_name, dataset_hash=ds.hash, simulated_data=symbol.startswith("SIM:"))
    db.save_report(f"backtest:{symbol}", summary)
    return summary


def drift_check(db: Database, settings: Settings, registry: ModelRegistry, symbol: str,
                now: Optional[float] = None, hours: float = 6.0) -> Dict[str, Any]:
    champ = registry.champion(symbol)
    if champ is None:
        return {"symbol": symbol, "status": "NO_CHAMPION"}
    tm = registry.load(champ["version"])
    now = time.time() if now is None else now
    clean, _ = clean_ticks(db.load_ticks(symbol, start=now - hours * 3600 - 3600), symbol, min_ticks=10, min_coverage=0)
    e, q = clean["epoch"].values, clean["quote"].values
    times = make_decision_times(e, settings.decision_interval_seconds,
                                int(settings.contract_seconds + settings.execution_latency_seconds + 15))
    if len(times) < 30:
        return {"symbol": symbol, "status": "INSUFFICIENT_DATA"}
    feats = compute_features(e, q, times)
    lab = settle(e, q, times + settings.execution_latency_seconds, SettlementRule(settings.contract_seconds))
    ok = feats[list(tm.feature_names)].notna().all(axis=1).values
    p, _ = tm.predict(feats[list(tm.feature_names)].values[ok])
    y = (lab["outcome"][ok] == 1).astype(float)
    y[~lab["valid"][ok]] = np.nan
    report = DriftMonitor().evaluate(
        tm.reference_features or {}, {f: feats[f].values[ok] for f in tm.feature_names},
        tm.reference_predictions, p, tm.reference_brier, y, p,
    )
    db.record_drift(symbol, champ["version"], report.status, report.as_dict())
    if report.status == "HALT":
        db.set_state(STRATEGY_HALT_KEY.format(symbol=symbol), {"reason": "; ".join(report.reasons), "at": now})
        registry.demote(symbol, "drift halt: " + "; ".join(report.reasons))
    db.set_state(CONFIDENCE_KEY.format(symbol=symbol), report.confidence_penalty if report.status == "WARN" else 1.0)
    return {"symbol": symbol, "version": champ["version"], **report.as_dict()}


def clear_strategy_halt(db: Database, symbol: str, operator: str, note: str) -> None:
    if not operator.strip():
        raise ValueError("operator required")
    db.set_state(STRATEGY_HALT_KEY.format(symbol=symbol), None)
    db.audit("strategy_halt_cleared", {"symbol": symbol, "operator": operator, "note": note}, "warning")


def status(db: Database, settings: Settings) -> Dict[str, Any]:
    return {
        "settings": settings.public_dict(),
        "ticks": db.tick_summary(),
        "instruments": db.instruments(),
        "models": [{k: m[k] for k in ("version", "symbol", "model_type", "status", "approved_by")} for m in db.models()],
        "risk": {k: db.get_state(k) for k in ("risk_governor:paper", "risk_governor:demo") if db.get_state(k)},
        "open_trades": db.trades(status="open"),
    }
