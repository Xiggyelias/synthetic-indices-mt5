"""
Quantum Brain v2 dashboard (read-only)
======================================
Reads the research database and shows research rankings, data status, decisions,
backtests, paper and demo results, models, drift and the audit log.

This dashboard cannot place, start or schedule trades: it imports no client,
trader or executor, and opens the database for reading only.

  streamlit run dashboard_quantum.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from qb2.config import ConfigError, load_settings  # noqa: E402

SERIES = "#2a78d6"       # categorical slot 1 (single-series charts)
REFERENCE = "#8a8984"    # neutral reference line

st.set_page_config(page_title="Quantum Brain v2", layout="wide")

try:
    settings = load_settings()
except ConfigError as exc:
    st.error(f"Configuration refused: {exc}")
    st.stop()

db_path = Path(settings.db_path)
if not db_path.exists():
    st.title("Quantum Brain v2")
    st.info(f"No research database at `{db_path}` yet. Run `python quantum_brain.py migrate` and collect data first.")
    st.stop()


@st.cache_resource
def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


conn = connect(str(db_path))


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()


def state(key: str):
    df = q("SELECT value FROM kv_state WHERE key=?", (key,))
    return json.loads(df["value"].iloc[0]) if len(df) else None


def latest_report(kind: str):
    df = q("SELECT payload, created_at FROM research_reports WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
    return (json.loads(df["payload"].iloc[0]), df["created_at"].iloc[0]) if len(df) else (None, None)


def ago(ts) -> str:
    if ts is None or pd.isna(ts):
        return "never"
    s = time.time() - float(ts)
    return f"{s:.0f}s ago" if s < 120 else f"{s / 60:.0f} min ago" if s < 7200 else f"{s / 3600:.1f} h ago"


def line_chart(y, title: str, y_title: str) -> go.Figure:
    fig = go.Figure(go.Scatter(y=y, mode="lines", line=dict(color=SERIES, width=2),
                               hovertemplate="trade %{x}<br>" + y_title + " %{y:,.2f}<extra></extra>"))
    fig.update_layout(title=title, height=280, margin=dict(l=10, r=10, t=40, b=10), showlegend=False,
                      xaxis=dict(title="settled trades", showgrid=False), yaxis=dict(title=y_title),
                      hovermode="x unified")
    return fig


# ── Header & safety status ───────────────────────────────────────────────────
st.title("Quantum Brain v2")
st.caption("Statistical edge research for Deriv Volatility Indices · five-minute Rise/Fall · read-only view")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Trading mode", settings.trading_mode.upper())
c2.metric("Demo only", "YES" if settings.demo_only else "NO")
c3.metric("Execution", "ENABLED (demo)" if settings.execution_enabled else "DISABLED")
c4.metric("Contract", f"Rise/Fall {settings.duration}{settings.duration_unit}")
st.info("Probabilities on this page are model estimates. They are not guarantees, and they are not "
        "verified unless the model passed the out-of-sample gates and was promoted by a person. "
        "Simulated, paper and demo results are shown separately; no independently verified live results exist.")

tabs = st.tabs(["Overview", "Research", "Decisions", "Backtests", "Paper & demo", "Models & drift", "Audit"])

# ── Overview ─────────────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("Data status")
    ticks = q("SELECT symbol, COUNT(*) AS ticks, MIN(epoch) AS first_epoch, MAX(epoch) AS last_epoch FROM ticks GROUP BY symbol")
    if len(ticks):
        ticks["last tick"] = ticks["last_epoch"].map(ago)
        ticks["span (h)"] = ((ticks["last_epoch"] - ticks["first_epoch"]) / 3600).round(1)
        ticks["data"] = ticks["symbol"].map(lambda s: "SIMULATED" if s.startswith("SIM:") else "Deriv API")
        st.dataframe(ticks[["symbol", "data", "ticks", "span (h)", "last tick"]], hide_index=True, width="stretch")
    else:
        st.write("No ticks collected yet.")

    st.subheader("Risk governor")
    cols = st.columns(2)
    for col, mode in zip(cols, ("paper", "demo")):
        s = state(f"risk_governor:{mode}")
        with col:
            st.markdown(f"**{mode.title()}**")
            if not s:
                st.write("Not started.")
                continue
            dd = 1 - s["equity"] / s["peak_equity"] if s["peak_equity"] else 0
            lim = settings.risk
            if s["halted"]:
                st.error(f"HALTED: {s['halt_reason']}")
            st.dataframe(pd.DataFrame([
                {"limit": "Equity", "value": f"{s['equity']:.2f}", "limit value": ""},
                {"limit": "Daily realized P/L", "value": f"{s['daily_realized_pnl']:+.2f}",
                 "limit value": f"-{lim.max_daily_loss_fraction * s['start_of_day_equity']:.2f}"},
                {"limit": "Drawdown from peak", "value": f"{dd:.2%}", "limit value": f"{lim.max_drawdown_fraction:.0%}"},
                {"limit": "Trades today", "value": str(s["trades_today"]), "limit value": str(lim.max_trades_per_day)},
                {"limit": "Open positions", "value": str(len(s["open_positions"])), "limit value": str(lim.max_concurrent_positions)},
                {"limit": "Consecutive losses", "value": str(s["consecutive_losses"]), "limit value": str(lim.consecutive_loss_limit)},
                {"limit": "Cooldown", "value": "active" if s["cooldown_until"] > time.time() else "none", "limit value": ""},
            ]), hide_index=True, width="stretch")

    halts = q("SELECT key, value FROM kv_state WHERE key LIKE 'strategy_halt:%' AND value != 'null'")
    if len(halts):
        st.warning("Strategy halts (drift): " + ", ".join(k.split(":", 1)[1] for k in halts["key"]))

# ── Research ─────────────────────────────────────────────────────────────────
with tabs[1]:
    st.subheader("Instrument ranking")
    ranking, created = latest_report("instrument_ranking")
    if ranking:
        st.caption(f"Generated {ago(created)}. {ranking.get('note', '')}")
        df = pd.DataFrame(ranking["ranking"])
        st.dataframe(df, hide_index=True, width="stretch")
        if not df["merits_investigation"].any():
            st.success("No instrument shows a statistically defensible edge. NO_TRADE is the correct default.")
    else:
        st.write("No research run yet: `python quantum_brain.py research`.")

    inst = q("SELECT * FROM instruments ORDER BY symbol")
    if len(inst):
        st.subheader("Discovered Volatility Indices")
        st.dataframe(inst, hide_index=True, width="stretch")

    syms = q("SELECT DISTINCT kind FROM research_reports WHERE kind LIKE 'instrument:%'")
    if len(syms):
        pick = st.selectbox("Instrument report", [k.split(":", 1)[1] for k in syms["kind"]])
        rep, _ = latest_report(f"instrument:{pick}")
        if rep:
            a, b = st.columns(2)
            with a:
                st.markdown(f"**Verdict:** {rep.get('verdict')}")
                st.json(rep.get("evidence", {}), expanded=True)
                st.markdown("**Payout / break-even**")
                st.json(rep.get("payout", {}))
            with b:
                st.markdown("**Data quality**")
                st.json(rep.get("data_quality", {}), expanded=False)
                st.markdown("**Return distribution & dependence**")
                st.json(rep.get("returns", {}), expanded=False)
            models = rep.get("model_selection", {}).get("models", {})
            if models:
                st.markdown("**Walk-forward model comparison (development segment)**")
                st.dataframe(pd.DataFrame([
                    {"model": k, "brier": v["probability"]["brier"], "ece": v["probability"]["ece"],
                     "improvement vs base": v.get("brier_vs_base", {}).get("improvement"),
                     "CI low": v.get("brier_vs_base", {}).get("ci_low"),
                     "CI high": v.get("brier_vs_base", {}).get("ci_high")} for k, v in models.items()
                ]), hide_index=True, width="stretch")
            pats = rep.get("patterns", {}).get("patterns", [])
            if pats:
                st.markdown(f"**Pattern tests** (Holm + Benjamini-Hochberg across {rep['patterns']['tests_run']} tests)")
                st.dataframe(pd.DataFrame(pats), hide_index=True, width="stretch")
            if rep.get("regimes"):
                st.markdown("**Regimes**")
                st.json(rep["regimes"], expanded=False)

# ── Decisions ────────────────────────────────────────────────────────────────
with tabs[2]:
    dec = q("SELECT * FROM decisions ORDER BY id DESC LIMIT 500")
    if len(dec):
        payloads = [json.loads(p) for p in dec["payload"]]
        latest = {}
        for p, mode in zip(payloads, dec["mode"]):
            latest.setdefault(p["symbol"], (p, mode))
        st.subheader("Latest decision per instrument")
        for sym, (p, mode) in latest.items():
            box = st.container(border=True)
            label = {"TRADE": "TRADE", "NO_TRADE": "NO_TRADE", "HALTED": "HALTED"}[p["action"]]
            box.markdown(f"**{sym}** · {label} · {mode} · {ago(p['decided_at'])} · model `{p.get('model_version') or 'none'}`")
            m = box.columns(5)
            fmt = lambda v, f="{:.4f}": f.format(v) if v is not None else "—"  # noqa: E731
            m[0].metric("P(Rise) estimate", fmt(p.get("p_rise")), help="Model estimate, not a guarantee")
            m[1].metric("Uncertainty (σ)", fmt(p.get("uncertainty")))
            m[2].metric("EV CALL (conservative)", fmt(p.get("ev_call_conservative"), "{:+.4f}"))
            m[3].metric("EV PUT (conservative)", fmt(p.get("ev_put_conservative"), "{:+.4f}"))
            m[4].metric("Break-even", fmt(p.get("breakeven_call")))
            box.caption(f"Regime: {p.get('regime')} · data age: {fmt(p.get('data_age_seconds'), '{:.1f}s')} · "
                        f"Reasons: {'; '.join(p.get('reasons', []))}")
            box.caption("Assumptions: " + "; ".join(f"{k}: {v}" for k, v in (p.get("assumptions") or {}).items()))
        st.subheader("Decision history")
        hist = pd.DataFrame([{"time": pd.to_datetime(p["decided_at"], unit="s"), "symbol": p["symbol"], "mode": mode,
                              "action": p["action"], "contract": p.get("contract_type"), "p_rise": p.get("p_rise"),
                              "reason": (p.get("reasons") or [""])[0]} for p, mode in zip(payloads, dec["mode"])])
        st.dataframe(hist, hide_index=True, width="stretch")
        st.bar_chart(hist["action"].value_counts())
    else:
        st.write("No decisions recorded yet.")

# ── Backtests ────────────────────────────────────────────────────────────────
with tabs[3]:
    bts = q("SELECT kind, payload, created_at FROM research_reports WHERE kind LIKE 'backtest:%' ORDER BY id DESC LIMIT 20")
    if len(bts):
        choice = st.selectbox("Backtest", [f"{k} · {ago(c)}" for k, c in zip(bts["kind"], bts["created_at"])])
        rep = json.loads(bts["payload"].iloc[[f"{k} · {ago(c)}" for k, c in zip(bts["kind"], bts["created_at"])].index(choice)])
        status = rep.get("status")
        if rep.get("simulated_data"):
            st.error("SIMULATED DATA: this backtest says nothing about the real market.")
        if status == "COMPLETE":
            st.success("COMPLETE: priced with recorded Deriv proposals.")
        elif status == "INCOMPLETE_ASSUMED_PAYOUT":
            st.warning("INCOMPLETE: payout was ASSUMED, not taken from Deriv. Not evidence of profitability.")
        else:
            st.warning(f"{status}: no trades simulated.")
        for n in rep.get("notes", []):
            st.caption(n)
        met = rep.get("metrics", {})
        cols = st.columns(6)
        for col, (k, label, f) in zip(cols, [("trades", "Trades", "{:.0f}"), ("win_rate", "Win rate", "{:.1%}"),
                                             ("expectancy_per_stake", "Return / stake", "{:+.4f}"),
                                             ("profit_factor", "Profit factor", "{:.2f}"),
                                             ("max_drawdown", "Max drawdown", "{:.1%}"),
                                             ("longest_losing_streak", "Longest losing streak", "{:.0f}")]):
            v = met.get(k)
            col.metric(label, f.format(v) if isinstance(v, (int, float)) and v is not None else "—")
        if met.get("return_ci_low") is not None:
            st.caption(f"95% bootstrap CI of return per stake: [{met['return_ci_low']:+.4f}, {met['return_ci_high']:+.4f}]")
        if rep.get("equity_curve"):
            st.plotly_chart(line_chart(rep["equity_curve"], "Equity (simulated)", "equity"), width="stretch")
        st.markdown("**Decisions, including those that did not trade**")
        st.dataframe(pd.DataFrame(sorted(rep.get("decision_counts", {}).items(), key=lambda x: -x[1]),
                                  columns=["outcome", "count"]), hide_index=True, width="stretch")
        if rep.get("regime_performance"):
            st.markdown("**By regime**")
            st.dataframe(pd.DataFrame(rep["regime_performance"]), hide_index=True, width="stretch")
    else:
        st.write("No backtests yet: `python quantum_brain.py backtest --symbol R_100`.")

# ── Paper & demo ─────────────────────────────────────────────────────────────
with tabs[4]:
    for mode, title in (("paper", "Paper (simulated fills on live Deriv quotes)"),
                        ("demo", "Demo account (Deriv virtual money)")):
        st.subheader(title)
        tr = q("SELECT * FROM trades WHERE mode=? ORDER BY id", (mode,))
        if not len(tr):
            st.write("No trades.")
            continue
        done = tr[tr["status"].isin(["won", "lost"])]
        a, b, c, d = st.columns(4)
        a.metric("Settled", len(done))
        b.metric("Win rate", f"{(done['status'] == 'won').mean():.1%}" if len(done) else "—")
        c.metric("Net P/L", f"{done['profit'].sum():+.2f}")
        d.metric("Open", int((tr["status"] == "open").sum()))
        if len(done):
            st.plotly_chart(line_chart(done["profit"].cumsum().tolist(), f"Cumulative P/L ({mode})", "P/L"),
                            width="stretch")
            done = done.assign(day=pd.to_datetime(done["settled_at"], unit="s").dt.date)
            st.markdown("**Daily P/L**")
            st.dataframe(done.groupby("day")["profit"].agg(["count", "sum"]).rename(columns={"count": "trades", "sum": "P/L"}),
                         width="stretch")
        st.dataframe(tr.tail(200), hide_index=True, width="stretch")

# ── Models & drift ───────────────────────────────────────────────────────────
with tabs[5]:
    models = q("SELECT version, symbol, model_type, status, approved_by, approved_at, created_at, metrics, notes FROM models ORDER BY created_at DESC")
    if len(models):
        rows = []
        for _, r in models.iterrows():
            m = json.loads(r["metrics"])
            acc = m.get("acceptance", {})
            rows.append({"version": r["version"], "symbol": r["symbol"], "type": r["model_type"], "status": r["status"],
                         "approved by": r["approved_by"], "OOS brier": m.get("probability", {}).get("brier"),
                         "brier CI low": m.get("brier_vs_base", {}).get("ci_low"), "ECE": m.get("probability", {}).get("ece"),
                         "payout source": m.get("payout_source"), "gate failures": "; ".join(acc.get("failures", []))})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        pick = st.selectbox("Validation report", models["version"])
        m = json.loads(models.set_index("version").loc[pick, "metrics"])
        cal = m.get("probability", {}).get("calibration", [])
        if cal:
            cdf = pd.DataFrame(cal)
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=[0.3, 0.7], y=[0.3, 0.7], mode="lines", name="perfect calibration",
                                     line=dict(color=REFERENCE, width=1, dash="dot"), hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=cdf["mean_predicted"], y=cdf["observed_rate"], mode="lines+markers",
                                     name="model (final test)", line=dict(color=SERIES, width=2), marker=dict(size=8),
                                     customdata=cdf["n"],
                                     hovertemplate="predicted %{x:.3f}<br>observed %{y:.3f}<br>n=%{customdata}<extra></extra>"))
            fig.update_layout(title="Reliability on the final test segment", height=320,
                              xaxis_title="mean predicted P(Rise)", yaxis_title="observed Rise rate",
                              margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, width="stretch")
            st.dataframe(cdf, hide_index=True)
        st.json({k: v for k, v in m.items() if k not in ("probability",)}, expanded=False)
    else:
        st.write("No models trained.")
    st.subheader("Drift alerts")
    drift = q("SELECT created_at, symbol, model_version, status, report FROM drift_events ORDER BY id DESC LIMIT 100")
    if len(drift):
        drift["when"] = drift["created_at"].map(ago)
        drift["reasons"] = drift["report"].map(lambda r: "; ".join(json.loads(r).get("reasons", [])))
        st.dataframe(drift[["when", "symbol", "model_version", "status", "reasons"]], hide_index=True, width="stretch")
    else:
        st.write("No drift checks yet.")

# ── Audit ────────────────────────────────────────────────────────────────────
with tabs[6]:
    st.subheader("Audit events")
    st.dataframe(q("SELECT datetime(created_at, 'unixepoch') AS time, severity, event_type, payload FROM audit_events ORDER BY id DESC LIMIT 300"),
                 hide_index=True, width="stretch")
    st.subheader("Ingestion lineage")
    st.dataframe(q("SELECT id, kind, symbol, source, datetime(started_at, 'unixepoch') AS started, status, rows_inserted, notes FROM ingestion_runs ORDER BY id DESC LIMIT 100"),
                 hide_index=True, width="stretch")
