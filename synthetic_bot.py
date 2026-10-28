#!/usr/bin/env python3
"""
QUANTA-SYNTH: Synthetic Indices Autonomous Bot
==============================================
Statistical engine for Deriv MT5 synthetic markets.

Usage:
  python synthetic_bot.py --run
  python synthetic_bot.py --analyze "Volatility 75 Index"
  python synthetic_bot.py --scan
  python synthetic_bot.py --status
  python synthetic_bot.py --backtest "Volatility 75 Index" --bars 1000
  python synthetic_bot.py --math-formulas
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import pandas as pd

from mt5_connection import MT5Connection, MT5Credentials
from quant_brain import CalculativeBrain
from synthetic_bot_system import SyntheticBotConfig, SyntheticBotSystem, resolve_symbol
from synthetic_calibration import ProbabilityCalibrator
from synthetic_strategy import var_risk_level


def setup_logger(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("synthetic-quant-bot")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def load_config() -> SyntheticBotConfig:
    """Read config/config_synthetic.json with priority overrides from environment variables."""
    cfg_path = ROOT / "config" / "config_synthetic.json"
    data = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    rm = data.get("risk_management", {})
    hold = data.get("trade_holding", {})
    defaults = SyntheticBotConfig()

    env_syms = os.getenv("SYMBOLS")
    if env_syms:
        active_symbols = [s.strip() for s in env_syms.split(",") if s.strip()]
    else:
        active_symbols = data.get("active_symbols", defaults.symbols)

    return SyntheticBotConfig(
        symbols=active_symbols,
        timeframe=os.getenv("TIMEFRAME", data.get("timeframe", defaults.timeframe)),
        lookback_bars=int(data.get("lookback_bars", defaults.lookback_bars)),
        poll_seconds=int(os.getenv("POLL_SECONDS", data.get("poll_seconds", defaults.poll_seconds))),
        fast_poll_seconds=int(data.get("fast_poll_seconds", defaults.fast_poll_seconds)),
        slow_poll_seconds=int(data.get("slow_poll_seconds", defaults.slow_poll_seconds)),
        magic=int(os.getenv("BOT_MAGIC", data.get("magic_number", defaults.magic))),
        min_math_edge=float(os.getenv("MIN_MATH_EDGE", data.get("min_math_edge", 60.0))),
        max_risk_pct=float(os.getenv("MAX_RISK_PER_TRADE", rm.get("max_risk_per_trade", defaults.max_risk_pct))),
        kelly_fraction=float(os.getenv("FRACTIONAL_KELLY", rm.get("fractional_kelly_multiplier", defaults.kelly_fraction))),
        max_drawdown_limit=float(rm.get("max_drawdown_limit", defaults.max_drawdown_limit)),
        max_open_trades=int(os.getenv("MAX_OPEN_TRADES", rm.get("max_open_trades", defaults.max_open_trades))),
        be_trigger_r=float(rm.get("be_trigger_r", defaults.be_trigger_r)),
        aggressive_mode=os.getenv("AGGRESSIVE_MODE", str(rm.get("aggressive_mode", defaults.aggressive_mode))).lower() in ("true", "1", "yes"),
        var_emergency_threshold=float(rm.get("var_emergency_threshold", defaults.var_emergency_threshold)),
        kalman_trail_enable=bool(rm.get("kalman_trail_enable", defaults.kalman_trail_enable)),
        spike_harvest_r=float(rm.get("spike_harvest_r", defaults.spike_harvest_r)),
        runner_strategies=list(hold.get("runner_strategies", defaults.runner_strategies)),
        runner_close_fraction=float(hold.get("runner_close_fraction", defaults.runner_close_fraction)),
        runner_lock_r=float(hold.get("runner_lock_r", defaults.runner_lock_r)),
        trail_atr_mult=float(hold.get("trail_atr_mult", defaults.trail_atr_mult)),
        max_hold_hours=float(hold.get("max_hold_hours", defaults.max_hold_hours)),
        thesis_exit_enabled=bool(hold.get("thesis_exit_enabled", defaults.thesis_exit_enabled)),
        extend_targets=bool(hold.get("extend_targets", defaults.extend_targets)),
        pyramid_enabled=bool(hold.get("pyramid_enabled", defaults.pyramid_enabled)),
        pyramid_max_adds=int(hold.get("pyramid_max_adds", defaults.pyramid_max_adds)),
        calibration_enabled=bool(data.get("calibration", {}).get("enabled", defaults.calibration_enabled)),
        calibration_path=str(data.get("calibration", {}).get("path", defaults.calibration_path)),
        brain_params=data.get("calculative_brain", {}),
    )


def get_connection() -> MT5Connection:
    path_val = os.getenv("MT5_PATH") or os.getenv("MT5_TERMINAL_PATH") or None
    creds = MT5Credentials(
        login=int(os.getenv("MT5_LOGIN", 0) or 0),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", "Deriv-Demo"),
        terminal_path=path_val,
        path=path_val,
        portable=False,
        is_demo=True,
    )
    return MT5Connection(creds)


def cmd_analyze(symbol: str, timeframe: str = "M5") -> int:
    """Run the full decomposition on the latest closed bars and print diagnostics."""
    print("=" * 65)
    print(f"🧠 QUANTA-SYNTH CALCULATIVE BRAIN: {symbol} ({timeframe})")
    print("=" * 65)

    cfg = load_config()
    conn = get_connection()
    if not conn.initialize():
        print("❌ Could not connect to MT5. Ensure MT5 is running with Deriv account.")
        return 1

    try:
        resolved = resolve_symbol(conn, symbol)
        rates = conn.get_rates(resolved, timeframe=timeframe, count=cfg.lookback_bars + 1)
        if rates is None or len(rates) < 31:
            print(f"❌ Insufficient candle data received from MT5 for {symbol}.")
            return 1

        df = pd.DataFrame(rates[:-1])  # closed bars only
        analysis = CalculativeBrain.from_config(cfg.brain_params).analyze(df, symbol=resolved, timeframe=timeframe)

        print(f"Current Price:           {analysis.current_price:.5f}")
        print("-" * 65)
        print("📊 1. Fractal Hurst Analysis (Rescaled Range R/S)")
        print(f"  • Hurst Exponent (H):   {analysis.hurst.hurst_exponent:.3f}")
        print(f"  • Fractal Regime:       {analysis.hurst.regime}")
        print(f"  • R-Squared Fit:        {analysis.hurst.r_squared:.3f}")
        print(f"  • Tradable Edge:        {'✅ YES' if analysis.hurst.is_tradable else '❌ RANDOM WALK (STAND ASIDE)'}")

        print("\n📈 2. Ornstein-Uhlenbeck (O-U) Mean Reversion")
        print(f"  • Reversion Speed (θ):  {analysis.ou.theta:.5f}")
        print(f"  • Equilibrium Level (μ):{analysis.ou.mu:.5f}")
        print(f"  • Half-Life (τ):        {analysis.ou.half_life:.1f} bars")
        print(f"  • Current Z-Score:      {analysis.ou.current_zscore:+.2f}σ")
        print(f"  • ADF t-statistic:      {analysis.ou.adf_tstat:+.2f} (stationary below -2.89)")
        print(f"  • Stat Reversion Edge:  {'✅ ACTIVE' if analysis.ou.is_reverting else '❌ INACTIVE'}")

        print("\n⚡ 3. Volatility & Information Entropy")
        print(f"  • Garman-Klass Vol:     {analysis.garman_klass_vol:.5f} per bar")
        print(f"  • Shannon Entropy:      {analysis.entropy.normalized_entropy:.3f} / 1.000")
        print(f"  • Flow Coherence:       {'✅ ORDERED / PREDICTABLE' if analysis.entropy.is_ordered else '❌ DISORDERED'}")

        if analysis.poisson:
            ph = analysis.poisson
            clustering = f"YES (η={ph.branching_ratio:.2f})" if ph.branching_ratio > 0 else "No (memoryless)"
            print("\n💥 4. Poisson / Hawkes Spike Model (Boom/Crash)")
            print(f"  • Spikes in Window:      {ph.spike_count} (base rate {ph.base_rate:.3f}/bar)")
            print(f"  • Bars Since Last Spike: {ph.bars_since_spike}")
            print(f"  • Spike Probability (5b):{ph.spike_prob_next_k:.1%}")
            print(f"  • Clustering Detected:   {clustering}")
            print(f"  • Drift Harvest:         p={ph.drift_win_prob:.1%}, E={ph.drift_expectancy_r:+.2f}R")
            print(f"  • Spike Catch:           p={ph.spike_win_prob:.1%}, E={ph.spike_expectancy_r:+.2f}R")

        if analysis.step_analysis:
            print("\n🎲 5. Step Index Bernoulli Runs & Markov Engine")
            print(f"  • Current Streak:        {analysis.step_analysis.current_streak_length} bars ({analysis.step_analysis.current_streak_direction})")
            print(f"  • Empirical P(continue): {analysis.step_analysis.streak_continuation_prob:.1%}")
            print(f"  • Runs Test Z-Score:     {analysis.step_analysis.runs_zscore:+.2f} (p={analysis.step_analysis.p_value:.3f})")
            print(f"  • Markov P(Up|Up):       {analysis.step_analysis.markov_p_up_given_up:.1%}")
            print(f"  • Markov P(Down|Down):   {analysis.step_analysis.markov_p_down_given_down:.1%}")
            print(f"  • Significant Alternation: {'🎯 YES (REVERSAL EDGE)' if analysis.step_analysis.is_statistically_stretched else 'No'}")

        print("\n🔬 6. Kalman State-Space Filter")
        print(f"  • Kalman Fair Value:     {analysis.kalman_fair_value:.5f}")
        print(f"  • Kalman Residual Z:     {analysis.kalman_zscore:+.3f}σ")
        dev_pct = ((analysis.current_price - analysis.kalman_fair_value) / analysis.current_price * 100) if analysis.current_price > 0 else 0
        print(f"  • Price Deviation:       {dev_pct:+.3f}%")

        print("\n📉 7. Tail Risk: VaR & CVaR")
        print(f"  • VaR 95% (5-bar):       {analysis.var_95_pct:.3f}%")
        print(f"  • CVaR / Exp Shortfall:  {analysis.cvar_95_pct:.3f}%")
        print(f"  • Risk Tier:             {var_risk_level(analysis.var_95_pct)[0]}")

        print("\n📐 8. Performance Metrics")
        print(f"  • Instantaneous Sharpe:  {analysis.instantaneous_sharpe:+.3f}")
        print(f"  • Strategy Archetype:    {analysis.strategy_archetype}")

        print("-" * 65)
        print(f"🎯 QUANTITATIVE VERDICT:  {analysis.recommended_action}")
        print(f"  • Composite Edge Score: {analysis.math_edge_score}/100.0")
        print(f"  • Win Probability:      {analysis.calculated_win_probability:.1%}")
        if analysis.recommended_action != "STAND_ASIDE":
            print(f"  • Invalidation (SL):    {analysis.invalidation_price:.5f}")
            print(f"  • Optimal Target (TP1): {analysis.target_price:.5f}")
            print(f"  • Runner Target (TP2):  {analysis.target_price_2:.5f}")
            print(f"  • Holding Horizon:      {analysis.half_life_horizon_bars} bars")
            print(f"  • Expected Yield:       {analysis.expected_yield_r:+.2f}R")
        print("=" * 65)
        return 0
    finally:
        conn.shutdown()


def cmd_math_formulas() -> int:
    """Display the exact mathematical formulas governing Deriv Synthetic Indices."""
    print("=" * 75)
    print("🧠 MATHEMATICAL MODELS USED BY THE QUANTA-SYNTH BRAIN")
    print("=" * 75)
    print("""
1. GEOMETRIC BROWNIAN MOTION (VOLATILITY INDICES: V10, V25, V50, V75, V100):
   • Stochastic Differential Equation:
     dS_t = mu * S_t dt + sigma * S_t dW_t
   • Ornstein-Uhlenbeck (O-U) Microstructure Reversion:
     dX_t = theta * (mu - X_t) dt + sigma * dW_t
   • Stationary Fokker-Planck Density:
     p_inf(x) = sqrt(theta / (pi * sigma^2)) * exp( -theta * (x - mu)^2 / sigma^2 )
   • Reversion is only accepted when the Dickey-Fuller t-stat of the AR(1) fit < -2.89
   • P(hit mu before stop | z0) = (s(z_stop) - s(z0)) / (s(z_stop) - s(0)),
     s(z) = integral_0^z exp(u^2/2) du   (O-U scale function)
   • Half-life: tau_half = ln(2) / theta

2. COMPOUND POISSON-HAWKES JUMP PROCESS (BOOM & CRASH INDICES):
   • Jump-Diffusion SDE:
     dS_t = mu_drift * S_t dt + sigma * S_t dW_t - J_t dN_t  (Crash)
     dS_t = mu_drift * S_t dt + sigma * S_t dW_t + J_t dN_t  (Boom)
   • Self-Excited Hawkes Intensity lambda(t):
     lambda(t) = mu_0 + sum_{t_i < t} alpha * exp(-beta * (t - t_i))
   • Conditional Jump Probability in Horizon Delta_t:
     P(T <= t + Delta_t | T > t) = 1 - exp(-int_t^{t+Delta_t} lambda(s) ds)
   • Clustering (alpha > 0) is only used when spikes 1-4 bars after a spike are
     significantly more frequent than the base rate (Poisson test, 5%).
     Deriv documents these spikes as random, so normally alpha = 0.

3. DISCRETE BERNOULLI MARKOV STEPS (STEP INDEX):
   • Step Movement: S_{t+1} = S_t + delta * B_t, where B_t in {-1, +1}, P(B_t = +1) = 0.5
   • Steps are independent: P(next step up) = 0.5 whatever the current streak length.
     A long streak does not make a reversal more likely.
   • Wald-Wolfowitz runs test: trade reversals only if runs Z >= +1.96 (moves alternate)
   • 2-State Markov Transition Matrix (Laplace-smoothed, estimated from the window):
     P = [[P(U|U), P(D|U)], [P(U|D), P(D|D)]]

4. KALMAN STATE-SPACE FILTER:
   • State Equation:  x_k = x_{k-1} + w_k,  w_k ~ N(0, Q)
   • Measurement:     z_k = x_k + v_k,       v_k ~ N(0, R)
   • Kalman Gain:     K_k = P_{k|k-1} / (P_{k|k-1} + R)
   • Z = latest residual / std(residuals); shown for context, not used for scoring.

5. VALUE-AT-RISK & CONDITIONAL VaR:
   • VaR_alpha = -(mu_h - z_alpha * sigma_h)
   • CVaR_alpha = -(mu_h - sigma_h * phi(z_alpha) / (1 - alpha))
   • Dynamic risk scaling: Reduce position size in elevated/extreme VaR regimes.

6. FRACTIONAL KELLY POSITION SIZING:
   • Logarithmic Utility Growth:
     g(f) = E[ln(1 + f * R)]
   • Kelly fraction for win b*R / loss L*R:
     f* = p / L - (1 - p) / b
     f_trade = clip(f* * kelly_multiplier(EdgeScore), 0.2%, max_risk_per_trade)
   • Compounding Capital Law:
     W_T = W_0 * prod_{i=1}^T (1 + f_i * R_i)
   • Fractional Kelly lowers ruin risk but does not remove it; it is only as good
     as the win probability fed into it.
    """)
    print("=" * 75)
    return 0


def cmd_scan(timeframe: str = "M5") -> int:
    """Analyze every configured symbol and rank by edge score."""
    cfg = load_config()
    print("=" * 75)
    print(f"⚡ QUANTA-SYNTH MULTI-MARKET SCAN ({timeframe})")
    print("=" * 75)

    conn = get_connection()
    is_live = conn.initialize()
    if not is_live:
        print("🟡 MT5 offline: rows marked [SIM] use simulated data and are NOT tradable signals.")

    from synthetic_backtester import generate_synthetic_market_data
    brain = CalculativeBrain.from_config(cfg.brain_params)

    results = []
    simulated = set()
    for sym in cfg.symbols:
        df = None
        if is_live:
            try:
                resolved = resolve_symbol(conn, sym)
                rates = conn.get_rates(resolved, timeframe=timeframe, count=cfg.lookback_bars + 1)
                if rates is not None and len(rates) >= 31:
                    df = pd.DataFrame(rates[:-1])
            except Exception:
                df = None

        if df is None:
            simulated.add(sym)
            df = generate_synthetic_market_data(symbol=sym, n_bars=cfg.lookback_bars, seed=sum(map(ord, sym)))

        analysis = brain.analyze(df, symbol=sym, timeframe=timeframe)
        results.append(analysis)

    if is_live:
        conn.shutdown()

    # Sort by math edge score descending
    results.sort(key=lambda a: a.math_edge_score, reverse=True)

    print(f"{'SYMBOL':<26} | {'ACTION':<14} | {'EDGE':<6} | {'WIN':<5} | {'R:R':<5} | {'VaR%':<5} | {'ARCHETYPE'}")
    print("-" * 90)
    for a in results:
        rr = round(abs(a.target_price - a.current_price) / max(abs(a.current_price - a.invalidation_price), 1e-4), 1) if a.recommended_action != 'STAND_ASIDE' else 0.0
        strat = getattr(a, 'strategy_archetype', 'N/A')
        act_str = f"🟢 {a.recommended_action}" if a.recommended_action == "SNIPE_BUY" else (f"🔴 {a.recommended_action}" if a.recommended_action == "SNIPE_SELL" else f"⚪ {a.recommended_action}")
        name = f"[SIM] {a.symbol}" if a.symbol in simulated else a.symbol
        print(f"{name:<26} | {act_str:<14} | {a.math_edge_score:>5.1f} | {a.calculated_win_probability:>4.0%} | {rr:>4.1f} | {a.var_95_pct:>4.1f} | {strat}")

    print("=" * 90)
    top = results[0]
    print(f"🏆 HIGHEST CONVICTION: {top.symbol} (Edge={top.math_edge_score:.1f}, {top.recommended_action}, VaR={top.var_95_pct:.2f}%)")
    print("=" * 90)
    return 0

def cmd_status() -> int:
    """Print current synthetic open positions and performance metrics."""
    cfg = load_config()
    print("=" * 65)
    print("🤖 QUANTA-SYNTH BOT STATUS")
    print("=" * 65)

    path = Path(cfg.state_path)
    if not path.exists():
        print("No synthetic trade state recorded yet.")
        print("=" * 65)
        return 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        open_trades = data.get("open_trades", {})
        closed_trades = data.get("closed_trades", [])
        cycle_count = data.get("cycle_count", 0)

        print(f"Active Positions:  {len(open_trades)}")
        print(f"Cycle Count:       {cycle_count}")
        for ticket, t in open_trades.items():
            kalman = t.get("kalman_confirmation", "N/A")
            var_lvl = t.get("var_risk_level", "N/A")
            print(
                f"  - Ticket {ticket}: {t.get('symbol')} {t.get('direction')} "
                f"Vol={t.get('volume')} Entry={t.get('entry_price')} SL={t.get('stop_loss')} TP={t.get('take_profit')} "
                f"Strategy={t.get('strategy_type')} Kalman={kalman} VaR={var_lvl}"
            )

        print(f"\nHistorical Trades: {len(closed_trades)}")
        print(f"Last State Sync:   {data.get('updated_at', 'N/A')}")
    except Exception as exc:
        print(f"Error reading state: {exc}")
        return 1

    calibration = ProbabilityCalibrator(cfg.calibration_path).summary()
    if calibration:
        print("\nSelf-calibration (claimed vs realised win rate per strategy):")
        for arch, s in calibration.items():
            print(f"  - {arch:<28} trades={s['trades']:<4} claimed={s['mean_claimed']:.0%} "
                  f"realised={s['realised_win_rate']:.0%} → factor {s['factor']:.2f}")
    print("=" * 65)
    return 0


def run_cloud_surveillance_loop(cfg: SyntheticBotConfig, logger: logging.Logger) -> int:
    """Continuous 24/7 autonomous quantitative surveillance for cloud environments (Linux / Dokploy)."""
    from datetime import datetime, timezone
    from deriv_web_feed import get_deriv_live_candles
    from synthetic_backtester import generate_synthetic_market_data
    from synthetic_strategy import SyntheticStrategy
    from synthetic_risk_manager import SyntheticRiskManager
    from telegram_notifier import build_telegram_config_from_env, TelegramNotifier
    from trade_storage import TradeStorageManager

    brain = CalculativeBrain.from_config(cfg.brain_params)
    if cfg.calibration_enabled:
        brain.calibrator = ProbabilityCalibrator(cfg.calibration_path)
    risk_mgr = SyntheticRiskManager(
        max_risk_pct=cfg.max_risk_pct,
        kelly_fraction=cfg.kelly_fraction,
        max_drawdown_limit=cfg.max_drawdown_limit,
        max_open_positions=cfg.max_open_trades,
    )
    strategy = SyntheticStrategy(
        brain=brain,
        risk_manager=risk_mgr,
        min_math_edge_score=cfg.min_math_edge,
        kalman_veto_contradictions=True,
    )

    telegram_cfg = build_telegram_config_from_env(os.getenv)
    notifier = TelegramNotifier(telegram_cfg) if telegram_cfg.enabled else None

    state_path = Path(cfg.state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    import psutil
    stop_flag = ROOT / "data" / "engine_manual_stop.flag"

    # Guard against multiple engine instances running concurrently
    pid_file = ROOT / "data" / "synthetic_bot.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            if old_pid != os.getpid() and psutil.pid_exists(old_pid):
                try:
                    old_proc = psutil.Process(old_pid)
                    old_cmd = " ".join(old_proc.cmdline()).lower()
                    if "synthetic_bot.py" in old_cmd:
                        logger.warning("⚠️ Another QUANTA-SYNTH instance (PID %d) is already active. Exiting duplicate process.", old_pid)
                        return 0
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    storage = TradeStorageManager(state_file=state_path)
    init_state = storage.get_all_state()
    open_trades: Dict[str, Any] = init_state.get("open_trades", {})
    equity = float(init_state.get("equity", 10.0))
    balance = float(init_state.get("balance", 10.0))

    cycle_count = 0
    if notifier:
        notifier._send_message("⚡ QUANTA-SYNTH Cloud Engine started on Dokploy! 24/7 Autonomous surveillance active.")

    logger.info("⚡ 24/7 Cloud Surveillance ACTIVE (PID %d). Monitoring %d instruments. Press Ctrl+C to stop.", os.getpid(), len(cfg.symbols))

    try:
        while True:
            if stop_flag.exists():
                logger.info("🛑 [MANUAL STOP] engine_manual_stop.flag detected. Shutting down surveillance engine cleanly.")
                break
            cycle_count += 1
            # Real-time state synchronization with MongoDB / Dashboard
            live_state = storage.get_all_state()
            open_trades = live_state.get("open_trades", {})

            # Clean up any open trades whose symbol is no longer in active_symbols or duplicate positions
            seen_symbols = set()
            for d_ticket in list(open_trades.keys()):
                d_t = open_trades.get(d_ticket)
                if not d_t:
                    continue
                d_sym = d_t.get("symbol", "")

                # 1. Close if symbol is not in active symbols (e.g. Volatility 100 on micro account)
                if d_sym not in cfg.symbols:
                    entry_p_del = float(d_t.get("entry_price", 0.0))
                    storage.close_trade(d_ticket, exit_price=entry_p_del, pnl_usd=0.0, pnl_r=0.0, close_reason="DECOMMISSIONED_SYMBOL_REMOVED")
                    if d_ticket in open_trades:
                        del open_trades[d_ticket]
                    logger.info("🗑️ [CLEANUP] Closed position #%s on removed symbol '%s'", d_ticket, d_sym)
                    continue

                # 2. Close duplicates if more than 1 trade exists on the same symbol!
                if d_sym in seen_symbols:
                    entry_p_del = float(d_t.get("entry_price", 0.0))
                    storage.close_trade(d_ticket, exit_price=entry_p_del, pnl_usd=0.0, pnl_r=0.0, close_reason="DUPLICATE_POSITION_PURGED")
                    if d_ticket in open_trades:
                        del open_trades[d_ticket]
                    logger.info("🗑️ [DUPLICATE PURGE] Closed stacked position #%s on '%s'", d_ticket, d_sym)
                else:
                    seen_symbols.add(d_sym)
            equity = float(live_state.get("equity", equity))
            balance = float(live_state.get("balance", balance))
            candidate_signals = []
            for sym in cfg.symbols:
                # 1. Fetch live Deriv WebSocket market candles
                df = get_deriv_live_candles(sym, cfg.timeframe, cfg.lookback_bars)
                if df is None or len(df) < 30:
                    df = generate_synthetic_market_data(symbol=sym, n_bars=cfg.lookback_bars, seed=(hash(sym) + cycle_count) % 100000)

                curr_p = float(df["close"].iloc[-1])
                high_p = float(df["high"].iloc[-1]) if "high" in df else curr_p
                low_p = float(df["low"].iloc[-1]) if "low" in df else curr_p

                # 2. Position lifecycle management for open trades on this symbol
                for ticket in list(open_trades.keys()):
                    t = open_trades.get(ticket)
                    if not t or t.get("symbol") != sym:
                        continue

                    direction = t.get("direction", "BUY")
                    entry_p = float(t.get("entry_price", curr_p))
                    sl_p = float(t.get("stop_loss", curr_p))
                    tp_p = float(t.get("take_profit", curr_p))
                    r_dist = max(abs(entry_p - sl_p), 1e-4)
                    vol = float(t.get("volume", 0.1))

                    closed = False
                    reason = ""
                    exit_price = curr_p

                    # Peak R tracking for dynamic TP and trailing profit lock
                    curr_pts = (curr_p - entry_p) if direction == "BUY" else (entry_p - curr_p)
                    high_pts = (high_p - entry_p) if direction == "BUY" else (entry_p - low_p)
                    curr_r = curr_pts / r_dist
                    peak_r = max(float(t.get("peak_r", 0.0)), round(high_pts / r_dist, 2))
                    t["peak_r"] = peak_r

                    if direction == "BUY":
                        # 1. Full Take Profit hit
                        if curr_p >= tp_p or high_p >= tp_p:
                            closed = True
                            reason = "TAKE_PROFIT"
                            exit_price = tp_p
                        # 2. Stop Loss hit
                        elif curr_p <= sl_p or low_p <= sl_p:
                            closed = True
                            reason = "STOP_LOSS" if not t.get("moved_to_be") else "TRAILING_STOP"
                            exit_price = sl_p
                        # 3. Dynamic Take Profit: bank profit when trade achieved >= 1.75R peak and retraces by >= 0.45R (locking in >= +1.30R banked cash)
                        elif peak_r >= 1.75 and (peak_r - curr_r) >= 0.45:
                            closed = True
                            reason = "DYNAMIC_TAKE_PROFIT"
                            exit_price = curr_p
                        # 4. Dynamic Runner Trailing at +1.75R and beyond: trail SL dynamically at peak_r - 0.45R
                        elif peak_r >= 1.75:
                            trail_sl = round(entry_p + (peak_r - 0.45) * r_dist, 5)
                            if sl_p < trail_sl:
                                t["stop_loss"] = trail_sl
                                t["moved_to_be"] = True
                                storage.save_open_trade(ticket, t)
                                logger.info("🚀 [RUNNER TRAIL] Ticket %s trailed SL to +%.2fR @ %.5f", ticket, peak_r - 0.45, trail_sl)
                        # 5. Guaranteed Profit Lock at +1.30R: guarantee +0.75R cash
                        elif (curr_p >= entry_p + 1.30 * r_dist or high_p >= entry_p + 1.30 * r_dist):
                            lock_sl = round(entry_p + 0.75 * r_dist, 5)
                            if sl_p < lock_sl:
                                t["stop_loss"] = lock_sl
                                t["moved_to_be"] = True
                                storage.save_open_trade(ticket, t)
                                logger.info("💰 [PROFIT LOCK] Ticket %s locked +0.75R profit @ %.5f", ticket, lock_sl)
                        # 6. Fast Risk Neutralization at +0.80R: move SL to entry + 0.05R buffer
                        elif (curr_p >= entry_p + 0.80 * r_dist or high_p >= entry_p + 0.80 * r_dist) and not t.get("moved_to_be"):
                            t["stop_loss"] = round(entry_p + 0.05 * r_dist, 5)
                            t["moved_to_be"] = True
                            storage.save_open_trade(ticket, t)
                            logger.info("🛡️ [BE LOCK] Ticket %s moved to Breakeven+buffer @ %.5f (+0.80R trigger)", ticket, t["stop_loss"])
                    else:
                        # SELL DIRECTION
                        # 1. Full Take Profit hit
                        if curr_p <= tp_p or low_p <= tp_p:
                            closed = True
                            reason = "TAKE_PROFIT"
                            exit_price = tp_p
                        # 2. Stop Loss hit
                        elif curr_p >= sl_p or high_p >= sl_p:
                            closed = True
                            reason = "STOP_LOSS" if not t.get("moved_to_be") else "TRAILING_STOP"
                            exit_price = sl_p
                        # 3. Dynamic Take Profit: bank profit when trade achieved >= 1.75R peak and retraces by >= 0.45R (locking in >= +1.30R banked cash)
                        elif peak_r >= 1.75 and (peak_r - curr_r) >= 0.45:
                            closed = True
                            reason = "DYNAMIC_TAKE_PROFIT"
                            exit_price = curr_p
                        # 4. Dynamic Runner Trailing at +1.75R and beyond: trail SL dynamically at peak_r - 0.45R
                        elif peak_r >= 1.75:
                            trail_sl = round(entry_p - (peak_r - 0.45) * r_dist, 5)
                            if sl_p > trail_sl:
                                t["stop_loss"] = trail_sl
                                t["moved_to_be"] = True
                                storage.save_open_trade(ticket, t)
                                logger.info("🚀 [RUNNER TRAIL] Ticket %s trailed SL to +%.2fR @ %.5f", ticket, peak_r - 0.45, trail_sl)
                        # 5. Guaranteed Profit Lock at +1.30R: guarantee +0.75R cash
                        elif (curr_p <= entry_p - 1.30 * r_dist or low_p <= entry_p - 1.30 * r_dist):
                            lock_sl = round(entry_p - 0.75 * r_dist, 5)
                            if sl_p > lock_sl:
                                t["stop_loss"] = lock_sl
                                t["moved_to_be"] = True
                                storage.save_open_trade(ticket, t)
                                logger.info("💰 [PROFIT LOCK] Ticket %s locked +0.75R profit @ %.5f", ticket, lock_sl)
                        # 6. Fast Risk Neutralization at +0.80R: move SL to entry - 0.05R buffer
                        elif (curr_p <= entry_p - 0.80 * r_dist or low_p <= entry_p - 0.80 * r_dist) and not t.get("moved_to_be"):
                            t["stop_loss"] = round(entry_p - 0.05 * r_dist, 5)
                            t["moved_to_be"] = True
                            storage.save_open_trade(ticket, t)
                            logger.info("🛡️ [BE LOCK] Ticket %s moved to Breakeven+buffer @ %.5f (+0.80R trigger)", ticket, t["stop_loss"])

                    if closed:
                        pnl_points = (exit_price - entry_p) if direction == "BUY" else (entry_p - exit_price)
                        pnl_r = round(pnl_points / r_dist, 2)
                        # Exact mathematical dollar PnL based on calibrated risk
                        trade_risk_dollars = float(t.get("risk_dollars", 0.0))
                        if trade_risk_dollars <= 0.0:
                            trade_risk_dollars = min(equity * 0.04, 0.45) if equity <= 50.0 else max(r_dist * vol, 0.50)
                        pnl_usd = round(pnl_r * trade_risk_dollars, 2)
                        equity = round(equity + pnl_usd, 2)
                        balance = round(balance + pnl_usd, 2)

                        storage.close_trade(
                            ticket=ticket,
                            exit_price=round(exit_price, 5),
                            pnl_usd=pnl_usd,
                            pnl_r=pnl_r,
                            close_reason=reason,
                        )
                        if brain.calibrator:
                            brain.calibrator.record(
                                archetype=t.get("strategy_type", "FRACTAL_MOMENTUM"),
                                claimed_p=float(t.get("win_probability", 0.60)),
                                won=(pnl_usd > 0),
                            )
                        if ticket in open_trades:
                            del open_trades[ticket]
                        logger.info("🏁 [TRADE CLOSED] #%s %s %s -> %s | PnL: %+.2fR ($%+.2f) | Equity: $%.2f",
                                    ticket, direction, sym, reason, pnl_r, pnl_usd, equity)

                # 3. Quantitative Signal Evaluation (Primary M5 with M1 Multi-Timeframe Scan)
                # Micro-account gate: skip instruments whose minimum lot exceeds safe risk
                is_tradable, rej_msg = risk_mgr.is_symbol_tradable_for_account(sym, equity)
                if not is_tradable:
                    continue

                signal = strategy.evaluate(df, symbol=sym, account_balance=equity, timeframe=cfg.timeframe)
                if not signal or not signal.sizing or not signal.sizing.is_valid:
                    # Multi-Timeframe High-Yield Scan: evaluate M1 for rapid micro-scalps
                    df_m1 = get_deriv_live_candles(sym, "M1", cfg.lookback_bars)
                    if df_m1 is not None and len(df_m1) >= 40:
                        m1_signal = strategy.evaluate(df_m1, symbol=sym, account_balance=equity, timeframe="M1")
                        if m1_signal and m1_signal.sizing and m1_signal.sizing.is_valid and m1_signal.calculated_win_prob >= 0.58:
                            signal = m1_signal
                if signal and signal.sizing and signal.sizing.is_valid:
                    # STRICT RULE 1: Maximum 1 open position per symbol! Never stack duplicate trades on the same market
                    existing_sym_trades = [t for t in open_trades.values() if t.get("symbol") == signal.symbol]
                    if len(existing_sym_trades) >= 1:
                        continue

                    # STRICT RULE 2: Minimum 55% win probability floor
                    if signal.calculated_win_prob < 0.55:
                        continue

                    candidate_signals.append(signal)

            # STAGGERED ENTRY THROTTLE: Open at most 1 trade per cycle (highest edge first)
            max_allowed_trades = 2 if equity <= 50.0 else cfg.max_open_trades
            if candidate_signals and len(open_trades) < max_allowed_trades:
                candidate_signals.sort(key=lambda s: (s.math_edge_score * s.calculated_win_prob), reverse=True)
                best_signal = candidate_signals[0]

                ticket = str(900000 + (cycle_count * 100 + len(open_trades)) % 100000)
                if ticket not in open_trades:
                    new_trade_doc = {
                        "ticket": ticket,
                        "symbol": best_signal.symbol,
                        "direction": best_signal.direction,
                        "volume": best_signal.sizing.volume,
                        "risk_dollars": round(best_signal.sizing.risk_dollars, 2),
                        "entry_price": round(best_signal.entry_price, 5),
                        "stop_loss": round(best_signal.stop_loss, 5),
                        "take_profit": round(best_signal.take_profit, 5),
                        "strategy_type": best_signal.strategy_type,
                        "math_edge_score": round(best_signal.math_edge_score, 1),
                        "win_probability": round(best_signal.calculated_win_prob, 3),
                        "kalman_confirmation": best_signal.kalman_confirmation,
                        "var_risk_level": best_signal.var_risk_level,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "moved_to_be": False,
                        "tp1_reached": False,
                    }
                    open_trades[ticket] = new_trade_doc
                    storage.save_open_trade(ticket, new_trade_doc)
                    logger.info("🎯 [CLOUD SIGNAL] %s %s | Edge: %.1f%% | Vol: %.3f | Risk: $%.2f | Ticket: %s",
                                best_signal.symbol, best_signal.direction, best_signal.math_edge_score,
                                best_signal.sizing.volume, best_signal.sizing.risk_dollars, ticket)
                    if notifier:
                        notifier.send_trade_execution({
                            "direction": best_signal.direction,
                            "symbol": best_signal.symbol,
                            "entry_price": str(best_signal.entry_price),
                            "stop_loss": str(best_signal.stop_loss),
                            "take_profit": str(best_signal.take_profit),
                            "math_edge": f"{best_signal.math_edge_score:.1f}%",
                            "win_prob": f"{best_signal.calculated_win_prob:.1%}",
                            "strategy": best_signal.strategy_type,
                        })

            storage.update_portfolio_state(equity=equity, balance=balance, mode="CLOUD_AUTONOMOUS_LIVE_WEB")
            if cycle_count % 5 == 1:
                logger.info("⚡ [SURVEILLANCE HEARTBEAT] Cycle %d | Open Pos: %d | Equity: $%.2f | Monitored: %d instruments",
                            cycle_count, len(open_trades), equity, len(cfg.symbols))
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        logger.info("Cloud surveillance engine stopped cleanly.")
    finally:
        pid_file.unlink(missing_ok=True)
    return 0


def cmd_run() -> int:
    """Run the 24/7 surveillance and execution loop with adaptive polling."""
    logger = setup_logger()
    cfg = load_config()

    logger.info("Initializing QUANTA-SYNTH Autonomous Engine...")
    logger.info("Active Synthetic Symbols: %s", ", ".join(cfg.symbols))
    logger.info("Timeframe: %s | Base Poll: %ds | Fast: %ds | Slow: %ds",
                cfg.timeframe, cfg.poll_seconds, cfg.fast_poll_seconds, cfg.slow_poll_seconds)
    logger.info("Kalman Trailing: %s | VaR Emergency Threshold: %.1f%%",
                "ENABLED" if cfg.kalman_trail_enable else "DISABLED", cfg.var_emergency_threshold)

    conn = get_connection()
    if conn.initialize():
        bot = SyntheticBotSystem(conn, cfg)
        logger.info("⚡ Live MT5 Surveillance ACTIVE (24/7/365). Press Ctrl+C to stop.")
        try:
            while True:
                bot.run_cycle()
                time.sleep(bot.get_adaptive_poll_interval())
        except KeyboardInterrupt:
            logger.info("Received termination signal. Shutting down cleanly...")
        finally:
            conn.shutdown()
            logger.info("MT5 connection closed.")
        return 0

    logger.warning("MT5 desktop terminal not found on this host (Linux container / cloud environment).")
    logger.info("⚡ Starting 24/7 CLOUD QUANTITATIVE SURVEILLANCE & PAPER TRADING ENGINE.")
    logger.info("Calculative Brain will continuously scan all 14 markets, compute fractional Kelly sizes, and dispatch alerts to Telegram.")
    return run_cloud_surveillance_loop(cfg, logger)


def cmd_edge_scan(bars: int = 20000, timeframe: str = "M5") -> int:
    """Test real MT5 history of every configured symbol for deviations from fair randomness."""
    from edge_scanner import scan
    from synthetic_backtester import generate_synthetic_market_data

    cfg = load_config()
    print("=" * 96)
    print(f"🔎 EDGE SCAN: {bars} {timeframe} bars per symbol, false-discovery rate 5%")
    print("=" * 96)

    conn = get_connection()
    is_live = conn.initialize()
    frames, simulated = {}, set()
    for sym in cfg.symbols:
        df = None
        if is_live:
            resolved = resolve_symbol(conn, sym)
            rates = conn.get_rates(resolved, timeframe=timeframe, count=bars + 1)
            if rates is not None and len(rates) > 200:
                df = pd.DataFrame(rates[:-1])
        if df is None:
            simulated.add(sym)
            df = generate_synthetic_market_data(symbol=sym, n_bars=bars, seed=sum(map(ord, sym)))
        frames[sym] = df
    if is_live:
        conn.shutdown()
    if simulated:
        print(f"🟡 No MT5 history for {len(simulated)} symbol(s); scanned simulated data instead [SIM].")
        print("   Simulated data is fair by construction, so it should show no edge.\n")

    results = scan(frames)
    print(f"{'SYMBOL':<30} {'TEST':<18} {'STAT':>8} {'P':>9} {'Q (FDR)':>9}  FINDING")
    print("-" * 96)
    for t in results:
        name = f"[SIM] {t.symbol}" if t.symbol in simulated else t.symbol
        flag = "⚡ " if t.significant else "   "
        print(f"{name:<30} {t.test:<18} {t.statistic:>8.2f} {t.p_value:>9.2g} {t.q_value:>9.2g}  {flag}{t.interpretation}")

    found = [t for t in results if t.significant and t.symbol not in simulated]
    print("=" * 96)
    if found:
        print(f"⚡ {len(found)} real deviation(s) survive the false-discovery correction:")
        for t in found:
            print(f"   {t.symbol}: {t.interpretation}")
        print("   Confirm on a second, later sample before trading it, and check the effect beats the spread.")
    else:
        print("No real deviation from fair randomness found. The bot's best play is to stay selective.")

    out = ROOT / "data" / "edge_scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([t.as_dict() for t in results], indent=2), encoding="utf-8")
    print(f"Saved: {out}")
    return 0


def cmd_backtest(symbol: str = "Volatility 75 Index", bars: int = 1000, spread: float = 0.0) -> int:
    """Run a bar-by-bar backtest on simulated data, charging `spread` per trade."""
    from synthetic_backtester import SyntheticBacktestEngine, generate_synthetic_market_data
    cfg = load_config()
    df = generate_synthetic_market_data(symbol=symbol, n_bars=bars)
    engine = SyntheticBacktestEngine(
        brain=CalculativeBrain.from_config(cfg.brain_params),
        min_math_edge_score=cfg.min_math_edge,
        lookback_bars=cfg.lookback_bars,
        be_trigger_r=cfg.be_trigger_r,
        spike_harvest_r=cfg.spike_harvest_r,
        runner_strategies=cfg.runner_strategies,
        runner_close_fraction=cfg.runner_close_fraction,
        runner_lock_r=cfg.runner_lock_r,
        trail_atr_mult=cfg.trail_atr_mult,
        max_hold_hours=cfg.max_hold_hours,
        spread=spread,
        thesis_exit_enabled=cfg.thesis_exit_enabled,
        extend_targets=cfg.extend_targets,
        pyramid_enabled=cfg.pyramid_enabled,
        pyramid_max_adds=cfg.pyramid_max_adds,
    )
    rep = engine.run(df=df, symbol=symbol)
    print(rep.summary_text())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QUANTA-SYNTH: Synthetic Indices Autonomous Bot")
    parser.add_argument("--run", "--loop", action="store_true", help="Start the 24/7 autonomous loop")
    parser.add_argument("--scan", action="store_true", help="Analyze and rank all configured synthetic markets")
    parser.add_argument("--math-formulas", action="store_true", help="Print exact mathematical equations for all synthetic indices")
    parser.add_argument("--analyze", type=str, metavar="SYMBOL", help="Run quantitative decomposition on symbol")
    parser.add_argument("--status", action="store_true", help="Show active synthetic positions and status")
    parser.add_argument("--backtest", type=str, metavar="SYMBOL", help="Run bar-by-bar quantitative simulation on symbol")
    parser.add_argument("--edge-scan", action="store_true",
                        help="Test real MT5 history for statistically significant deviations from fair randomness")
    parser.add_argument("--bars", type=int, default=None,
                        help="Bars to use (default 1000 for --backtest, 20000 for --edge-scan)")
    parser.add_argument("--spread", type=float, default=0.0, help="Spread in price units charged per backtest trade")
    parser.add_argument("--timeframe", type=str, default="M5", help="Timeframe (default M5)")
    args = parser.parse_args()

    if args.math_formulas:
        return cmd_math_formulas()
    elif args.scan:
        return cmd_scan(timeframe=args.timeframe)
    elif args.analyze:
        return cmd_analyze(args.analyze, timeframe=args.timeframe)
    elif args.edge_scan:
        return cmd_edge_scan(bars=args.bars or 20000, timeframe=args.timeframe)
    elif args.backtest:
        return cmd_backtest(symbol=args.backtest, bars=args.bars or 1000, spread=args.spread)
    elif args.status:
        return cmd_status()
    elif args.run:
        return cmd_run()
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
