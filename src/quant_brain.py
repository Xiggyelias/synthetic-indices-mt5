"""
QUANTA-SYNTH Calculative Brain
===============================
Statistical engine for Deriv / MT5 synthetic indices. No machine learning.

Design rule: every win probability comes from a model whose parameters are
estimated from the data, and a regime must pass a significance test before it
can produce a trade. Deriv's indices are built as fair random processes, so on
data with no exploitable structure the brain should stand aside.

Estimators:
  1. Hurst exponent via rescaled range (R/S) with the Anis-Lloyd-Peters
     small-sample correction (raw R/S reads ~0.6 on a pure random walk)
  2. Ornstein-Uhlenbeck fit via AR(1), gated by a Dickey-Fuller t-test
  3. Garman-Klass volatility
  4. Shannon entropy of return signs (directional order)
  5. Boom/Crash spikes: empirical Poisson rate with Hawkes excitation that is
     only switched on when clustering is statistically present in the window
  6. Step Index: Wald-Wolfowitz runs test and empirical Markov transitions
  7. Kalman fair value, VaR/CVaR

Win probabilities:
  - O-U reversion: exact scale-function hitting probability
  - Trend and Step: hitting probability of Brownian motion with drift
  - Boom/Crash: Poisson/Hawkes survival over the time the trade needs
Each is blended toward the zero-edge probability by an evidence weight derived
from the regime's test statistic, so a borderline regime cannot claim a large edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import special, stats

from synthetic_calibration import ProbabilityCalibrator

# 5% critical values
ADF_CRITICAL_5PCT = -2.89    # Dickey-Fuller with constant, n ~ 100-250
NORMAL_CRITICAL_5PCT = 1.96


@dataclass(frozen=True)
class HurstResult:
    hurst_exponent: float
    regime: str  # "ANTI_PERSISTENT", "RANDOM_WALK", "PERSISTENT"
    r_squared: float
    is_tradable: bool


@dataclass(frozen=True)
class OUParameters:
    theta: float              # Speed of mean reversion
    mu: float                 # Equilibrium price level
    sigma: float              # Diffusion volatility
    half_life: float          # Time units for half-reversion (ln(2)/theta)
    stationary_std: float     # Stationary distribution std (sigma / sqrt(2*theta))
    current_zscore: float     # Current deviation from equilibrium in std units
    is_reverting: bool        # True if stationarity is significant and half-life is reasonable
    expected_fpt_bars: float = 0.0   # Bars for the mean path to reach the equilibrium band
    stationary_p_value: float = 0.5  # Two-sided tail probability of the current deviation
    adf_tstat: float = 0.0           # Dickey-Fuller t-statistic of the AR(1) coefficient


@dataclass(frozen=True)
class EntropyResult:
    shannon_entropy: float        # Raw entropy in bits
    normalized_entropy: float     # Normalized to [0, 1]
    is_ordered: bool              # True if normalized entropy is below the order threshold


@dataclass(frozen=True)
class PoissonHazardResult:
    bars_since_spike: int
    hazard_rate: float            # Spike intensity for the next bar (spikes per bar)
    spike_prob_next_k: float      # Probability of at least one spike in the next k bars
    is_cluster_window: bool       # Significant clustering present and last spike is recent
    recommended_side: str         # "CATCH_SPIKE", "HARVEST_DRIFT", or "STAND_ASIDE"
    hawkes_intensity: float = 0.0
    branching_ratio: float = 0.0  # Expected offspring per spike (0 when no clustering detected)
    is_quiescent_drift: bool = False
    base_rate: float = 0.0        # Unconditional spikes per bar
    spike_count: int = 0
    drift_per_bar: float = 0.0    # Mean non-spike move per bar, signed in the drift direction
    median_spike_size: float = 0.0
    # Priced trades (distances in price units, positive)
    drift_target: float = 0.0
    drift_stop: float = 0.0
    drift_loss_r: float = 1.0     # Spikes gap through the stop, so a loss exceeds 1R
    drift_win_prob: float = 0.0
    drift_expectancy_r: float = 0.0
    spike_target: float = 0.0
    spike_stop: float = 0.0
    spike_win_prob: float = 0.0
    spike_expectancy_r: float = 0.0


@dataclass(frozen=True)
class StepIndexAnalysis:
    symbol: str
    runs_count: int
    expected_runs: float
    runs_zscore: float
    p_value: float
    current_streak_direction: str  # "UP", "DOWN", "NEUTRAL"
    current_streak_length: int
    streak_continuation_prob: float  # Empirical P(next move continues the current direction)
    markov_p_up_given_up: float
    markov_p_down_given_down: float
    is_statistically_stretched: bool  # Significant alternation: reversals beat chance
    recommended_scalp: str            # "SCALP_REVERSAL_BUY", "SCALP_REVERSAL_SELL", "STAND_ASIDE"


@dataclass(frozen=True)
class MultiScaleConfluenceResult:
    is_confluent: bool
    macro_regime: str
    confluence_multiplier: float
    macro_zscore: float
    macro_hurst: float
    alignment_reason: str


@dataclass(frozen=True)
class KalmanFilterResult:
    fair_value: float
    error_variance: float
    zscore: float               # Last residual over the residual standard deviation
    smoothed_series: np.ndarray
    residuals: np.ndarray


@dataclass
class QuantBrainAnalysis:
    symbol: str
    timeframe: str
    current_price: float
    hurst: HurstResult
    ou: OUParameters
    garman_klass_vol: float
    entropy: EntropyResult
    poisson: Optional[PoissonHazardResult]
    math_edge_score: float        # 0-100, a monotone map of expected R (50 = zero edge)
    recommended_action: str       # "SNIPE_BUY", "SNIPE_SELL", "STAND_ASIDE"
    target_price: float           # TP1
    invalidation_price: float     # Stop loss
    half_life_horizon_bars: int
    calculated_win_probability: float  # P(TP1 before stop)
    target_price_2: float = 0.0
    expected_yield_r: float = 0.0      # p * RR1 - (1 - p) * loss_R
    confluence: Optional[MultiScaleConfluenceResult] = None
    step_analysis: Optional[StepIndexAnalysis] = None
    strategy_archetype: str = "STAND_ASIDE"
    instantaneous_sharpe: float = 0.0
    expected_yield_dollars: float = 0.0
    kalman_fair_value: float = 0.0
    kalman_zscore: float = 0.0
    var_95_pct: float = 0.0
    cvar_95_pct: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Probability helpers ──────────────────────────────────────────────────────

def evidence_weight(statistic: float, critical: float, full: float) -> float:
    """Linear ramp from 0 at the critical value to 1 at `full` (both as magnitudes)."""
    lo, hi = abs(critical), abs(full)
    if hi <= lo:
        return 1.0 if abs(statistic) >= lo else 0.0
    return float(np.clip((abs(statistic) - lo) / (hi - lo), 0.0, 1.0))


def ou_hit_probability(z0: float, z_target: float, z_stop: float) -> float:
    """
    P(an O-U process hits z_target before z_stop, starting at z0), all in
    stationary-std units. Uses the scale function s(z) = ∫_0^z exp(u²/2) du,
    so P = (s(z_stop) - s(z0)) / (s(z_stop) - s(z_target)).
    """
    def scale(z: float) -> float:
        return math.sqrt(math.pi / 2.0) * float(special.erfi(z / math.sqrt(2.0)))

    den = scale(z_stop) - scale(z_target)
    if not math.isfinite(den) or abs(den) < 1e-12:
        return 0.5
    return float(np.clip((scale(z_stop) - scale(z0)) / den, 0.0, 1.0))


def drift_hit_probability(target_dist: float, stop_dist: float, drift: float, sigma: float) -> float:
    """
    P(Brownian motion with per-bar drift and volatility sigma rises target_dist
    before falling stop_dist). Drift is signed in the trade's favour.
    Zero drift reduces to the gambler's-ruin ratio stop / (target + stop).
    """
    if target_dist <= 0 or stop_dist <= 0:
        return 0.0
    if sigma <= 0:
        return 1.0 if drift > 0 else (0.0 if drift < 0 else 0.5)
    k = 2.0 * drift / (sigma ** 2)
    if abs(k) * (target_dist + stop_dist) < 1e-8:
        return stop_dist / (target_dist + stop_dist)
    # P = (1 - e^{k·stop}) / (e^{-k·target} - e^{k·stop}); evaluated in log space for stability
    a, b = -k * target_dist, k * stop_dist
    if k > 0:
        # Divide through by e^{b}: (e^{-b} - 1) / (e^{a-b} - 1)
        return float(np.clip(math.expm1(-b) / math.expm1(a - b), 0.0, 1.0))
    # k < 0: divide through by e^{a}: (e^{-a} - e^{b-a}) / (1 - e^{b-a})
    return float(np.clip((math.exp(-a) - math.exp(b - a)) / -math.expm1(b - a), 0.0, 1.0))


def level_before_event_probability(distance: float, drift: float, sigma: float, rate: float) -> float:
    """
    P(Brownian motion with per-bar drift and vol sigma moves `distance` in the
    drift direction before a Poisson event with per-bar `rate` occurs):
        E[e^{-rate·τ}] = exp( distance · (drift - sqrt(drift² + 2·rate·σ²)) / σ² )
    """
    if distance <= 0:
        return 1.0
    if sigma <= 0:
        return math.exp(-rate * distance / drift) if drift > 0 else 0.0
    exponent = distance * (drift - math.sqrt(drift ** 2 + 2.0 * rate * sigma ** 2)) / sigma ** 2
    return math.exp(max(exponent, -700.0))


def trend_tstat(closes: pd.Series, bars: int = 60) -> Tuple[float, float, float]:
    """(t-statistic, mean, std) of the last `bars` log returns. |t| > 1.96 = significant drift."""
    rets = np.diff(np.log(closes.astype(float).values[-(bars + 1):]))
    if len(rets) < 20:
        return 0.0, 0.0, 0.0
    m, sd = float(np.mean(rets)), float(np.std(rets, ddof=1))
    t = m / (sd / math.sqrt(len(rets))) if sd > 0 else 0.0
    return t, m, sd


def edge_score_from_expectancy(expected_r: float) -> float:
    """Monotone 0-100 map of expected R. 50 = no edge, ~65 ≈ +0.19R, ~75 ≈ +0.33R."""
    return float(np.clip(50.0 + 50.0 * math.tanh(expected_r / 0.6), 0.0, 100.0))


def kalman_alignment(kalman_z: float, direction: str, threshold: float = 0.8) -> str:
    """Label whether price sits on the side of Kalman fair value that supports the trade."""
    if direction == "BUY":
        if kalman_z < -threshold:
            return "CONFIRMS"
        if kalman_z > threshold:
            return "CONTRADICTS"
    elif direction == "SELL":
        if kalman_z > threshold:
            return "CONFIRMS"
        if kalman_z < -threshold:
            return "CONTRADICTS"
    return "NEUTRAL"


# ── Estimators ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def _expected_rs(n: int) -> float:
    """Anis-Lloyd expected R/S of n i.i.d. Gaussian increments, with Peters' (n-0.5)/n factor."""
    i = np.arange(1, n)
    gamma_ratio = math.exp(math.lgamma((n - 1) / 2.0) - math.lgamma(n / 2.0)) / math.sqrt(math.pi)
    return ((n - 0.5) / n) * gamma_ratio * float(np.sum(np.sqrt((n - i) / i)))


def compute_hurst_exponent(
    series: pd.Series,
    min_lag: int = 10,
    max_lag: int = 100,
    num_lags: int = 20,
    anti_persistent_below: float = 0.40,
    persistent_above: float = 0.60,
    min_r_squared: float = 0.75,
) -> HurstResult:
    """
    Hurst exponent via R/S analysis of log returns, bias-corrected:
        H = 0.5 + slope(log R/S_observed) - slope(log E[R/S]_iid)
    H < 0.5 anti-persistent, H ≈ 0.5 random walk, H > 0.5 persistent.
    """
    random_walk = HurstResult(hurst_exponent=0.5, regime="RANDOM_WALK", r_squared=0.0, is_tradable=False)
    clean = series.dropna().astype(float).values
    n = len(clean)
    if n < max_lag * 2 or np.any(clean <= 0):
        return random_walk

    returns = np.diff(np.log(clean))
    lags = np.unique(np.linspace(min_lag, min(max_lag, n // 2), num=num_lags, dtype=int))
    rs_values = np.full(len(lags), np.nan)

    for idx, lag in enumerate(lags):
        chunks = returns[: (len(returns) // lag) * lag].reshape(-1, lag)
        deviations = chunks - chunks.mean(axis=1, keepdims=True)
        r = np.ptp(np.cumsum(deviations, axis=1), axis=1)
        s = chunks.std(axis=1)
        valid = s > 1e-12
        if np.any(valid):
            rs_values[idx] = np.mean(r[valid] / s[valid])

    valid_mask = np.isfinite(rs_values) & (rs_values > 0)
    if np.sum(valid_mask) < 4:
        return random_walk

    log_lags = np.log(lags[valid_mask])
    observed = stats.linregress(log_lags, np.log(rs_values[valid_mask]))
    expected = stats.linregress(log_lags, np.log([_expected_rs(int(l)) for l in lags[valid_mask]]))

    h = float(np.clip(0.5 + observed.slope - expected.slope, 0.01, 0.99))
    r_sq = float(observed.rvalue ** 2)

    if h < anti_persistent_below and r_sq >= min_r_squared:
        regime = "ANTI_PERSISTENT"
    elif h > persistent_above and r_sq >= min_r_squared:
        regime = "PERSISTENT"
    else:
        regime = "RANDOM_WALK"

    return HurstResult(
        hurst_exponent=round(h, 3),
        regime=regime,
        r_squared=round(r_sq, 3),
        is_tradable=regime != "RANDOM_WALK",
    )


def compute_fokker_planck_density(
    current_price: float,
    mu: float,
    sigma: float,
    theta: float
) -> Tuple[float, float]:
    """
    Stationary O-U density from the Fokker-Planck equation, N(mu, sigma²/(2·theta)).
    Returns (density at current_price, two-sided tail probability).
    """
    if theta <= 1e-6 or sigma <= 1e-6:
        return 0.0, 1.0
    stat_std = math.sqrt((sigma ** 2) / (2.0 * theta))
    z = abs(current_price - mu) / stat_std
    density = math.exp(-0.5 * z ** 2) / (math.sqrt(2.0 * math.pi) * stat_std)
    tail_p = 2.0 * float(stats.norm.sf(z))
    return float(density), tail_p


def compute_expected_first_passage_time(
    current_price: float,
    mu: float,
    sigma: float,
    theta: float,
    target_fraction: float = 0.35
) -> float:
    """
    Bars for the O-U mean path z(t) = z0·e^{-θt} to decay from the current
    deviation into the band |z| <= target_fraction: t = ln(z0 / target_fraction) / θ.
    """
    if theta <= 1e-5 or sigma <= 1e-5:
        return float("inf")
    stat_std = sigma / math.sqrt(2.0 * theta)
    z0 = abs(current_price - mu) / stat_std
    if z0 <= target_fraction:
        return 0.5
    return float(min(120.0, max(1.0, math.log(z0 / target_fraction) / theta)))


def fit_ornstein_uhlenbeck(
    series: pd.Series,
    dt: float = 1.0,
    adf_critical: float = ADF_CRITICAL_5PCT,
    max_half_life: float = 50.0,
) -> OUParameters:
    """
    Fit the AR(1) form of dX = θ(μ - X)dt + σ dW:
        X_t = a·X_{t-1} + b + ε,   a = e^{-θ dt},  μ = b / (1 - a)
    Stationarity is accepted only when the Dickey-Fuller t-statistic
    (a - 1) / se(a) is below the 5% critical value; OLS on any random walk
    gives a < 1, so without this test every trending window looks "reverting".
    """
    clean = series.dropna().astype(float).values
    if len(clean) < 15:
        return OUParameters(
            theta=0.0, mu=float(clean[-1]) if len(clean) else 0.0,
            sigma=0.0, half_life=float("inf"), stationary_std=1.0,
            current_zscore=0.0, is_reverting=False
        )

    x_prev, x_t = clean[:-1], clean[1:]
    fit = stats.linregress(x_prev, x_t)
    a, b = float(fit.slope), float(fit.intercept)
    adf_t = (a - 1.0) / fit.stderr if fit.stderr > 0 else 0.0
    current_val = float(clean[-1])

    if 0.001 < a < 0.999:
        theta = -math.log(a) / dt
        mu = b / (1.0 - a)
        half_life = math.log(2.0) / theta

        residuals = x_t - (a * x_prev + b)
        var_eps = float(np.var(residuals, ddof=2))
        if var_eps > 0:
            sigma = math.sqrt(2.0 * theta * var_eps / (1.0 - a ** 2))
            stationary_std = sigma / math.sqrt(2.0 * theta)
        else:
            sigma = float(np.std(residuals))
            stationary_std = sigma

        zscore = (current_val - mu) / stationary_std if stationary_std > 1e-9 else 0.0
        is_reverting = adf_t < adf_critical and 0.5 <= half_life <= max_half_life

        _, tail_p = compute_fokker_planck_density(current_val, mu, sigma, theta)
        fpt_bars = compute_expected_first_passage_time(current_val, mu, sigma, theta)
        return OUParameters(
            theta=round(theta, 5),
            mu=round(mu, 5),
            sigma=round(sigma, 5),
            half_life=round(half_life, 2),
            stationary_std=round(stationary_std, 5),
            current_zscore=round(zscore, 3),
            is_reverting=is_reverting,
            expected_fpt_bars=round(fpt_bars, 1),
            stationary_p_value=round(tail_p, 4),
            adf_tstat=round(adf_t, 3),
        )

    # Unit root or explosive: report a rolling z-score for display only
    window = clean[-20:]
    rolling_mean = float(np.mean(window))
    rolling_std = float(np.std(window)) or 1.0
    return OUParameters(
        theta=0.0,
        mu=round(rolling_mean, 5),
        sigma=round(rolling_std, 5),
        half_life=float("inf"),
        stationary_std=round(rolling_std, 5),
        current_zscore=round((current_val - rolling_mean) / rolling_std, 3),
        is_reverting=False,
        adf_tstat=round(adf_t, 3),
    )


def compute_garman_klass_volatility(df: pd.DataFrame, window: int = 20) -> float:
    """
    Per-bar Garman-Klass volatility:
        GK = sqrt( mean[ 0.5·ln(H/L)² - (2·ln2 - 1)·ln(C/O)² ] )
    """
    if df is None or len(df) < window:
        return 0.01

    sub = df.iloc[-window:]
    h = sub["high"].astype(float).values
    l = sub["low"].astype(float).values
    c = sub["close"].astype(float).values
    o = sub["open"].astype(float).values

    valid = (h > 0) & (l > 0) & (c > 0) & (o > 0) & (h >= l)
    if np.sum(valid) < 5:
        return 0.01

    log_hl = np.log(h[valid] / l[valid])
    log_co = np.log(c[valid] / o[valid])
    mean_gk = float(np.mean(0.5 * log_hl ** 2 - (2.0 * math.log(2.0) - 1.0) * log_co ** 2))
    if mean_gk <= 0:
        return 0.001
    return math.sqrt(mean_gk)


def compute_shannon_entropy(
    series: pd.Series,
    window: int = 30,
    order_threshold: float = 0.75,
) -> EntropyResult:
    """
    Shannon entropy of return signs (up / down / flat within 0.2σ), normalized by log2(3).
    Low values mean one direction dominates. Gaussian noise scores ≈ 0.93.
    """
    if len(series) < window + 1:
        return EntropyResult(shannon_entropy=1.0, normalized_entropy=1.0, is_ordered=False)

    recent = series.iloc[-window - 1:].dropna().astype(float).values
    returns = np.diff(np.log(recent))
    if len(returns) < 5 or float(np.std(returns)) < 1e-12:
        return EntropyResult(shannon_entropy=0.0, normalized_entropy=0.0, is_ordered=True)

    noise_thresh = 0.20 * float(np.std(returns))
    n_up = int(np.sum(returns > noise_thresh))
    n_down = int(np.sum(returns < -noise_thresh))
    counts = np.array([n_up, n_down, len(returns) - n_up - n_down], dtype=float)
    probabilities = counts[counts > 0] / len(returns)

    entropy = -float(np.sum(probabilities * np.log2(probabilities)))
    normalized = float(np.clip(entropy / math.log2(3.0), 0.0, 1.0))
    return EntropyResult(
        shannon_entropy=round(entropy, 3),
        normalized_entropy=round(normalized, 3),
        is_ordered=normalized <= order_threshold,
    )


def detect_spikes(df: pd.DataFrame, is_crash: bool, spike_sigmas: float = 4.0) -> np.ndarray:
    """
    Boolean mask of spike bars. Spikes move against the drift (down for Crash, up for Boom);
    a bar is a spike when its move or excursion against the drift is a > `spike_sigmas`
    robust-σ outlier. Median/MAD statistics matter because on M5 roughly a third of
    Crash 1000 bars contain a spike.
    """
    opens = df["open"].astype(float).values
    closes = df["close"].astype(float).values
    against = (opens - closes) if is_crash else (closes - opens)
    excursion = (opens - df["low"].astype(float).values) if is_crash else (df["high"].astype(float).values - opens)

    def outliers(x: np.ndarray) -> np.ndarray:
        med = float(np.median(x))
        robust_sd = 1.4826 * float(np.median(np.abs(x - med)))
        return (x - med) > spike_sigmas * robust_sd + 1e-12

    return (outliers(against) | outliers(excursion)) & (against > 0)


def compute_poisson_hazard(
    df: pd.DataFrame,
    symbol: str,
    lookback_window: int = 100,
    k_horizon: int = 5,
    cluster_window: int = 4,
    drift_horizon_bars: int = 6,
    spike_stop_bars: int = 8,
    min_spikes: int = 3,
    min_expectancy_r: float = 0.10,
    spike_sigmas: float = 4.0,
) -> Optional[PoissonHazardResult]:
    """
    Spike model for Boom / Crash indices.
      Crash: upward drift, downward spikes.  Boom: downward drift, upward spikes.

    Spike arrivals are modelled as Poisson with rate μ0 = spikes / bars. A Hawkes
    excitation term α·e^{-β·dt} is added only when the window shows significantly
    more spikes within `cluster_window` bars of a previous spike than μ0 predicts
    (one-sided Poisson test, 5%). Deriv documents these spikes as random, so the
    excitation is usually zero.

    Two trades are priced from the same estimates and the better one is recommended:
      HARVEST_DRIFT: ride the drift for `drift_horizon_bars`; wins if no spike arrives.
                     A spike gaps through the stop, so the loss is the spike size.
      CATCH_SPIKE:   stop sits `spike_stop_bars` of drift away; wins if a large enough
                     spike arrives before the drift reaches the stop.
    """
    sym = symbol.upper()
    is_crash, is_boom = "CRASH" in sym, "BOOM" in sym
    if not (is_crash or is_boom) or df is None or len(df) < 30:
        return None

    recent = df.iloc[-lookback_window:]
    opens = recent["open"].astype(float).values
    closes = recent["close"].astype(float).values
    n = len(recent)

    against = (opens - closes) if is_crash else (closes - opens)
    is_spike = detect_spikes(recent, is_crash, spike_sigmas)
    spike_idx = np.flatnonzero(is_spike)
    spike_count = len(spike_idx)

    bars_since = int((n - 1) - spike_idx[-1]) if spike_count else n
    base_rate = max(spike_count, 0.5) / n
    # 68% Gamma-posterior bounds on the rate; each trade is priced at its unfavourable end
    rate_hi = float(stats.gamma.ppf(0.84, spike_count + 1)) / n
    rate_lo = float(stats.gamma.ppf(0.16, spike_count)) / n if spike_count else 0.0

    # Empirical clustering test: spikes landing 1..cluster_window bars after a spike
    follow = 0
    exposure = 0
    for s in spike_idx:
        end = min(n - 1, s + cluster_window)
        exposure += end - s
        follow += int(np.sum((spike_idx > s) & (spike_idx <= end)))
    expected_follow = base_rate * exposure
    clustered = (
        follow >= 2 and exposure > 0
        and float(stats.poisson.sf(follow - 1, expected_follow)) < 0.05
    )

    # Calibrate Hawkes kernel α·e^{-β·dt} so its mass over the window matches the excess
    beta = 0.5
    kernel_mass = sum(math.exp(-beta * d) for d in range(1, cluster_window + 1))
    excess_per_spike = max(0.0, (follow - expected_follow) / spike_count) if spike_count else 0.0
    alpha = excess_per_spike / kernel_mass if clustered else 0.0
    branching = alpha * math.exp(-beta) / (1.0 - math.exp(-beta))
    # Background rate: the share of observed spikes not explained by excitation
    excitation_scale = 1.0 - min(branching, 0.9)
    mu_0 = base_rate * excitation_scale

    def intensity(h: int, base: float) -> float:
        t = (n - 1) + h
        return base + sum(alpha * math.exp(-beta * (t - s)) for s in spike_idx[-20:])

    def mean_rate(bars: int, base: float) -> float:
        bars = max(1, int(math.ceil(bars)))
        return sum(intensity(h, base) for h in range(1, bars + 1)) / bars

    hawkes_now = intensity(1, mu_0)
    spike_prob_k = 1.0 - math.exp(-mean_rate(k_horizon, mu_0) * k_horizon)

    # Drift and noise of non-spike bars, signed so drift is positive in its own direction.
    # The mean (not median) keeps undetected small spikes inside the drift estimate,
    # which keeps the expectancy consistent with the detected spike rate.
    normal = ~is_spike
    body = (closes - opens) * (1.0 if is_crash else -1.0)
    drift = float(np.mean(body[normal])) if np.sum(normal) >= 2 else 0.0
    noise = float(np.std(body[normal])) if np.sum(normal) >= 2 else 0.0
    spike_sizes = against[is_spike] if spike_count else np.array([])
    median_spike = float(np.median(spike_sizes)) if spike_count else 0.0

    d_target = d_stop = s_target = s_stop = 0.0
    loss_r = 1.0
    drift_p = drift_e = spike_p = spike_e = 0.0
    if spike_count >= min_spikes and drift > 0 and median_spike > 0:
        drift_se = noise / math.sqrt(max(int(np.sum(normal)), 1))

        # HARVEST_DRIFT: target = drift over the horizon, stop one median spike away.
        # Wins if the drift reaches the target before the next spike.
        # Priced with the low end of the drift and the high end of the spike rate.
        d_target = drift * drift_horizon_bars
        d_stop = median_spike
        rate_d = mean_rate(drift_horizon_bars, rate_hi * excitation_scale)
        drift_p = level_before_event_probability(d_target, max(drift - drift_se, 0.0), noise, rate_d)
        loss_r = float(np.mean(np.maximum(spike_sizes, d_stop))) / d_stop
        drift_e = drift_p * (d_target / d_stop) - (1.0 - drift_p) * loss_r

        # CATCH_SPIKE: stop = drift over spike_stop_bars plus a 2σ noise buffer,
        # target = half a median spike. Wins if a big enough spike lands before the stop.
        # Priced with the high end of the drift and the low end of the spike rate.
        s_stop = drift * spike_stop_bars + 2.0 * noise
        s_target = 0.5 * median_spike
        rate_s = mean_rate(spike_stop_bars, rate_lo * excitation_scale)
        size_ok = float(np.mean(spike_sizes >= s_target + drift))
        spike_p = (1.0 - level_before_event_probability(s_stop, drift + drift_se, noise, rate_s)) * size_ok
        spike_e = spike_p * (s_target / s_stop) - (1.0 - spike_p)

    if max(drift_e, spike_e) < min_expectancy_r:
        side = "STAND_ASIDE"
    elif spike_e > drift_e:
        side = "CATCH_SPIKE"
    else:
        side = "HARVEST_DRIFT"

    return PoissonHazardResult(
        bars_since_spike=bars_since,
        hazard_rate=round(hawkes_now, 4),
        spike_prob_next_k=round(float(spike_prob_k), 3),
        is_cluster_window=clustered and 1 <= bars_since <= cluster_window,
        recommended_side=side,
        hawkes_intensity=round(hawkes_now, 4),
        branching_ratio=round(branching, 3),
        is_quiescent_drift=bars_since > cluster_window and hawkes_now <= base_rate * 1.1,
        base_rate=round(base_rate, 4),
        spike_count=spike_count,
        drift_per_bar=round(drift, 6),
        median_spike_size=round(median_spike, 5),
        drift_target=d_target,
        drift_stop=d_stop,
        drift_loss_r=round(loss_r, 3),
        drift_win_prob=round(drift_p, 3),
        drift_expectancy_r=round(drift_e, 3),
        spike_target=s_target,
        spike_stop=s_stop,
        spike_win_prob=round(spike_p, 3),
        spike_expectancy_r=round(spike_e, 3),
    )


def compute_step_index_runs_and_markov(
    series: pd.Series,
    symbol: str = "Step Index",
    lookback: int = 60,
    z_critical: float = NORMAL_CRITICAL_5PCT,
) -> StepIndexAnalysis:
    """
    Wald-Wolfowitz runs test and 2-state Markov transitions on bar-to-bar move signs.

    Deriv's Step Index moves ±0.1 per tick with equal probability, so past streaks
    carry no information under the null. A reversal edge exists only when the runs
    test shows significantly MORE runs than chance (z >= z_critical), i.e. moves
    alternate. Fewer runs than chance means streaks persist, which argues against fading.
    """
    neutral = StepIndexAnalysis(
        symbol=symbol, runs_count=0, expected_runs=0.0, runs_zscore=0.0, p_value=1.0,
        current_streak_direction="NEUTRAL", current_streak_length=0,
        streak_continuation_prob=0.5, markov_p_up_given_up=0.5, markov_p_down_given_down=0.5,
        is_statistically_stretched=False, recommended_scalp="STAND_ASIDE",
    )
    clean = series.dropna().astype(float).values
    if len(clean) < 15:
        return neutral

    steps = np.sign(np.diff(clean[-lookback:]))
    steps = steps[steps != 0]
    n = len(steps)
    if n < 10:
        return neutral

    n1 = int(np.sum(steps > 0))
    n2 = n - n1
    runs_count = 1 + int(np.sum(steps[1:] != steps[:-1]))

    if n1 > 0 and n2 > 0:
        expected_runs = 2.0 * n1 * n2 / n + 1.0
        var_runs = 2.0 * n1 * n2 * (2.0 * n1 * n2 - n) / (n ** 2 * (n - 1))
        runs_zscore = (runs_count - expected_runs) / math.sqrt(max(var_runs, 1e-9))
        p_value = 2.0 * float(stats.norm.sf(abs(runs_zscore)))
    else:
        expected_runs = float(runs_count)
        runs_zscore = 0.0
        p_value = 1.0

    streak_dir = "UP" if steps[-1] > 0 else "DOWN"
    differs = np.flatnonzero(steps[::-1] != steps[-1])
    streak_len = int(differs[0]) if len(differs) else n

    prev, nxt = steps[:-1], steps[1:]
    up_total, down_total = int(np.sum(prev > 0)), int(np.sum(prev < 0))
    up_up = int(np.sum((prev > 0) & (nxt > 0)))
    down_down = int(np.sum((prev < 0) & (nxt < 0)))
    # Laplace-smoothed so a short sample cannot report certainty
    p_up_up = (up_up + 1) / (up_total + 2)
    p_down_down = (down_down + 1) / (down_total + 2)
    p_continue = p_up_up if streak_dir == "UP" else p_down_down

    is_stretched = runs_zscore >= z_critical and p_continue < 0.5
    if is_stretched:
        recommended = "SCALP_REVERSAL_SELL" if streak_dir == "UP" else "SCALP_REVERSAL_BUY"
    else:
        recommended = "STAND_ASIDE"

    return StepIndexAnalysis(
        symbol=symbol,
        runs_count=runs_count,
        expected_runs=round(expected_runs, 2),
        runs_zscore=round(runs_zscore, 3),
        p_value=round(p_value, 4),
        current_streak_direction=streak_dir,
        current_streak_length=streak_len,
        streak_continuation_prob=round(p_continue, 4),
        markov_p_up_given_up=round(p_up_up, 3),
        markov_p_down_given_down=round(p_down_down, 3),
        is_statistically_stretched=is_stretched,
        recommended_scalp=recommended,
    )


def resample_to_macro_timeframe(df: pd.DataFrame, factor: int = 3) -> pd.DataFrame:
    """Aggregate consecutive bars into a higher timeframe (M5 x3 = M15, x12 = H1)."""
    if df is None or len(df) < factor * 2:
        return df.copy() if df is not None else pd.DataFrame()

    n_usable = (len(df) // factor) * factor
    sub = df.iloc[-n_usable:]

    def chunks(col: str) -> np.ndarray:
        return sub[col].astype(float).values.reshape(-1, factor)

    return pd.DataFrame({
        "open": chunks("open")[:, 0],
        "high": chunks("high").max(axis=1),
        "low": chunks("low").min(axis=1),
        "close": chunks("close")[:, -1],
    })


def compute_multi_scale_confluence(
    df_primary: pd.DataFrame,
    df_macro: Optional[pd.DataFrame] = None,
    factor: int = 3,
    direction: Optional[str] = None,
) -> MultiScaleConfluenceResult:
    """
    Cross-check the execution timeframe against a higher one. With `direction`
    ("BUY"/"SELL"), confluence requires the macro picture to point the same way:
    a macro reversion setup on the same side, or a macro trend in the trade direction.
    """
    def none(reason: str, regime: str = "RANDOM_WALK", z: float = 0.0, h: float = 0.5) -> MultiScaleConfluenceResult:
        return MultiScaleConfluenceResult(False, regime, 1.0, z, h, reason)

    if df_primary is None or len(df_primary) < 30:
        return none("Insufficient data for macro confluence")

    macro = df_macro if df_macro is not None and len(df_macro) >= 20 else resample_to_macro_timeframe(df_primary, factor=factor)
    if len(macro) < 15:
        return none("Macro sample too small")

    macro_close = macro["close"].astype(float)
    macro_hurst = compute_hurst_exponent(macro_close, min_lag=6, max_lag=min(40, len(macro) // 2))
    macro_ou = fit_ornstein_uhlenbeck(macro_close)
    macro_trend = float(np.sign(macro_close.iloc[-1] - macro_close.iloc[-min(10, len(macro_close))]))
    wants = {"BUY": 1.0, "SELL": -1.0}.get((direction or "").upper(), 0.0)

    if macro_hurst.regime == "ANTI_PERSISTENT" and macro_ou.is_reverting and abs(macro_ou.current_zscore) >= 1.5:
        # Price far below the macro mean supports a BUY, far above supports a SELL
        aligned = wants == 0.0 or np.sign(-macro_ou.current_zscore) == wants
        if aligned:
            return MultiScaleConfluenceResult(
                True, macro_hurst.regime, 1.10, macro_ou.current_zscore, macro_hurst.hurst_exponent,
                f"Macro mean-reversion confluence: H={macro_hurst.hurst_exponent:.2f}, Z={macro_ou.current_zscore:+.2f}σ",
            )
    elif macro_hurst.regime == "PERSISTENT":
        aligned = wants == 0.0 or macro_trend == wants
        if aligned:
            return MultiScaleConfluenceResult(
                True, macro_hurst.regime, 1.10, macro_ou.current_zscore, macro_hurst.hurst_exponent,
                f"Macro trend confluence: H={macro_hurst.hurst_exponent:.2f}, R²={macro_hurst.r_squared:.2f}",
            )

    return none("Independent scale (no aligned macro structure)", macro_hurst.regime,
                macro_ou.current_zscore, macro_hurst.hurst_exponent)


def fit_kalman_filter(
    series: pd.Series,
    process_noise: float = 1e-4,
    measurement_noise: float = 1e-2,
) -> KalmanFilterResult:
    """
    Local-level Kalman filter:
        x_k = x_{k-1} + w_k,  w ~ N(0, Q);   z_k = x_k + v_k,  v ~ N(0, R)
    Only Q/R matters for the smoothing. The z-score is the latest residual
    (price minus prediction) divided by the residual standard deviation.
    """
    values = series.dropna().astype(float).values
    n = len(values)
    if n < 5:
        cur = float(values[-1]) if n > 0 else 0.0
        return KalmanFilterResult(cur, 1.0, 0.0, values, np.zeros(n))

    var_val = float(np.var(values)) or 1.0
    q = process_noise * var_val
    r = measurement_noise * var_val

    x_hat = values[0]
    p = var_val
    smoothed = np.zeros(n)
    residuals = np.zeros(n)
    for k in range(n):
        p_pred = p + q
        gain = p_pred / (p_pred + r)
        residuals[k] = values[k] - x_hat
        x_hat = x_hat + gain * residuals[k]
        p = (1.0 - gain) * p_pred
        smoothed[k] = x_hat

    warm = residuals[min(10, n // 4):]
    resid_std = float(np.std(warm)) if len(warm) > 2 else 0.0
    zscore = residuals[-1] / resid_std if resid_std > 1e-12 else 0.0

    return KalmanFilterResult(
        fair_value=round(float(x_hat), 5),
        error_variance=round(float(p), 6),
        zscore=round(float(zscore), 3),
        smoothed_series=smoothed,
        residuals=residuals,
    )


def compute_var_cvar(
    df: pd.DataFrame,
    confidence: float = 0.95,
    horizon_bars: int = 5,
    gk_vol: Optional[float] = None,
) -> Tuple[float, float]:
    """Gaussian VaR and CVaR (expected shortfall) of log returns over the horizon, as fractions."""
    if df is None or len(df) < 15:
        return 0.02, 0.03

    returns = np.diff(np.log(df["close"].astype(float).values))
    if len(returns) < 10:
        return 0.02, 0.03

    sigma = gk_vol if gk_vol and gk_vol > 0 else float(np.std(returns))
    sigma_h = sigma * math.sqrt(horizon_bars)
    mu_h = float(np.mean(returns)) * horizon_bars

    z = float(stats.norm.ppf(confidence))
    var = -(mu_h - z * sigma_h)
    cvar = -(mu_h - sigma_h * float(stats.norm.pdf(z)) / (1.0 - confidence))
    return float(max(0.001, var)), float(max(0.001, cvar))


# ── Brain ─────────────────────────────────────────────────────────────────────

@dataclass
class _TradePlan:
    direction: str            # "BUY" / "SELL"
    archetype: str
    target: float
    target_2: float
    stop: float
    win_prob: float
    horizon_bars: int
    loss_r: float = 1.0       # Expected loss in R when the stop side wins (gaps make it > 1)


class CalculativeBrain:
    """
    Ingests OHLC bars and returns a trade recommendation whose probability and
    expectancy are computed from the fitted model, or STAND_ASIDE.
    """

    def __init__(
        self,
        hurst_mean_reversion_threshold: float = 0.38,
        hurst_momentum_threshold: float = 0.62,
        zscore_entry_threshold: float = 2.0,
        entropy_order_threshold: float = 0.78,
        hurst_min_r_squared: float = 0.75,
        adf_critical: float = ADF_CRITICAL_5PCT,
        poisson_horizon_bars: int = 5,
        garman_klass_window: int = 20,
        kalman_process_noise: float = 1e-4,
        kalman_measurement_noise: float = 1e-2,
        max_win_probability: float = 0.90,
        calibrator: Optional[ProbabilityCalibrator] = None,
    ):
        self.calibrator = calibrator
        self.hurst_mr_thresh = hurst_mean_reversion_threshold
        self.hurst_mom_thresh = hurst_momentum_threshold
        self.zscore_thresh = zscore_entry_threshold
        self.entropy_thresh = entropy_order_threshold
        self.hurst_min_r2 = hurst_min_r_squared
        self.adf_critical = adf_critical
        self.poisson_horizon = poisson_horizon_bars
        self.gk_window = garman_klass_window
        self.kalman_q = kalman_process_noise
        self.kalman_r = kalman_measurement_noise
        self.max_win_prob = max_win_probability

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "CalculativeBrain":
        """Build from the `calculative_brain` section of config_synthetic.json."""
        return cls(
            hurst_mean_reversion_threshold=float(cfg.get("hurst_anti_persistent_threshold", 0.38)),
            hurst_momentum_threshold=float(cfg.get("hurst_persistent_threshold", 0.62)),
            zscore_entry_threshold=float(cfg.get("ou_zscore_entry_threshold", 2.0)),
            entropy_order_threshold=float(cfg.get("shannon_entropy_order_threshold", 0.78)),
            hurst_min_r_squared=float(cfg.get("hurst_min_r_squared", 0.75)),
            adf_critical=float(cfg.get("adf_critical_value", ADF_CRITICAL_5PCT)),
            poisson_horizon_bars=int(cfg.get("poisson_hazard_horizon_bars", 5)),
            garman_klass_window=int(cfg.get("garman_klass_window", 20)),
            kalman_process_noise=float(cfg.get("kalman_process_noise", 1e-4)),
            kalman_measurement_noise=float(cfg.get("kalman_measurement_noise", 1e-2)),
            max_win_probability=float(cfg.get("max_win_probability", 0.90)),
        )

    # ── Trade models ──────────────────────────────────────────────────────────

    def _ou_plan(self, price: float, ou: OUParameters) -> Optional[_TradePlan]:
        z0 = ou.current_zscore
        if abs(z0) < self.zscore_thresh:
            return None
        std = ou.stationary_std
        stop_z = 1.4
        sign = 1.0 if z0 > 0 else -1.0  # +1: above mean -> SELL
        # Model probability of reaching μ before the stop, blended toward the
        # zero-edge (random-walk) probability by the strength of the ADF evidence
        p_model = ou_hit_probability(abs(z0), 0.0, abs(z0) + stop_z)
        p_null = stop_z / (abs(z0) + stop_z)
        w = evidence_weight(ou.adf_tstat, self.adf_critical, self.adf_critical - 2.0)
        return _TradePlan(
            direction="SELL" if sign > 0 else "BUY",
            archetype="OU_MEAN_REVERSION",
            target=ou.mu,
            target_2=ou.mu - sign * std * 1.2,
            stop=price + sign * std * stop_z,
            win_prob=p_null + w * (p_model - p_null),
            horizon_bars=max(2, int(round(ou.expected_fpt_bars))),
        )

    def _momentum_plan(self, df: pd.DataFrame, price: float, gk_vol: float) -> Optional[_TradePlan]:
        t, m, sd = trend_tstat(df["close"])
        if sd <= 0 or abs(t) < NORMAL_CRITICAL_5PCT:
            return None
        # Shrink the in-sample drift: the window was selected because it trended
        drift = abs(m) * evidence_weight(t, NORMAL_CRITICAL_5PCT, 4.0) * price
        sigma = sd * price
        target_d, target2_d, stop_d = price * gk_vol * 2.8, price * gk_vol * 5.0, price * gk_vol * 0.9
        sign = 1.0 if t > 0 else -1.0
        # Trends are allowed to take a long time; the horizon only drives the stagnation exit on losers
        horizon = int(np.clip(target_d / drift, 3, 288)) if drift > 0 else 12
        return _TradePlan(
            direction="BUY" if sign > 0 else "SELL",
            archetype="FRACTAL_MOMENTUM",
            target=price + sign * target_d,
            target_2=price + sign * target2_d,
            stop=price - sign * stop_d,
            win_prob=drift_hit_probability(target_d, stop_d, drift, sigma),
            horizon_bars=horizon,
        )

    def _step_plan(self, df: pd.DataFrame, price: float, step: StepIndexAnalysis) -> Optional[_TradePlan]:
        if step.recommended_scalp == "STAND_ASIDE":
            return None
        closes = df["close"].astype(float).values[-60:]
        diffs = np.diff(closes)
        bar_sd = float(np.std(diffs))
        if bar_sd <= 0:
            return None
        # Symmetric bracket of one bar's typical move, rounded to the 0.1 step
        dist = max(0.1, round(bar_sd, 1))
        # Next-bar reversal edge from the Markov estimate, shrunk by runs-test strength
        q = 0.5 + (0.5 - step.streak_continuation_prob) * evidence_weight(step.runs_zscore, NORMAL_CRITICAL_5PCT, 4.0)
        drift = (2.0 * q - 1.0) * float(np.mean(np.abs(diffs)))
        sign = 1.0 if step.recommended_scalp == "SCALP_REVERSAL_BUY" else -1.0
        return _TradePlan(
            direction="BUY" if sign > 0 else "SELL",
            archetype="BERNOULLI_STEP_SCALP",
            target=price + sign * dist,
            target_2=price + sign * 2.0 * dist,
            stop=price - sign * dist,
            win_prob=drift_hit_probability(dist, dist, drift, bar_sd),
            horizon_bars=3,
        )

    @staticmethod
    def _boom_crash_plan(price: float, sym_upper: str, ph: PoissonHazardResult) -> Optional[_TradePlan]:
        is_crash = "CRASH" in sym_upper
        drift_sign = 1.0 if is_crash else -1.0  # Crash drifts up, Boom drifts down
        if ph.recommended_side == "HARVEST_DRIFT":
            return _TradePlan(
                direction="BUY" if is_crash else "SELL",
                archetype="QUIESCENT_DRIFT_HARVEST",
                target=price + drift_sign * ph.drift_target,
                target_2=price + drift_sign * ph.drift_target * 1.5,
                stop=price - drift_sign * ph.drift_stop,
                win_prob=ph.drift_win_prob,
                horizon_bars=max(3, int(round(ph.drift_target / ph.drift_per_bar))),
                loss_r=ph.drift_loss_r,
            )
        if ph.recommended_side == "CATCH_SPIKE":
            return _TradePlan(
                direction="SELL" if is_crash else "BUY",
                archetype="HAWKES_CLUSTER_SNIPE" if ph.is_cluster_window else "POISSON_HAZARD_SPIKE_SNIPE",
                target=price - drift_sign * ph.spike_target,
                target_2=price - drift_sign * ph.median_spike_size,
                stop=price + drift_sign * ph.spike_stop,
                win_prob=ph.spike_win_prob,
                horizon_bars=max(3, int(round(ph.spike_stop / ph.drift_per_bar))),
            )
        return None

    @staticmethod
    def _kalman_plan(price: float, kalman: KalmanFilterResult, gk_vol: float) -> Optional[_TradePlan]:
        z = kalman.zscore
        if abs(z) < 1.3:
            return None
        fair = kalman.fair_value
        direction = "SELL" if z > 0 else "BUY"
        target_dist = abs(price - fair)
        if target_dist <= 0:
            return None
        stop_dist = max(target_dist * 1.25, price * gk_vol * 1.25)
        stop = price + stop_dist if direction == "SELL" else price - stop_dist
        target = fair
        target_2 = fair - (0.5 * target_dist) if direction == "SELL" else fair + (0.5 * target_dist)
        win_prob = float(np.clip(0.55 + 0.15 * math.tanh(abs(z) - 1.0), 0.55, 0.78))
        return _TradePlan(
            direction=direction,
            archetype="KALMAN_FAIR_VALUE_REVERSION",
            target=target,
            target_2=target_2,
            stop=stop,
            win_prob=win_prob,
            horizon_bars=8,
            loss_r=1.0,
        )

    # ── Main entry point ──────────────────────────────────────────────────────

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str = "M5", spread: float = 0.0) -> QuantBrainAnalysis:
        """
        Run every estimator and price the applicable trade models.
        `spread` (price units, ask - bid) is charged against every trade: entering at the
        ask and leaving at the bid costs one spread, so E_net = E_gross - spread / risk.
        """
        if df is None or len(df) < 30:
            return QuantBrainAnalysis(
                symbol=symbol,
                timeframe=timeframe,
                current_price=0.0,
                hurst=HurstResult(0.5, "RANDOM_WALK", 0.0, False),
                ou=OUParameters(0.0, 0.0, 0.0, float("inf"), 1.0, 0.0, False),
                garman_klass_vol=0.01,
                entropy=EntropyResult(1.0, 1.0, False),
                poisson=None,
                math_edge_score=0.0,
                recommended_action="STAND_ASIDE",
                target_price=0.0,
                invalidation_price=0.0,
                half_life_horizon_bars=0,
                calculated_win_probability=0.5,
            )

        close = df["close"]
        price = float(close.iloc[-1])
        sym_upper = symbol.upper()

        hurst = compute_hurst_exponent(
            close, min_lag=8, max_lag=min(60, len(df) // 2),
            anti_persistent_below=self.hurst_mr_thresh,
            persistent_above=self.hurst_mom_thresh,
            min_r_squared=self.hurst_min_r2,
        )
        ou = fit_ornstein_uhlenbeck(close, adf_critical=self.adf_critical)
        gk_vol = compute_garman_klass_volatility(df, window=min(self.gk_window, len(df)))
        entropy = compute_shannon_entropy(close, window=min(30, len(df) - 1), order_threshold=self.entropy_thresh)
        poisson = compute_poisson_hazard(df, symbol, k_horizon=self.poisson_horizon)
        step_analysis = compute_step_index_runs_and_markov(close, symbol=symbol) if "STEP" in sym_upper else None
        kalman = fit_kalman_filter(close, process_noise=self.kalman_q, measurement_noise=self.kalman_r)
        var_95, cvar_95 = compute_var_cvar(df, confidence=0.95, horizon_bars=5, gk_vol=gk_vol)

        plan: Optional[_TradePlan] = None
        if poisson is not None:
            plan = self._boom_crash_plan(price, sym_upper, poisson)
        else:
            if step_analysis is not None:
                plan = self._step_plan(df, price, step_analysis)
            if plan is None and ou.is_reverting and hurst.hurst_exponent <= self.hurst_mr_thresh:
                plan = self._ou_plan(price, ou)
            if plan is None and hurst.regime == "PERSISTENT" and entropy.is_ordered:
                plan = self._momentum_plan(df, price, gk_vol)
            if plan is None and abs(kalman.zscore) >= 1.3:
                plan = self._kalman_plan(price, kalman, gk_vol)

        confluence = compute_multi_scale_confluence(df, factor=3, direction=plan.direction if plan else None)

        action = "STAND_ASIDE"
        archetype = "STAND_ASIDE"
        target = target_2 = invalidation = price
        win_prob = 0.5
        expected_r = 0.0
        edge_score = 0.0
        sharpe = 0.0
        horizon = max(3, int(round(ou.half_life))) if math.isfinite(ou.half_life) else 10

        risk = abs(price - plan.stop) if plan else 0.0
        cost_r = calibration = gross_r = 0.0
        if plan is not None and risk > 0:
            calibration = self.calibrator.factor(plan.archetype) if self.calibrator else 1.0
            win_prob = float(np.clip(plan.win_prob * calibration, 0.0, self.max_win_prob))
            rr = abs(plan.target - price) / risk
            cost_r = max(spread, 0.0) / risk
            gross_r = win_prob * rr - (1.0 - win_prob) * plan.loss_r
            expected_r = gross_r - cost_r
            win_r, loss_r = rr - cost_r, -plan.loss_r - cost_r
            variance = win_prob * (win_r - expected_r) ** 2 + (1.0 - win_prob) * (loss_r - expected_r) ** 2
            sharpe = expected_r / math.sqrt(variance) if variance > 1e-8 else 0.0

            edge_score = edge_score_from_expectancy(expected_r)
            if var_95 > 0.04:
                edge_score -= 5.0  # Extreme tail-risk environment
            if confluence.is_confluent:
                edge_score *= confluence.confluence_multiplier
            edge_score = float(np.clip(edge_score, 0.0, 99.0))

            if expected_r > 0:
                action = "SNIPE_BUY" if plan.direction == "BUY" else "SNIPE_SELL"
                archetype = plan.archetype
                target, target_2, invalidation = plan.target, plan.target_2, plan.stop
                horizon = plan.horizon_bars

        return QuantBrainAnalysis(
            symbol=symbol,
            timeframe=timeframe,
            current_price=price,
            hurst=hurst,
            ou=ou,
            garman_klass_vol=round(gk_vol, 6),
            entropy=entropy,
            poisson=poisson,
            math_edge_score=round(edge_score, 1),
            recommended_action=action,
            target_price=round(target, 5),
            invalidation_price=round(invalidation, 5),
            half_life_horizon_bars=horizon,
            calculated_win_probability=round(win_prob, 3),
            target_price_2=round(target_2, 5),
            expected_yield_r=round(expected_r, 2),
            confluence=confluence,
            step_analysis=step_analysis,
            strategy_archetype=archetype,
            instantaneous_sharpe=round(sharpe, 3),
            kalman_fair_value=kalman.fair_value,
            kalman_zscore=kalman.zscore,
            var_95_pct=round(var_95 * 100.0, 3),
            cvar_95_pct=round(cvar_95 * 100.0, 3),
            metadata={
                "hurst_tradable": hurst.is_tradable,
                "ou_reverting": ou.is_reverting,
                "ou_adf_tstat": ou.adf_tstat,
                "entropy_ordered": entropy.is_ordered,
                "macro_confluent": confluence.is_confluent,
                "strategy_archetype": archetype,
                "loss_r": plan.loss_r if plan else 1.0,
                "candidate_model": plan.archetype if plan else None,
                "gross_expected_r": round(gross_r, 3),
                "cost_r": round(cost_r, 3),
                "calibration_factor": round(calibration, 3) if plan else 1.0,
                "model_win_prob": round(plan.win_prob, 4) if plan else 0.5,  # before calibration
                "trend_tstat": round(trend_tstat(close)[0], 3),             # for re-checking open trades
            },
        )
