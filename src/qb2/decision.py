"""
Five-minute Rise/Fall decision engine
=====================================
For one instrument at one moment, produce TRADE, NO_TRADE or HALTED with the
numbers and reasons behind it.

Pricing uses the actual Deriv proposal: `stake` is the ask price, `payout` is
the total returned on a win (stake included), so

    EV(CALL) = P(Rise) · payout - stake          break-even P = stake / payout
    EV(PUT)  = P(Fall) · payout - stake          P(Fall) = 1 - P(Rise) - P(tie)

A tie loses both sides. The probability is first lowered by z·σ (model
uncertainty) and a trade needs that conservative EV to exceed `min_edge_fraction`
of the stake. The model estimates P(exit spot > entry spot) where the entry spot
is the first tick after purchase (see labels.py), not the tick visible now.

The engine has no access to the account or the order path. Its output is only a
request: the RiskGovernor decides whether anything is placed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from qb2.config import Settings
from qb2.schema import FALL, RISE, ContractQuote

TRADE, NO_TRADE, HALTED = "TRADE", "NO_TRADE", "HALTED"


@dataclass
class DecisionInput:
    symbol: str
    now: float
    last_tick_epoch: Optional[float]
    p_rise: Optional[float]
    p_std: float = 0.0
    p_tie: float = 0.0
    regime: str = "UNKNOWN"
    model_version: Optional[str] = None
    model_approved: bool = False               # only a human-promoted champion counts
    quotes: Dict[str, ContractQuote] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""
    confidence_penalty: float = 1.0            # >1 widens uncertainty after drift warnings


@dataclass
class Decision:
    symbol: str
    decided_at: float
    action: str
    reasons: List[str]
    contract_type: Optional[str] = None
    p_rise: Optional[float] = None
    p_fall: Optional[float] = None
    p_tie: float = 0.0
    uncertainty: float = 0.0
    ev_call: Optional[float] = None
    ev_put: Optional[float] = None
    ev_call_conservative: Optional[float] = None
    ev_put_conservative: Optional[float] = None
    breakeven_call: Optional[float] = None
    breakeven_put: Optional[float] = None
    stake: Optional[float] = None
    payout: Optional[float] = None
    proposal_id: Optional[str] = None
    regime: str = "UNKNOWN"
    data_age_seconds: Optional[float] = None
    model_version: Optional[str] = None
    assumptions: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DecisionEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _quote_problems(self, q: Optional[ContractQuote], ctype: str, now: float) -> List[str]:
        s = self.settings
        if q is None:
            return [f"no {ctype} quote"]
        problems = []
        if q.contract_type != ctype:
            problems.append(f"{ctype} quote has contract type {q.contract_type}")
        if q.currency != s.currency:
            problems.append(f"{ctype} quote currency {q.currency} != {s.currency}")
        if q.duration_seconds != s.contract_seconds:
            problems.append(f"{ctype} quote duration {q.duration_seconds}s != {s.contract_seconds}s")
        if not (q.stake > 0 and q.payout > q.stake):
            problems.append(f"{ctype} quote payout {q.payout} does not exceed stake {q.stake}")
        if q.source in ("deriv_proposal",) and now - q.quote_time > s.risk.max_quote_age_seconds:
            problems.append(f"{ctype} quote is {now - q.quote_time:.1f}s old")
        return problems

    def decide(self, inp: DecisionInput) -> Decision:
        s = self.settings
        d = Decision(symbol=inp.symbol, decided_at=inp.now, action=NO_TRADE, reasons=[],
                     regime=inp.regime, model_version=inp.model_version, p_tie=inp.p_tie)
        d.assumptions = {
            "contract": f"Rise/Fall {s.contract_seconds}s, entry = first tick after purchase, "
                        f"exit = last tick at or before expiry, tie loses",
            "uncertainty_z": s.uncertainty_z * inp.confidence_penalty,
            "min_edge_fraction": s.min_edge_fraction,
        }

        if inp.halted:
            d.action = HALTED
            d.reasons.append(f"halted: {inp.halt_reason or 'unspecified'}")
            return d

        if inp.last_tick_epoch is None:
            d.reasons.append("no market data")
            return d
        d.data_age_seconds = float(inp.now - inp.last_tick_epoch)
        if d.data_age_seconds > s.risk.max_tick_age_seconds:
            d.reasons.append(f"stale data: last tick {d.data_age_seconds:.1f}s ago")
            return d

        if inp.p_rise is None or not math.isfinite(inp.p_rise):
            d.reasons.append("no model probability")
            return d
        d.p_rise = float(inp.p_rise)
        d.p_fall = float(max(0.0, 1.0 - inp.p_rise - inp.p_tie))
        d.uncertainty = float(max(inp.p_std, 0.0))
        if not inp.model_approved:
            d.reasons.append("model is not an approved champion (research estimate only)")

        call_q, put_q = inp.quotes.get(RISE), inp.quotes.get(FALL)
        problems = self._quote_problems(call_q, RISE, inp.now) + self._quote_problems(put_q, FALL, inp.now)
        if problems:
            d.reasons.extend(problems)
            return d
        assert call_q is not None and put_q is not None
        d.assumptions["payout_source"] = call_q.source

        z = s.uncertainty_z * inp.confidence_penalty
        d.breakeven_call, d.breakeven_put = call_q.breakeven_probability, put_q.breakeven_probability
        d.ev_call = d.p_rise * call_q.payout - call_q.stake
        d.ev_put = d.p_fall * put_q.payout - put_q.stake
        p_rise_lo = max(0.0, d.p_rise - z * d.uncertainty)
        p_fall_lo = max(0.0, d.p_fall - z * d.uncertainty)
        d.ev_call_conservative = p_rise_lo * call_q.payout - call_q.stake
        d.ev_put_conservative = p_fall_lo * put_q.payout - put_q.stake

        best, q, ev_lo = (RISE, call_q, d.ev_call_conservative)
        if d.ev_put_conservative > d.ev_call_conservative:
            best, q, ev_lo = FALL, put_q, d.ev_put_conservative
        required = s.min_edge_fraction * q.stake
        d.contract_type, d.stake, d.payout, d.proposal_id = best, q.stake, q.payout, q.proposal_id

        if ev_lo < required:
            d.reasons.append(
                f"best conservative EV {ev_lo:+.4f} ({best}) below required {required:.4f} "
                f"(P lowered by {z:.1f}σ, break-even {q.breakeven_probability:.4f})"
            )
            d.contract_type = None
            return d
        if not inp.model_approved:
            d.contract_type = None
            return d

        d.action = TRADE
        d.reasons.append(
            f"{best}: conservative EV {ev_lo:+.4f} on stake {q.stake:.2f} "
            f"(P={d.p_rise if best == RISE else d.p_fall:.4f} ± {d.uncertainty:.4f}, break-even {q.breakeven_probability:.4f})"
        )
        return d
