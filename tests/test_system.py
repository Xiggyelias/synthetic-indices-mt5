import os
import sys
import json
import tempfile
import pytest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock

# Add src and scripts to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from trading_bot_engulfing_v2 import EngulfingBotV2, TradingConfig, Signal
from mt5_system import load_trading_config_from_json, _config_raw_for_symbol
from trade_manager import ManagedTrade, TradeManager
from trade_supervisor import TradeSupervisor
from forward_test_tracker import ForwardTestTracker
from monitor_forward_test import ForwardTestMonitor


def generate_synthetic_candles(n=300):
    dates = pd.date_range("2026-01-01", periods=n, freq="1h")
    # Generate random walk with slight upward drift
    np.random.seed(42)
    returns = np.random.normal(0.0002, 0.001, size=n)
    price = 1.0800 * np.cumprod(1 + returns)
    
    high = price * (1 + np.abs(np.random.normal(0, 0.0005, size=n)))
    low = price * (1 - np.abs(np.random.normal(0, 0.0005, size=n)))
    open_ = (high + low) / 2.0
    close = price
    vol = np.random.uniform(500, 2000, size=n)
    
    df = pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": vol
    }, index=dates)
    return df


def test_deep_merge_and_symbol_overrides():
    raw_cfg = {
        "risk_management": {"risk_per_trade": 0.01, "max_open_trades": 3},
        "strategy_parameters": {"timeframe": "H1", "volume_multiplier": 1.2},
        "symbol_overrides": {
            "XAUUSD": {
                "enabled": False,
                "allowed_directions": [],
                "risk_management": {"risk_per_trade": 0.005}
            }
        }
    }
    
    # Base / EURUSD config
    eur_raw = _config_raw_for_symbol(raw_cfg, "EURUSD")
    assert eur_raw["risk_management"]["risk_per_trade"] == 0.01
    assert eur_raw.get("enabled", True) is True
    
    # XAUUSD config
    xau_raw = _config_raw_for_symbol(raw_cfg, "XAUUSD")
    assert xau_raw.get("enabled") is False
    assert xau_raw.get("allowed_directions") == []
    assert xau_raw["risk_management"]["risk_per_trade"] == 0.005
    assert xau_raw["risk_management"]["max_open_trades"] == 3


def test_load_trading_config_from_json():
    config_file = os.path.join(BASE_DIR, "config", "config_rsi_pullback.json")
    if not os.path.exists(config_file):
        pytest.skip("config_rsi_pullback.json not found")
        
    cfg_eur = load_trading_config_from_json(config_file, symbol="EURUSD")
    assert cfg_eur.enabled is True
    assert "buy" in cfg_eur.allowed_directions
    assert "sell" in cfg_eur.allowed_directions
    
    cfg_xau = load_trading_config_from_json(config_file, symbol="XAUUSD")
    assert cfg_xau.enabled is False
    assert cfg_xau.allowed_directions == []


def test_strategy_indicators_and_signals():
    cfg = TradingConfig()
    bot = EngulfingBotV2(cfg)
    df = generate_synthetic_candles(300)
    
    df_calc = bot.calculate_indicators(df)
    assert "EMA_50" in df_calc.columns
    assert "EMA_200" in df_calc.columns
    assert "RSI" in df_calc.columns
    assert "ATR" in df_calc.columns
    
    # Disabled strategy returns no signal
    cfg_disabled = TradingConfig(enabled=False)
    bot_disabled = EngulfingBotV2(cfg_disabled)
    sig, score, mult = bot_disabled.generate_signal(df_calc, len(df_calc) - 2)
    assert sig is None
    assert score == 0


def test_strategy_direction_filter():
    cfg_buy_only = TradingConfig(enabled=True, allowed_directions=["buy"])
    bot_buy_only = EngulfingBotV2(cfg_buy_only)
    
    # Create an artificial bearish engulfing at index 100
    df = generate_synthetic_candles(150)
    df.iloc[99, df.columns.get_loc("open")] = 1.0800
    df.iloc[99, df.columns.get_loc("close")] = 1.0820
    df.iloc[100, df.columns.get_loc("open")] = 1.0830
    df.iloc[100, df.columns.get_loc("close")] = 1.0780
    
    df_calc = bot_buy_only.calculate_indicators(df)
    is_bear = bot_buy_only.is_bearish_engulfing(df_calc, 100)
    if is_bear:
        sig, _, _ = bot_buy_only.generate_signal(df_calc, 100)
        # Sell signals must be blocked because only buy is allowed
        assert sig != Signal.SELL


def test_trade_supervisor_be_trail_and_partial():
    mock_conn = MagicMock()
    mock_conn.get_positions.return_value = []
    
    # Mock single position
    pos = MagicMock()
    pos.ticket = 12345
    pos.magic = 20260202
    pos.symbol = "EURUSD"
    pos.sl = 1.0750
    pos.tp = 1.0950
    pos.volume = 0.10
    pos.profit = 50.0
    pos.time = 1000
    mock_conn.get_position.return_value = pos
    
    rules = MagicMock()
    rules.point = 0.00001
    mock_conn.get_broker_rules.return_value = rules
    
    tick = MagicMock()
    tick.bid = 1.0880
    tick.ask = 1.0882
    mock_conn.get_symbol_price.return_value = tick
    
    rates = [
        {"time": 1000, "open": 1.0800, "high": 1.0820, "low": 1.0790, "close": 1.0810},
        {"time": 2000, "open": 1.0810, "high": 1.0890, "low": 1.0805, "close": 1.0880}, # closed bar reaching 1.5R
        {"time": 3000, "open": 1.0880, "high": 1.0885, "low": 1.0875, "close": 1.0882}, # live forming bar
    ]
    mock_conn.get_rates.return_value = rates
    mock_conn.validate_stops.return_value = (True, "OK")
    mock_conn.set_position_sltp.return_value = MagicMock()
    mock_conn.normalize_volume.return_value = 0.05
    mock_conn.close_position.return_value = MagicMock()
    
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name
        
    try:
        tm = TradeManager(state_path)
        trade = ManagedTrade(
            ticket=12345,
            symbol="EURUSD",
            direction="buy",
            volume=0.10,
            entry_price=1.0800,
            stop_loss=1.0750,  # R = 0.0050
            take_profit=1.0950,
            magic=20260202,
        )
        tm.upsert(trade)
        
        cfg = TradingConfig(allow_partial_close=True, max_slippage_points=10)
        supervisor = TradeSupervisor(
            connection=mock_conn,
            trade_manager=tm,
            magic=20260202,
            config=cfg
        )
        
        supervisor.manage_position(12345)
        
        # Verify SL was updated to trailing floor / swing
        assert mock_conn.set_position_sltp.called
        # Verify partial close was called with correct deviation (not 0)
        assert mock_conn.close_position.called
        call_kwargs = mock_conn.close_position.call_args.kwargs
        assert call_kwargs["deviation"] == 10
        assert call_kwargs["volume"] == 0.05
    finally:
        if os.path.exists(state_path):
            os.remove(state_path)


def test_forward_test_tracker_empty_metrics():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        config_path = f.name
        
    try:
        tracker = ForwardTestTracker(config_file=config_path, use_db=False)
        metrics = tracker.calculate_current_metrics()
        
        assert "win_rate" in metrics
        assert metrics["total_trades"] == 0
        assert metrics["win_rate"] == 0.0
        assert metrics["total_pnl"] == 0.0
        
        # Test monitor reporting does not raise KeyError
        monitor = ForwardTestMonitor(tracker=tracker)
        eval_report = monitor.generate_report("evaluation")
        assert "GO/NO-GO EVALUATION REPORT" in eval_report
        assert "No completed trades" in eval_report
        
        summary_report = monitor.generate_report("summary")
        assert "FORWARD-TEST SUMMARY REPORT" in summary_report
    finally:
        if os.path.exists(config_path):
            os.remove(config_path)


def test_config_hot_reload():
    from mt5_system import MT5BotSystem, SystemConfig
    
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        cfg_path = f.name
        json.dump({
            "risk_management": {"risk_per_trade": 0.01},
            "strategy_parameters": {
                "volume_settings": {"volume_multiplier": 1.2}
            }
        }, f)
        
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f_state:
        state_path = f_state.name

    try:
        mock_conn = MagicMock()
        mock_conn.get_connection_status.return_value = {"connected": True, "trade_allowed": True}
        mock_conn.get_account_summary.return_value = {"balance": 10000.0, "trade_allowed": True}
        
        sys_cfg = SystemConfig(
            symbols=["EURUSD"],
            timeframe="H1",
            poll_seconds=60,
            magic=20260202,
            state_path=state_path,
            avoid_news_minutes=30,
            config_json_path=cfg_path,
        )
        system = MT5BotSystem(mock_conn, sys_cfg)
        assert system.strategy_cfg.risk_per_trade == 0.01
        assert system.strategy_cfg.volume_multiplier == 1.2
        
        # Modify the config file
        import time
        time.sleep(0.05)
        with open(cfg_path, "w") as f:
            json.dump({
                "risk_management": {"risk_per_trade": 0.02},
                "strategy_parameters": {
                    "volume_settings": {"volume_multiplier": 1.5}
                }
            }, f)
            
        system._reload_config_if_modified()
        assert system.strategy_cfg.risk_per_trade == 0.02
        assert system.strategy_cfg.volume_multiplier == 1.5
    finally:
        for p in (cfg_path, state_path):
            if os.path.exists(p):
                os.remove(p)


def test_bot_runtime_control_process_matching():
    import bot_runtime_control
    
    proc = MagicMock()
    proc.info = {"name": "python.exe", "cmdline": ["python", "mt5_bot.py", "--system", "--loop"]}
    assert bot_runtime_control._is_bot_process(proc) is True
    
    # Excluded flags should not match
    proc_status = MagicMock()
    proc_status.info = {"name": "python.exe", "cmdline": ["python", "mt5_bot.py", "--status-check"]}
    assert bot_runtime_control._is_bot_process(proc_status) is False
    
    # Watchdog process matching
    proc_watchdog = MagicMock()
    proc_watchdog.info = {}
    proc_watchdog.cmdline.return_value = ["cmd.exe", "/c", "scripts\\run_bot_forever.bat"]
    assert bot_runtime_control._is_watchdog_process(proc_watchdog) is True


def test_market_regime_innovations():
    from market_regime import compute_kaufman_efficiency_ratio, compute_choppiness_index, RegimeClassifier

    # Pure straight-line trend has KER = 1.0
    trending_series = pd.Series([100.0 + i for i in range(15)])
    ker_trend = compute_kaufman_efficiency_ratio(trending_series, period=10)
    assert ker_trend == 1.0

    # Alternating choppy series has low KER
    choppy_series = pd.Series([100.0 if i % 2 == 0 else 102.0 for i in range(15)])
    ker_chop = compute_kaufman_efficiency_ratio(choppy_series, period=10)
    assert ker_chop < 0.25

    # Test DataFrame for Choppiness Index and Classifier
    df = pd.DataFrame({
        "open": [100.0 + i for i in range(35)],
        "high": [101.0 + i for i in range(35)],
        "low": [99.5 + i for i in range(35)],
        "close": [100.5 + i for i in range(35)],
        "ATR": [1.5] * 35,
        "ADX": [30.0] * 35,
    })
    chop = compute_choppiness_index(df, period=14)
    assert 0.0 <= chop <= 100.0

    classifier = RegimeClassifier()
    metrics = classifier.get_regime_metrics(df)
    assert "regime" in metrics
    assert "ker" in metrics
    assert "chop" in metrics
    assert metrics["is_efficient"] is True


def test_liquidity_sweep_and_efficiency_confluence():
    cfg = TradingConfig()
    bot = EngulfingBotV2(cfg)

    # Construct candle series with a liquidity sweep (pierced prior low then closed high)
    lows = [1.0850] * 15
    highs = [1.0900] * 15
    opens = [1.0870] * 15
    closes = [1.0880] * 15

    # Candle 15 sweeps below 1.0850 (prior low) to 1.0840 and reclaims to 1.0890
    lows.append(1.0840)
    highs.append(1.0895)
    opens.append(1.0860)
    closes.append(1.0890)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000] * 16,
        "RSI": [40.0] * 16,
        "EMA_50": [1.0800] * 16,
        "EMA_200": [1.0700] * 16,
        "EMA_20": [1.0850] * 16,
        "ADX": [25.0] * 16,
        "ATR": [0.0020] * 16,
        "MACD": [0.0005] * 16,
        "MACD_Signal": [0.0002] * 16,
        "Stoch_K": [30.0] * 16,
        "Stoch_D": [25.0] * 16,
        "FVG_Bull": [True] * 16,
        "FVG_Bear": [False] * 16,
    })

    sweep = bot.check_liquidity_sweep(df, 15, Signal.BUY, lookback=10)
    assert sweep is True

    eff = bot.check_market_efficiency(df, 15)
    assert isinstance(eff, bool)

    score, details = bot.confluence_score(df, 15, Signal.BUY)
    assert "liquidity_sweep" in details
    assert "market_efficiency" in details
    assert details["liquidity_sweep"] is True


def test_trade_supervisor_stagnation_stop():
    mock_conn = MagicMock()
    rules = MagicMock()
    rules.point = 0.0001
    mock_conn.get_broker_rules.return_value = rules

    pos = MagicMock()
    pos.symbol = "EURUSD"
    pos.time = 1000  # Position opened at t=1000
    pos.sl = 1.0750
    pos.tp = 1.0950
    pos.profit = 10.0
    mock_conn.get_position.return_value = pos

    tick = MagicMock()
    tick.bid = 1.0815
    tick.ask = 1.0817
    mock_conn.get_symbol_price.return_value = tick

    # 12 closed bars elapsed since pos.time (stagnation reached)
    rates = [{"time": 1000 + (i * 3600), "open": 1.0805, "high": 1.0820, "low": 1.0795, "close": 1.0810} for i in range(13)]
    mock_conn.get_rates.return_value = rates
    mock_conn.validate_stops.return_value = (True, "OK")
    mock_conn.set_position_sltp.return_value = MagicMock()

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name

    try:
        tm = TradeManager(state_path)
        trade = ManagedTrade(
            ticket=55555,
            symbol="EURUSD",
            direction="buy",
            volume=0.10,
            entry_price=1.0800,
            stop_loss=1.0750,
            take_profit=1.0950,
            magic=20260202,
        )
        tm.upsert(trade)

        cfg = TradingConfig(stagnation_bars=10)
        supervisor = TradeSupervisor(connection=mock_conn, trade_manager=tm, magic=20260202, config=cfg)
        supervisor.manage_position(55555)

        # Stagnation stop should have moved SL to entry (1.0800)
        assert mock_conn.set_position_sltp.called
        kwargs = mock_conn.set_position_sltp.call_args.kwargs
        assert kwargs["sl"] == 1.0800
        assert kwargs["comment"] == "STAGNATION@BE"
    finally:
        if os.path.exists(state_path):
            os.remove(state_path)


def test_currency_beta_exposure_guard():
    from mt5_system import MT5BotSystem, SystemConfig

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f_state:
        state_path = f_state.name

    try:
        mock_conn = MagicMock()
        mock_conn.get_connection_status.return_value = {"connected": True, "trade_allowed": True}
        mock_conn.get_account_summary.return_value = {"balance": 10000.0, "trade_allowed": True}

        sys_cfg = SystemConfig(symbols=["EURUSD", "XAUUSD"], timeframe="H1", poll_seconds=60, magic=20260202, state_path=state_path, avoid_news_minutes=30)
        system = MT5BotSystem(mock_conn, sys_cfg)

        # Decompose currency pair
        base, quote = system._currency_breakdown("EURUSD")
        assert base == "EUR"
        assert quote == "USD"

        base_gold, quote_gold = system._currency_breakdown("XAUUSD")
        assert base_gold == "XAU"
        assert quote_gold == "USD"

        # Check net currency risk limit: limit is 2.5%
        system.strategy_cfg.max_net_currency_risk = 0.025
        ok, reason = system._check_net_currency_risk_limit("EURUSD", "buy", candidate_risk_fraction=0.01)
        assert ok is True

        # Candidate exceeding 2.5% should be blocked
        ok_excess, reason_excess = system._check_net_currency_risk_limit("EURUSD", "buy", candidate_risk_fraction=0.03)
        assert ok_excess is False
        assert "exceeds ceiling" in reason_excess
    finally:
        if os.path.exists(state_path):
            os.remove(state_path)


def test_monte_carlo_and_strategy_health():
    from performance_tracker import PerformanceTracker

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f_state:
        state_path = f_state.name

    try:
        tracker = PerformanceTracker(state_path=state_path, db=None)
        
        # Test Monte Carlo forward projection
        mc = tracker.run_monte_carlo(initial_capital=10000.0, num_simulations=500, horizon_trades=30)
        assert mc.num_simulations == 500
        assert mc.horizon_trades == 30
        assert mc.median_ending_equity > 0
        assert 0.0 <= mc.prob_profitable <= 100.0

        # Test Strategy Health Diagnostics
        diag = tracker.get_strategy_health_diagnostics()
        assert "status" in diag
        assert "health_score" in diag
        assert "throttle_risk_factor" in diag
        assert diag["throttle_risk_factor"] == 1.0
    finally:
        if os.path.exists(state_path):
            os.remove(state_path)


