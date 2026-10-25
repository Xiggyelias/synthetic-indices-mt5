"""Quantum Brain v2: configuration, schema, client, storage, data quality, labels, features, validation."""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest

from qb2.config import ConfigError, RiskLimits, Settings, load_settings, with_overrides
from qb2.data_quality import clean_ticks
from qb2.deriv_client import DerivAPIError, DerivClient
from qb2.features import MODEL_FEATURES, compute_features, make_decision_times
from qb2.labels import SettlementRule, settle
from qb2.schema import (
    confirms_rise_fall,
    is_volatility_index,
    normalize_active_symbol,
    normalize_open_contract,
    normalize_proposal,
    parse_duration_seconds,
    proposal_request,
    rise_fall_available,
)
from qb2.stats_utils import benjamini_hochberg, holm, longest_losing_streak, max_drawdown
from qb2.storage import Database
from qb2.ticks import simulate_volatility_ticks
from qb2.validation import (
    AcceptanceGates,
    HoldoutAlreadyUsed,
    HoldoutGuard,
    acceptance,
    chronological_partition,
    walk_forward_folds,
)


# ── Configuration safety ─────────────────────────────────────────────────────

def test_defaults_are_safe():
    s = load_settings(env={})
    assert s.deriv_api_version == "new" and s.ws_url_with_app_id.endswith("/options/ws/public")
    assert s.trading_mode == "paper" and s.demo_only and not s.enable_trading
    assert not s.execution_enabled
    assert s.currency == "USD" and s.contract_seconds == 300
    assert s.risk.max_concurrent_positions == 1


@pytest.mark.parametrize("env", [
    {"TRADING_MODE": "live"},
    {"TRADING_MODE": "real"},
    {"DEMO_ONLY": "false"},
    {"QB2_DURATION_UNIT": "t"},
])
def test_unsafe_configurations_are_refused(env):
    with pytest.raises(ConfigError):
        load_settings(env=env)


def test_overrides_cannot_bypass_validation():
    with pytest.raises(ConfigError):
        with_overrides(Settings(), trading_mode="live")
    with pytest.raises(ConfigError):
        RiskLimits(max_stake_fraction=0.5).validate()


def test_execution_needs_every_flag():
    s = load_settings(env={"ENABLE_TRADING": "true"})
    assert not s.execution_enabled          # still paper
    s = load_settings(env={"ENABLE_TRADING": "true", "TRADING_MODE": "demo"})
    assert s.execution_enabled


def test_token_never_exposed():
    s = load_settings(env={"DERIV_API_TOKEN": "secret-token-123"})
    assert "secret-token-123" not in repr(s)
    assert "secret-token-123" not in json.dumps(s.public_dict())


def test_token_in_config_file_rejected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"deriv_api_token": "x"}))
    with pytest.raises(ConfigError):
        load_settings(env={}, config_path=str(p))


# ── Deriv schema (legacy and new API) ────────────────────────────────────────

def test_active_symbol_legacy_and_new_fields():
    legacy = normalize_active_symbol({"symbol": "R_100", "display_name": "Volatility 100 Index", "market": "synthetic_index",
                                      "submarket": "random_index", "pip": 0.01, "exchange_is_open": 1})
    new = normalize_active_symbol({"underlying_symbol": "R_100", "underlying_symbol_name": "Volatility 100 Index",
                                   "market": "synthetic_index", "submarket": "random_index", "pip_size": "0.01"})
    assert legacy.symbol == new.symbol == "R_100"
    assert legacy.pip_size == new.pip_size == 0.01
    assert is_volatility_index(legacy) and is_volatility_index(new)
    jump = normalize_active_symbol({"symbol": "JD10", "display_name": "Jump 10 Index", "market": "synthetic_index",
                                    "submarket": "jump_index"})
    assert not is_volatility_index(jump)


def test_duration_parsing_distinguishes_ticks():
    assert parse_duration_seconds("5m") == 300
    assert parse_duration_seconds("15s") == 15
    assert parse_duration_seconds("365d") == 365 * 86400
    assert parse_duration_seconds("5t") is None


def test_rise_fall_availability():
    cf = {"available": [
        {"contract_type": "CALL", "contract_category": "callput", "barriers": 0, "min_contract_duration": "15s", "max_contract_duration": "365d"},
        {"contract_type": "PUT", "contract_category": "callput", "barriers": 0, "min_contract_duration": "15s", "max_contract_duration": "365d"},
        {"contract_type": "CALL", "contract_category": "callput", "barriers": 1, "min_contract_duration": "1m", "max_contract_duration": "1d"},
    ]}
    assert rise_fall_available(cf, 300)[0]
    tick_only = {"available": [dict(c, min_contract_duration="1t", max_contract_duration="10t") for c in cf["available"][:2]]}
    assert not rise_fall_available(tick_only, 300)[0]


def test_new_api_contracts_for_shape_checked_live():
    """contracts_for from the new public endpoint (2026-10-24): CALL/PUT only with default barriers."""
    entry = {"barrier": "+0.40", "barriers": 1, "contract_category": "callput", "expiry_type": "intraday",
             "max_contract_duration": "1d", "min_contract_duration": "15s"}
    cf = {"available": [dict(entry, contract_type="CALL"), dict(entry, contract_type="PUT")]}
    assert rise_fall_available(cf, 300, api_version="new")[0]
    assert not rise_fall_available(cf, 300, api_version="legacy")[0]


def test_longcode_confirms_rise_fall():
    q = normalize_proposal({"proposal": {"id": "x", "ask_price": 1, "payout": 1.96, "longcode":
                            "Win payout if Volatility 100 Index is strictly higher than entry spot at 5 minutes after contract start time."}},
                           "R_100", "CALL", 300, "USD")
    assert confirms_rise_fall(q)
    q.contract_type = "PUT"
    assert not confirms_rise_fall(q)


def test_proposal_parsing_accepts_strings():
    q = normalize_proposal({"proposal": {"id": "abc", "ask_price": "10.00", "payout": "19.54", "spot": "1234.5"}},
                           "R_100", "CALL", 300, "USD", received_at=1.0)
    assert q.stake == 10.0 and q.payout == 19.54 and q.breakeven_probability == pytest.approx(10 / 19.54)
    with pytest.raises(ValueError):
        normalize_proposal({"proposal": {"ask_price": 1}}, "R_100", "CALL", 300, "USD")


def test_proposal_request_symbol_field_per_api_version():
    assert "symbol" in proposal_request("R_100", "CALL", 1, 5, "m", "USD", "legacy")
    req = proposal_request("R_100", "PUT", 1.005, 5, "m", "USD", "new")
    assert req["underlying_symbol"] == "R_100" and "symbol" not in req and req["amount"] == 1.0
    with pytest.raises(ValueError):
        proposal_request("R_100", "CALLE", 1, 5, "m", "USD")


def test_open_contract_exit_spot_fallback():
    c = normalize_open_contract({"proposal_open_contract": {"contract_id": 1, "is_sold": "1", "profit": "-1.00",
                                                            "sell_spot": "100.1", "entry_spot": 100.0}})
    assert c.is_sold and c.profit == -1.0 and c.exit_spot == 100.1


# ── WebSocket client against a fake server ───────────────────────────────────

class FakeWS:
    def __init__(self, handler):
        self.handler = handler
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        for reply in self.handler(msg):
            await self.inbox.put(json.dumps(reply))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


def _handler(msg):
    rid = msg["req_id"]
    if "active_symbols" in msg:
        yield {"req_id": rid, "msg_type": "active_symbols", "active_symbols": [{"symbol": "R_100"}]}
    elif "proposal" in msg:
        yield {"req_id": rid, "msg_type": "proposal", "error": {"code": "InvalidSymbol", "message": "bad"}}
    elif "ticks" in msg:
        for i in range(3):
            yield {"req_id": rid, "msg_type": "tick", "tick": {"symbol": msg["ticks"], "epoch": 100 + i, "quote": 1.0 + i}}


def test_client_matches_requests_errors_and_streams():
    async def run():
        ws = FakeWS(_handler)

        async def connect(url):
            return ws
        client = DerivClient("wss://fake", connect=connect, ping_interval=0)
        await client.connect()
        resp = await client.active_symbols()
        assert resp["active_symbols"][0]["symbol"] == "R_100"
        with pytest.raises(DerivAPIError) as err:
            await client.proposal({"proposal": 1})
        assert err.value.code == "InvalidSymbol"
        got = []
        async for msg in client.subscribe({"ticks": "R_100"}):
            got.append(msg["tick"]["epoch"])
            if len(got) == 3:
                break
        assert got == [100, 101, 102]
        assert ws.sent[-1]["subscribe"] == 1
        await client.close()
    asyncio.run(run())


def test_client_reports_disconnect():
    async def run():
        ws = FakeWS(lambda m: [])
        events = []

        async def connect(url):
            return ws
        client = DerivClient("wss://fake", connect=connect, ping_interval=0, on_disconnect=events.append)
        await client.connect()
        await ws.inbox.put(ConnectionResetError("dropped"))
        await asyncio.sleep(0.05)
        assert events and not client.connected
        await client.close()
    asyncio.run(run())


# ── Storage ──────────────────────────────────────────────────────────────────

def test_migrations_idempotent_and_ticks_deduplicated(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    assert db.migrate() == ["0001_initial.sql"]
    assert db.migrate() == []
    run = db.start_run("test", "unit")
    assert db.insert_ticks("R_100", [1, 2, 3], [1.0, 1.1, 1.2], run) == 3
    assert db.insert_ticks("R_100", [3, 4], [9.9, 1.3], run) == 1   # epoch 3 kept as first received
    df = db.load_ticks("R_100")
    assert df["epoch"].tolist() == [1, 2, 3, 4] and df["quote"].iloc[2] == 1.2
    with pytest.raises(ValueError):
        db.insert_trade({"mode": "live"})


def test_state_roundtrip_handles_nan(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    db.set_state("k", {"a": float("nan"), "b": [1, 2]})
    assert db.get_state("k") == {"a": None, "b": [1, 2]}


# ── Data quality ─────────────────────────────────────────────────────────────

def test_quality_checks_find_problems():
    e = np.arange(0, 4000, 2)
    q = 100 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 1e-4, len(e))))
    df = pd.DataFrame({"epoch": e, "quote": q})
    df.loc[500, "quote"] *= 1.05                                  # isolated bad print
    df = pd.concat([df, df.iloc[[10]].assign(quote=1.0)])          # conflicting duplicate
    df = df[(df.epoch < 2000) | (df.epoch > 2600)]                 # 10-minute gap
    df.loc[len(df) + 5] = [3000, -1.0]                             # invalid price
    clean, rep = clean_ticks(df.sample(frac=1, random_state=1), "R_100", min_ticks=100)
    assert rep.non_positive == 1 and rep.duplicate_epochs == 1 and rep.conflicting_duplicates == 1
    assert rep.out_of_order > 0 and rep.gaps == 1 and rep.bad_prints_removed == 1
    assert clean["epoch"].is_monotonic_increasing and not rep.passed


# ── Contract labels ──────────────────────────────────────────────────────────

def test_settlement_uses_next_tick_and_last_tick_before_expiry():
    e = np.array([0, 2, 4, 300, 302, 304, 306])
    q = np.array([1.0, 2.0, 3.0, 2.5, 9.0, 9.0, 9.0])
    lab = settle(e, q, np.array([2.0]), SettlementRule(300))
    # start 2.0 -> entry is the tick at 4 (strictly after start); expiry 302 -> exit tick at 302
    assert lab["entry_quote"][0] == 3.0 and lab["exit_quote"][0] == 9.0 and lab["outcome"][0] == 1
    lab = settle(e, q, np.array([1.0]), SettlementRule(300))     # entry tick 2 (2.0), exit tick 300 (2.5)
    assert lab["outcome"][0] == 1 and lab["exit_epoch"][0] == 300


def test_settlement_ties_and_voids():
    e = np.arange(0, 1000, 2)
    q = np.ones(len(e))
    assert settle(e, q, np.array([10.0]))["outcome"][0] == 0           # tie: both sides lose
    gap = e[(e < 200) | (e > 400)]
    assert not settle(gap, np.ones(len(gap)), np.array([10.0]))["valid"][0]   # gap spans expiry
    assert not settle(e, q, np.array([900.0]))["valid"][0]               # data ends before expiry


def test_five_minutes_is_time_not_ticks():
    for sym, ticks_per_contract in (("R_100", 150), ("1HZ100V", 300)):
        df = simulate_volatility_ticks(sym, seconds=3600, seed=0)
        e = df["epoch"].values
        lab = settle(e, df["quote"].values, np.array([e[100] + 0.5]), SettlementRule(300))
        span = np.searchsorted(e, lab["exit_epoch"][0]) - np.searchsorted(e, lab["entry_epoch"][0])
        assert abs(span - ticks_per_contract) <= 1


# ── Features ─────────────────────────────────────────────────────────────────

def test_features_are_causal():
    df = simulate_volatility_ticks("R_100", seconds=6 * 3600, seed=2)
    e, q = df["epoch"].values, df["quote"].values
    t = make_decision_times(e, 60, 330)
    base = compute_features(e, q, t)
    cut = len(t) // 2
    q2 = q.copy()
    q2[e > t[cut]] *= np.exp(np.random.default_rng(9).normal(0, 0.01, (e > t[cut]).sum()))   # rewrite the future
    after = compute_features(e, q2, t)
    pd.testing.assert_frame_equal(base.iloc[: cut + 1], after.iloc[: cut + 1])
    assert not base.iloc[cut + 1:].equals(after.iloc[cut + 1:])   # the rewrite did reach later rows


def test_features_are_scale_free_where_models_use_them():
    df = simulate_volatility_ticks("R_100", seconds=4 * 3600, seed=3)
    e, q = df["epoch"].values, df["quote"].values
    t = make_decision_times(e, 60, 330)
    a = compute_features(e, q, t)[list(MODEL_FEATURES)]
    b = compute_features(e, q * 37.0, t)[list(MODEL_FEATURES)]
    pd.testing.assert_frame_equal(a, b, atol=1e-6, check_exact=False)
    assert a.notna().all().all()


# ── Statistics & validation ──────────────────────────────────────────────────

def test_multiple_testing_corrections():
    p = [0.001, 0.01, 0.04, 0.2]
    assert np.allclose(holm(p), [0.004, 0.03, 0.08, 0.2])
    q = benjamini_hochberg(p)
    assert q[0] == pytest.approx(0.004) and np.all(q >= np.array(p))


def test_trade_sequence_metrics():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert longest_losing_streak([1, -1, -1, 2, -1, -1, -1]) == 3


def test_walk_forward_purges_overlapping_labels():
    t = np.arange(0, 60 * 1000, 60, dtype=float)
    end = t + 301
    for fold in walk_forward_folds(t, end, n_folds=4):
        assert end[fold.train].max() < t[fold.test].min()
        assert t[fold.train].max() < t[fold.test].min()
    part = chronological_partition(t, end, 0.8, embargo_seconds=300)
    assert end[part["development"]].max() < t[part["test"]].min()
    assert t[part["test"]].min() >= t[int(len(t) * 0.8)] + 300


def test_holdout_can_only_be_used_once(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    guard = HoldoutGuard(db)
    assert guard.evaluate("hash1", None, lambda: 42) == 42
    with pytest.raises(HoldoutAlreadyUsed):
        guard.evaluate("hash1", None, lambda: 43)
    assert guard.evaluate("hash1", None, lambda: 44, allow_reuse=True) == 44
    assert "REUSED" in db.holdout_uses("hash1")[-1]["purpose"]


def test_acceptance_rejects_assumed_payout_and_weak_evidence():
    good = {"payout_source": "historical_proposal",
            "probability": {"n": 5000, "ece": 0.01},
            "brier_vs_base": {"ci_low": 0.001},
            "trading": {"trades": 500, "return_ci_low": 0.01},
            "stability": {"positive_fraction": 0.8}}
    assert acceptance(good, AcceptanceGates())[0]
    for change in ({"payout_source": "assumed"}, {"brier_vs_base": {"ci_low": -0.001}},
                   {"trading": {"trades": 500, "return_ci_low": -0.01}}, {"stability": {"positive_fraction": 0.4}},
                   {"probability": {"n": 5000, "ece": 0.08}}):
        ok, failures = acceptance(dict(good, **change), AcceptanceGates())
        assert not ok and failures
