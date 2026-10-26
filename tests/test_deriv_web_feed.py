"""
Unit tests for Deriv Web Feed & WebSocket Client
"""

from __future__ import annotations

import pandas as pd
import pytest

from deriv_web_feed import (
    DERIV_SYMBOL_MAP,
    DerivWebAccountInfo,
    DerivWebConnection,
    normalize_deriv_symbol,
)


def test_symbol_normalization():
    assert normalize_deriv_symbol("Volatility 75 Index") == "R_75"
    assert normalize_deriv_symbol("volatility 75 index") == "R_75"
    assert normalize_deriv_symbol("Volatility 75 (1s) Index") == "1HZ75V"
    assert normalize_deriv_symbol("volatility 75 (1S) index") == "1HZ75V"
    assert normalize_deriv_symbol("R_75") == "R_75"
    assert normalize_deriv_symbol("1HZ75V") == "1HZ75V"
    assert normalize_deriv_symbol("Crash 1000 Index") == "CRASH1000"
    assert normalize_deriv_symbol("Boom 1000 Index") == "BOOM1000"
    assert normalize_deriv_symbol("Step Index") == "stpRNG"


def test_deriv_web_connection_interface():
    conn = DerivWebConnection()
    assert conn.ensure_connected() is True
    assert conn.ensure_symbol("Volatility 75 Index") == "R_75"

    acc = conn.get_account_info()
    assert isinstance(acc, DerivWebAccountInfo)
    assert acc.equity >= 0.0
    assert "WebSocket" in acc.server


def test_synthetic_risk_manager_argument_compatibility():
    from synthetic_risk_manager import SyntheticRiskManager

    # Accepts both max_open_positions and max_open_trades without error
    mgr1 = SyntheticRiskManager(max_open_positions=5)
    assert mgr1.max_open_positions == 5

    mgr2 = SyntheticRiskManager(max_open_trades=8)
    assert mgr2.max_open_positions == 8
