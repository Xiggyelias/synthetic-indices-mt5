"""
Unit tests for TradeStorageManager (MongoDB + Local JSON fallback)
"""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from trade_storage import TradeStorageManager


@pytest.fixture
def temp_state_file(tmp_path):
    return tmp_path / "test_synthetic_state.json"


def test_trade_storage_initialization(temp_state_file):
    mgr = TradeStorageManager(state_file=temp_state_file)
    summary = mgr.get_status_summary()
    assert "is_connected" in summary
    assert "engine" in summary
    state = mgr.get_all_state()
    assert "open_trades" in state
    assert "closed_trades" in state
    assert state["equity"] == 10000.0


def test_save_and_close_trade_lifecycle(temp_state_file):
    mgr = TradeStorageManager(state_file=temp_state_file)

    # 1. Save open trade
    trade_data = {
        "symbol": "Volatility 100 Index",
        "direction": "BUY",
        "volume": 0.50,
        "entry_price": 2000.0,
        "stop_loss": 1980.0,
        "take_profit": 2040.0,
        "strategy_type": "KALMAN_FAIR_VALUE_REVERSION",
        "math_edge_score": 75.0,
        "win_probability": 0.65,
    }
    mgr.save_open_trade("TEST-100", trade_data)

    state = mgr.get_all_state()
    assert "TEST-100" in state["open_trades"]
    assert state["open_trades"]["TEST-100"]["symbol"] == "Volatility 100 Index"

    # 2. Close trade with profit
    mgr.close_trade(
        ticket="TEST-100",
        exit_price=2040.0,
        pnl_usd=20.0,
        pnl_r=2.0,
        close_reason="TAKE_PROFIT",
    )

    state2 = mgr.get_all_state()
    assert "TEST-100" not in state2["open_trades"]
    assert len(state2["closed_trades"]) == 1
    closed = state2["closed_trades"][0]
    assert closed["ticket"] == "TEST-100"
    assert closed["outcome"] == "WIN"
    assert closed["pnl_usd"] == 20.0
    assert closed["pnl_r"] == 2.0
    assert state2["equity"] == 10020.0


def test_close_trade_loss(temp_state_file):
    mgr = TradeStorageManager(state_file=temp_state_file)

    trade_data = {
        "symbol": "Volatility 25 Index",
        "direction": "SELL",
        "volume": 1.0,
        "entry_price": 1500.0,
        "stop_loss": 1520.0,
        "take_profit": 1460.0,
    }
    mgr.save_open_trade("TEST-25", trade_data)

    mgr.close_trade(
        ticket="TEST-25",
        exit_price=1520.0,
        pnl_usd=-20.0,
        pnl_r=-1.0,
        close_reason="STOP_LOSS",
    )

    state = mgr.get_all_state()
    assert len(state["closed_trades"]) == 1
    assert state["closed_trades"][0]["outcome"] == "LOSS"
    assert state["equity"] == 9980.0


def test_reset_history(temp_state_file):
    mgr = TradeStorageManager(state_file=temp_state_file)
    mgr.save_open_trade("OPEN-1", {"symbol": "Step Index"})
    mgr.close_trade("OPEN-1", exit_price=100.0, pnl_usd=50.0, pnl_r=1.0, close_reason="MANUAL")

    assert len(mgr.get_all_state()["closed_trades"]) == 1
    mgr.reset_history(10000.0, 10000.0)

    state = mgr.get_all_state()
    assert len(state["closed_trades"]) == 0
    assert state["equity"] == 10000.0
