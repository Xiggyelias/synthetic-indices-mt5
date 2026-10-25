"""
Unit & Mathematical Verification Test Suite for QUANTA-SYNTH
============================================================
Verifies:
  1. Hurst Exponent (R/S analysis) fractal classification
  2. Ornstein-Uhlenbeck SDE parameter fitting and half-life calculation
  3. Garman-Klass volatility estimation
  4. Shannon entropy flow order / disorder detection
  5. Poisson jump hazard rate on Boom / Crash
  6. Fractional Kelly position sizing and Deriv contract normalization
  7. SyntheticStrategy quantitative signal generation
  8. Calibration: no edge is claimed on fair random processes
"""

import math
import numpy as np
import pandas as pd
import pytest

from quant_brain import (
    compute_hurst_exponent,
    fit_ornstein_uhlenbeck,
    compute_garman_klass_volatility,
    compute_shannon_entropy,
    compute_poisson_hazard,
    compute_step_index_runs_and_markov,
    compute_multi_scale_confluence,
    compute_fokker_planck_density,
    compute_expected_first_passage_time,
    fit_kalman_filter,
    resample_to_macro_timeframe,
    drift_hit_probability,
    ou_hit_probability,
    CalculativeBrain,
)
from synthetic_risk_manager import SyntheticRiskManager
from synthetic_strategy import SyntheticStrategy
from synthetic_backtester import SyntheticBacktestEngine, generate_synthetic_market_data


def test_hurst_exponent_calculation():
    # 1. Pure persistent linear trend with autocorrelation: H should be high (H >= 0.60)
    np.random.seed(42)
    innovations = np.random.normal(0, 1, 200)
    ar = np.zeros(200)
    for t in range(1, 200):
        ar[t] = 0.7 * ar[t - 1] + innovations[t]
    trend_prices = pd.Series(100.0 + np.cumsum(ar + 0.5))
    hurst_trend = compute_hurst_exponent(trend_prices, min_lag=10, max_lag=40)
    assert hurst_trend.hurst_exponent >= 0.60
    assert hurst_trend.regime == "PERSISTENT"
    assert hurst_trend.r_squared >= 0.70
    assert hurst_trend.is_tradable is True

    # 2. Oscillating mean-reverting series: H should be low (H < 0.45)
    revert_prices = pd.Series([100.0 + (3.0 if i % 2 == 0 else -3.0) for i in range(120)])
    hurst_revert = compute_hurst_exponent(revert_prices, min_lag=8, max_lag=40)
    assert hurst_revert.hurst_exponent < 0.45


def test_ornstein_uhlenbeck_fitting():
    # Generate synthetic O-U process: dX = theta*(mu - X)*dt + sigma*dW
    np.random.seed(123)
    mu_true = 50.0
    theta_true = 0.20
    sigma_noise = 1.0

    prices = [50.0]
    for _ in range(150):
        prev = prices[-1]
        step = prev + theta_true * (mu_true - prev) + sigma_noise * np.random.normal(0, 1)
        prices.append(step)

    ou_fit = fit_ornstein_uhlenbeck(pd.Series(prices), dt=1.0)
    assert ou_fit.is_reverting is True
    assert ou_fit.theta > 0.05
    assert abs(ou_fit.mu - mu_true) < 5.0
    assert 1.0 <= ou_fit.half_life <= 30.0
    assert isinstance(ou_fit.current_zscore, float)


def test_garman_klass_volatility():
    df = pd.DataFrame({
        "open": [100.0 + i for i in range(30)],
        "high": [102.0 + i for i in range(30)],
        "low": [99.0 + i for i in range(30)],
        "close": [101.0 + i for i in range(30)],
    })
    vol = compute_garman_klass_volatility(df, window=20)
    assert vol > 0.0
    assert not math.isnan(vol)


def test_shannon_entropy():
    # Highly ordered monotonic returns
    ordered_prices = pd.Series([100.0 + i * 0.5 for i in range(40)])
    entropy_ord = compute_shannon_entropy(ordered_prices, window=30)
    assert 0.0 <= entropy_ord.normalized_entropy <= 1.0
    assert entropy_ord.is_ordered is True


def test_poisson_hazard_boom_crash():
    # Simulate a Crash 1000 index: steady upward drift with a sudden downward drop
    opens = [1000.0 + i * 0.2 for i in range(45)]
    closes = [1000.0 + (i + 1) * 0.2 for i in range(45)]
    highs = [max(o, c) + 0.05 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.05 for o, c in zip(opens, closes)]

    # At candle 40 (5th from the end, indices 0..44):
    # Crash spike: opens at 1008.0, plunges to 983.0 (-25.0 drop)
    opens[40] = 1008.0
    closes[40] = 983.0
    highs[40] = 1008.1
    lows[40] = 982.9

    # Subsequent candles drift upward again from 983.0
    for i in range(41, 45):
        opens[i] = closes[i - 1]
        closes[i] = opens[i] + 0.2
        highs[i] = closes[i] + 0.05
        lows[i] = opens[i] - 0.05

    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})

    hazard = compute_poisson_hazard(df, symbol="Crash 1000 Index", lookback_window=45)
    assert hazard is not None
    assert hazard.bars_since_spike == 4  # 4 candles since spike at index 40
    assert hazard.spike_count == 1
    assert hazard.hazard_rate > 0.0
    assert 0.0 <= hazard.spike_prob_next_k <= 1.0
    # One spike is no evidence of self-excitation, and too few spikes to price a trade
    assert hazard.branching_ratio == 0.0
    assert hazard.is_cluster_window is False
    assert hazard.recommended_side == "STAND_ASIDE"


def test_fractional_kelly_sizing():
    risk_manager = SyntheticRiskManager(
        max_risk_pct=0.025,
        kelly_fraction=0.25,
    )

    # 1. High probability (p=0.70, b=2.0): Full Kelly = (0.7*2 - 0.3)/2 = 0.55
    # Quarter Kelly = 0.55 * 0.25 = 0.1375, clamped to max_risk_pct 0.025 (2.5%)
    kelly_risk = risk_manager.compute_kelly_risk(win_probability=0.70, reward_risk_ratio=2.0)
    assert kelly_risk == 0.025

    # 2. Negative expectancy (p=0.20, b=1.0): Full Kelly is negative -> 0.0
    neg_kelly = risk_manager.compute_kelly_risk(win_probability=0.20, reward_risk_ratio=1.0)
    assert neg_kelly == 0.0

    # 3. Deriv Volatility 75 contract normalization (min lot 0.001, step 0.001)
    vol_norm = risk_manager.normalize_volume("Volatility 75 Index", 0.00345)
    assert vol_norm == 0.003

    # Volume under min lot clamps to min lot
    vol_small = risk_manager.normalize_volume("Volatility 75 Index", 0.0002)
    assert vol_small == 0.001

    # Full position size calculation
    sizing = risk_manager.calculate_position_size(
        symbol="Volatility 75 Index",
        entry_price=1000.0,
        stop_loss=980.0,
        target_price=1050.0,
        account_balance=10000.0,
        win_probability=0.65,
    )
    assert sizing.is_valid is True
    assert sizing.volume >= 0.001
    assert sizing.risk_percentage <= 2.5


def test_synthetic_strategy_evaluation():
    brain = CalculativeBrain()
    risk_manager = SyntheticRiskManager()
    strategy = SyntheticStrategy(brain=brain, risk_manager=risk_manager, min_math_edge_score=50.0)

    # Simulate an oversold mean-reverting series on Volatility 75
    np.random.seed(99)
    mu = 1000.0
    prices = [mu]
    for _ in range(80):
        p = prices[-1] + 0.3 * (mu - prices[-1]) + np.random.normal(0, 1)
        prices.append(p)

    # Push last 5 prices down to create negative Z-score
    prices[-1] = mu - 15.0
    prices[-2] = mu - 12.0

    df = pd.DataFrame({
        "open": prices,
        "high": [p + 2.0 for p in prices],
        "low": [p - 2.0 for p in prices],
        "close": prices,
    })

    sig = strategy.evaluate(df, symbol="Volatility 75 Index", account_balance=10000.0)
    assert sig is None or sig.sizing.is_valid
    # The brain should calculate valid diagnostics without errors
    analysis = brain.analyze(df, symbol="Volatility 75 Index")
    assert isinstance(analysis.math_edge_score, float)
    assert isinstance(analysis.calculated_win_probability, float)


def test_step_index_runs_and_markov():
    # 1. Stretched upward streak: 6 consecutive +0.1 steps
    prices = [1000.0]
    for i in range(24):
        # Initial alternating steps ending with downward step
        prices.append(prices[-1] + (0.1 if i % 2 == 0 else -0.1))
    # Add 6 consecutive upward steps at the end
    for _ in range(6):
        prices.append(prices[-1] + 0.1)

    step_analysis = compute_step_index_runs_and_markov(pd.Series(prices), symbol="Step Index")
    assert step_analysis.current_streak_direction == "UP"
    assert step_analysis.current_streak_length == 6
    # Empirical, not 0.5**L: ups were mostly followed by downs in this window
    assert step_analysis.streak_continuation_prob < 0.5
    assert step_analysis.runs_zscore >= 1.96  # significant alternation
    assert step_analysis.is_statistically_stretched is True
    assert step_analysis.recommended_scalp == "SCALP_REVERSAL_SELL"
    assert 0.0 <= step_analysis.markov_p_up_given_up <= 1.0
    assert 0.0 <= step_analysis.markov_p_down_given_down <= 1.0


def _crash_bars(n, spikes, drift=0.2, spike=25.0):
    """Deterministic Crash bars: constant upward drift, downward spike at the given indices."""
    opens, closes = [], []
    price = 1000.0
    for i in range(n):
        opens.append(price)
        price = price - spike if i in spikes else price + drift
        closes.append(price)
    highs = [max(o, c) + 0.05 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.05 for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})


def test_hawkes_intensity_spike_clustering():
    # Spikes arrive in tight bursts with long gaps: far more follow-ups than the base rate predicts
    spikes = {10, 11, 12, 40, 41, 42, 70, 71, 72, 100, 101, 102, 117, 118}
    hazard = compute_poisson_hazard(_crash_bars(120, spikes), symbol="Crash 1000 Index", lookback_window=120)

    assert hazard is not None
    assert hazard.bars_since_spike == 1
    assert 0.0 < hazard.branching_ratio < 1.0  # Stable subcritical Hawkes process
    assert hazard.is_cluster_window is True
    assert hazard.hawkes_intensity > hazard.base_rate


def test_memoryless_spikes_have_no_cluster_window():
    # Evenly spaced spikes: no excess follow-ups, so no excitation
    hazard = compute_poisson_hazard(_crash_bars(100, set(range(9, 100, 10))),
                                    symbol="Crash 1000 Index", lookback_window=100)
    assert hazard.branching_ratio == 0.0
    assert hazard.is_cluster_window is False


def test_synthetic_backtest_engine():
    df = generate_synthetic_market_data(symbol="Volatility 75 Index", n_bars=300, seed=123)
    engine = SyntheticBacktestEngine(initial_balance=10000.0, min_math_edge_score=50.0)
    report = engine.run(df=df, symbol="Volatility 75 Index")

    assert report.total_bars == 300
    assert report.initial_balance == 10000.0
    assert report.final_balance > 0.0
    assert len(report.equity_curve) > 0
    assert isinstance(report.profit_factor, float)
    assert isinstance(report.sharpe_ratio, float)


def test_multi_scale_fractal_confluence():
    df = generate_synthetic_market_data(symbol="Volatility 75 Index", n_bars=120, seed=42)
    macro_df = resample_to_macro_timeframe(df, factor=3)

    assert len(macro_df) == len(df) // 3
    assert set(["open", "high", "low", "close"]).issubset(macro_df.columns)

    confluence = compute_multi_scale_confluence(df, factor=3)
    assert confluence is not None
    assert isinstance(confluence.is_confluent, bool)
    assert confluence.confluence_multiplier >= 1.0
    assert isinstance(confluence.macro_zscore, float)


def test_adaptive_kelly_and_expected_yield():
    risk_manager = SyntheticRiskManager(max_risk_pct=0.15, kelly_fraction=0.25)

    # 1. High edge score (88.0) scales Kelly multiplier up vs moderate edge score (65.0)
    risk_ultra = risk_manager.compute_adaptive_kelly_risk(win_probability=0.55, reward_risk_ratio=1.5, math_edge_score=88.0)
    risk_mod = risk_manager.compute_adaptive_kelly_risk(win_probability=0.55, reward_risk_ratio=1.5, math_edge_score=65.0)

    assert risk_ultra > risk_mod
    assert risk_ultra <= 0.15

    # 2. Dollar expectancy calculation
    # p=0.60, risk $100, reward $200 -> E[$] = 0.60*200 - 0.40*100 = 120 - 40 = $80.0
    exp_dollars = risk_manager.compute_expected_dollar_yield(win_probability=0.60, risk_dollars=100.0, reward_dollars=200.0)
    assert abs(exp_dollars - 80.0) < 1e-6


def test_fokker_planck_and_first_passage_time():
    mu = 1000.0
    sigma = 10.0
    theta = 0.25

    # At mu, density is maximized and tail p-value is 1.0
    density_center, tail_p_center = compute_fokker_planck_density(mu, mu, sigma, theta)
    assert density_center > 0.0
    assert abs(tail_p_center - 1.0) < 1e-4

    # At 2.5 sigma deviation, tail p-value should be small (p < 0.05)
    stat_std = sigma / math.sqrt(2.0 * theta)
    density_dev, tail_p_dev = compute_fokker_planck_density(mu + 2.5 * stat_std, mu, sigma, theta)
    assert density_dev < density_center
    assert tail_p_dev < 0.05

    # First Passage Time: should scale with distance from equilibrium
    fpt_close = compute_expected_first_passage_time(mu + 0.5 * stat_std, mu, sigma, theta)
    fpt_far = compute_expected_first_passage_time(mu + 3.0 * stat_std, mu, sigma, theta)
    assert fpt_far > fpt_close
    assert fpt_far < 120.0


def test_quiescent_drift_and_hawkes_modes():
    # Strong drift relative to rare, small spikes: harvesting the drift is +EV
    hazard = compute_poisson_hazard(_crash_bars(100, {10, 25, 40, 55, 70, 88}, drift=2.0, spike=10.0),
                                    symbol="Crash 1000 Index", lookback_window=100)
    assert hazard is not None
    assert hazard.bars_since_spike == 11
    assert hazard.is_cluster_window is False
    assert hazard.is_quiescent_drift is True
    assert hazard.drift_expectancy_r > 0.1 > hazard.spike_expectancy_r
    assert hazard.recommended_side == "HARVEST_DRIFT"

    # Zero net drift (0.06 spikes/bar x 10 = 0.6 drift/bar): neither side has an edge
    fair = compute_poisson_hazard(_crash_bars(50, {10, 25, 38}, drift=0.6, spike=10.0),
                                  symbol="Crash 1000 Index", lookback_window=50)
    assert fair.drift_expectancy_r < 0.1 and fair.spike_expectancy_r < 0.1
    assert fair.recommended_side == "STAND_ASIDE"

    # Spikes outweigh the drift (net falling market): selling for the spike is the +EV side
    falling = compute_poisson_hazard(_crash_bars(50, {10, 25, 38}, drift=0.2, spike=25.0),
                                     symbol="Crash 1000 Index", lookback_window=50)
    assert falling.drift_expectancy_r < 0
    assert falling.recommended_side == "CATCH_SPIKE"


def test_super_elite_aggressive_half_kelly():
    # Aggressive mode should allow up to 6.5% risk on supreme conviction
    rm_aggressive = SyntheticRiskManager(
        max_risk_pct=0.065,
        kelly_fraction=0.50,
        aggressive_mode=True,
    )

    risk_ultra = rm_aggressive.compute_adaptive_kelly_risk(
        win_probability=0.75,
        reward_risk_ratio=2.5,
        math_edge_score=92.0,
    )
    # Full Kelly = (0.75*2.5 - 0.25)/2.5 = 0.65
    # Half-Kelly with aggressive boost = 0.65 * 0.65 = 0.42, clamped to max_risk_pct 0.065 (6.5%)
    assert risk_ultra == 0.065

    # Check sizing output with aggressive mode
    sizing = rm_aggressive.calculate_position_size(
        symbol="Volatility 75 Index",
        entry_price=1000.0,
        stop_loss=980.0,
        target_price=1050.0,
        account_balance=10000.0,
        win_probability=0.75,
        math_edge_score=92.0,
    )
    assert sizing.is_valid is True
    assert sizing.risk_dollars <= 650.0
    assert sizing.risk_percentage <= 6.5


def test_kalman_filter_state_tracking():
    # Drift around 50.0 with random noise
    np.random.seed(42)
    true_mean = 50.0
    noise = np.random.normal(0, 1.0, 100)
    prices = pd.Series([true_mean + (0.05 * i) + noise[i] for i in range(100)])

    res = fit_kalman_filter(prices, process_noise=1e-4, measurement_noise=1e-2)
    assert abs(res.fair_value - prices.iloc[-1]) < 3.0
    assert res.error_variance > 0.0
    assert len(res.smoothed_series) == 100
    assert len(res.residuals) == 100
    assert isinstance(res.zscore, float)


def test_streaks_alone_do_not_trigger_step_reversal():
    # A long streak inside persistent (few-runs) data must not be faded
    prices = [1000.0]
    for block in range(6):
        for _ in range(5):
            prices.append(prices[-1] + (0.1 if block % 2 == 0 else -0.1))
    step = compute_step_index_runs_and_markov(pd.Series(prices), symbol="Step Index")
    assert step.current_streak_length >= 5
    assert step.runs_zscore < 0
    assert step.recommended_scalp == "STAND_ASIDE"


def test_hurst_is_unbiased_on_random_walk():
    rng = np.random.default_rng(1)
    h = [
        compute_hurst_exponent(pd.Series(1000 * np.exp(np.cumsum(rng.normal(0, 0.002, 300)))),
                               min_lag=8, max_lag=60).hurst_exponent
        for _ in range(60)
    ]
    assert abs(np.mean(h) - 0.5) < 0.06


def test_ou_fit_requires_stationarity_evidence():
    rng = np.random.default_rng(2)
    walks = [pd.Series(1000 + np.cumsum(rng.normal(0, 1, 300))) for _ in range(100)]
    false_positive_rate = np.mean([fit_ornstein_uhlenbeck(w).is_reverting for w in walks])
    assert false_positive_rate < 0.12  # ~5% by construction of the 5% ADF test

    x = np.zeros(300)
    for t in range(1, 300):
        x[t] = 0.85 * x[t - 1] + rng.normal(0, 1)
    fit = fit_ornstein_uhlenbeck(pd.Series(1000 + x))
    assert fit.is_reverting is True
    assert fit.adf_tstat < -2.89


def test_hitting_probabilities():
    # O-U: boundary values and the pull toward the mean
    assert ou_hit_probability(0.0, 0.0, 3.4) == pytest.approx(1.0)
    assert ou_hit_probability(3.4, 0.0, 3.4) == pytest.approx(0.0)
    assert ou_hit_probability(2.0, 0.0, 3.4) > 1.4 / 3.4  # better than a driftless walk

    # Drifted Brownian motion: zero drift is the gambler's-ruin ratio
    assert drift_hit_probability(2.0, 1.0, 0.0, 1.0) == pytest.approx(1.0 / 3.0)
    assert drift_hit_probability(2.0, 1.0, 0.2, 1.0) > 1.0 / 3.0
    assert drift_hit_probability(2.0, 1.0, -0.2, 1.0) < 1.0 / 3.0
    assert 0.0 <= drift_hit_probability(50.0, 50.0, 5.0, 0.1) <= 1.0  # no overflow


def test_kalman_zscore_is_scaled_to_residuals():
    rng = np.random.default_rng(3)
    zs = [abs(fit_kalman_filter(pd.Series(1000 + np.cumsum(rng.normal(0, 1, 200)))).zscore) for _ in range(50)]
    assert np.median(zs) < 2.0


def test_brain_stands_aside_on_fair_processes():
    brain = CalculativeBrain()
    for sym in ("Volatility 75 Index", "Step Index", "Crash 1000 Index"):
        df = generate_synthetic_market_data(symbol=sym, n_bars=900, seed=11)
        signals = 0
        for i in range(300, 900, 20):
            a = brain.analyze(df.iloc[i - 300:i], symbol=sym)
            signals += a.recommended_action != "STAND_ASIDE" and a.math_edge_score >= 65
        assert signals <= 3, sym


def _ou_dip_frame(seed=1):
    rng = np.random.default_rng(seed)
    x = np.zeros(300)
    for t in range(1, 300):
        x[t] = 0.8 * x[t - 1] + rng.normal(0, 1.0)
    x[-1] = -3.0 * np.std(x)
    c = 1000 + x
    return pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c + 0.3, "low": c - 0.3, "close": c})


def test_strategy_signal_on_genuine_reversion():
    # Regression: VaR scaling used to build SizingResult(risk_percent=...) and raise TypeError
    rm = SyntheticRiskManager(max_risk_pct=0.02)
    sig = SyntheticStrategy(risk_manager=rm).evaluate(_ou_dip_frame(), "Volatility 75 Index", account_balance=10000.0)
    assert sig is not None
    assert sig.direction == "BUY"
    assert sig.strategy_type == "OU_MEAN_REVERSION"
    assert sig.sizing.risk_percentage <= 2.0 + 1e-9
    assert sig.take_profit > sig.entry_price > sig.stop_loss


def test_risk_scale_can_only_reduce_risk():
    rm = SyntheticRiskManager(max_risk_pct=0.02, kelly_fraction=0.35)
    kwargs = dict(symbol="Volatility 75 Index", entry_price=1000.0, stop_loss=990.0, target_price=1020.0,
                  account_balance=10000.0, win_probability=0.6, math_edge_score=75.0)
    base = rm.calculate_position_size(**kwargs)
    boosted = rm.calculate_position_size(**kwargs, risk_scale=1.5)
    halved = rm.calculate_position_size(**kwargs, risk_scale=0.5)
    assert boosted.risk_dollars == base.risk_dollars
    assert halved.risk_dollars < base.risk_dollars


def test_min_lot_that_exceeds_risk_cap_is_rejected():
    # $5 account, Volatility 100 min lot 0.10, 20-point stop -> $2 risk = 40% of balance
    rm = SyntheticRiskManager(max_risk_pct=0.065)
    sizing = rm.calculate_position_size(
        symbol="Volatility 100 Index", entry_price=1000.0, stop_loss=980.0, target_price=1040.0,
        account_balance=5.0, win_probability=0.7, math_edge_score=80.0,
    )
    assert sizing.is_valid is False
    assert "Minimum lot" in sizing.rejection_reason


def test_volume_normalization_is_float_safe():
    assert SyntheticRiskManager().normalize_volume("Volatility 75 Index", 0.003) == 0.003


def _runner_trade(**overrides):
    trade = {
        "entry_bar": 0, "direction": "BUY", "strategy_type": "FRACTAL_MOMENTUM",
        "entry_price": 100.0, "stop_loss": 99.0, "initial_sl": 99.0,
        "take_profit": 103.0, "take_profit_2": 110.0, "risk_dollars": 100.0,
        "half_life_bars": 50, "be_locked": False, "runner": True, "tp1_reached": False,
        "banked_r": 0.0, "remaining": 1.0, "best_price": 100.0,
    }
    trade.update(overrides)
    return trade


def _bar(high, low, close):
    return pd.Series({"open": close, "high": high, "low": low, "close": close})


def test_backtest_runner_banks_tp1_and_trails_the_rest():
    engine = SyntheticBacktestEngine(be_trigger_r=1.5, trail_atr_mult=3.0)
    t = _runner_trade()

    # TP1 hit: half banked at 3R, stop locked at +0.25R, trade stays open
    assert engine._check_exit(t, _bar(103.5, 102.5, 103.2), 1, False, 0.3) is None
    assert t["tp1_reached"] and t["banked_r"] == pytest.approx(1.5) and t["remaining"] == 0.5
    assert t["stop_loss"] >= 100.25

    # Trend keeps going: the stop follows 3 ATR behind the best high, never backwards
    for i, high in enumerate([104.0, 105.0, 106.0, 107.0], start=2):
        assert engine._check_exit(t, _bar(high, high - 0.5, high - 0.2), i, False, 0.3) is None
    assert t["stop_loss"] == pytest.approx(107.0 - 0.9)
    assert engine._check_exit(t, _bar(106.9, 106.5, 106.7), 6, False, 0.3) is None
    assert t["stop_loss"] == pytest.approx(106.1)

    # Pullback takes out the trailed stop: runner exits at 6.1R, total 1.5 + 0.5 * 6.1 = 4.55R
    exit_price, reason = engine._check_exit(t, _bar(106.3, 105.5, 105.8), 7, False, 0.3)
    assert reason == "TRAILED_STOP" and exit_price == pytest.approx(106.1)
    assert t["banked_r"] + t["remaining"] * (exit_price - 100.0) == pytest.approx(4.55)


def test_backtest_non_runner_exits_at_tp1():
    engine = SyntheticBacktestEngine()
    t = _runner_trade(strategy_type="OU_MEAN_REVERSION", runner=False)
    assert engine._check_exit(t, _bar(103.5, 102.5, 103.2), 1, False, 0.3) == (103.0, "TAKE_PROFIT")


class _FakeConn:
    """Just enough of MT5Connection for position management."""

    def __init__(self, bars):
        self.bars, self.price = bars, 100.0
        self.sltp, self.closes = [], []

    def get_position(self, ticket): return object()
    def get_symbol_price(self, symbol): return type("P", (), {"bid": self.price, "ask": self.price})()
    def get_broker_rules(self, symbol): return None
    def get_rates(self, symbol, timeframe, count): return self.bars[-count:]

    def set_position_sltp(self, ticket, sl, tp, comment=""):
        self.sltp.append((comment, round(sl, 4), tp))
        return type("R", (), {"retcode": 10009})()

    def close_position(self, ticket, comment="", volume=None):
        self.closes.append((comment, volume))
        return type("R", (), {"retcode": 10009})()


def test_live_runner_partial_close_and_trailing(tmp_path):
    from synthetic_bot_system import SyntheticBotConfig, SyntheticBotSystem

    n = 40
    bars = np.zeros(n, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
    bars["close"] = 100.0
    bars["open"] = 100.0
    bars["high"] = 100.15   # ATR(14) = 0.3
    bars["low"] = 99.85
    conn = _FakeConn(bars)
    quiet = type("N", (), {"send": lambda self, msg: None})()
    bot = SyntheticBotSystem(conn, SyntheticBotConfig(state_path=str(tmp_path / "s.json"), be_trigger_r=1.5),
                             notifier=quiet)
    bot.open_trades[1] = {
        "ticket": 1, "symbol": "Volatility 75 Index", "direction": "BUY", "strategy_type": "FRACTAL_MOMENTUM",
        "entry_price": 100.0, "initial_stop_loss": 99.0, "stop_loss": 99.0,
        "take_profit": 110.0, "take_profit_1": 103.0, "take_profit_2": 110.0,
        "runner": True, "tp1_reached": False, "best_price": 100.0, "volume": 0.01,
        "half_life_horizon_bars": 50, "opened_at": "2026-10-23T00:00:00+00:00",
    }
    t = bot.open_trades[1]

    conn.price = 103.2               # past TP1
    bot.manage_positions()
    assert conn.closes == [("Q-TP1-BANK", 0.005)]
    assert t["volume"] == pytest.approx(0.005) and t["tp1_reached"]
    assert t["stop_loss"] >= 102.3   # trailed 3 ATR behind the best price
    assert all(tp == 110.0 for _, _, tp in conn.sltp)  # broker TP stays at TP2

    conn.price = 106.0
    bot.manage_positions()
    assert t["stop_loss"] == pytest.approx(105.1)
    conn.price = 104.0               # pullback: stop must not move back
    bot.manage_positions()
    assert t["stop_loss"] == pytest.approx(105.1)
    assert len(conn.closes) == 1     # no other rule closed the runner


def test_generators_follow_index_mechanics():
    step = generate_synthetic_market_data("Step Index", n_bars=50, seed=5)
    tenths = np.diff(step["close"].values) * 10
    assert np.allclose(tenths, np.round(tenths))  # moves are whole 0.1 steps

    crash = generate_synthetic_market_data("Crash 500 Index", n_bars=400, seed=5)
    body = (crash["close"] - crash["open"]).values
    assert np.median(body) > 0                         # drifts up between spikes
    assert body.min() < -5 * np.median(np.abs(body))   # with sharp drops


# ── Costs, self-calibration, edge scanner ───────────────────────────────────

def test_spread_is_charged_against_expectancy():
    brain = CalculativeBrain()
    df = _ou_dip_frame()
    free = brain.analyze(df, "Volatility 75 Index")
    risk = abs(free.current_price - free.invalidation_price)
    costly = brain.analyze(df, "Volatility 75 Index", spread=0.25 * risk)
    assert costly.metadata["cost_r"] == pytest.approx(0.25, abs=1e-3)
    assert costly.expected_yield_r == pytest.approx(free.expected_yield_r - 0.25, abs=0.02)
    assert costly.math_edge_score <= free.math_edge_score  # both may sit at the 99 cap

    # A spread larger than the gross edge turns the trade off
    gross = free.metadata["gross_expected_r"]
    priced_out = brain.analyze(df, "Volatility 75 Index", spread=(gross + 0.1) * risk)
    assert priced_out.recommended_action == "STAND_ASIDE"


def test_calibrator_learns_from_results(tmp_path):
    from synthetic_calibration import ProbabilityCalibrator

    path = tmp_path / "cal.json"
    cal = ProbabilityCalibrator(str(path), prior_strength=20)
    assert cal.factor("OU_MEAN_REVERSION") == 1.0  # no record: trust the model

    # Model claims 80% but only 40% of 100 trades win
    for i in range(100):
        cal.record("OU_MEAN_REVERSION", 0.8, won=i % 5 < 2)
    f = cal.factor("OU_MEAN_REVERSION")
    assert 0.5 < f < 0.6                        # pulled most of the way to 0.4 / 0.8
    assert cal.adjust("OU_MEAN_REVERSION", 0.8) == pytest.approx(0.8 * f)
    assert cal.factor("FRACTAL_MOMENTUM") == 1.0  # other archetypes untouched

    # Persisted and reloaded
    assert ProbabilityCalibrator(str(path)).factor("OU_MEAN_REVERSION") == pytest.approx(f)

    # A few bad trades barely move it: the prior keeps it stable
    small = ProbabilityCalibrator(None, prior_strength=20)
    for _ in range(3):
        small.record("X", 0.8, won=False)
    assert small.factor("X") > 0.8


def test_brain_applies_calibration():
    from synthetic_calibration import ProbabilityCalibrator

    df = _ou_dip_frame()
    plain = CalculativeBrain().analyze(df, "Volatility 75 Index")
    cal = ProbabilityCalibrator(None, prior_strength=0)
    for i in range(50):
        cal.record("OU_MEAN_REVERSION", plain.metadata["model_win_prob"], won=i % 2 == 0)
    tuned = CalculativeBrain(calibrator=cal).analyze(df, "Volatility 75 Index")
    assert tuned.calculated_win_probability < plain.calculated_win_probability
    assert tuned.metadata["model_win_prob"] == plain.metadata["model_win_prob"]


def test_live_bot_records_closed_trades_for_calibration(tmp_path):
    from synthetic_bot_system import SyntheticBotConfig, SyntheticBotSystem

    class ClosedConn(_FakeConn):
        def get_position(self, ticket): return None  # broker already closed it
        def get_position_history(self, ticket):
            deal = type("D", (), {"profit": -12.0, "commission": -0.5, "swap": 0.0})
            return [deal()]

    quiet = type("N", (), {"send": lambda self, msg: None})()
    cfg = SyntheticBotConfig(state_path=str(tmp_path / "s.json"), calibration_path=str(tmp_path / "cal.json"))
    bot = SyntheticBotSystem(ClosedConn(np.zeros(0)), cfg, notifier=quiet)
    bot.open_trades[5] = {"ticket": 5, "symbol": "Volatility 75 Index", "direction": "BUY",
                          "strategy_type": "OU_MEAN_REVERSION", "win_probability": 0.7,
                          "model_win_probability": 0.8}
    bot.manage_positions()

    assert bot.closed_trades[-1]["realised_pnl"] == pytest.approx(-12.5)
    stats = bot.brain.calibrator.stats["OU_MEAN_REVERSION"]
    assert stats == {"n": 1, "wins": 0, "claimed_sum": pytest.approx(0.8)}  # learns from the raw model p


def _frame_from_closes(c):
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0005,
                         "low": np.minimum(o, c) * 0.9995, "close": c})


def test_edge_scanner_quiet_on_fair_indices():
    from edge_scanner import scan

    frames = {s: generate_synthetic_market_data(s, 4000, seed=i)
              for i, s in enumerate(["Volatility 75 Index", "Step Index", "Crash 1000 Index", "Boom 500 Index"])}
    results = scan(frames)
    assert results and not any(t.significant for t in results)


def test_edge_scanner_finds_planted_flaws():
    from edge_scanner import scan

    rng = np.random.default_rng(3)
    r, e = np.zeros(5000), rng.normal(0, 0.002, 5000)
    for t in range(1, 5000):
        r[t] = -0.12 * r[t - 1] + e[t]
    frames = {
        "Mean reverting": _frame_from_closes(1000 * np.exp(np.cumsum(r))),
        "Drifting": _frame_from_closes(1000 * np.exp(np.cumsum(rng.normal(0.0002, 0.002, 5000)))),
    }
    found = {(t.symbol, t.test) for t in scan(frames) if t.significant}
    assert ("Mean reverting", "variance_ratio_2") in found
    assert ("Drifting", "drift") in found


def test_spike_dispersion_detects_bursts():
    from edge_scanner import spike_dispersion_test

    regular = np.zeros(4000, dtype=bool)
    regular[::10] = True
    bursty = np.zeros(4000, dtype=bool)
    for start in range(0, 4000, 200):
        bursty[start:start + 20] = True
    assert spike_dispersion_test(regular)[1] > 0.5
    chi2, p, d = spike_dispersion_test(bursty)
    assert p < 1e-6 and d > 3


def test_benjamini_hochberg_controls_discoveries():
    from edge_scanner import EdgeTest, benjamini_hochberg

    tests = [EdgeTest("S", f"t{i}", 0.0, p, 0.0, "") for i, p in enumerate([0.001, 0.01, 0.03, 0.04, 0.5])]
    benjamini_hochberg(tests, alpha=0.05)
    assert [t.significant for t in tests] == [True, True, True, True, False]
    qs = sorted(tests, key=lambda t: t.p_value)
    assert all(a.q_value <= b.q_value for a, b in zip(qs, qs[1:]))
    # q = p * m / rank: 0.04 * 5 / 4 = 0.05, exactly on the boundary
    assert tests[3].q_value == pytest.approx(0.05)
    # A lone p = 0.03 among 20 tests is not a discovery (0.03 * 20 = 0.6)
    many = [EdgeTest("S", f"t{i}", 0.0, 0.03 if i == 0 else 0.5, 0.0, "") for i in range(20)]
    assert not any(t.significant for t in benjamini_hochberg(many))


# ── Thesis-based holding and pyramiding ─────────────────────────────────────

def _fake_analysis(trend_t=3.0, regime="PERSISTENT", adf_t=-4.0):
    from types import SimpleNamespace
    return SimpleNamespace(metadata={"trend_tstat": trend_t}, hurst=SimpleNamespace(regime=regime),
                           ou=SimpleNamespace(adf_tstat=adf_t))


def test_thesis_rules_use_looser_exit_than_entry():
    from synthetic_strategy import thesis_intact

    # Trend: entry needed t > 1.96; a weaker but still positive drift keeps the trade
    assert thesis_intact(_fake_analysis(trend_t=0.4), "BUY", "FRACTAL_MOMENTUM")
    assert not thesis_intact(_fake_analysis(trend_t=-0.4), "BUY", "FRACTAL_MOMENTUM")
    assert thesis_intact(_fake_analysis(trend_t=-0.4), "SELL", "FRACTAL_MOMENTUM")
    assert not thesis_intact(_fake_analysis(regime="ANTI_PERSISTENT"), "BUY", "FRACTAL_MOMENTUM")
    # Reversion: entry needed ADF t < -2.89; only clear loss of evidence exits
    assert thesis_intact(_fake_analysis(adf_t=-2.0), "BUY", "OU_MEAN_REVERSION")
    assert not thesis_intact(_fake_analysis(adf_t=-1.0), "BUY", "OU_MEAN_REVERSION")
    assert thesis_intact(_fake_analysis(), "BUY", "QUIESCENT_DRIFT_HARVEST")


def _holding_bot(tmp_path):
    from synthetic_bot_system import SyntheticBotConfig, SyntheticBotSystem

    bars = np.zeros(40, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
    bars["open"] = bars["close"] = 100.0
    bars["high"], bars["low"] = 100.15, 99.85      # ATR(14) = 0.3
    conn = _FakeConn(bars)
    quiet = type("N", (), {"send": lambda self, msg: None})()
    cfg = SyntheticBotConfig(state_path=str(tmp_path / "s.json"), calibration_enabled=False, be_trigger_r=1.5)
    return SyntheticBotSystem(conn, cfg, notifier=quiet), conn


def _runner_record(**overrides):
    t = {"ticket": 1, "symbol": "Volatility 75 Index", "direction": "BUY", "strategy_type": "FRACTAL_MOMENTUM",
         "entry_price": 100.0, "initial_stop_loss": 99.0, "stop_loss": 99.0,
         "take_profit": 105.0, "take_profit_1": 103.0, "take_profit_2": 105.0,
         "runner": True, "tp1_reached": False, "best_price": 100.0, "volume": 0.01,
         "half_life_horizon_bars": 50, "opened_at": "2026-10-23T00:00:00+00:00"}
    t.update(overrides)
    return t


def test_live_thesis_break_exits_and_intact_thesis_extends_target(tmp_path):
    bot, conn = _holding_bot(tmp_path)

    # Thesis intact, runner past TP1 and within one ATR of its target: target moves out a step
    bot.open_trades[1] = _runner_record(tp1_reached=True, stop_loss=103.9, best_price=104.8)
    conn.price = 104.8
    bot._latest_analysis["Volatility 75 Index"] = (1000, _fake_analysis(trend_t=1.0))
    bot.manage_positions()
    assert bot.open_trades[1]["take_profit"] == pytest.approx(107.0)
    assert not conn.closes

    # Same candle again: checked once per candle, no second extension
    bot.manage_positions()
    assert bot.open_trades[1]["take_profit"] == pytest.approx(107.0)

    # Next candle the trend has reversed: the bot leaves instead of waiting for the stop
    bot._latest_analysis["Volatility 75 Index"] = (1300, _fake_analysis(trend_t=-1.2))
    bot.manage_positions()
    assert conn.closes[-1][0] == "THESIS_BROKEN"


def test_live_pyramiding_only_onto_locked_winners(tmp_path):
    from types import SimpleNamespace

    bot, _ = _holding_bot(tmp_path)
    sig = SimpleNamespace(direction="BUY")
    unlocked = _runner_record(stop_loss=99.0)
    locked = _runner_record(stop_loss=100.25)

    assert not bot._can_pyramid(sig, [unlocked])                 # first leg can still lose
    assert bot._can_pyramid(sig, [locked])                       # first leg is risk-free
    assert not bot._can_pyramid(SimpleNamespace(direction="SELL"), [locked])
    assert not bot._can_pyramid(sig, [locked, locked, locked])   # max 2 adds
    bot.config.pyramid_enabled = False
    assert not bot._can_pyramid(sig, [locked])


def test_backtest_thesis_exit_and_pyramid(monkeypatch):
    from types import SimpleNamespace
    import synthetic_backtester as bt

    engine = bt.SyntheticBacktestEngine(lookback_bars=60, pyramid_max_adds=1)
    closes = np.linspace(100, 110, 200)
    df = pd.DataFrame({"open": closes, "high": closes + 0.05, "low": closes - 0.05, "close": closes})

    trend = {"t": 3.0}
    monkeypatch.setattr(engine.brain, "analyze", lambda *a, **k: _fake_analysis(trend_t=trend["t"]))

    calls = {"n": 0}

    def fake_signal(analysis, account_balance, **_):
        calls["n"] += 1
        i = 60 + calls["n"] - 1
        price = closes[min(i, len(closes) - 1)]
        if calls["n"] in (1, 40):  # first entry, then a second signal much later
            return SimpleNamespace(direction="BUY", strategy_type="FRACTAL_MOMENTUM", entry_price=price,
                                   stop_loss=price - 0.5, take_profit=price + 50, take_profit_2=price + 80,
                                   sizing=SimpleNamespace(risk_dollars=100.0), half_life_horizon_bars=500,
                                   raw_analysis=None, calculated_win_prob=0.5)
        if calls["n"] == 120:
            trend["t"] = -1.0       # trend reverses: both legs should exit on thesis
        return None

    monkeypatch.setattr(engine.strategy, "signal_from_analysis", fake_signal)
    report = engine.run(df, "Volatility 75 Index")

    reasons = [t["exit_reason"] for t in report.trades]
    assert len(report.trades) == 2 and reasons == ["THESIS_BROKEN", "THESIS_BROKEN"]
    assert [t["pyramid_add"] for t in report.trades] == [False, True]  # the add came once the first leg was locked
    assert all(t["pnl_r"] > 0 for t in report.trades)
