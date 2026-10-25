"""Quantum Brain v2: models, regimes, patterns and the end-to-end research pipeline."""

import numpy as np
import pytest

from qb2.config import load_settings
from qb2.models import LogisticModel, PlattCalibrator, make_model
from qb2.patterns import discover_patterns, ljung_box, nonoverlap_mask
from qb2.pipeline import backtest_symbol, research_symbols, train_symbol
from qb2.regime import GaussianHMM, cusum_change_points, hmm_state_count_by_bic, transition_matrix
from qb2.registry import ModelRegistry
from qb2.research import build_dataset, final_test, research_instrument
from qb2.storage import Database
from qb2.ticks import simulate_volatility_ticks
from qb2.validation import HoldoutAlreadyUsed, HoldoutGuard

SETTINGS = load_settings(env={})


# ── Models ───────────────────────────────────────────────────────────────────

def test_logistic_learns_planted_signal_and_reports_uncertainty():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 3))
    p = 1 / (1 + np.exp(-0.8 * X[:, 0]))
    y = (rng.random(4000) < p).astype(int)
    m = LogisticModel(purge_rows=2).fit(X[:3000], y[:3000])
    pred, sd = m.predict(X[3000:])
    assert np.corrcoef(pred, p[3000:])[0, 1] > 0.9
    assert np.all(sd >= 0) and sd.mean() < 0.05


def test_platt_calibration_fixes_overconfidence():
    rng = np.random.default_rng(1)
    true = rng.uniform(0.4, 0.6, 5000)
    y = (rng.random(5000) < true).astype(int)
    over = 1 / (1 + np.exp(-4 * np.log(true / (1 - true))))      # 4x too confident
    cal = PlattCalibrator().fit(over, y)
    assert cal.a < 0.5
    assert np.mean(np.abs(cal.apply(over) - true)) < np.mean(np.abs(over - true)) / 2


def test_unknown_model_rejected():
    with pytest.raises(ValueError):
        make_model("lstm")


# ── Regimes ──────────────────────────────────────────────────────────────────

def test_hmm_recovers_two_volatility_states_and_filter_is_causal():
    rng = np.random.default_rng(2)
    x = np.concatenate([rng.normal(0, 1, 400), rng.normal(0, 4, 400), rng.normal(0, 1, 400)])
    hmm = GaussianHMM(n_states=2).fit(x)
    assert hmm.variances[1] / hmm.variances[0] > 8
    probs = hmm.filter(x)
    assert probs[200, 0] > 0.9 and probs[600, 1] > 0.9
    np.testing.assert_allclose(hmm.filter(x[:500]), probs[:500])     # filtering never uses later data
    assert hmm_state_count_by_bic(rng.normal(0, 1, 800))["best_states"] == 1


def test_cusum_and_transition_matrix():
    rng = np.random.default_rng(3)
    x = np.concatenate([rng.normal(0, 1, 300), rng.normal(3, 1, 300)])
    cps = cusum_change_points(x)
    assert cps and 300 <= cps[0] <= 320
    tm = transition_matrix(np.array(["A", "A", "B", "B", "A"]))
    np.testing.assert_allclose(tm.sum(axis=1).values, 1.0)


# ── Patterns ─────────────────────────────────────────────────────────────────

def test_ljung_box_and_nonoverlap():
    rng = np.random.default_rng(4)
    assert ljung_box(rng.normal(size=3000))["p_value"] > 0.01
    ar = np.zeros(3000)
    for i in range(1, 3000):
        ar[i] = 0.3 * ar[i - 1] + rng.normal()
    assert ljung_box(ar)["p_value"] < 1e-6
    keep = nonoverlap_mask(np.arange(0, 3000, 60.0), 300)
    assert keep.sum() == 10


def test_pattern_discovery_quiet_on_fair_data():
    df = simulate_volatility_ticks("R_100", seconds=5 * 86400, seed=11)
    ds = build_dataset(df, "R_100", SETTINGS)
    res = discover_patterns(ds.features, ds.outcome, 300, min_samples=30)
    assert res["tests_run"] > 10
    assert res["discoveries_fwer"] == []
    untested = [p for p in res["patterns"] if not p["tested"]]
    assert all(p.get("p_value") is None for p in untested)      # small samples are reported, never tested


# ── End-to-end research ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fair_dataset():
    return build_dataset(simulate_volatility_ticks("R_100", seconds=4 * 86400, seed=3), "R_100", SETTINGS)


def test_research_finds_no_edge_on_fair_index(fair_dataset):
    rep = research_instrument(fair_dataset, SETTINGS)
    assert rep["verdict"] == "NO_EVIDENCE_OF_EDGE" and not rep["merits_investigation"]
    assert rep["evidence"]["chosen_model"] == "base_rate"
    assert rep["payout"]["source"] == "unavailable"


def test_research_detects_planted_edge_but_gates_still_reject_assumed_payout(tmp_path):
    ds = build_dataset(simulate_volatility_ticks("R_100", seconds=4 * 86400, seed=3, ou_ratio=3.0), "R_100", SETTINGS)
    rep = research_instrument(ds, SETTINGS)
    assert rep["merits_investigation"] and rep["evidence"]["chosen_model"] != "base_rate"
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    trained, metrics = final_test(ds, rep["evidence"]["chosen_model"], SETTINGS, guard=HoldoutGuard(db),
                                  payout_ratio=1.95, payout_source="assumed")
    assert metrics["brier_vs_base"]["ci_low"] > 0
    with pytest.raises(HoldoutAlreadyUsed):
        final_test(ds, "logistic", SETTINGS, guard=HoldoutGuard(db), payout_ratio=1.95, payout_source="assumed")
    reg = ModelRegistry(db, str(tmp_path / "models"))
    version, status = reg.register_candidate(trained, metrics)
    assert status == "rejected"
    assert any("payout" in f for f in db.model(version)["metrics"]["acceptance"]["failures"])


def test_pipeline_commands_on_simulated_store(tmp_path):
    db = Database(str(tmp_path / "qb.db"))
    db.migrate()
    df = simulate_volatility_ticks("R_100", seconds=3 * 86400, seed=7)
    db.insert_ticks("SIM:R_100", df["epoch"].values, df["quote"].values)
    ranking = research_symbols(db, SETTINGS, ["SIM:R_100"])["ranking"]
    assert ranking[0]["symbol"] == "SIM:R_100" and not ranking[0]["merits_investigation"]
    bt = backtest_symbol(db, SETTINGS, "SIM:R_100", "logistic", payout_ratio=1.95)
    assert bt["status"] == "INCOMPLETE_ASSUMED_PAYOUT" and bt["simulated_data"]
    out = train_symbol(db, SETTINGS, "SIM:R_100", ModelRegistry(db, str(tmp_path / "models")))
    assert out["status"] == "rejected"
    assert db.latest_report("instrument_ranking") is not None
