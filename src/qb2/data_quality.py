"""
Tick data validation
====================
Checks run before any research or decision:
  - non-finite / non-positive prices          -> removed
  - out-of-order rows                          -> sorted (counted)
  - duplicate epochs                           -> first kept; conflicting quotes counted
  - gaps longer than gap_factor x median spacing -> reported (labels spanning them are void)
  - outliers: tick log returns beyond outlier_z robust sigmas -> flagged; removed only
    when they are isolated bad prints (jump out and straight back)
Raw ticks in the database are never modified; cleaning happens on read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


@dataclass
class DataQualityReport:
    symbol: str
    n_raw: int
    n_clean: int
    non_positive: int = 0
    out_of_order: int = 0
    duplicate_epochs: int = 0
    conflicting_duplicates: int = 0
    outliers_flagged: int = 0
    bad_prints_removed: int = 0
    gaps: int = 0
    max_gap_seconds: float = 0.0
    median_interval: float = 0.0
    interval_cv: float = 0.0
    first_epoch: int = 0
    last_epoch: int = 0
    coverage_ratio: float = 0.0       # time not inside gaps / total span
    issues: List[str] = field(default_factory=list)
    passed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def clean_ticks(
    df: pd.DataFrame,
    symbol: str,
    gap_factor: float = 5.0,
    min_gap_seconds: float = 10.0,
    outlier_z: float = 10.0,
    min_ticks: int = 1000,
    min_coverage: float = 0.95,
) -> Tuple[pd.DataFrame, DataQualityReport]:
    rep = DataQualityReport(symbol=symbol, n_raw=len(df), n_clean=0)
    if df is None or len(df) == 0:
        rep.issues.append("no ticks")
        return pd.DataFrame({"epoch": np.array([], dtype=np.int64), "quote": np.array([], dtype=float)}), rep

    d = df[["epoch", "quote"]].copy()
    d["quote"] = pd.to_numeric(d["quote"], errors="coerce")
    bad = ~np.isfinite(d["quote"].values) | (d["quote"].values <= 0)
    rep.non_positive = int(bad.sum())
    d = d[~bad]

    rep.out_of_order = int(np.sum(np.diff(d["epoch"].values) < 0))
    d = d.sort_values("epoch", kind="mergesort")

    dup = d["epoch"].duplicated(keep="first")
    rep.duplicate_epochs = int(dup.sum())
    if rep.duplicate_epochs:
        nunique = d.groupby("epoch")["quote"].nunique()
        rep.conflicting_duplicates = int((nunique > 1).sum())
    d = d[~dup].reset_index(drop=True)

    q = d["quote"].values.astype(float)
    e = d["epoch"].values.astype(np.int64)
    if len(q) >= 3:
        r = np.diff(np.log(q))
        med = float(np.median(r))
        mad = 1.4826 * float(np.median(np.abs(r - med))) or float(np.std(r)) or 1e-12
        z = (r - med) / mad
        flagged = np.abs(z) > outlier_z
        rep.outliers_flagged = int(flagged.sum())
        # Bad print at tick i: big move into i and an opposite big move out of i
        into, out = z[:-1], z[1:]
        isolated = (np.abs(into) > outlier_z) & (np.abs(out) > outlier_z) & (np.sign(into) != np.sign(out))
        drop = np.zeros(len(q), dtype=bool)
        drop[1:-1] = isolated
        rep.bad_prints_removed = int(drop.sum())
        q, e = q[~drop], e[~drop]

    rep.n_clean = len(q)
    if len(e) >= 2:
        intervals = np.diff(e).astype(float)
        rep.median_interval = float(np.median(intervals))
        rep.interval_cv = float(np.std(intervals) / np.mean(intervals)) if np.mean(intervals) > 0 else 0.0
        gap_limit = max(min_gap_seconds, gap_factor * rep.median_interval)
        gaps = intervals > gap_limit
        rep.gaps = int(gaps.sum())
        rep.max_gap_seconds = float(intervals.max())
        span = float(e[-1] - e[0])
        rep.coverage_ratio = float(1.0 - intervals[gaps].sum() / span) if span > 0 else 0.0
        rep.first_epoch, rep.last_epoch = int(e[0]), int(e[-1])

    if rep.n_clean < min_ticks:
        rep.issues.append(f"only {rep.n_clean} clean ticks (< {min_ticks})")
    if rep.coverage_ratio < min_coverage:
        rep.issues.append(f"coverage {rep.coverage_ratio:.1%} below {min_coverage:.0%}")
    if rep.conflicting_duplicates:
        rep.issues.append(f"{rep.conflicting_duplicates} epochs with conflicting quotes")
    if rep.outliers_flagged > max(5, 0.001 * rep.n_clean):
        rep.issues.append(f"{rep.outliers_flagged} outlier returns flagged")
    rep.passed = not rep.issues
    return pd.DataFrame({"epoch": e, "quote": q}), rep
