"""
Trading Bot Dashboard  —  RSI-Pullback v2.4
============================================
Streamlit dashboard: live scanner feed, performance analytics,
trade journal, forward-test tracker, log viewer.

Run:
    streamlit run dashboard.py --server.port 8502
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import psutil
import streamlit as st

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="RSI-Pullback Bot | Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Master CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Base dark theme ── */
html, body, [class*="css"] { font-family: 'Inter', 'Segoe UI', sans-serif; }
.stApp { background: #0d1117; }

/* ── Hero header ── */
.hero-banner {
    background: linear-gradient(135deg, #1a1f2e 0%, #161b27 50%, #0d1117 100%);
    border: 1px solid #21262d;
    border-radius: 12px;
    padding: 20px 28px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.hero-title { font-size: 22px; font-weight: 700; color: #e6edf3; margin: 0; }
.hero-sub   { font-size: 13px; color: #8b949e; margin: 4px 0 0 0; }

/* ── Live pulse dot ── */
.live-dot {
    display: inline-block;
    width: 10px; height: 10px;
    background: #3fb950;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 1.8s infinite;
    box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7);
}
.live-dot-off {
    display: inline-block;
    width: 10px; height: 10px;
    background: #f85149;
    border-radius: 50%;
    margin-right: 6px;
}
@keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(63,185,80,0.7); }
    70%  { box-shadow: 0 0 0 8px rgba(63,185,80,0); }
    100% { box-shadow: 0 0 0 0 rgba(63,185,80,0); }
}

/* ── Regime badge ── */
.badge-trending  { background:#1a3a1f; color:#3fb950; border:1px solid #2ea043; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }
.badge-ranging   { background:#3a2a0a; color:#d29922; border:1px solid #9e6a03; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }
.badge-volatile  { background:#3a0a0a; color:#f85149; border:1px solid #da3633; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }
.badge-unknown   { background:#21262d; color:#8b949e; border:1px solid #30363d; padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600; }

/* ── KPI cards ── */
.kpi-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 18px 20px;
    text-align: center;
    transition: border-color 0.2s;
}
.kpi-card:hover { border-color: #388bfd; }
.kpi-label { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 8px; }
.kpi-value { color: #e6edf3; font-size: 26px; font-weight: 700; line-height: 1; }
.kpi-delta-pos { color: #3fb950; font-size: 12px; margin-top: 6px; }
.kpi-delta-neg { color: #f85149; font-size: 12px; margin-top: 6px; }
.kpi-delta-neu { color: #8b949e; font-size: 12px; margin-top: 6px; }

/* ── Activity feed ── */
.feed-row {
    display: flex;
    align-items: center;
    padding: 7px 12px;
    border-radius: 6px;
    margin-bottom: 4px;
    background: #161b22;
    border-left: 3px solid #21262d;
    font-size: 13px;
    color: #c9d1d9;
}
.feed-row.scan    { border-left-color: #388bfd; }
.feed-row.trade   { border-left-color: #3fb950; }
.feed-row.warning { border-left-color: #d29922; }
.feed-row.error   { border-left-color: #f85149; }
.feed-row.system  { border-left-color: #6e40c9; }
.feed-time { color: #8b949e; font-size: 11px; min-width: 58px; margin-right: 10px; font-family: monospace; }
.feed-sym  { color: #79c0ff; font-weight: 600; min-width: 60px; margin-right: 8px; }

/* ── Scanner table ── */
.scan-row {
    display: grid;
    grid-template-columns: 90px 130px 130px 1fr;
    padding: 10px 16px;
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 8px;
    margin-bottom: 6px;
    align-items: center;
}
.scan-sym { color: #79c0ff; font-weight: 700; font-size: 14px; }

/* ── Streamlit overrides ── */
div[data-testid="stMetric"] {
    background: #161b22 !important;
    border: 1px solid #21262d !important;
    border-radius: 10px !important;
    padding: 16px !important;
}
div[data-testid="stMetric"]:hover { border-color: #388bfd !important; }
[data-testid="stMetricLabel"] > div { color: #8b949e !important; font-size: 12px !important; }
[data-testid="stMetricValue"] > div { color: #e6edf3 !important; }

.stTabs [data-baseweb="tab"] { color: #8b949e !important; font-size: 14px; }
.stTabs [aria-selected="true"] { color: #e6edf3 !important; border-bottom: 2px solid #388bfd !important; }
hr { border-color: #21262d !important; }

/* ── Sidebar ── */
section[data-testid="stSidebar"] { background: #0d1117 !important; border-right: 1px solid #21262d; }
</style>
""", unsafe_allow_html=True)

# ── Database ──────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_db():
    try:
        from database import DatabaseManager
        db = DatabaseManager.from_env()
        return db if db.available else None
    except Exception:
        return None

DB = get_db()

# ── Data loaders ──────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def load_trades_state() -> dict:
    return _read_json(ROOT / "data" / "trades_state.json")


def load_performance() -> List[dict]:
    if DB:
        try:
            return DB.fetch_trades(period_days=365)
        except Exception:
            pass
    raw = _read_json(ROOT / "data" / "performance_state.json")
    return raw.get("trades", [])


def load_journal() -> List[dict]:
    if DB:
        try:
            return DB.fetch_journal_entries(limit=500)
        except Exception:
            pass
    raw = _read_json(ROOT / "data" / "trade_journal.json")
    return raw.get("entries", [])


def load_forward_test() -> dict:
    if DB:
        try:
            trades  = DB.fetch_ft_trades(include_open=True)
            metrics = DB.fetch_daily_metrics(days=90)
            return {"trades": trades, "daily_metrics": metrics}
        except Exception:
            pass
    return _read_json(ROOT / "config" / "forward_test_config.json")


def load_config() -> dict:
    return _read_json(ROOT / "config" / "config_rsi_pullback.json")


def load_log_tail(n: int = 200) -> List[str]:
    log_path = ROOT / "logs" / "bot_run.log"
    if not log_path.exists():
        return []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def get_bot_status() -> str:
    path = ROOT / "data" / "bot_switch.txt"
    try:
        return path.read_text().strip().lower()
    except Exception:
        return "unknown"


def set_bot_status(status: str):
    path = ROOT / "data" / "bot_switch.txt"
    path.write_text(status)


def check_bot_process() -> Optional[int]:
    """
    Check if mt5_bot.py is actively running.
    Strategy:
      1. psutil cmdline scan (works when cmdline is readable)
      2. Log-file recency fallback — if bot_run.log was written within
         the last 3 minutes the bot is definitely alive even if psutil
         can't read the cmdline (common on Windows).
    Returns the PID if found via psutil, or -1 as a sentinel when the
    log-file heartbeat confirms the process is running.
    """
    EXCLUDE = {'--status-check', '--backtest', '--backtest-v2',
               '--safety-check', '--close-all', '--emergency-status'}

    # ── Method 1: psutil cmdline scan ────────────────────────────────
    try:
        current_pid = os.getpid()
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['pid'] == current_pid:
                    continue
                name = (proc.info.get('name') or '').lower()
                if 'python' not in name:
                    continue
                cmdline = proc.info.get('cmdline') or []
                # cmdline can be a list of args or a single joined string
                full_cmd = ' '.join(cmdline)
                if 'mt5_bot.py' not in full_cmd:
                    continue
                if any(flag in full_cmd for flag in EXCLUDE):
                    continue
                return proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass

    # ── Method 2: log-file heartbeat (last-write within 3 minutes) ───
    try:
        log_path = ROOT / "logs" / "bot_run.log"
        if log_path.exists():
            age_seconds = time.time() - log_path.stat().st_mtime
            if age_seconds < 300:          # 5-minute window
                return -1                  # alive but PID unknown
    except Exception:
        pass

    return None


# ── Log parser — live scanner data ────────────────────────────────────────────

_RE_REGIME   = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*Market Regime for (\w+): (\w+)")
_RE_HEARTBEAT= re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*Heartbeat: (.+)")
_RE_TRADE    = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*((?:Placing|Opened|Closed|TRADE|BUY|SELL|ticket=).+)")
_RE_WARN     = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*\[WARNING\] (.+)")
_RE_ERROR    = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*\[ERROR\] (.+)")
_RE_HALT     = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*(RISK HALT|capital protection|losing streak).+", re.IGNORECASE)
_RE_SIG      = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*(Signal|signal score|confluence).+", re.IGNORECASE)
_RE_STRUCT   = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*(Structure|partial close|TRAIL|BE@).+")


def parse_scanner_data(log_lines: List[str]) -> dict:
    """Extract live scanner state from log lines."""
    regimes: Dict[str, str]    = {}
    last_scan: Dict[str, str]  = {}
    events: List[dict]         = []

    for line in log_lines:
        m = _RE_REGIME.search(line)
        if m:
            ts, sym, reg = m.group(1), m.group(2), m.group(3)
            regimes[sym]   = reg
            last_scan[sym] = ts[-8:]          # HH:MM:SS only
            events.append({"ts": ts[-8:], "sym": sym,  "msg": f"Regime → {reg}", "kind": "scan"})
            continue

        m = _RE_HEARTBEAT.search(line)
        if m:
            events.append({"ts": m.group(1)[-8:], "sym": "SYS", "msg": m.group(2)[:70], "kind": "system"})
            continue

        m = _RE_HALT.search(line)
        if m:
            events.append({"ts": m.group(1)[-8:], "sym": "RISK", "msg": line.split("]")[-1].strip()[:70], "kind": "warning"})
            continue

        m = _RE_ERROR.search(line)
        if m:
            events.append({"ts": m.group(1)[-8:], "sym": "ERR",  "msg": m.group(2)[:70], "kind": "error"})
            continue

        m = _RE_STRUCT.search(line)
        if m:
            events.append({"ts": m.group(1)[-8:], "sym": "MGR",  "msg": line.split("]")[-1].strip()[:70], "kind": "trade"})
            continue

        m = _RE_SIG.search(line)
        if m:
            events.append({"ts": m.group(1)[-8:], "sym": "SIG",  "msg": line.split("]")[-1].strip()[:70], "kind": "trade"})
            continue

    # Deduplicate consecutive identical scan lines
    filtered = []
    prev = None
    for e in events:
        key = (e["sym"], e["msg"])
        if key != prev:
            filtered.append(e)
        prev = key

    return {"regimes": regimes, "last_scan": last_scan, "events": list(reversed(filtered))[:25]}


# ── Metrics helpers ───────────────────────────────────────────────────────────

def calc_metrics(trades: List[dict]) -> dict:
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    for col in ("pnl", "r_achieved"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["pnl"])
    if df.empty:
        return {}

    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    win_rate    = len(wins) / len(df) * 100 if len(df) else 0
    gross_profit= wins["pnl"].sum()
    gross_loss  = abs(losses["pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    total_pnl   = df["pnl"].sum()

    df_s = df.copy()
    if "closed_at" in df_s.columns:
        df_s["closed_at"] = pd.to_datetime(df_s["closed_at"], errors="coerce")
        df_s = df_s.sort_values("closed_at")
    equity    = 10000 + df_s["pnl"].cumsum()
    peak      = equity.cummax()
    drawdown  = ((equity - peak) / peak * 100).min()
    avg_r     = df["r_achieved"].mean() if "r_achieved" in df.columns else 0

    return {
        "total_trades": len(df),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "avg_win": wins["pnl"].mean() if len(wins) else 0,
        "avg_loss": losses["pnl"].mean() if len(losses) else 0,
        "max_drawdown": drawdown,
        "avg_r": avg_r,
    }


def build_equity_curve(trades: List[dict]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    if "pnl" not in df.columns:
        return pd.DataFrame()
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df = df.dropna(subset=["pnl"])
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")
        df = df.sort_values("closed_at")
    df["equity"]       = 10000 + df["pnl"].cumsum()
    df["peak"]         = df["equity"].cummax()
    df["drawdown_pct"] = (df["equity"] - df["peak"]) / df["peak"] * 100
    df["trade_num"]    = range(1, len(df) + 1)
    return df


# ── Chart helpers ─────────────────────────────────────────────────────────────
BG    = "#0d1117"
GRID  = "#21262d"
C     = {"win": "#3fb950", "loss": "#f85149", "neutral": "#388bfd",
         "line": "#a371f7", "peak": "#56d364", "warn": "#d29922"}


def _base_layout(height=400, **kw) -> dict:
    return dict(
        paper_bgcolor=BG, plot_bgcolor=BG, font_color="#c9d1d9",
        legend=dict(orientation="h", y=1.04, x=0, font_size=12),
        margin=dict(t=10, b=30, l=0, r=0), height=height,
        xaxis=dict(gridcolor=GRID, zerolinecolor=GRID),
        yaxis=dict(gridcolor=GRID, zerolinecolor=GRID),
        **kw,
    )


def chart_equity(df: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.72, 0.28], vertical_spacing=0.03)
    x = df.get("closed_at", df.get("trade_num", range(len(df))))

    fig.add_trace(go.Scatter(
        x=x, y=df["equity"], name="Equity", mode="lines",
        line=dict(color=C["line"], width=2.5),
        fill="tozeroy", fillcolor="rgba(163,113,247,0.07)",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=x, y=df["peak"], name="Peak", mode="lines",
        line=dict(color=C["peak"], width=1, dash="dot"), opacity=0.5,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=x, y=df["drawdown_pct"], name="Drawdown %", mode="lines",
        fill="tozeroy", fillcolor="rgba(248,81,73,0.18)",
        line=dict(color=C["loss"], width=1.5),
    ), row=2, col=1)

    fig.update_layout(**_base_layout(height=440,
        yaxis_title="Account ($)", yaxis2_title="DD %"))
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID)
    return fig


def chart_monthly_pnl(trades: List[dict]) -> go.Figure:
    df = pd.DataFrame(trades)
    df["pnl"] = pd.to_numeric(df.get("pnl", []), errors="coerce")
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")
        df["month"] = df["closed_at"].dt.to_period("M").astype(str)
    else:
        df["month"] = "All"
    monthly = df.groupby("month")["pnl"].sum().reset_index()
    monthly["color"] = monthly["pnl"].apply(lambda x: C["win"] if x >= 0 else C["loss"])
    fig = go.Figure(go.Bar(
        x=monthly["month"], y=monthly["pnl"],
        marker_color=monthly["color"],
        text=monthly["pnl"].round(2), textposition="outside", textfont_size=12,
    ))
    fig.update_layout(**_base_layout(height=290, yaxis_title="Net PnL ($)"))
    return fig


def chart_win_loss_pie(wins: int, losses: int) -> go.Figure:
    fig = go.Figure(go.Pie(
        labels=["Wins", "Losses"], values=[wins or 0, losses or 0],
        marker_colors=[C["win"], C["loss"]],
        hole=0.58, textinfo="percent+label", textfont_size=13,
        marker=dict(line=dict(color=BG, width=2)),
    ))
    fig.update_layout(paper_bgcolor=BG, font_color="#c9d1d9",
                      showlegend=False, margin=dict(t=10,b=10,l=0,r=0), height=250)
    return fig


def chart_r_histogram(trades: List[dict]) -> go.Figure:
    df = pd.DataFrame(trades)
    if "r_achieved" not in df.columns:
        return go.Figure()
    df["r_achieved"] = pd.to_numeric(df["r_achieved"], errors="coerce").dropna()
    colors = [C["win"] if r > 0 else C["loss"] for r in df["r_achieved"]]
    fig = go.Figure(go.Bar(
        x=list(range(len(df))), y=df["r_achieved"],
        marker_color=colors, marker_line_width=0, name="R",
    ))
    fig.add_hline(y=0, line_color=GRID, line_width=1)
    fig.update_layout(**_base_layout(height=250,
        yaxis_title="R Multiple", xaxis_title="Trade #"))
    return fig


def chart_rolling_winrate(trades: List[dict], window: int = 20) -> go.Figure:
    df = pd.DataFrame(trades)
    if "pnl" not in df.columns or len(df) < 3:
        return go.Figure()
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df["win"] = (df["pnl"] > 0).astype(int)
    df["rolling_wr"] = df["win"].rolling(min_periods=1, window=window).mean() * 100
    df["trade_num"]  = range(1, len(df) + 1)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["trade_num"], y=df["rolling_wr"],
        mode="lines", line=dict(color=C["neutral"], width=2),
        fill="tozeroy", fillcolor="rgba(56,139,253,0.08)",
        name=f"{window}-trade rolling WR",
    ))
    fig.add_hline(y=75.0, line_dash="dot", line_color=C["win"], opacity=0.6,
                  annotation_text="75.0% target", annotation_position="bottom right",
                  annotation_font_color=C["win"])
    fig.update_layout(**_base_layout(height=250,
        yaxis_title="Win Rate %", xaxis_title="Trade #", yaxis_range=[0, 105]))
    return fig


def chart_forward_progress(ft: dict) -> go.Figure:
    trades = ft.get("trades", [])
    if not trades:
        return go.Figure()
    df = pd.DataFrame(trades)
    if "profit_loss" in df.columns:
        closed = df[df["profit_loss"].notna()].copy()
        closed["profit_loss"] = pd.to_numeric(closed["profit_loss"], errors="coerce")
        colors = [C["win"] if p > 0 else C["loss"] for p in closed["profit_loss"]]
        fig = go.Figure(go.Bar(
            x=list(range(1, len(closed) + 1)),
            y=closed["profit_loss"], marker_color=colors, marker_line_width=0,
        ))
        fig.add_hline(y=0, line_color=GRID, line_width=1)
        fig.update_layout(**_base_layout(height=270,
            yaxis_title="PnL ($)", xaxis_title="Trade #"))
        return fig
    return go.Figure()


# ── Regime badge HTML ─────────────────────────────────────────────────────────
def regime_badge(regime: str) -> str:
    r = (regime or "").upper()
    if "TREND" in r:
        return f'<span class="badge-trending">▲ {regime}</span>'
    elif "RANG" in r or "CHOP" in r:
        return f'<span class="badge-ranging">↔ {regime}</span>'
    elif "VOLAT" in r:
        return f'<span class="badge-volatile">⚡ {regime}</span>'
    else:
        return f'<span class="badge-unknown">? {regime or "Unknown"}</span>'


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Bot Control")
    st.divider()

    bot_status = get_bot_status()
    pid = check_bot_process()

    st.markdown("### Status Diagnostics")
    
    # 1. Logical Control Switch Status
    if bot_status == "on":
        st.markdown('**Control Switch:** <span style="color:#79c0ff;font-weight:600">ON</span>', unsafe_allow_html=True)
    else:
        st.markdown('**Control Switch:** <span style="color:#8b949e;font-weight:600">OFF</span>', unsafe_allow_html=True)
        
    # 2. Actual Process Status
    if pid and pid > 0:
        st.markdown(f'<span class="live-dot"></span> **Process:** `ACTIVE (PID: {pid})`', unsafe_allow_html=True)
    elif pid == -1:
        st.markdown('<span class="live-dot"></span> **Process:** `ACTIVE`', unsafe_allow_html=True)
    else:
        st.markdown('<span class="live-dot-off"></span> **Process:** `INACTIVE`', unsafe_allow_html=True)

    col_on, col_off = st.columns(2)
    with col_on:
        if st.button("▶ Start", type="primary", use_container_width=True,
                     disabled=(bot_status == "on")):
            set_bot_status("on")
            st.success("Bot started")
            st.rerun()
    with col_off:
        if st.button("⏹ Stop", type="secondary", use_container_width=True,
                     disabled=(bot_status == "off")):
            set_bot_status("off")
            st.warning("Bot stopped")
            st.rerun()

    st.divider()
    st.markdown("### 🛡️ Pre-Flight Check")
    with st.expander("Run Safety Diagnostic", expanded=False):
        st.caption("Perform a live pre-flight safety diagnostic check on-demand. This executes the bot's self-check suite.")
        if st.button("🛡️ Run Safety Check", use_container_width=True):
            with st.spinner("Running safety checks..."):
                try:
                    res = subprocess.run(
                        ["python", "mt5_bot.py", "--safety-check"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        cwd=str(ROOT)
                    )
                    st.markdown("**Diagnostic Output:**")
                    st.code(res.stdout + "\n" + res.stderr, language="text")
                    if res.returncode == 0:
                        st.success("✅ Safety check passed!")
                    else:
                        st.error(f"❌ Safety check failed with code {res.returncode}")
                except Exception as e:
                    st.error(f"Error running safety check: {e}")

    st.divider()
    st.markdown("### 🗄️ Database")
    db_label = "✅ MySQL Connected" if DB else "📁 JSON Fallback"
    st.info(db_label)

    st.divider()
    st.markdown("### ⚡ Refresh")
    auto_refresh = st.checkbox("Auto-refresh (10s)", value=False)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    cfg = load_config()
    rp  = cfg.get("strategy_parameters", {})
    rm  = cfg.get("risk_management", {})
    rsi = rp.get("rsi_settings", {})
    vol = rp.get("volume_settings", {})
    st.markdown(f"### 📋 Strategy v{cfg.get('version', '?')}")
    st.markdown(f"""
| Parameter | Value |
|-----------|-------|
| Timeframe | **{rp.get('timeframe','?')}** |
| Risk/trade | **{rm.get('risk_per_trade',0)*100:.0f}%** |
| Min RR | **{rm.get('min_risk_reward','?')}** |
| RSI OB/OS | **{rsi.get('rsi_overbought','?')} / {rsi.get('rsi_oversold','?')}** |
| Volume | **{vol.get('volume_multiplier','?')}x** |
| Target WR | **75.0%+** |
""")

# ── Cached data loaders ───────────────────────────────────────────────────────
@st.cache_data(ttl=10)
def _c_performance():   return load_performance()

@st.cache_data(ttl=10)
def _c_state():         return load_trades_state()

@st.cache_data(ttl=10)
def _c_journal():       return load_journal()

@st.cache_data(ttl=10)
def _c_ft():            return load_forward_test()

@st.cache_data(ttl=5)
def _c_log(n=300):      return load_log_tail(n)

trades      = _c_performance()
state       = _c_state()
journal     = _c_journal()
ft_data     = _c_ft()
metrics     = calc_metrics(trades)
equity_df   = build_equity_curve(trades)
log_lines   = _c_log()
scan_data   = parse_scanner_data(log_lines)

open_trades = state.get("open_trades", {})
meta        = state.get("meta", {})
n_open      = len(open_trades)
halted      = meta.get("halted", False)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HERO HEADER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
live_dot = '<span class="live-dot"></span>' if bot_status == "on" else '<span class="live-dot-off"></span>'
halt_warn = " &nbsp;⚠️ RISK HALTED" if halted else ""

# Regime pills in header
regime_pills = " &nbsp; ".join(
    f"<b style='color:#79c0ff'>{sym}</b> {regime_badge(reg)}"
    for sym, reg in scan_data["regimes"].items()
) if scan_data["regimes"] else "Scanning…"

st.markdown(f"""
<div class="hero-banner">
  <div>
    <p class="hero-title">⚡ RSI-Pullback Bot &nbsp;<span style="color:#8b949e;font-size:14px;font-weight:400">v{cfg.get('version','2.4')}</span></p>
    <p class="hero-sub">{live_dot} EURUSD &bull; XAUUSD &nbsp;&bull;&nbsp; H1 &nbsp;&bull;&nbsp; {regime_pills}{halt_warn}</p>
  </div>
  <div style="text-align:right">
    <p style="color:#8b949e;font-size:12px;margin:0">Last refresh</p>
    <p style="color:#e6edf3;font-size:14px;font-weight:600;margin:4px 0 0 0">{now_str}</p>
  </div>
</div>
""", unsafe_allow_html=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TOP KPI ROW
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
t1, t2, t3, t4, t5 = st.columns(5)

daily_start = float(meta.get("daily_start_eff") or 10000)
peak_eq     = float(meta.get("peak_eff") or 10000)

t1.metric("💰 Balance",       f"${daily_start:,.2f}", delta="Daily start")
t2.metric("📊 Total Trades",  metrics.get("total_trades", 0),
          delta=f"{metrics.get('wins',0)}W  /  {metrics.get('losses',0)}L")
t3.metric("🎯 Win Rate",
          f"{metrics.get('win_rate',0):.1f}%" if metrics else "—",
          delta=f"{(metrics.get('win_rate',0)-75.0):+.1f}% vs 75.0%" if metrics else "Target 75.0%",
          delta_color="normal" if metrics else "off")
t4.metric("📈 Profit Factor",
          f"{metrics.get('profit_factor',0):.2f}" if metrics and metrics.get("profit_factor") != float("inf") else ("∞" if metrics else "—"),
          delta=f"{(metrics.get('profit_factor',0)-1.06):+.2f} vs 1.06" if metrics and metrics.get("profit_factor") != float("inf") else "Target 1.06",
          delta_color="normal" if metrics else "off")
t5.metric("⚠️ Open Positions", n_open,
          delta="✅ No halt" if not halted else "🚨 HALTED",
          delta_color="off" if not halted else "inverse")

st.divider()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TABS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tab_live, tab_overview, tab_trades, tab_perf, tab_ft, tab_logs = st.tabs([
    "🔴 Live Scanner",
    "📊 Overview",
    "📋 Trades",
    "📈 Performance",
    "🧪 Forward Test",
    "📜 Logs",
])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB: LIVE SCANNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_live:
    left_col, right_col = st.columns([1, 1], gap="large")

    with left_col:
        st.markdown("#### 🔍 Symbol Status")

        config_syms = ["EURUSD", "XAUUSD"]
        for sym in config_syms:
            regime    = scan_data["regimes"].get(sym, "Unknown")
            last_time = scan_data["last_scan"].get(sym, "—")
            badge     = regime_badge(regime)
            scan_icon = "🟢" if bot_status == "on" else "⚪"

            st.markdown(f"""
<div class="scan-row">
  <span class="scan-sym">{scan_icon} {sym}</span>
  <span>{badge}</span>
  <span style="color:#8b949e;font-size:12px">🕐 {last_time}</span>
  <span style="color:#8b949e;font-size:12px">H1 · ATR spread · Vol 1.2x · RSI 42/58 &bull; Tol 0.4%</span>
</div>""", unsafe_allow_html=True)

        st.markdown("")
        # Open positions live cards
        if open_trades:
            st.markdown("#### 📂 Open Positions")
            for ticket, t in open_trades.items():
                direction = t.get("direction", "").upper()
                dir_color = "#3fb950" if direction == "BUY" else "#f85149"
                be_flag   = "✅ BE" if t.get("moved_to_break_even") else ""
                partial   = "🔀 Partial" if t.get("partial_closed") else ""
                st.markdown(f"""
<div style="background:#161b22;border:1px solid #21262d;border-left:4px solid {dir_color};
            border-radius:8px;padding:12px 16px;margin-bottom:8px;">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <span style="color:{dir_color};font-weight:700;font-size:15px">{direction} {t.get('symbol','')}</span>
    <span style="color:#8b949e;font-size:12px">#{ticket}</span>
  </div>
  <div style="color:#c9d1d9;font-size:13px;margin-top:6px">
    Entry: <b>{t.get('entry_price','')}</b> &nbsp;|&nbsp;
    SL: <b>{t.get('stop_loss','')}</b> &nbsp;|&nbsp;
    TP: <b>{t.get('take_profit','')}</b>
    &nbsp; {be_flag} {partial}
  </div>
  <div style="color:#8b949e;font-size:11px;margin-top:4px">
    Vol: {t.get('volume','')} &nbsp;|&nbsp; Opened: {str(t.get('opened_at',''))[:16]}
  </div>
</div>""", unsafe_allow_html=True)
        else:
            st.markdown("""
<div style="background:#161b22;border:1px dashed #21262d;border-radius:8px;
            padding:20px;text-align:center;color:#8b949e;font-size:13px">
  No open positions — scanning for next signal
</div>""", unsafe_allow_html=True)

    with right_col:
        st.markdown("#### 📡 Activity Feed")
        if scan_data["events"]:
            kind_color = {
                "scan": "#388bfd", "trade": "#3fb950",
                "warning": "#d29922", "error": "#f85149", "system": "#a371f7",
            }
            for ev in scan_data["events"]:
                color  = kind_color.get(ev["kind"], "#8b949e")
                st.markdown(f"""
<div class="feed-row {ev['kind']}">
  <span class="feed-time">{ev['ts']}</span>
  <span class="feed-sym" style="color:{color}">{ev['sym']}</span>
  <span>{ev['msg']}</span>
</div>""", unsafe_allow_html=True)
        else:
            st.info("Waiting for bot activity — check that the bot is running.")

        st.markdown("")
        # Risk state summary
        st.markdown("#### 🛡️ Risk State")
        r1, r2 = st.columns(2)
        daily_dd = 0.0
        if daily_start > 0 and peak_eq >= daily_start:
            # No daily loss yet
            daily_dd = 0.0
        elif daily_start > 0:
            daily_dd = (daily_start - peak_eq) / daily_start * 100

        r1.metric("Daily Drawdown",  f"{daily_dd:.2f}%", delta="Limit 6%",  delta_color="off")
        r2.metric("Bot Halted",      "YES 🚨" if halted else "No ✅",
                  delta=str(meta.get("halt_reason",""))[:30] if halted else "Limits OK",
                  delta_color="off")
        cons_loss = state.get("meta", {}).get("consecutive_losses", 0)

        r3, r4 = st.columns(2)
        r3.metric("Peak Equity",       f"${peak_eq:,.2f}")
        r4.metric("Max Open Trades",   cfg.get("risk_management", {}).get("max_open_trades", 3))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB: OVERVIEW
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_overview:
    if not metrics:
        st.markdown("""
<div style="background:#161b22;border:1px dashed #21262d;border-radius:12px;
            padding:40px;text-align:center;">
  <p style="font-size:32px;margin:0">⏳</p>
  <p style="color:#e6edf3;font-size:18px;font-weight:600;margin:12px 0 6px">No closed trades yet</p>
  <p style="color:#8b949e;font-size:14px;margin:0">
    The bot is scanning. Expecting 2–4 signals/week per symbol on H1.
  </p>
</div>""", unsafe_allow_html=True)
    else:
        k1, k2, k3, k4, k5 = st.columns(5)
        wr = metrics.get("win_rate", 0)
        k1.metric("Win Rate",      f"{wr:.1f}%",
                  delta=f"{wr-75.0:+.1f}% vs 75.0%", delta_color="normal")
        k2.metric("Total P&L",     f"${metrics.get('total_pnl',0):+,.2f}",
                  delta=f"{metrics.get('wins',0)}W / {metrics.get('losses',0)}L")
        k3.metric("Profit Factor",
                  f"{metrics.get('profit_factor',0):.2f}" if metrics.get("profit_factor") != float("inf") else "∞",
                  delta=f"{(metrics.get('profit_factor',0)-1.06):+.2f} vs 1.06" if metrics.get("profit_factor") != float("inf") else "Target 1.06",
                  delta_color="normal" if metrics.get("profit_factor") != float("inf") else "off")
        k4.metric("Max Drawdown",  f"{abs(metrics.get('max_drawdown',0)):.1f}%",
                  delta="Limit: 15.0%", delta_color="off")
        k5.metric("Avg R",         f"{metrics.get('avg_r',0):.2f}R",
                  delta=f"{metrics.get('total_trades',0)} trades")

        st.divider()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Trades",  metrics.get("total_trades", 0))
        c2.metric("Avg Win",       f"${metrics.get('avg_win',0):+,.2f}")
        c3.metric("Avg Loss",      f"${metrics.get('avg_loss',0):,.2f}")
        payoff = abs(metrics.get("avg_win",0) / metrics.get("avg_loss",1)) if metrics.get("avg_loss") else 0
        c4.metric("Payoff Ratio",  f"{payoff:.2f}")

        st.divider()
        st.subheader("Equity Curve & Drawdown")
        if not equity_df.empty:
            st.plotly_chart(chart_equity(equity_df), use_container_width=True, key="eq_main")

    # Account state regardless
    st.subheader("Account State")
    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Daily Start",    f"${meta.get('daily_start_eff', 0):,.2f}")
    mc2.metric("Peak Equity",    f"${meta.get('peak_eff', 0):,.2f}")
    mc3.metric("Open Positions", n_open)
    mc4.metric("Risk Halted",    "YES ⚠️" if halted else "No ✅")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB: TRADES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_trades:
    st.subheader("Closed Trade History")
    if not trades:
        st.info("No trades recorded yet.")
    else:
        df_trades = pd.DataFrame(trades)
        fc1, fc2, fc3 = st.columns(3)
        syms_avail = sorted(df_trades["symbol"].dropna().unique()) if "symbol" in df_trades else []
        dirs_avail = sorted(df_trades["direction"].dropna().unique()) if "direction" in df_trades else []
        sel_sym = fc1.multiselect("Symbol", syms_avail, default=syms_avail)
        sel_dir = fc2.multiselect("Direction", dirs_avail, default=dirs_avail)
        sel_out = fc3.selectbox("Outcome", ["All", "Wins only", "Losses only"])

        filt = df_trades.copy()
        if sel_sym and "symbol" in filt:
            filt = filt[filt["symbol"].isin(sel_sym)]
        if sel_dir and "direction" in filt:
            filt = filt[filt["direction"].isin(sel_dir)]
        if sel_out == "Wins only" and "pnl" in filt:
            filt["pnl"] = pd.to_numeric(filt["pnl"], errors="coerce")
            filt = filt[filt["pnl"] > 0]
        elif sel_out == "Losses only" and "pnl" in filt:
            filt["pnl"] = pd.to_numeric(filt["pnl"], errors="coerce")
            filt = filt[filt["pnl"] <= 0]

        show_cols = [c for c in ["ticket","symbol","direction","entry_price","exit_price",
                                  "volume","pnl","r_achieved","confluence_score",
                                  "session","opened_at","closed_at","win"]
                     if c in filt.columns]
        disp = filt[show_cols].copy()
        if "pnl" in disp.columns:
            disp["pnl"] = pd.to_numeric(disp["pnl"], errors="coerce").round(2)
        if "r_achieved" in disp.columns:
            disp["r_achieved"] = pd.to_numeric(disp["r_achieved"], errors="coerce").round(3)

        st.dataframe(
            disp.sort_values("closed_at", ascending=False) if "closed_at" in disp else disp,
            use_container_width=True, hide_index=True,
            column_config={
                "pnl":              st.column_config.NumberColumn("P&L ($)",  format="$%.2f"),
                "r_achieved":       st.column_config.NumberColumn("R",        format="%.3f"),
                "win":              st.column_config.CheckboxColumn("Win"),
                "confluence_score": st.column_config.NumberColumn("Score",    format="%d"),
            }
        )
        st.caption(f"Showing {len(disp)} of {len(df_trades)} trades")

        if len(disp) and "pnl" in disp.columns:
            s1, s2, s3 = st.columns(3)
            s1.metric("Filtered P&L",      f"${disp['pnl'].sum():,.2f}")
            s2.metric("Filtered Win Rate",  f"{(disp['pnl']>0).sum()/len(disp)*100:.1f}%")
            s3.metric("Trades Shown",       len(disp))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB: PERFORMANCE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_perf:
    if not trades or not metrics:
        st.info("Performance charts appear once enough closed trades are recorded.")
    else:
        st.subheader("Monthly P&L")
        st.plotly_chart(chart_monthly_pnl(trades), use_container_width=True, key="monthly")

        p1, p2 = st.columns(2)
        with p1:
            st.subheader("Win / Loss Split")
            st.plotly_chart(
                chart_win_loss_pie(metrics.get("wins",0), metrics.get("losses",0)),
                use_container_width=True, key="pie")
        with p2:
            st.subheader("R-Multiple per Trade")
            st.plotly_chart(chart_r_histogram(trades), use_container_width=True, key="rhist")

        st.subheader(f"Rolling {20}-Trade Win Rate")
        st.plotly_chart(chart_rolling_winrate(trades), use_container_width=True, key="rollwr")

        if journal:
            st.subheader("Session Breakdown")
            df_j = pd.DataFrame(journal)
            if "session" in df_j.columns and "pnl" in df_j.columns:
                df_j["pnl"] = pd.to_numeric(df_j["pnl"], errors="coerce")
                sess = df_j.groupby("session").agg(
                    Trades=("pnl","count"),
                    Wins=("pnl", lambda x:(x>0).sum()),
                    PnL=("pnl","sum"),
                ).reset_index()
                sess["Win Rate"] = (sess["Wins"]/sess["Trades"]*100).round(1).astype(str)+"%"
                sess["PnL"] = sess["PnL"].round(2)
                st.dataframe(sess, use_container_width=True, hide_index=True)

        if trades:
            st.subheader("Symbol Breakdown")
            df_t = pd.DataFrame(trades)
            if "symbol" in df_t.columns and "pnl" in df_t.columns:
                df_t["pnl"] = pd.to_numeric(df_t["pnl"], errors="coerce")
                agg_dict = {"Trades":("pnl","count"), "Wins":("pnl",lambda x:(x>0).sum()), "PnL":("pnl","sum")}
                if "r_achieved" in df_t.columns:
                    agg_dict["Avg R"] = ("r_achieved","mean")
                sym = df_t.groupby("symbol").agg(**agg_dict).reset_index()
                sym["Win Rate"] = (sym["Wins"]/sym["Trades"]*100).round(1).astype(str)+"%"
                sym["PnL"] = sym["PnL"].round(2)
                if "Avg R" in sym.columns:
                    sym["Avg R"] = sym["Avg R"].round(3)
                st.dataframe(sym, use_container_width=True, hide_index=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB: FORWARD TEST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_ft:
    st.subheader("Forward Test Progress")
    ft_trades = ft_data.get("trades", [])
    ft_daily  = ft_data.get("daily_metrics", [])
    TARGETS = {
        "win_rate": 0.75,
        "profit_factor": 1.06,
        "expectancy": 0.20,
        "max_slippage": 1.5,
        "max_drawdown": 15.0,
        "target_trades": 20
    }

    completed = [t for t in ft_trades if t.get("profit_loss") is not None and t.get("exit_time")]
    open_ft   = [t for t in ft_trades if t.get("profit_loss") is None]

    if not ft_trades:
        st.info("No forward test trades recorded yet.")
    else:
        wins_ft   = [t for t in completed if float(t.get("profit_loss",0) or 0)>0]
        losses_ft = [t for t in completed if float(t.get("profit_loss",0) or 0)<=0]
        n_comp    = len(completed)
        wr_ft     = len(wins_ft)/n_comp*100 if n_comp else 0
        pnl_ft    = sum(float(t.get("profit_loss",0) or 0) for t in completed)
        gp_ft     = sum(float(t.get("profit_loss",0)) for t in wins_ft)
        gl_ft     = abs(sum(float(t.get("profit_loss",0)) for t in losses_ft))
        pf_ft     = gp_ft/gl_ft if gl_ft>0 else float("inf")
        exp_ft    = pnl_ft/n_comp if n_comp else 0
        prog_pct  = min(n_comp/TARGETS["target_trades"]*100, 100)

        # Calculate max drawdown dynamically from completed trades
        ft_df_completed = pd.DataFrame(completed)
        if not ft_df_completed.empty and "profit_loss" in ft_df_completed.columns:
            ft_df_completed["profit_loss"] = pd.to_numeric(ft_df_completed["profit_loss"], errors="coerce")
            ft_cum_pnl = ft_df_completed["profit_loss"].cumsum()
            ft_initial_capital = 10000.0
            ft_equity = ft_initial_capital + ft_cum_pnl
            ft_peak = ft_equity.cummax()
            ft_dd = ((ft_equity - ft_peak) / ft_peak * 100).min()
            ft_max_dd = abs(ft_dd)
        else:
            ft_max_dd = 0.0

        st.progress(int(prog_pct),
                    text=f"Progress: {n_comp} / {TARGETS['target_trades']} trades ({prog_pct:.0f}%)")

        f1,f2,f3,f4 = st.columns(4)
        f1.metric("Win Rate",      f"{wr_ft:.1f}%",
                  delta=f"{wr_ft - TARGETS['win_rate']*100:+.1f}% vs {TARGETS['win_rate']*100:.1f}% Target",
                  delta_color="normal")
        f2.metric("Profit Factor", f"{pf_ft:.2f}" if pf_ft!=float("inf") else "∞",
                  delta=f"{pf_ft - TARGETS['profit_factor']:+.2f} vs {TARGETS['profit_factor']:.3f} Target" if pf_ft!=float("inf") else "Target 1.078",
                  delta_color="normal" if pf_ft!=float("inf") else "off")
        f3.metric("Expectancy",    f"${exp_ft:.2f}",
                  delta=f"${exp_ft - TARGETS['expectancy']:+.2f} vs ${TARGETS['expectancy']:.2f} Target",
                  delta_color="normal")
        f4.metric("Total P&L",     f"${pnl_ft:.2f}",
                  delta=f"{len(open_ft)} open positions")

        st.divider()
        col_c, col_t = st.columns([3,2])
        with col_c:
            st.subheader("Per-Trade P&L")
            st.plotly_chart(chart_forward_progress(ft_data), use_container_width=True, key="ft_pnl")
        with col_t:
            st.subheader("Execution Quality")
            avg_sl = sum(float(t.get("slippage_pips",0) or 0) for t in ft_trades)/len(ft_trades) if ft_trades else 0
            max_sl = max((float(t.get("slippage_pips",0) or 0) for t in ft_trades), default=0)
            avg_sp = sum(float(t.get("spread_at_entry",0) or 0) for t in ft_trades)/len(ft_trades) if ft_trades else 0
            e1,e2 = st.columns(2)
            e1.metric("Avg Slippage", f"{avg_sl:.1f} pips")
            e2.metric("Max Slippage", f"{max_sl:.1f} pips")
            e1.metric("Avg Spread",   f"{avg_sp:.1f} pips")

        st.divider()
        
        # Decision logic
        status_trades = n_comp >= TARGETS["target_trades"]
        status_wr = wr_ft >= (TARGETS["win_rate"] * 100)
        status_pf = pf_ft >= TARGETS["profit_factor"]
        status_exp = exp_ft >= TARGETS["expectancy"]
        status_dd = ft_max_dd <= TARGETS["max_drawdown"]
        status_sl = avg_sl <= TARGETS["max_slippage"]
        
        strict_pass = status_trades and status_wr and status_pf and status_exp and status_dd and status_sl
        
        # Conditional pass requires >= 10 trades and 90% target thresholds
        cond_trades = n_comp >= 10
        cond_wr = wr_ft >= (TARGETS["win_rate"] * 0.9 * 100)
        cond_pf = pf_ft >= (TARGETS["profit_factor"] * 0.9)
        cond_exp = exp_ft >= (TARGETS["expectancy"] * 0.9)
        cond_dd = ft_max_dd <= (TARGETS["max_drawdown"] * 1.1)
        cond_sl = avg_sl <= (TARGETS["max_slippage"] * 1.1)
        
        conditional_pass = cond_trades and cond_wr and cond_pf and cond_exp and cond_dd and cond_sl

        if n_comp < 5:
            verdict = "INSUFFICIENT DATA"
            verdict_color = "#8b949e"
            verdict_desc = "Accumulate more forward-test trades before evaluation (minimum 5, target 20)."
        elif strict_pass:
            verdict = "GO (PRODUCTION READY)"
            verdict_color = "#3fb950"
            verdict_desc = "All v2.4 strategy criteria have been fully satisfied. The system is ready for live deployment."
        elif conditional_pass:
            verdict = "CONDITIONAL GO"
            verdict_color = "#d29922"
            verdict_desc = "Core metrics are close to baseline targets (within 90% tolerance). Proceed with caution and reduced size."
        else:
            verdict = "NO-GO (RE-EVALUATE)"
            verdict_color = "#f85149"
            verdict_desc = "One or more safety or performance metrics have breached limits. Strategy requires refinement."
            
        st.markdown(f"""
<div style="background:#161b22; border:1px solid #21262d; border-radius:12px; padding:24px; margin-top:10px; margin-bottom:20px;">
    <h3 style="margin-top:0; color:#e6edf3;">📋 Go/No-Go Decision Report Card</h3>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; padding:16px 20px; background:#0d1117; border-radius:8px; border-left:5px solid {verdict_color};">
        <div>
            <span style="font-size:12px; color:#8b949e; text-transform:uppercase; letter-spacing:1px;">System Recommendation</span>
            <h2 style="margin:4px 0 0 0; color:{verdict_color}; font-weight:700;">{verdict}</h2>
        </div>
        <div style="max-width:60%; text-align:right;">
            <p style="margin:0; font-size:13px; color:#c9d1d9;">{verdict_desc}</p>
        </div>
    </div>
    
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:16px;">
        <!-- Trade Count -->
        <div style="background:#0d1117; padding:12px 16px; border-radius:8px; border:1px solid {'#2ea043' if status_trades else ('#9e6a03' if cond_trades else '#30363d')};">
            <p style="margin:0; font-size:12px; color:#8b949e;">Completed Trades</p>
            <p style="margin:4px 0; font-size:18px; font-weight:700; color:#e6edf3;">{n_comp} / {TARGETS['target_trades']}</p>
            <span style="font-size:11px; color:{'#3fb950' if status_trades else ('#d29922' if cond_trades else '#f85149')};">
                {'✓ Met Target' if status_trades else (f'⚠ Warning (>= {TARGETS["target_trades"] // 2})' if cond_trades else '✗ Under Target')}
            </span>
        </div>
        <!-- Win Rate -->
        <div style="background:#0d1117; padding:12px 16px; border-radius:8px; border:1px solid {'#2ea043' if status_wr else ('#9e6a03' if cond_wr else '#30363d')};">
            <p style="margin:0; font-size:12px; color:#8b949e;">Win Rate (Target: >= {TARGETS['win_rate']*100:.1f}%)</p>
            <p style="margin:4px 0; font-size:18px; font-weight:700; color:#e6edf3;">{wr_ft:.1f}%</p>
            <span style="font-size:11px; color:{'#3fb950' if status_wr else ('#d29922' if cond_wr else '#f85149')};">
                {'✓ Met Target' if status_wr else (f'⚠ Warning (>= {TARGETS["win_rate"]*0.9*100:.1f}%)' if cond_wr else '✗ Under Target')}
            </span>
        </div>
        <!-- Profit Factor -->
        <div style="background:#0d1117; padding:12px 16px; border-radius:8px; border:1px solid {'#2ea043' if status_pf else ('#9e6a03' if cond_pf else '#30363d')};">
            <p style="margin:0; font-size:12px; color:#8b949e;">Profit Factor (Target: >= {TARGETS['profit_factor']:.2f})</p>
            <p style="margin:4px 0; font-size:18px; font-weight:700; color:#e6edf3;">{pf_ft:.2f}</p>
            <span style="font-size:11px; color:{'#3fb950' if status_pf else ('#d29922' if cond_pf else '#f85149')};">
                {'✓ Met Target' if status_pf else (f'⚠ Warning (>= {TARGETS["profit_factor"]*0.9:.2f})' if cond_pf else '✗ Under Target')}
            </span>
        </div>
        <!-- Expectancy -->
        <div style="background:#0d1117; padding:12px 16px; border-radius:8px; border:1px solid {'#2ea043' if status_exp else ('#9e6a03' if cond_exp else '#30363d')};">
            <p style="margin:0; font-size:12px; color:#8b949e;">Expectancy (Target: >= ${TARGETS['expectancy']:.2f})</p>
            <p style="margin:4px 0; font-size:18px; font-weight:700; color:#e6edf3;">${exp_ft:.2f}</p>
            <span style="font-size:11px; color:{'#3fb950' if status_exp else ('#d29922' if cond_exp else '#f85149')};">
                {'✓ Met Target' if status_exp else (f'⚠ Warning (>= ${TARGETS["expectancy"]*0.9:.2f})' if cond_exp else '✗ Under Target')}
            </span>
        </div>
        <!-- Max Drawdown -->
        <div style="background:#0d1117; padding:12px 16px; border-radius:8px; border:1px solid {'#2ea043' if status_dd else ('#9e6a03' if cond_dd else '#30363d')};">
            <p style="margin:0; font-size:12px; color:#8b949e;">Max Drawdown (Target: <= {TARGETS['max_drawdown']:.1f}%)</p>
            <p style="margin:4px 0; font-size:18px; font-weight:700; color:#e6edf3;">{ft_max_dd:.2f}%</p>
            <span style="font-size:11px; color:{'#3fb950' if status_dd else ('#d29922' if cond_dd else '#f85149')};">
                {'✓ Met Target' if status_dd else (f'⚠ Warning (<= {TARGETS["max_drawdown"]*1.1:.1f}%)' if cond_dd else '✗ Over Limit')}
            </span>
        </div>
        <!-- Avg Slippage -->
        <div style="background:#0d1117; padding:12px 16px; border-radius:8px; border:1px solid {'#2ea043' if status_sl else ('#9e6a03' if cond_sl else '#30363d')};">
            <p style="margin:0; font-size:12px; color:#8b949e;">Avg Slippage (Target: <= {TARGETS['max_slippage']:.1f} pips)</p>
            <p style="margin:4px 0; font-size:18px; font-weight:700; color:#e6edf3;">{avg_sl:.2f} pips</p>
            <span style="font-size:11px; color:{'#3fb950' if status_sl else ('#d29922' if cond_sl else '#f85149')};">
                {'✓ Met Target' if status_sl else (f'⚠ Warning (<= {TARGETS["max_slippage"]*1.1:.2f})' if cond_sl else '✗ Over Limit')}
            </span>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

        if ft_trades:
            st.subheader("All Forward Test Trades")
            ft_df = pd.DataFrame(ft_trades)
            show  = [c for c in ["trade_id","direction","entry_time","entry_price",
                                   "exit_price","profit_loss","slippage_pips",
                                   "spread_at_entry","exit_reason"] if c in ft_df.columns]
            st.dataframe(ft_df[show] if show else ft_df, use_container_width=True, hide_index=True,
                         column_config={
                             "profit_loss":   st.column_config.NumberColumn("P&L ($)",  format="$%.2f"),
                             "slippage_pips": st.column_config.NumberColumn("Slippage", format="%.1f pip"),
                         })

        if ft_daily:
            st.subheader("Daily Metrics")
            st.dataframe(pd.DataFrame(ft_daily), use_container_width=True, hide_index=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB: LOGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
with tab_logs:
    st.subheader("Bot Run Log")

    l1, l2, l3 = st.columns([2,1,1])
    search    = l1.text_input("Filter lines", placeholder="e.g. ERROR, BUY, EURUSD…")
    log_level = l2.selectbox("Level", ["All","INFO","WARNING","ERROR"])
    n_lines   = l3.slider("Lines", 20, 300, 100, step=20)

    lines_raw  = load_log_tail(n_lines)
    lines_disp = lines_raw.copy()

    if log_level != "All":
        lines_disp = [l for l in lines_disp if f"[{log_level}]" in l]
    if search:
        lines_disp = [l for l in lines_disp if search.lower() in l.lower()]

    # Colour-coded log (simple approach via code block)
    st.code("\n".join(lines_disp) if lines_disp else "(no matching lines)", language="")
    st.caption(f"{len(lines_disp)} lines shown")

    # Error / warning summary
    all_200   = load_log_tail(200)
    errors    = [l for l in all_200 if "[ERROR]" in l]
    warnings  = [l for l in all_200 if "[WARNING]" in l and "MySQL" not in l]  # ignore MySQL noise

    if errors or warnings:
        st.subheader("Recent Issues")
        lc1, lc2 = st.columns(2)
        lc1.metric("Errors (last 200)",   len(errors),   delta_color="off")
        lc2.metric("Warnings (last 200)", len(warnings), delta_color="off")
        if errors:
            with st.expander(f"Last {min(5,len(errors))} errors"):
                for e in errors[-5:]:
                    st.error(e)
        if warnings:
            with st.expander(f"Last {min(5,len(warnings))} warnings"):
                for w in warnings[-5:]:
                    st.warning(w)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AUTO-REFRESH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if auto_refresh:
    time.sleep(10)
    st.cache_data.clear()
    st.rerun()
