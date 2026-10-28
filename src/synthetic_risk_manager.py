"""
Quantitative Risk & Fractional Kelly Engine for Synthetic Indices
=================================================================
  - Fractional Kelly sizing from the brain's win probability and payoff
  - Deriv synthetic contract specs (Volatility, Boom/Crash, Step)
  - Drawdown damping, and rejection when the minimum lot alone would exceed the risk cap
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np


# Default specifications for Deriv Synthetic Indices if broker metadata is offline
DERIV_SYNTHETIC_SPECS: Dict[str, Dict[str, float]] = {
    "VOLATILITY 75 INDEX":       {"min_lot": 0.001, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "R_75":                     {"min_lot": 0.001, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "VOLATILITY 75 (1S) INDEX":  {"min_lot": 0.005, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "1HZ75V":                   {"min_lot": 0.005, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "VOLATILITY 100 INDEX":      {"min_lot": 0.10,  "max_lot": 50.0, "step": 0.01,  "point": 0.01},
    "R_100":                    {"min_lot": 0.10,  "max_lot": 50.0, "step": 0.01,  "point": 0.01},
    "VOLATILITY 100 (1S) INDEX": {"min_lot": 0.10,  "max_lot": 50.0, "step": 0.01,  "point": 0.01},
    "1HZ100V":                  {"min_lot": 0.10,  "max_lot": 50.0, "step": 0.01,  "point": 0.01},
    "VOLATILITY 25 INDEX":       {"min_lot": 0.50,  "max_lot": 100.0, "step": 0.01, "point": 0.001},
    "R_25":                     {"min_lot": 0.50,  "max_lot": 100.0, "step": 0.01, "point": 0.001},
    "VOLATILITY 50 INDEX":       {"min_lot": 0.001, "max_lot": 50.0, "step": 0.001, "point": 0.0001},
    "R_50":                     {"min_lot": 0.001, "max_lot": 50.0, "step": 0.001, "point": 0.0001},
    "VOLATILITY 50 (1S) INDEX":  {"min_lot": 0.005, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "1HZ50V":                   {"min_lot": 0.005, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "VOLATILITY 25 (1S) INDEX":  {"min_lot": 0.005, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "1HZ25V":                   {"min_lot": 0.005, "max_lot": 20.0, "step": 0.001, "point": 0.01},
    "VOLATILITY 10 INDEX":       {"min_lot": 0.20,  "max_lot": 100.0, "step": 0.01, "point": 0.001},
    "R_10":                     {"min_lot": 0.20,  "max_lot": 100.0, "step": 0.01, "point": 0.001},
    "VOLATILITY 10 (1S) INDEX":  {"min_lot": 0.20,  "max_lot": 100.0, "step": 0.01, "point": 0.01},
    "1HZ10V":                   {"min_lot": 0.20,  "max_lot": 100.0, "step": 0.01, "point": 0.01},
    "CRASH 1000 INDEX":         {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "CRASH_1000":               {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "BOOM 1000 INDEX":          {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "BOOM_1000":                {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "CRASH 500 INDEX":          {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "CRASH_500":                {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "BOOM 500 INDEX":           {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "BOOM_500":                 {"min_lot": 0.20,  "max_lot": 50.0, "step": 0.01,  "point": 0.001},
    "CRASH 300 INDEX":          {"min_lot": 0.10,  "max_lot": 30.0, "step": 0.01,  "point": 0.001},
    "CRASH_300":                {"min_lot": 0.10,  "max_lot": 30.0, "step": 0.01,  "point": 0.001},
    "CRASH300N":                {"min_lot": 0.10,  "max_lot": 30.0, "step": 0.01,  "point": 0.001},
    "BOOM 300 INDEX":           {"min_lot": 0.10,  "max_lot": 30.0, "step": 0.01,  "point": 0.001},
    "BOOM_300":                 {"min_lot": 0.10,  "max_lot": 30.0, "step": 0.01,  "point": 0.001},
    "BOOM300N":                 {"min_lot": 0.10,  "max_lot": 30.0, "step": 0.01,  "point": 0.001},
    "STEP INDEX":               {"min_lot": 0.10,  "max_lot": 100.0, "step": 0.01, "point": 0.1},
}


@dataclass
class SizingResult:
    volume: float
    risk_dollars: float
    risk_percentage: float     # Actual risk as % of balance after lot rounding
    kelly_fraction: float      # Target risk as % of balance before lot rounding
    is_valid: bool
    rejection_reason: str = ""


def _rejected(reason: str) -> SizingResult:
    return SizingResult(0.0, 0.0, 0.0, 0.0, False, reason)


class SyntheticRiskManager:
    """Fractional Kelly position sizing for Deriv synthetic indices."""

    def __init__(
        self,
        max_risk_pct: float = 0.035,
        kelly_fraction: float = 0.35,
        max_drawdown_limit: float = 0.18,
        max_open_positions: int = 6,
        max_open_trades: Optional[int] = None,
        aggressive_mode: bool = False,
    ):
        if max_open_trades is not None:
            max_open_positions = max_open_trades
        self.aggressive_mode = aggressive_mode
        self.max_risk_pct = max_risk_pct
        self.kelly_fraction = kelly_fraction
        self.max_drawdown_limit = max_drawdown_limit
        self.max_open_positions = max_open_positions

    @staticmethod
    def full_kelly(win_probability: float, reward_risk_ratio: float, loss_r: float = 1.0) -> float:
        """
        Kelly fraction for a bet that wins b·R with probability p and loses L·R otherwise:
            f* = p / L - (1 - p) / b      (L = 1 gives the familiar (p·b - (1 - p)) / b)
        """
        if reward_risk_ratio <= 0.01 or loss_r <= 0:
            return 0.0
        p = float(np.clip(win_probability, 0.05, 0.95))
        return p / loss_r - (1.0 - p) / reward_risk_ratio

    def compute_kelly_risk(
        self,
        win_probability: float,
        reward_risk_ratio: float,
        loss_r: float = 1.0,
    ) -> float:
        """Fractional Kelly risk, clamped to [0.2%, max_risk_pct]; 0 when expectancy is negative."""
        f = self.full_kelly(win_probability, reward_risk_ratio, loss_r)
        if f <= 0.0:
            return 0.0
        return float(np.clip(f * self.kelly_fraction, 0.002, self.max_risk_pct))

    def compute_adaptive_kelly_risk(
        self,
        win_probability: float,
        reward_risk_ratio: float,
        math_edge_score: float = 70.0,
        loss_r: float = 1.0,
    ) -> float:
        """
        Fractional Kelly with a multiplier that steps with the edge score:
          normal:     >=85: 1.3x (max 0.45) | 70-84: 1.0x | <70: 0.6x (min 0.15)
          aggressive: >=88: 1.3x (max 0.65) | 75-87: 1.0x | <75: 0.7x (min 0.20)
        """
        if self.aggressive_mode:
            if math_edge_score >= 88.0:
                mult = min(0.65, self.kelly_fraction * 1.3)
            elif math_edge_score >= 75.0:
                mult = self.kelly_fraction
            else:
                mult = max(0.20, self.kelly_fraction * 0.7)
        else:
            if math_edge_score >= 85.0:
                mult = min(0.45, self.kelly_fraction * 1.3)
            elif math_edge_score >= 70.0:
                mult = self.kelly_fraction
            else:
                mult = max(0.15, self.kelly_fraction * 0.6)

        f = self.full_kelly(win_probability, reward_risk_ratio, loss_r)
        if f <= 0.0:
            return 0.0
        return float(np.clip(f * mult, 0.002, self.max_risk_pct))

    @staticmethod
    def compute_expected_dollar_yield(
        win_probability: float,
        risk_dollars: float,
        reward_dollars: float,
    ) -> float:
        """E[$] = p · Reward$ - (1 - p) · Risk$"""
        p = float(np.clip(win_probability, 0.0, 1.0))
        return p * reward_dollars - (1.0 - p) * risk_dollars

    @staticmethod
    def is_symbol_tradable_for_account(symbol: str, balance: float) -> tuple[bool, str]:
        """
        Micro-account capital preservation gate.
        For accounts < $50, only allow instruments with true micro-fractional lot sizes (<= 0.005),
        such as Volatility 75 (0.001), Volatility 75 1s (0.005), Volatility 50 (0.001), Volatility 50 1s (0.005).
        Instruments with 0.10+ min lots (Crash/Boom/Step/Vol 25) can wipe micro accounts on single adverse ticks.
        """
        spec = DERIV_SYNTHETIC_SPECS.get(
            symbol.upper().replace("_", " ").strip(),
            DERIV_SYNTHETIC_SPECS.get(symbol.upper(), {}),
        )
        min_lot = float(spec.get("min_lot", 0.01))
        if balance < 50.0 and min_lot > 0.005:
            return False, f"Minimum lot {min_lot:g} too large for micro account (${balance:.2f} < $50.00)"
        return True, ""

    @staticmethod
    def volume_limits(symbol: str, broker_rules: Optional[Any]) -> tuple[float, float, float]:
        """(min_lot, max_lot, lot_step) from broker rules, falling back to the Deriv specs."""
        spec = DERIV_SYNTHETIC_SPECS.get(
            symbol.upper().replace("_", " ").strip(),
            DERIV_SYNTHETIC_SPECS.get(symbol.upper(), {}),
        )
        min_vol = float(getattr(broker_rules, "volume_min", 0.0) or spec.get("min_lot", 0.01))
        max_vol = float(getattr(broker_rules, "volume_max", 0.0) or spec.get("max_lot", 50.0))
        step = float(getattr(broker_rules, "volume_step", 0.0) or spec.get("step", 0.01))
        return min_vol, max_vol, step if step > 0 else 0.01

    def normalize_volume(
        self,
        symbol: str,
        raw_volume: float,
        broker_rules: Optional[Any] = None
    ) -> float:
        """Round down to the lot step and clamp into [min_lot, max_lot]."""
        min_vol, max_vol, step = self.volume_limits(symbol, broker_rules)
        # Epsilon guards against 0.003 / 0.001 = 2.9999999999999996
        rounded = round(math.floor(raw_volume / step + 1e-9) * step, 6)
        return float(min(max(rounded, min_vol), max_vol))

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        account_balance: float,
        win_probability: float,
        broker_rules: Optional[Any] = None,
        current_drawdown_fraction: float = 0.0,
        math_edge_score: float = 70.0,
        loss_r: float = 1.0,
        risk_scale: float = 1.0,
        reward_risk_ratio: Optional[float] = None,
    ) -> SizingResult:
        """
        Lot size from adaptive fractional Kelly. `risk_scale` (clamped to [0, 1])
        lets callers shrink risk, for example in high-VaR regimes; it can never raise it.
        `reward_risk_ratio` overrides the TP/SL ratio, e.g. with the payoff net of costs.
        """
        if account_balance <= 0:
            return _rejected("Zero or negative balance")

        tradable, rej_msg = self.is_symbol_tradable_for_account(symbol, account_balance)
        if not tradable:
            return _rejected(rej_msg)

        price_risk = abs(float(entry_price) - float(stop_loss))
        if price_risk <= 0:
            return _rejected("Invalid price risk (entry == stop_loss)")

        if current_drawdown_fraction >= self.max_drawdown_limit:
            return _rejected(f"Max drawdown limit reached ({current_drawdown_fraction:.1%})")

        rr = abs(float(target_price) - float(entry_price)) / price_risk if reward_risk_ratio is None else reward_risk_ratio
        kelly_risk = self.compute_adaptive_kelly_risk(
            win_probability=win_probability,
            reward_risk_ratio=rr,
            math_edge_score=math_edge_score,
            loss_r=loss_r,
        )
        if kelly_risk <= 0.0:
            return _rejected("Mathematical expectancy is negative (f* <= 0)")

        dd_factor = max(0.4, 1.0 - current_drawdown_fraction * 3.0) if current_drawdown_fraction > 0.05 else 1.0
        target_risk = kelly_risk * dd_factor * float(np.clip(risk_scale, 0.0, 1.0))
        if target_risk <= 0.0:
            return _rejected("Risk scaled to zero")

        tick_size = float(getattr(broker_rules, "tick_size", 0.0) or 0.0)
        tick_value = float(getattr(broker_rules, "tick_value", 0.0) or 0.0)
        value_per_price_unit = tick_value / tick_size if tick_size > 0 and tick_value > 0 else 1.0
        risk_per_lot = price_risk * value_per_price_unit

        min_vol, _, _ = self.volume_limits(symbol, broker_rules)
        volume = self.normalize_volume(symbol, account_balance * target_risk / risk_per_lot, broker_rules)
        actual_risk = volume * risk_per_lot
        # Clamping up to the broker's minimum lot can multiply the intended risk;
        # on micro accounts (<= $50), allow a buffer (up to 1.8x cap) for broker minimum lots
        # while strictly refusing catastrophic exposures (>12% / 40%).
        risk_cap = (
            self.max_risk_pct * 2.5
            if (account_balance <= 50.0 and volume <= min_vol * 1.0001)
            else self.max_risk_pct
        )
        if actual_risk > account_balance * risk_cap * 1.0001:
            return _rejected(
                f"Minimum lot {volume:g} risks {actual_risk / account_balance:.1%} of balance "
                f"(cap {self.max_risk_pct:.1%})"
            )

        return SizingResult(
            volume=volume,
            risk_dollars=round(actual_risk, 2),
            risk_percentage=round(actual_risk / account_balance * 100.0, 3),
            kelly_fraction=round(target_risk * 100.0, 3),
            is_valid=True,
        )
