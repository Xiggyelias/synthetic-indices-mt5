"""
Quantitative Strategy Engine for Synthetic Indices
===================================================
Turns a CalculativeBrain analysis into an executable, sized signal.

The brain owns all probabilities, targets and scores. This layer only:
  - applies the minimum edge gate
  - labels Kalman alignment (informational, never changes the score)
  - shrinks size in elevated VaR regimes (never enlarges it)
  - sizes with fractional Kelly and prices the expected dollar yield
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import pandas as pd

from quant_brain import CalculativeBrain, QuantBrainAnalysis, kalman_alignment
from synthetic_risk_manager import SizingResult, SyntheticRiskManager


@dataclass
class SyntheticSignal:
    symbol: str
    direction: str              # "BUY" or "SELL"
    strategy_type: str          # The brain's strategy archetype
    entry_price: float
    stop_loss: float
    take_profit: float          # TP1, the level the win probability refers to
    math_edge_score: float      # 0 to 100
    calculated_win_prob: float
    reward_risk_ratio: float
    half_life_horizon_bars: int
    take_profit_2: float = 0.0  # Extended target (informational)
    expected_yield_dollars: float = 0.0
    sizing: Optional[SizingResult] = None
    rationale: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_analysis: Optional[QuantBrainAnalysis] = None
    kalman_confirmation: str = "NEUTRAL"  # "CONFIRMS", "NEUTRAL", "CONTRADICTS"
    var_risk_level: str = "NORMAL"        # "LOW", "NORMAL", "ELEVATED", "EXTREME"


def thesis_intact(analysis: QuantBrainAnalysis, direction: str, strategy_type: str) -> bool:
    """
    Does the reason a trade was opened still hold on the latest bars?
      FRACTAL_MOMENTUM:  the 60-bar drift still points the trade's way and the market has
                         not flipped to anti-persistent
      OU_MEAN_REVERSION: reversion evidence has not disappeared (ADF t < -1.5)
    Exits use looser bars than entries (drift t > 0 vs > 1.96, ADF t < -1.5 vs < -2.89),
    so ordinary noise around the entry threshold does not shake a valid trade out.
    Other archetypes have short, fixed horizons and no model-based invalidation.
    """
    sign = 1.0 if direction == "BUY" else -1.0
    if strategy_type == "FRACTAL_MOMENTUM":
        return sign * float(analysis.metadata.get("trend_tstat", 0.0)) > 0 and analysis.hurst.regime != "ANTI_PERSISTENT"
    if strategy_type == "OU_MEAN_REVERSION":
        return analysis.ou.adf_tstat < -1.5
    return True


def var_risk_level(var_95_pct: float) -> Tuple[str, float]:
    """VaR tier and the position-size multiplier applied to it (never above 1.0)."""
    if var_95_pct < 1.0:
        return "LOW", 1.0
    if var_95_pct < 2.5:
        return "NORMAL", 1.0
    if var_95_pct < 4.0:
        return "ELEVATED", 0.75
    return "EXTREME", 0.50


def _rationale(a: QuantBrainAnalysis, kalman: str, var_level: str, rr: float) -> str:
    tail = (
        f"p={a.calculated_win_probability:.0%}, E={a.expected_yield_r:+.2f}R, R:R {rr:.2f}. "
        f"Kalman: {kalman} (Z={a.kalman_zscore:+.2f}). VaR {a.var_95_pct:.2f}% ({var_level})."
    )
    arch = a.strategy_archetype
    if arch == "OU_MEAN_REVERSION":
        head = (f"O-U mean reversion. H={a.hurst.hurst_exponent:.3f}, Z={a.ou.current_zscore:+.2f}σ, "
                f"ADF t={a.ou.adf_tstat:.2f}, τ½={a.ou.half_life:.1f}b.")
    elif arch == "FRACTAL_MOMENTUM":
        head = (f"Persistent trend. H={a.hurst.hurst_exponent:.3f}, R²={a.hurst.r_squared:.3f}, "
                f"entropy={a.entropy.normalized_entropy:.3f}.")
    elif arch == "BERNOULLI_STEP_SCALP" and a.step_analysis is not None:
        s = a.step_analysis
        head = (f"Step alternation. Runs Z={s.runs_zscore:+.2f}, last move {s.current_streak_direction}, "
                f"P(continue)={s.streak_continuation_prob:.0%}.")
    elif arch == "KALMAN_FAIR_VALUE_REVERSION":
        head = (f"Kalman fair value reversion. Deviation Z={a.kalman_zscore:+.2f}σ, "
                f"Fair={a.kalman_fair_value:.4f}.")
    elif arch == "FRACTIONAL_KINETIC_SNIPE":
        head = (f"Fractional Kinetic Snipe. Z_ffd={a.metadata.get('z_ffd', 0.0):+.2f}σ, "
                f"Vel={a.metadata.get('sg_vel', 0.0):+.3f}, Acc={a.metadata.get('sg_acc', 0.0):+.3f}.")
    elif a.poisson is not None:
        ph = a.poisson
        head = (f"{arch.replace('_', ' ').title()}. Spikes/bar={ph.base_rate:.3f}, "
                f"bars since spike={ph.bars_since_spike}, λ={ph.hawkes_intensity:.4f}, η={ph.branching_ratio:.2f}.")
    else:
        head = arch
    return f"{head} {tail}"


class SyntheticStrategy:
    """Gate, size and package the brain's recommendation."""

    def __init__(
        self,
        brain: Optional[CalculativeBrain] = None,
        risk_manager: Optional[SyntheticRiskManager] = None,
        min_math_edge_score: float = 52.0,
        kalman_confirm_threshold: float = 0.8,
        kalman_veto_contradictions: bool = True,
    ):
        self.brain = brain or CalculativeBrain()
        self.risk_manager = risk_manager or SyntheticRiskManager()
        self.min_math_edge_score = min_math_edge_score
        self.kalman_confirm_threshold = kalman_confirm_threshold
        self.kalman_veto_contradictions = kalman_veto_contradictions
        self.last_rejection = ""  # Why the most recent edge-passing signal could not be sized

    def evaluate(
        self,
        df: pd.DataFrame,
        symbol: str,
        account_balance: float = 10000.0,
        timeframe: str = "M5",
        broker_rules: Optional[Any] = None,
        current_drawdown: float = 0.0,
        spread: float = 0.0,
    ) -> Optional[SyntheticSignal]:
        """Analyze `df` and return a sized signal, or None when there is no edge after costs."""
        analysis = self.brain.analyze(df, symbol=symbol, timeframe=timeframe, spread=spread)
        return self.signal_from_analysis(analysis, account_balance, broker_rules, current_drawdown)

    def signal_from_analysis(
        self,
        analysis: QuantBrainAnalysis,
        account_balance: float,
        broker_rules: Optional[Any] = None,
        current_drawdown: float = 0.0,
    ) -> Optional[SyntheticSignal]:
        self.last_rejection = ""
        if analysis.recommended_action not in ("SNIPE_BUY", "SNIPE_SELL"):
            return None
        if analysis.math_edge_score < self.min_math_edge_score:
            return None
        # Strict win probability floor: only institutional setups (do not rush into trades)
        if analysis.calculated_win_probability < 0.60:
            self.last_rejection = f"Win probability {analysis.calculated_win_probability:.1%} below 60% institutional floor"
            return None
        if analysis.expected_yield_r < 0.25:
            self.last_rejection = f"Expected yield {analysis.expected_yield_r:+.2f}R below 0.25R institutional floor"
            return None

        direction = "BUY" if analysis.recommended_action == "SNIPE_BUY" else "SELL"
        entry = float(analysis.current_price)
        sl = float(analysis.invalidation_price)
        tp = float(analysis.target_price)
        risk_dist = abs(entry - sl)
        if risk_dist <= 0:
            return None
        rr = abs(tp - entry) / risk_dist
        loss_r = float(analysis.metadata.get("loss_r", 1.0))
        cost_r = float(analysis.metadata.get("cost_r", 0.0))

        kalman = kalman_alignment(analysis.kalman_zscore, direction, self.kalman_confirm_threshold)
        if self.kalman_veto_contradictions and kalman == "CONTRADICTS":
            self.last_rejection = f"Kalman state-space filter contradicts {direction} (Z={analysis.kalman_zscore:+.2f})"
            return None

        var_level, var_scale = var_risk_level(analysis.var_95_pct)

        sizing = self.risk_manager.calculate_position_size(
            symbol=analysis.symbol,
            entry_price=entry,
            stop_loss=sl,
            target_price=tp,
            account_balance=account_balance,
            win_probability=analysis.calculated_win_probability,
            broker_rules=broker_rules,
            current_drawdown_fraction=current_drawdown,
            math_edge_score=analysis.math_edge_score,
            loss_r=loss_r + cost_r,
            risk_scale=var_scale,
            reward_risk_ratio=max(rr - cost_r, 0.0),
        )
        if not sizing.is_valid or sizing.volume <= 0:
            self.last_rejection = sizing.rejection_reason or "zero volume"
            return None
        self.last_rejection = ""

        expected_dollars = self.risk_manager.compute_expected_dollar_yield(
            win_probability=analysis.calculated_win_probability,
            risk_dollars=sizing.risk_dollars * (loss_r + cost_r),
            reward_dollars=sizing.risk_dollars * max(rr - cost_r, 0.0),
        )

        return SyntheticSignal(
            symbol=analysis.symbol,
            direction=direction,
            strategy_type=analysis.strategy_archetype,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            math_edge_score=analysis.math_edge_score,
            calculated_win_prob=analysis.calculated_win_probability,
            reward_risk_ratio=round(rr, 2),
            half_life_horizon_bars=analysis.half_life_horizon_bars,
            take_profit_2=float(analysis.target_price_2) or tp,
            expected_yield_dollars=round(expected_dollars, 2),
            sizing=sizing,
            rationale=_rationale(analysis, kalman, var_level, rr),
            raw_analysis=analysis,
            kalman_confirmation=kalman,
            var_risk_level=var_level,
        )
