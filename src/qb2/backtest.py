"""
Contract-aware Rise/Fall backtester
===================================
Replays out-of-sample probabilities through the same DecisionEngine and
RiskGovernor used for paper and demo trading:

  - payout from recorded Deriv proposals when one exists within `quote_match_seconds`
    before the decision (payout/stake ratio applied to the governor's stake);
    otherwise an explicitly assumed ratio, which marks the result INCOMPLETE;
    with neither, no trade is simulated and the status is NO_PAYOUT_DATA
  - purchase time = decision time + latency + random jitter; a fraction of quotes
    is rejected (stale proposal / price moved)
  - settlement with labels.settle: entry = first tick after purchase, exit = last
    tick at or before expiry, ties lose; contracts spanning a data gap are void and
    refunded
  - stakes rounded down to the cent; one position at a time by default; daily
    limits, drawdown halt and cooldowns apply exactly as live
  - every decision is counted, including the ones where nothing was traded
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from qb2.config import Settings
from qb2.decision import TRADE, DecisionEngine, DecisionInput
from qb2.labels import SettlementRule, settle
from qb2.regime import regime_performance
from qb2.risk_governor import OrderRequest, RiskGovernor
from qb2.schema import FALL, RISE, ContractQuote
from qb2.stats_utils import bootstrap_ci, trade_metrics
from qb2.ticks import asof_index


@dataclass
class BacktestConfig:
    initial_equity: float = 1000.0
    payout_ratio: Optional[float] = None
    latency_seconds: float = 1.0
    latency_jitter_seconds: float = 0.5
    quote_reject_probability: float = 0.02
    quote_match_seconds: float = 30.0
    seed: int = 0


@dataclass
class BacktestResult:
    symbol: str
    status: str
    payout_source: str
    notes: List[str]
    metrics: Dict[str, Any]
    decision_counts: Dict[str, int]
    trades: pd.DataFrame
    equity_curve: List[float] = field(default_factory=list)
    regime_performance: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "payout_source": self.payout_source,
            "notes": self.notes,
            "metrics": self.metrics,
            "decision_counts": self.decision_counts,
            "regime_performance": self.regime_performance,
            "equity_curve": self.equity_curve[-2000:],
        }


def _historical_ratio(hist: Optional[pd.DataFrame], ctype: str, t: float, duration: int, window: float) -> Optional[float]:
    if hist is None or len(hist) == 0:
        return None
    h = hist[(hist["contract_type"] == ctype) & (hist["duration_seconds"] == duration)]
    if len(h) == 0:
        return None
    times = h["quote_time"].values
    i = np.searchsorted(times, t, side="right") - 1
    if i < 0 or t - times[i] > window:
        return None
    row = h.iloc[i]
    return float(row["payout"]) / float(row["stake"]) if row["stake"] > 0 else None


def _reason_key(reason: str) -> str:
    return reason.split(":")[0].split("(")[0].strip()[:60]


def run_contract_backtest(
    symbol: str,
    epochs: np.ndarray,
    quotes: np.ndarray,
    times: np.ndarray,
    p_rise: np.ndarray,
    p_std: np.ndarray,
    p_tie: float,
    settings: Settings,
    cfg: BacktestConfig = BacktestConfig(),
    regimes: Optional[np.ndarray] = None,
    historical_quotes: Optional[pd.DataFrame] = None,
) -> BacktestResult:
    epochs = np.asarray(epochs, np.int64)
    quotes = np.asarray(quotes, float)
    times = np.asarray(times, float)
    rng = np.random.default_rng(cfg.seed)
    duration = settings.contract_seconds
    rule = SettlementRule(duration_seconds=duration)
    hist = historical_quotes.sort_values("quote_time") if historical_quotes is not None and len(historical_quotes) else None

    clock = {"now": float(times[0]) if len(times) else 0.0}
    governor = RiskGovernor(settings.risk, cfg.initial_equity, store=None, state_key="backtest",
                            clock=lambda: clock["now"])
    engine = DecisionEngine(settings)
    counts: Counter = Counter()
    sources: Counter = Counter()
    pending: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    equity = [cfg.initial_equity]
    last_idx = asof_index(epochs, times)

    def settle_due(now: float) -> None:
        nonlocal pending
        due = [p for p in pending if p["expiry"] <= now]
        pending = [p for p in pending if p["expiry"] > now]
        for p in sorted(due, key=lambda x: x["expiry"]):
            governor.on_settled(p["key"], p["profit"], now=p["expiry"])
            trades.append(p)
            equity.append(governor.state.equity)

    for k in np.argsort(times):
        t = float(times[k])
        clock["now"] = t
        settle_due(t)
        counts["decisions"] += 1
        if not np.isfinite(p_rise[k]):
            counts["no_trade: no out-of-sample prediction"] += 1
            continue
        stake = governor.stake_for()
        if stake <= 0:
            counts["no_trade: stake below minimum"] += 1
            continue

        qmap: Dict[str, ContractQuote] = {}
        for ctype in (RISE, FALL):
            ratio, source = _historical_ratio(hist, ctype, t, duration, cfg.quote_match_seconds), "historical_proposal"
            if ratio is None and cfg.payout_ratio:
                ratio, source = cfg.payout_ratio, "assumed"
            if ratio is not None:
                qmap[ctype] = ContractQuote(symbol, ctype, stake, round(stake * ratio, 2), duration,
                                            settings.currency, t, source=source)
        if len(qmap) < 2:
            counts["no_trade: no payout data"] += 1
            continue

        li = last_idx[k]
        decision = engine.decide(DecisionInput(
            symbol=symbol, now=t, last_tick_epoch=float(epochs[li]) if li >= 0 else None,
            p_rise=float(p_rise[k]), p_std=float(p_std[k]) if np.isfinite(p_std[k]) else 0.0,
            p_tie=p_tie, regime=str(regimes[k]) if regimes is not None else "UNKNOWN",
            model_version="backtest", model_approved=True, quotes=qmap, halted=governor.state.halted,
            halt_reason=governor.state.halt_reason,
        ))
        if decision.action != TRADE:
            counts[f"{decision.action.lower()}: {_reason_key(decision.reasons[0]) if decision.reasons else ''}"] += 1
            continue

        q = qmap[decision.contract_type]
        key = f"{symbol}:{int(t)}"
        verdict = governor.approve(OrderRequest(symbol, q.contract_type, q.stake, q.payout, key, t, t,
                                                float(epochs[li]), quote_source=q.source), now=t)
        if not verdict.approved:
            counts[f"risk: {_reason_key(verdict.reasons[0])}"] += 1
            continue
        if rng.random() < cfg.quote_reject_probability:
            governor.release(key)
            counts["no_trade: quote rejected (simulated)"] += 1
            continue

        start = t + cfg.latency_seconds + rng.uniform(0.0, cfg.latency_jitter_seconds)
        lab = settle(epochs, quotes, np.array([start]), rule)
        side = 1 if q.contract_type == RISE else -1
        if not lab["valid"][0]:
            profit, status = 0.0, "void"
        else:
            win = lab["outcome"][0] == side
            profit = round(q.payout - q.stake, 2) if win else -q.stake
            status = "won" if win else "lost"
        sources[q.source] += 1
        counts["trades"] += 1
        pending.append({
            "key": key, "decision_time": t, "start": start, "expiry": start + duration,
            "contract_type": q.contract_type, "stake": q.stake, "payout": q.payout, "profit": profit,
            "status": status, "entry_quote": lab["entry_quote"][0], "exit_quote": lab["exit_quote"][0],
            "p_rise": float(p_rise[k]), "p_std": float(p_std[k]), "payout_source": q.source,
            "regime": str(regimes[k]) if regimes is not None else "UNKNOWN",
            "equity_before": governor.state.equity,
        })

    settle_due(float("inf"))
    tdf = pd.DataFrame(trades)
    settled = tdf[tdf["status"] != "void"] if len(tdf) else tdf
    metrics = trade_metrics(settled["profit"].values if len(settled) else [],
                            settled["stake"].values if len(settled) else [], cfg.initial_equity)
    if len(settled) >= 20:
        _, lo, hi = bootstrap_ci(settled["profit"].values / settled["stake"].values, np.mean, n_boot=1000, block=5)
        metrics.update({"return_ci_low": lo, "return_ci_high": hi})
    metrics.update({"final_equity": governor.state.equity, "void_trades": int((tdf["status"] == "void").sum()) if len(tdf) else 0,
                    "halted": governor.state.halted, "halt_reason": governor.state.halt_reason})

    notes = [
        f"latency {cfg.latency_seconds}s + U(0, {cfg.latency_jitter_seconds}s) and "
        f"{cfg.quote_reject_probability:.0%} quote rejection are assumptions",
        "settlement follows Deriv's published Rise/Fall rule (entry = first tick after start); "
        "real settlement comes from proposal_open_contract",
    ]
    if counts["trades"] == 0 and counts["no_trade: no payout data"]:
        status, src = "NO_PAYOUT_DATA", "none"
        notes.append("no historical proposals and no assumed payout: nothing simulated")
    elif sources.get("assumed"):
        status, src = "INCOMPLETE_ASSUMED_PAYOUT", "assumed"
        notes.append(f"{sources['assumed']} trade(s) priced with an ASSUMED payout ratio {cfg.payout_ratio}; "
                     "these results are not evidence of profitability")
    else:
        status, src = "COMPLETE", "historical_proposal" if sources else "none"

    reg_perf = []
    if len(settled):
        reg_perf = regime_performance(settled["regime"].values, settled["profit"].values / settled["stake"].values)
    return BacktestResult(symbol, status, src, notes, metrics, dict(counts), tdf,
                          [round(float(v), 2) for v in equity], reg_perf)
