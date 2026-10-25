"""
Decision loop (paper by default, demo only through DemoExecutor)
================================================================
Each step for one symbol:
  1. settle due trades (paper: from stored ticks with the Deriv settlement rule;
     demo: from proposal_open_contract via the executor)
  2. halts: governor halt, per-symbol strategy halt (drift), missing champion
  3. features from stored ticks up to `now`; champion model -> P(Rise) ± σ
  4. live Deriv proposals for both sides at the governor's stake
  5. DecisionEngine -> TRADE / NO_TRADE / HALTED, recorded with its reasons
  6. TRADE -> RiskGovernor.approve -> paper fill, or demo purchase by the executor

Without an approved champion the loop still records decisions (as research
estimates) but never trades. Without quotes there is no EV and no trade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import numpy as np

from qb2.config import Settings
from qb2.data_quality import clean_ticks
from qb2.decision import TRADE, Decision, DecisionEngine, DecisionInput
from qb2.features import compute_features
from qb2.labels import SettlementRule, settle
from qb2.models import TrainedModel
from qb2.registry import ModelRegistry, RegistryError
from qb2.risk_governor import OrderRequest, RiskGovernor
from qb2.schema import RISE, ContractQuote
from qb2.storage import Database

QuoteSource = Callable[[str, float], Awaitable[Dict[str, ContractQuote]]]
log = logging.getLogger("qb2.trader")

STRATEGY_HALT_KEY = "strategy_halt:{symbol}"
CONFIDENCE_KEY = "confidence_penalty:{symbol}"


class Trader:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        registry: ModelRegistry,
        governor: RiskGovernor,
        quote_source: Optional[QuoteSource] = None,
        executor: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ):
        if executor is not None and not settings.execution_enabled:
            raise RuntimeError("An executor was supplied but demo execution is not enabled in settings")
        self.settings = settings
        self.db = db
        self.registry = registry
        self.governor = governor
        self.quote_source = quote_source
        self.executor = executor
        self.mode = "demo" if executor is not None else "paper"
        self.clock = clock
        self.engine = DecisionEngine(settings)
        self._models: Dict[str, Tuple[str, TrainedModel]] = {}

    # ── Model & prediction ───────────────────────────────────────────────────

    def _champion(self, symbol: str) -> Optional[Tuple[str, TrainedModel]]:
        meta = self.registry.champion(symbol)
        if meta is None:
            self._models.pop(symbol, None)
            return None
        cached = self._models.get(symbol)
        if cached is None or cached[0] != meta["version"]:
            try:
                self._models[symbol] = (meta["version"], self.registry.load(meta["version"]))
            except RegistryError as exc:
                self.db.audit("model_load_failed", {"symbol": symbol, "error": str(exc)}, "critical")
                return None
        return self._models[symbol]

    def predict(self, symbol: str, now: float) -> Dict[str, Any]:
        ticks = self.db.load_ticks(symbol, start=now - 3600, end=now)
        clean, _ = clean_ticks(ticks, symbol, min_ticks=10, min_coverage=0.0)
        out: Dict[str, Any] = {"last_tick_epoch": float(clean["epoch"].values[-1]) if len(clean) else None,
                               "p_rise": None, "p_std": 0.0, "p_tie": 0.0, "model_version": None, "approved": False}
        champ = self._champion(symbol)
        if champ is None or len(clean) < 10:
            return out
        version, tm = champ
        feats = compute_features(clean["epoch"].values, clean["quote"].values, np.array([now]))
        x = feats[list(tm.feature_names)].values
        if not np.isfinite(x).all():
            return out
        p, s = tm.predict(x)
        out.update({"p_rise": float(p[0]), "p_std": float(s[0]), "p_tie": tm.p_tie,
                    "model_version": version, "approved": True})
        return out

    # ── Settlement ───────────────────────────────────────────────────────────

    def settle_paper(self, now: float) -> int:
        settled = 0
        rule = SettlementRule(duration_seconds=self.settings.contract_seconds)
        for t in self.db.trades(mode="paper", status="open"):
            if now < t["expiry_time"] + rule.max_exit_staleness_seconds:
                continue
            ticks = self.db.load_ticks(t["symbol"], start=t["start_time"] - 60, end=t["expiry_time"] + 120)
            lab = settle(ticks["epoch"].values, ticks["quote"].values, np.array([t["start_time"]]), rule)
            if not lab["valid"][0]:
                if now - t["expiry_time"] < 600:
                    continue   # data may still arrive
                status, profit = "void", 0.0
            else:
                side = 1 if t["contract_type"] == RISE else -1
                won = lab["outcome"][0] == side
                status, profit = ("won", round(t["payout"] - t["stake"], 2)) if won else ("lost", -t["stake"])
            self.db.settle_trade(t["id"], status, profit, lab["entry_quote"][0], lab["exit_quote"][0])
            self.governor.on_settled(t["idempotency_key"], profit, now=now)
            settled += 1
        return settled

    # ── One decision ─────────────────────────────────────────────────────────

    async def step(self, symbol: str, now: Optional[float] = None) -> Decision:
        now = self.clock() if now is None else now
        if self.executor is not None:
            await self.executor.settle_open(now)
        else:
            self.settle_paper(now)

        strategy_halt = self.db.get_state(STRATEGY_HALT_KEY.format(symbol=symbol))
        penalty = float(self.db.get_state(CONFIDENCE_KEY.format(symbol=symbol), 1.0))
        pred = self.predict(symbol, now)

        quotes: Dict[str, ContractQuote] = {}
        stake = self.governor.stake_for()
        halted = self.governor.state.halted or bool(strategy_halt)
        if not halted and stake > 0 and self.quote_source is not None and pred["p_rise"] is not None:
            try:
                quotes = await self.quote_source(symbol, stake)
            except Exception as exc:
                self.governor.record_abnormal_response(f"quote source: {exc}")
                self.db.audit("quote_source_failed", {"symbol": symbol, "error": str(exc)}, "warning")

        decision = self.engine.decide(DecisionInput(
            symbol=symbol, now=now, last_tick_epoch=pred["last_tick_epoch"], p_rise=pred["p_rise"],
            p_std=pred["p_std"], p_tie=pred["p_tie"], model_version=pred["model_version"],
            model_approved=pred["approved"], quotes=quotes, halted=halted,
            halt_reason=self.governor.state.halt_reason or (strategy_halt or {}).get("reason", ""),
            confidence_penalty=penalty,
        ))
        if stake <= 0 and decision.action != TRADE:
            decision.reasons.append("governor stake is zero (equity below minimum stake)")
        if not quotes and self.quote_source is None and pred["p_rise"] is not None:
            decision.reasons.append("no quote source connected: expected value unknown")
        decision_id = self.db.record_decision(decision.as_dict(), self.mode)

        if decision.action == TRADE:
            await self._act(decision, quotes, now, decision_id)
        return decision

    async def _act(self, d: Decision, quotes: Dict[str, ContractQuote], now: float, decision_id: int) -> None:
        q = quotes[d.contract_type]
        key = f"{self.mode}:{d.symbol}:{int(now // self.settings.decision_interval_seconds)}"
        order = OrderRequest(d.symbol, q.contract_type, q.stake, q.payout, key, now, q.quote_time,
                             float(now - (d.data_age_seconds or 0.0)), quote_source=q.source)
        verdict = self.governor.approve(order, now=now)
        self.db.audit("risk_verdict", {"key": key, "approved": verdict.approved, "reasons": verdict.reasons,
                                       "decision_id": decision_id})
        if not verdict.approved:
            return
        if self.executor is not None:
            await self.executor.execute(order, q, d, decision_id)
            return
        start = now + self.settings.execution_latency_seconds
        self.db.insert_trade({
            "mode": "paper", "symbol": d.symbol, "contract_type": q.contract_type, "idempotency_key": key,
            "stake": q.stake, "payout": q.payout, "start_time": start,
            "expiry_time": start + self.settings.contract_seconds, "model_version": d.model_version,
            "decision_id": decision_id,
        })

    async def run(self, symbols: List[str], stop: asyncio.Event) -> None:
        interval = self.settings.decision_interval_seconds
        while not stop.is_set():
            now = self.clock()
            for sym in symbols:
                try:
                    d = await self.step(sym, now)
                    log.info("%s %s %s", sym, d.action, "; ".join(d.reasons)[:200])
                except Exception as exc:
                    log.exception("Decision step failed for %s", sym)
                    self.db.audit("decision_step_failed", {"symbol": sym, "error": str(exc)}, "critical")
                    self.governor.record_abnormal_response(f"step failure: {exc}")
            delay = interval - (self.clock() % interval)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
