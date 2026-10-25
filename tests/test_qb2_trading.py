"""Quantum Brain v2: decision engine, risk governor, backtester, registry, drift, trader, demo executor."""

import asyncio

import numpy as np
import pytest

from qb2.backtest import BacktestConfig, run_contract_backtest
from qb2.config import RiskLimits, load_settings
from qb2.decision import HALTED, NO_TRADE, TRADE, DecisionEngine, DecisionInput
from qb2.demo_executor import (
    DemoExecutionRefused,
    DemoExecutor,
    account_problems,
    configuration_problems,
    quote_problems,
)
from qb2.drift import DriftMonitor, psi
from qb2.features import MODEL_FEATURES
from qb2.models import BaseRateModel, TrainedModel
from qb2.registry import ModelRegistry, RegistryError
from qb2.risk_governor import OrderRequest, RiskGovernor
from qb2.schema import AccountInfo, ContractQuote
from qb2.storage import Database
from qb2.ticks import simulate_volatility_ticks
from qb2.trader import Trader

NOW = 1_700_000_000.0
SETTINGS = load_settings(env={})


LONGCODE = "Win payout if Volatility 100 Index is strictly {} than entry spot at 5 minutes after contract start time."


def quotes(stake=1.0, ratio=1.95, t=NOW, source="deriv_proposal"):
    return {c: ContractQuote("R_100", c, stake, round(stake * ratio, 2), 300, "USD", t, proposal_id=f"id-{c}", source=source,
                             longcode=LONGCODE.format("higher" if c == "CALL" else "lower"))
            for c in ("CALL", "PUT")}


def inp(**kw):
    base = dict(symbol="R_100", now=NOW, last_tick_epoch=NOW - 1, p_rise=0.5, p_std=0.0, model_version="v1",
                model_approved=True, quotes=quotes())
    base.update(kw)
    return DecisionInput(**base)


# ── Decision engine ──────────────────────────────────────────────────────────

def test_fair_probability_is_no_trade():
    d = DecisionEngine(SETTINGS).decide(inp(p_rise=0.5))
    assert d.action == NO_TRADE and d.ev_call < 0 and d.ev_put < 0
    assert d.breakeven_call == pytest.approx(1 / 1.95)


def test_positive_conservative_ev_trades_the_better_side():
    d = DecisionEngine(SETTINGS).decide(inp(p_rise=0.40, p_std=0.01))
    assert d.action == TRADE and d.contract_type == "PUT"
    assert d.ev_put_conservative > SETTINGS.min_edge_fraction * d.stake


def test_uncertainty_can_veto_a_trade():
    assert DecisionEngine(SETTINGS).decide(inp(p_rise=0.56, p_std=0.0)).action == TRADE
    d = DecisionEngine(SETTINGS).decide(inp(p_rise=0.56, p_std=0.05))
    assert d.action == NO_TRADE and "conservative EV" in d.reasons[0]


def test_ties_reduce_put_probability():
    d = DecisionEngine(SETTINGS).decide(inp(p_rise=0.30, p_tie=0.2))
    assert d.p_fall == pytest.approx(0.5)


@pytest.mark.parametrize("kw,expected", [
    (dict(halted=True, halt_reason="drawdown"), HALTED),
    (dict(last_tick_epoch=NOW - 60), NO_TRADE),
    (dict(last_tick_epoch=None), NO_TRADE),
    (dict(p_rise=None), NO_TRADE),
    (dict(quotes={}), NO_TRADE),
    (dict(quotes=quotes(t=NOW - 30)), NO_TRADE),          # expired proposal
    (dict(model_approved=False), NO_TRADE),
])
def test_guards_produce_no_trade_or_halt(kw, expected):
    d = DecisionEngine(SETTINGS).decide(inp(**{"p_rise": 0.40, **kw}))
    assert d.action == expected and d.reasons


def test_mismatched_quote_rejected():
    q = quotes()
    q["CALL"].duration_seconds = 60
    assert DecisionEngine(SETTINGS).decide(inp(p_rise=0.4, quotes=q)).action == NO_TRADE


# ── Risk governor ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def order(key, stake=1.0, t=NOW):
    return OrderRequest("R_100", "CALL", stake, round(stake * 1.95, 2), key, t, t, t - 1)


def test_stake_is_fraction_of_equity_and_never_grows_after_losses():
    clock = Clock()
    g = RiskGovernor(RiskLimits(consecutive_loss_limit=100, max_daily_loss_fraction=0.5, max_drawdown_fraction=0.5,
                                max_trades_per_day=1000), 1000.0, clock=clock)
    stakes = []
    for i in range(10):
        s = g.stake_for()
        stakes.append(s)
        assert g.approve(order(f"k{i}", s, clock.t), now=clock.t).approved
        g.on_settled(f"k{i}", -s, now=clock.t)
        clock.t += 400
    assert stakes[0] == 10.0 and all(b <= a for a, b in zip(stakes, stakes[1:]))   # no martingale


def test_governor_limits():
    clock = Clock()
    g = RiskGovernor(RiskLimits(max_trades_per_day=2, consecutive_loss_limit=2, cooldown_minutes=30), 1000.0, clock=clock)
    assert g.approve(order("a"), now=NOW).approved
    assert not g.approve(order("b"), now=NOW).approved                     # one concurrent position
    assert not g.approve(order("a"), now=NOW + 1).approved                 # duplicate key
    g.on_settled("a", -1.0, now=NOW + 300)
    assert g.approve(order("c", t=NOW + 301), now=NOW + 301).approved
    g.on_settled("c", -1.0, now=NOW + 600)                                 # 2nd loss -> cooldown
    v = g.approve(order("d", t=NOW + 601), now=NOW + 601)
    assert not v.approved and any("cooldown" in r for r in v.reasons)
    assert any("trades today" in r for r in v.reasons)
    assert not g.approve(order("e", stake=50.0, t=NOW + 9999), now=NOW + 9999).approved   # above allowed stake
    stale = order("f", t=NOW + 9999)
    stale.last_tick_epoch = NOW
    assert any("stale tick" in r for r in g.approve(stale, now=NOW + 9999).reasons)


def test_daily_loss_and_drawdown_halt():
    g = RiskGovernor(RiskLimits(max_daily_loss_fraction=0.02, max_drawdown_fraction=0.05, consecutive_loss_limit=99,
                                max_stake_fraction=0.05, max_stake=1000), 1000.0, clock=Clock())
    assert g.approve(order("a", 20.0), now=NOW).approved
    g.on_settled("a", -20.0, now=NOW + 300)
    v = g.approve(order("b", 10.0, NOW + 301), now=NOW + 301)
    assert not v.approved and any("daily loss" in r for r in v.reasons)
    g.on_settled("x", -40.0, now=NOW + 302)
    assert g.state.halted and "drawdown" in g.state.halt_reason


def test_halt_is_latched_and_needs_operator(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    g = RiskGovernor(RiskLimits(max_disconnects_per_hour=2), 1000.0, store=db, clock=Clock())
    for _ in range(3):
        g.record_disconnect(now=NOW)
    assert g.state.halted
    g2 = RiskGovernor(RiskLimits(), 1000.0, store=db, clock=Clock())        # restart keeps the halt
    assert g2.state.halted and not g2.approve(order("a"), now=NOW).approved
    with pytest.raises(ValueError):
        g2.reset_halt("  ", "x")
    g2.reset_halt("operator", "investigated")
    assert g2.approve(order("a"), now=NOW).approved


def test_reconciliation_and_abnormal_responses_halt():
    g = RiskGovernor(RiskLimits(max_abnormal_responses=2), 1000.0, clock=Clock())
    assert g.reconcile(1000.0)
    assert not g.reconcile(990.0) and g.state.halted
    g = RiskGovernor(RiskLimits(max_abnormal_responses=2), 1000.0, clock=Clock())
    g.record_abnormal_response("timeout")
    g.record_abnormal_response("bad json")
    assert g.state.halted


def test_model_cannot_override_governor():
    """A TRADE decision from an overconfident model is still refused by a halted governor."""
    g = RiskGovernor(RiskLimits(), 1000.0, clock=Clock())
    g.halt("manual")
    d = DecisionEngine(SETTINGS).decide(inp(p_rise=0.01, p_std=0.0, halted=g.state.halted, halt_reason=g.state.halt_reason))
    assert d.action == HALTED
    assert not g.approve(order("a", g.stake_for()), now=NOW).approved


# ── Backtester ───────────────────────────────────────────────────────────────

def _bt_data(hours=24, seed=0):
    df = simulate_volatility_ticks("R_100", seconds=hours * 3600, seed=seed)
    e, q = df["epoch"].values, df["quote"].values
    times = np.arange(e[0] + 1800, e[-1] - 400, 60, dtype=float)
    return e, q, times


def test_backtest_without_payout_data_simulates_nothing():
    e, q, t = _bt_data()
    res = run_contract_backtest("R_100", e, q, t, np.full(len(t), 0.6), np.zeros(len(t)), 0.0, SETTINGS)
    assert res.status == "NO_PAYOUT_DATA" and res.metrics["trades"] == 0


def test_assumed_payout_is_labelled_incomplete_and_fair_coin_loses():
    e, q, t = _bt_data(hours=48)
    rng = np.random.default_rng(1)
    p = np.where(rng.random(len(t)) < 0.5, 0.6, 0.4)          # confident but uninformed model
    res = run_contract_backtest("R_100", e, q, t, p, np.zeros(len(t)), 0.0, SETTINGS,
                                BacktestConfig(payout_ratio=1.95, quote_reject_probability=0.0))
    assert res.status == "INCOMPLETE_ASSUMED_PAYOUT"
    assert 30 <= res.metrics["trades"] <= 2 * SETTINGS.risk.max_trades_per_day   # daily cap applies
    assert res.metrics["expectancy_per_stake"] < 0.05        # no edge on a fair process
    assert res.decision_counts["decisions"] == len(t)
    assert any("open position" in k for k in res.decision_counts)
    assert any("trades today" in k for k in res.decision_counts)
    starts = res.trades["start"].values
    assert np.all(np.diff(starts) >= 300)                     # one position at a time
    cents = res.trades["stake"].values * 100
    assert np.allclose(cents, np.round(cents), atol=1e-6)    # cent precision


def test_backtest_uses_historical_quotes_when_present():
    import pandas as pd
    e, q, t = _bt_data()
    hist = pd.DataFrame([{"contract_type": c, "duration_seconds": 300, "quote_time": float(tt), "stake": 10.0, "payout": 19.5}
                         for tt in t for c in ("CALL", "PUT")])
    res = run_contract_backtest("R_100", e, q, t, np.full(len(t), 0.4), np.zeros(len(t)), 0.0, SETTINGS,
                                BacktestConfig(quote_reject_probability=0.0), historical_quotes=hist)
    assert res.status == "COMPLETE" and res.payout_source == "historical_proposal" and res.metrics["trades"] > 0


# ── Registry ─────────────────────────────────────────────────────────────────

def _trained(symbol="R_100"):
    return TrainedModel(model=BaseRateModel().fit(np.zeros((10, 1)), np.array([0, 1] * 5)), model_type="base_rate",
                        symbol=symbol, feature_names=MODEL_FEATURES, feature_version="v", p_tie=0.0,
                        duration_seconds=300, decision_interval=60, dataset_hash="abcdef123456", train_range=(0, 1))


GOOD = {"payout_source": "historical_proposal", "probability": {"n": 5000, "ece": 0.01}, "brier_vs_base": {"ci_low": 0.001},
        "trading": {"trades": 500, "return_ci_low": 0.01}, "stability": {"positive_fraction": 0.8}}


def test_registry_never_auto_promotes(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    reg = ModelRegistry(db, str(tmp_path / "models"))
    v_bad, s_bad = reg.register_candidate(_trained(), dict(GOOD, payout_source="assumed"))
    assert s_bad == "rejected" and reg.champion("R_100") is None
    with pytest.raises(RegistryError):
        reg.promote(v_bad, "me")
    v1, s1 = reg.register_candidate(_trained(), GOOD)
    assert s1 == "validated" and reg.champion("R_100") is None      # validated is not champion
    with pytest.raises(RegistryError):
        reg.promote(v1, "")
    reg.promote(v1, "Reviewer")
    assert reg.champion("R_100")["version"] == v1


def test_registry_rollback_and_tamper_detection(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    reg = ModelRegistry(db, str(tmp_path / "models"))
    v1, _ = reg.register_candidate(_trained(), GOOD)
    reg.promote(v1, "Reviewer")
    v2, _ = reg.register_candidate(_trained(), GOOD)
    assert v1 != v2
    reg.promote(v2, "Reviewer")
    assert reg.rollback("R_100", "Reviewer") == v1
    assert reg.champion("R_100")["version"] == v1
    with open(db.model(v1)["artifact_path"], "ab") as f:
        f.write(b"tampered")
    with pytest.raises(RegistryError):
        reg.load(v1)


# ── Drift ────────────────────────────────────────────────────────────────────

def test_drift_monitor_levels():
    rng = np.random.default_rng(0)
    ref = {"x": rng.normal(0, 1, 2000).tolist()}
    assert psi(np.array(ref["x"]), rng.normal(0, 1, 2000)) < 0.05
    mon = DriftMonitor()
    assert mon.evaluate(ref, {"x": rng.normal(0, 1, 500)}).status == "OK"
    assert mon.evaluate(ref, {"x": rng.normal(0.4, 1, 500)}).status == "WARN"
    assert mon.evaluate(ref, {"x": rng.normal(2.0, 1, 500)}).status == "HALT"
    y = (rng.random(500) < 0.3).astype(float)
    rep = mon.evaluate(ref, {"x": rng.normal(0, 1, 500)}, reference_brier=0.25, recent_outcomes=y,
                       recent_outcome_predictions=np.full(500, 0.6))
    assert rep.status == "HALT" and rep.calibration_p_value < 0.01


# ── Trader (paper) ───────────────────────────────────────────────────────────

class AlwaysUp:
    def predict(self, X):
        return np.full(len(X), 0.70), np.full(len(X), 0.01)


def _paper_setup(tmp_path, champion=True):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    df = simulate_volatility_ticks("R_100", seconds=3 * 3600, seed=4, start_epoch=int(NOW) - 3 * 3600 + 1)
    db.insert_ticks("R_100", df["epoch"].values, df["quote"].values)
    reg = ModelRegistry(db, str(tmp_path / "models"))
    if champion:
        tm = _trained()
        tm.model = AlwaysUp()
        v, _ = reg.register_candidate(tm, GOOD)
        reg.promote(v, "Reviewer")
    gov = RiskGovernor(SETTINGS.risk, 1000.0, store=db, clock=lambda: NOW)
    return db, reg, gov


def test_paper_trader_without_champion_never_trades(tmp_path):
    db, reg, gov = _paper_setup(tmp_path, champion=False)

    async def quote_source(sym, stake):
        return quotes(stake, t=NOW)
    d = asyncio.run(Trader(SETTINGS, db, reg, gov, quote_source=quote_source).step("R_100", NOW))
    assert d.action == NO_TRADE and db.trades() == [] and len(db.decisions()) == 1


def test_paper_trader_trades_settles_and_records(tmp_path):
    db, reg, gov = _paper_setup(tmp_path)
    last = float(db.load_ticks("R_100")["epoch"].iloc[-1])

    async def quote_source(sym, stake):
        return quotes(stake, t=last)
    trader = Trader(SETTINGS, db, reg, gov, quote_source=quote_source)
    d = asyncio.run(trader.step("R_100", last))
    assert d.action == TRADE and d.contract_type == "CALL"
    open_trades = db.trades(mode="paper", status="open")
    assert len(open_trades) == 1 and open_trades[0]["stake"] == gov.stake_for(1000.0)
    # a second step in the same interval cannot open a duplicate
    asyncio.run(trader.step("R_100", last + 1))
    assert len(db.trades(mode="paper")) == 1
    # add ticks past expiry, then settle
    more = simulate_volatility_ticks("R_100", seconds=900, seed=5, start_epoch=int(last) + 2,
                                     start_price=float(db.load_ticks("R_100")["quote"].iloc[-1]))
    db.insert_ticks("R_100", more["epoch"].values, more["quote"].values)
    assert trader.settle_paper(last + 700) == 1
    t = db.trades(mode="paper")[0]
    assert t["status"] in ("won", "lost") and gov.state.open_positions == {}


def test_trader_refuses_executor_unless_demo_enabled(tmp_path):
    db, reg, gov = _paper_setup(tmp_path, champion=False)
    with pytest.raises(RuntimeError):
        Trader(SETTINGS, db, reg, gov, executor=object())


# ── Demo executor ────────────────────────────────────────────────────────────

DEMO = load_settings(env={"ENABLE_TRADING": "true", "TRADING_MODE": "demo", "DERIV_API_TOKEN": "tok",
                          "DERIV_API_VERSION": "legacy", "DERIV_APP_ID": "1"})
DEMO_NEW = load_settings(env={"ENABLE_TRADING": "true", "TRADING_MODE": "demo", "DERIV_API_TOKEN": "tok",
                              "DERIV_ACCOUNT_ID": "DOT123"})


def test_demo_executor_refuses_default_configuration():
    assert len(configuration_problems(SETTINGS)) >= 2
    with pytest.raises(DemoExecutionRefused):
        DemoExecutor(client=None, settings=SETTINGS, governor=None, db=None)


def test_account_checks():
    assert account_problems(AccountInfo("VRTC123", True, "USD", 10000.0, ["read", "trade"]), DEMO) == []
    problems = account_problems(AccountInfo("CR123", False, "USD", 10.0, ["read"]), DEMO)
    assert any("NOT a virtual" in p for p in problems) and any("trade" in p for p in problems)


class FakeDemoClient:
    def __init__(self, virtual=True, url="wss://fake"):
        self.virtual = virtual
        self.url = url
        self.bought = []

    async def authorize(self):
        return {"authorize": {"loginid": "VRTC1" if self.virtual else "CR1", "is_virtual": int(self.virtual),
                              "currency": "USD", "balance": 10000, "scopes": ["read", "trade"]}}

    async def contracts_for(self, sym):
        return {"available": [{"contract_type": c, "contract_category": "callput", "barriers": 0,
                               "min_contract_duration": "15s", "max_contract_duration": "1d"} for c in ("CALL", "PUT")]}

    async def buy(self, proposal_id, max_price):
        self.bought.append((proposal_id, max_price))
        return {"buy": {"contract_id": 99, "buy_price": max_price, "start_time": NOW}}


def test_demo_preflight_halts_on_real_account(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    gov = RiskGovernor(DEMO.risk, 10000.0, clock=lambda: NOW)
    ex = DemoExecutor(FakeDemoClient(virtual=False), DEMO, gov, db)
    with pytest.raises(DemoExecutionRefused):
        asyncio.run(ex.preflight(["R_100"]))
    assert gov.state.halted


def test_demo_buy_validates_quote_and_caps_price(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    gov = RiskGovernor(DEMO.risk, 10000.0, clock=lambda: NOW)
    client = FakeDemoClient()
    ex = DemoExecutor(client, DEMO, gov, db, clock=lambda: NOW)
    asyncio.run(ex.preflight(["R_100"]))
    q = quotes(stake=10.0, t=NOW)["CALL"]
    o = OrderRequest("R_100", "CALL", 10.0, q.payout, "demo:k1", NOW, NOW, NOW - 1)
    assert gov.approve(o, now=NOW).approved
    stale = ContractQuote(**{**q.as_dict(), "quote_time": NOW - 60})
    assert quote_problems(stale, o, DEMO, NOW)
    assert asyncio.run(ex.execute(o, stale, None, None)) is None and client.bought == []
    assert "demo:k1" not in gov.state.open_positions                # released
    o2 = OrderRequest("R_100", "CALL", 10.0, q.payout, "demo:k2", NOW, NOW, NOW - 1)
    assert gov.approve(o2, now=NOW).approved
    assert asyncio.run(ex.execute(o2, q, None, None)) == "99"
    assert client.bought == [("id-CALL", 10.0)]
    assert db.trades(mode="demo")[0]["contract_id"] == "99"


def test_demo_config_needs_account_id_on_new_api():
    assert any("DERIV_ACCOUNT_ID" in p for p in configuration_problems(
        load_settings(env={"ENABLE_TRADING": "true", "TRADING_MODE": "demo", "DERIV_API_TOKEN": "tok"})))
    assert configuration_problems(DEMO_NEW) == []


@pytest.mark.parametrize("url,ok", [
    ("wss://api.derivws.com/trading/v1/options/ws/demo?otp=abc", True),
    ("wss://api.derivws.com/trading/v1/options/ws/real?otp=abc", False),
    ("wss://api.derivws.com/trading/v1/options/ws/public", False),
])
def test_new_api_preflight_requires_demo_endpoint(tmp_path, url, ok):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    gov = RiskGovernor(DEMO_NEW.risk, 10000.0, clock=lambda: NOW)
    ex = DemoExecutor(FakeDemoClient(url=url), DEMO_NEW, gov, db)
    if ok:
        assert asyncio.run(ex.preflight(["R_100"])).is_virtual
    else:
        with pytest.raises(DemoExecutionRefused):
            asyncio.run(ex.preflight(["R_100"]))
        assert gov.state.halted


def test_demo_buy_refuses_quote_without_rise_fall_text(tmp_path):
    q = quotes(stake=10.0)["CALL"]
    o = OrderRequest("R_100", "CALL", 10.0, q.payout, "k", NOW, NOW, NOW - 1)
    assert quote_problems(q, o, DEMO, NOW) == []
    q.longcode = "Win payout if Volatility 100 Index is higher than 700.00 at close on 2026-10-25."
    assert any("Rise/Fall" in p for p in quote_problems(q, o, DEMO, NOW))
