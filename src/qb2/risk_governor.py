"""
Deterministic risk governor
===========================
Sits between the decision engine and any order path. It never reads model
outputs except the requested stake and contract, so no prediction can widen a
limit or lift a halt.

  - stake <= max_stake_fraction x current equity, rounded down to the cent; it is
    a fixed fraction, so it shrinks after losses (no martingale, no doubling)
  - realized daily loss, drawdown from peak, trades per day, concurrent positions
  - cooldown after N consecutive losses
  - stale tick or quote rejection
  - duplicate-order prevention by idempotency key
  - balance reconciliation: a mismatch halts trading
  - circuit breakers: too many disconnects per hour or abnormal API responses halt
A halt is latched and persisted; only reset_halt() with an operator name clears it.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from qb2.config import RiskLimits


@dataclass
class OrderRequest:
    symbol: str
    contract_type: str
    stake: float
    payout: float
    idempotency_key: str
    decision_time: float
    quote_time: float
    last_tick_epoch: float
    quote_source: str = "deriv_proposal"


@dataclass
class RiskVerdict:
    approved: bool
    reasons: List[str]
    stake: float = 0.0


@dataclass
class GovernorState:
    equity: float
    peak_equity: float
    day: str
    start_of_day_equity: float
    daily_realized_pnl: float = 0.0
    trades_today: int = 0
    open_positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    seen_keys: List[str] = field(default_factory=list)
    disconnects: List[float] = field(default_factory=list)
    abnormal_responses: int = 0


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskGovernor:
    MAX_SEEN_KEYS = 5000

    def __init__(
        self,
        limits: RiskLimits,
        initial_equity: float,
        store: Optional[Any] = None,
        state_key: str = "risk_governor:paper",
        clock: Callable[[], float] = time.time,
        audit: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
    ):
        self.limits = limits
        self.store = store
        self.state_key = state_key
        self.clock = clock
        self.audit = audit or (lambda *_: None)
        saved = store.get_state(state_key) if store is not None else None
        if saved:
            self.state = GovernorState(**saved)
        else:
            now = clock()
            self.state = GovernorState(equity=initial_equity, peak_equity=initial_equity,
                                       day=utc_day(now), start_of_day_equity=initial_equity)
        self._save()

    def _save(self) -> None:
        if self.store is not None:
            self.store.set_state(self.state_key, asdict(self.state))

    def _roll_day(self, now: float) -> None:
        day = utc_day(now)
        if day != self.state.day:
            self.state.day = day
            self.state.start_of_day_equity = self.state.equity
            self.state.daily_realized_pnl = 0.0
            self.state.trades_today = 0

    # ── Halts ────────────────────────────────────────────────────────────────

    def halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            self.audit("risk_halt", {"reason": reason, "state": self.snapshot()}, "critical")
        self._save()

    def reset_halt(self, operator: str, note: str) -> None:
        if not operator.strip():
            raise ValueError("An operator name is required to clear a halt")
        self.audit("risk_halt_reset", {"operator": operator, "note": note, "previous_reason": self.state.halt_reason}, "warning")
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.abnormal_responses = 0
        self.state.disconnects = []
        self._save()

    def initialize_equity(self, balance: float) -> None:
        """Set the starting equity once, on a fresh state. Later runs reconcile instead."""
        if self.state.peak_equity > 0:
            return
        self.state.equity = self.state.peak_equity = self.state.start_of_day_equity = float(balance)
        self.audit("risk_equity_initialized", {"balance": balance}, "info")
        self._save()

    # ── Sizing ───────────────────────────────────────────────────────────────

    def stake_for(self, equity: Optional[float] = None) -> float:
        """Stake allowed now: a fixed fraction of equity, floored to the cent; 0 if below the minimum."""
        eq = self.state.equity if equity is None else equity
        raw = min(self.limits.max_stake, eq * self.limits.max_stake_fraction)
        stake = math.floor(raw * 100 + 1e-9) / 100.0
        return stake if stake >= self.limits.min_stake else 0.0

    # ── Approval ─────────────────────────────────────────────────────────────

    def approve(self, order: OrderRequest, now: Optional[float] = None) -> RiskVerdict:
        now = self.clock() if now is None else now
        self._roll_day(now)
        st, lim = self.state, self.limits
        reasons: List[str] = []

        if st.halted:
            reasons.append(f"halted: {st.halt_reason}")
        if order.idempotency_key in st.seen_keys or order.idempotency_key in st.open_positions:
            reasons.append("duplicate order (idempotency key already used)")
        if now < st.cooldown_until:
            reasons.append(f"cooldown after {lim.consecutive_loss_limit} consecutive losses until {st.cooldown_until:.0f}")
        if len(st.open_positions) >= lim.max_concurrent_positions:
            reasons.append(f"{len(st.open_positions)} open position(s) (max {lim.max_concurrent_positions})")
        if st.trades_today >= lim.max_trades_per_day:
            reasons.append(f"{st.trades_today} trades today (max {lim.max_trades_per_day})")
        if -st.daily_realized_pnl >= lim.max_daily_loss_fraction * st.start_of_day_equity:
            reasons.append(f"daily loss {-st.daily_realized_pnl:.2f} reached limit")
        drawdown = 1.0 - st.equity / st.peak_equity if st.peak_equity > 0 else 1.0
        if drawdown >= lim.max_drawdown_fraction:
            reasons.append(f"drawdown {drawdown:.1%} reached limit {lim.max_drawdown_fraction:.0%}")
            self.halt(f"max drawdown {drawdown:.1%}")
        if now - order.last_tick_epoch > lim.max_tick_age_seconds:
            reasons.append(f"stale tick ({now - order.last_tick_epoch:.1f}s)")
        if order.quote_source == "deriv_proposal" and now - order.quote_time > lim.max_quote_age_seconds:
            reasons.append(f"stale quote ({now - order.quote_time:.1f}s)")

        allowed = self.stake_for()
        if allowed <= 0:
            reasons.append(f"equity {st.equity:.2f} too small for the minimum stake {lim.min_stake:.2f}")
        elif order.stake > allowed + 1e-9:
            reasons.append(f"stake {order.stake:.2f} exceeds allowed {allowed:.2f}")
        if order.stake < lim.min_stake:
            reasons.append(f"stake {order.stake:.2f} below minimum {lim.min_stake:.2f}")
        # A worst-case loss of this stake must not breach the daily limit
        if -st.daily_realized_pnl + order.stake > lim.max_daily_loss_fraction * st.start_of_day_equity + 1e-9:
            reasons.append("a loss on this stake would exceed the daily loss limit")
        if not (order.payout > order.stake > 0):
            reasons.append("payout must exceed stake")

        if reasons:
            return RiskVerdict(False, reasons)

        st.open_positions[order.idempotency_key] = {"symbol": order.symbol, "stake": order.stake, "opened": now}
        st.seen_keys.append(order.idempotency_key)
        st.seen_keys = st.seen_keys[-self.MAX_SEEN_KEYS:]
        st.trades_today += 1
        self._save()
        return RiskVerdict(True, ["approved"], order.stake)

    def release(self, key: str) -> None:
        """Forget an approved position that was never opened (e.g. buy rejected)."""
        self.state.open_positions.pop(key, None)
        self._save()

    def on_settled(self, key: str, profit: float, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        self._roll_day(now)
        st = self.state
        st.open_positions.pop(key, None)
        st.equity += profit
        st.daily_realized_pnl += profit
        st.peak_equity = max(st.peak_equity, st.equity)
        if profit < 0:
            st.consecutive_losses += 1
            if st.consecutive_losses >= self.limits.consecutive_loss_limit:
                st.cooldown_until = now + self.limits.cooldown_minutes * 60
                st.consecutive_losses = 0
                self.audit("risk_cooldown", {"until": st.cooldown_until}, "warning")
        else:
            st.consecutive_losses = 0
        if st.peak_equity > 0 and 1.0 - st.equity / st.peak_equity >= self.limits.max_drawdown_fraction:
            self.halt(f"max drawdown reached ({1.0 - st.equity / st.peak_equity:.1%})")
        self._save()

    # ── Reconciliation & circuit breakers ────────────────────────────────────

    def reconcile(self, reported_balance: float, expected_balance: Optional[float] = None) -> bool:
        expected = self.state.equity if expected_balance is None else expected_balance
        diff = abs(reported_balance - expected)
        if diff > self.limits.reconciliation_tolerance:
            self.halt(f"balance mismatch: reported {reported_balance:.2f}, expected {expected:.2f}")
            return False
        return True

    def record_disconnect(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        self.state.disconnects = [t for t in self.state.disconnects if now - t < 3600] + [now]
        if len(self.state.disconnects) > self.limits.max_disconnects_per_hour:
            self.halt(f"{len(self.state.disconnects)} WebSocket disconnects in the last hour")
        self._save()

    def record_abnormal_response(self, detail: str) -> None:
        self.state.abnormal_responses += 1
        if self.state.abnormal_responses >= self.limits.max_abnormal_responses:
            self.halt(f"{self.state.abnormal_responses} abnormal API responses (last: {detail[:120]})")
        self._save()

    def snapshot(self) -> Dict[str, Any]:
        st = self.state
        return {
            "equity": round(st.equity, 2),
            "peak_equity": round(st.peak_equity, 2),
            "drawdown": round(1.0 - st.equity / st.peak_equity, 4) if st.peak_equity > 0 else None,
            "day": st.day,
            "daily_realized_pnl": round(st.daily_realized_pnl, 2),
            "daily_loss_limit": round(self.limits.max_daily_loss_fraction * st.start_of_day_equity, 2),
            "trades_today": st.trades_today,
            "max_trades_per_day": self.limits.max_trades_per_day,
            "open_positions": len(st.open_positions),
            "consecutive_losses": st.consecutive_losses,
            "cooldown_until": st.cooldown_until,
            "halted": st.halted,
            "halt_reason": st.halt_reason,
            "next_stake": self.stake_for(),
        }
