"""
Thin trading bot scaffold that uses MT5Connection to fetch account info,
symbol prices, and OHLCV data with selectable symbols/timeframes.

Execution is read-only by default; place orders only after validating rules.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import List

# Add src to path to allow direct imports from the src directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))

import pandas as pd

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from mt5_connection import MT5Connection, MT5Credentials, TIMEFRAME_MAP
from trading_bot_engulfing_v2 import EngulfingBotV2, Signal, TimeFrame, TradingConfig
from telegram_notifier import TelegramNotifier, build_telegram_config_from_env
# from mt5_strategy import EngulfingCandleStrategy, EngulfingConfig


def parse_args():
    parser = argparse.ArgumentParser(description="MT5 trading bot scaffold (read-only).")
    parser.add_argument("--login", type=int, required=False, help="MT5 account login number (or set MT5_LOGIN)")
    parser.add_argument(
        "--password",
        type=str,
        required=False,
        help="MT5 account password (or set MT5_PASSWORD). If omitted, tries attach-mode (terminal already logged in).",
    )
    parser.add_argument("--server", type=str, required=False, help="MT5 server name (or set MT5_SERVER)")
    parser.add_argument(
        "--terminal-path",
        type=str,
        required=False,
        help="Full path to terminal64.exe (or set MT5_TERMINAL_PATH)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["EURUSD"],
        help="Symbols to trade (space-separated and/or comma-separated, e.g., EURUSD GBPUSD or EURUSD,GBPUSD)",
    )
    parser.add_argument("--timeframe", type=str, default="H1", choices=list(TIMEFRAME_MAP.keys()), help="Timeframe code")
    parser.add_argument("--count", type=int, default=200, help="Number of candles to fetch")
    parser.add_argument("--live", action="store_true", help="Use live account (default demo)")
    parser.add_argument("--order-direction", choices=["buy", "sell"], help="Optional market order direction")
    parser.add_argument("--order-volume", type=float, help="Order volume in lots; required if --order-direction set")
    parser.add_argument("--order-sl", type=float, help="Optional stop loss price")
    parser.add_argument("--order-tp", type=float, help="Optional take profit price")
    parser.add_argument("--order-deviation", type=int, default=10, help="Max price deviation in points")
    parser.add_argument(
        "--confirm-trading",
        action="store_true",
        help="Required to actually send orders when --order-direction is used",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required when --live is used and you want to send orders",
    )
    parser.add_argument(
        "--strategy",
        choices=["none", "engulfing_v2", "engulfing"],
        default="none",
        help="Optional strategy mode. Use 'engulfing' (mt5_strategy.py) or 'engulfing_v2' to generate signals and optionally trade.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="When used with --strategy, continuously poll and evaluate new closed candles.",
    )
    parser.add_argument(
        "--backtest-v2",
        action="store_true",
        help="Run engulfing_v2 backtest on MT5 historical data for the first symbol and exit.",
    )
    parser.add_argument(
        "--backtest-years",
        type=float,
        default=2.0,
        help="How many years back from now to include in backtest (default 2.0).",
    )
    parser.add_argument(
        "--backtest-from",
        type=str,
        default="",
        help="Optional backtest start date (YYYY-MM-DD). Overrides --backtest-years when provided.",
    )
    parser.add_argument(
        "--backtest-to",
        type=str,
        default="",
        help="Optional backtest end date (YYYY-MM-DD). Overrides --backtest-years when provided.",
    )
    parser.add_argument(
        "--backtest-spread-points",
        type=float,
        default=0.0,
        help="Fixed spread to simulate during backtest, in points (0 = auto/default).",
    )
    parser.add_argument(
        "--backtest-commission-per-lot",
        type=float,
        default=0.0,
        help="Fixed commission per 1.00 lot round-turn in account currency.",
    )
    parser.add_argument(
        "--backtest-config-json",
        type=str,
        default="config/config_rsi_pullback.json",
        help="Path to engulfing_v2 config JSON used for backtest (default config/config_rsi_pullback.json).",
    )
    parser.add_argument(
        "--smoke-trade",
        action="store_true",
        help="Place a small smoke-test trade on the first symbol to confirm order execution (requires --confirm-trading).",
    )
    parser.add_argument(
        "--smoke-direction",
        choices=["buy", "sell"],
        default="buy",
        help="Direction for smoke-test trade (default buy).",
    )
    parser.add_argument(
        "--smoke-volume",
        type=float,
        default=0.01,
        help="Volume for smoke-test trade (default 0.01).",
    )
    parser.add_argument(
        "--smoke-close-seconds",
        type=int,
        default=10,
        help="Seconds to wait before auto-closing the smoke-test trade (0 = keep open).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="Polling interval in seconds when --loop is enabled.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Stop after N polling iterations when --loop is enabled (0 = infinite).",
    )

    parser.add_argument(
        "--system",
        action="store_true",
        help="Run system mode (state persistence, BE/trailing stop, Telegram notifications, news filter).",
    )
    parser.add_argument(
        "--bot-switch-path",
        type=str,
        default="data/bot_switch.txt",
        help="Path to bot switch file (default data/bot_switch.txt or BOT_SWITCH_PATH env). When switch is OFF the bot stays running but pauses actions.",
    )
    parser.add_argument(
        "--set-bot-switch",
        choices=["on", "off"],
        default=None,
        help="Set bot switch file to on/off and exit (no MT5 connection required).",
    )
    parser.add_argument(
        "--bot-magic",
        type=int,
        default=20260202,
        help="Magic number used for system mode tracking/adoption (default 20260202 or BOT_MAGIC env).",
    )
    parser.add_argument(
        "--system-state-path",
        type=str,
        default="data/trades_state.json",
        help="System mode state file path (default data/trades_state.json or TRADES_STATE_PATH env).",
    )
    parser.add_argument(
        "--system-config-json",
        type=str,
        default="config/config_rsi_pullback.json",
        help="System/backtest strategy config JSON path (default config/config_rsi_pullback.json or CONFIG_JSON env).",
    )
    parser.add_argument(
        "--show-positions",
        action="store_true",
        help="Print open positions (read-only) for provided symbols.",
    )
    parser.add_argument(
        "--close-ticket",
        type=int,
        default=0,
        help="Close an existing position by ticket (requires --confirm-trading).",
    )
    parser.add_argument(
        "--set-sltp-ticket",
        type=int,
        default=0,
        help="Set/repair SL+TP on an existing position ticket (requires --confirm-trading).",
    )
    parser.add_argument(
        "--scan-signals",
        type=int,
        default=0,
        help="(engulfing_v2) Scan last N closed candles and print any BUY/SELL signals (read-only).",
    )
    parser.add_argument(
        "--scan-debug",
        action="store_true",
        help="(engulfing_v2) When scanning signals, also print filter pass/fail counts (read-only).",
    )
    parser.add_argument(
        "--v2-strategy-mode",
        choices=["RSI_PULLBACK", "STANDARD"],
        default="RSI_PULLBACK",
        help="(engulfing_v2) Strategy mode override.",
    )
    parser.add_argument(
        "--v2-volume-multiplier",
        type=float,
        default=0.0,
        help="(engulfing_v2) Volume multiplier override (lower => more signals).",
    )
    parser.add_argument(
        "--v2-sr-tolerance",
        type=float,
        default=0.0,
        help="(engulfing_v2) Support/resistance tolerance override (higher => more signals).",
    )
    parser.add_argument(
        "--v2-rsi-overbought",
        type=float,
        default=0.0,
        help="(engulfing_v2) RSI overbought threshold override (0 = keep config).",
    )
    parser.add_argument(
        "--v2-rsi-oversold",
        type=float,
        default=0.0,
        help="(engulfing_v2) RSI oversold threshold override (0 = keep config).",
    )
    parser.add_argument(
        "--v2-risk-per-trade",
        type=float,
        default=0.0,
        help="(engulfing_v2) Risk per trade override (0 = keep config). Useful to avoid early stop on drawdown during backtest.",
    )
    parser.add_argument(
        "--v2-min-risk-reward",
        type=float,
        default=0.0,
        help="(engulfing_v2) Min risk:reward override (0 = keep config).",
    )

    parser.add_argument("--engulf-risk", type=float, default=0.01, help="(engulfing) Risk per trade as fraction of equity")
    parser.add_argument("--engulf-rr", type=float, default=2.0, help="(engulfing) Minimum risk:reward ratio")
    parser.add_argument("--engulf-ema-fast", type=int, default=50, help="(engulfing) EMA fast period")
    parser.add_argument("--engulf-ema-slow", type=int, default=200, help="(engulfing) EMA slow period")
    parser.add_argument("--engulf-no-trend", action="store_true", help="(engulfing) Disable EMA trend filter")
    parser.add_argument("--engulf-no-rsi", action="store_true", help="(engulfing) Disable RSI filter")
    parser.add_argument("--engulf-rsi-period", type=int, default=14, help="(engulfing) RSI period")
    parser.add_argument("--engulf-rsi-buy-max", type=float, default=70.0, help="(engulfing) Buy allowed only if RSI <= this")
    parser.add_argument("--engulf-rsi-sell-min", type=float, default=30.0, help="(engulfing) Sell allowed only if RSI >= this")
    parser.add_argument("--safety-check", action="store_true", help="Perform pre-flight safety check")
    parser.add_argument("--status-check", action="store_true", help="Quick bot diagnostic status check")
    parser.add_argument("--close-all", action="store_true", help="Emergency manual close of all active positions")
    parser.add_argument("--emergency-status", action="store_true", help="Print emergency status report")
    return parser.parse_args()


def parse_symbols(raw_symbols: List[str]) -> List[str]:
    symbols: List[str] = []
    for item in raw_symbols:
        for sym in str(item).split(","):
            sym = sym.strip().upper()
            if sym:
                symbols.append(sym)
    return symbols


def map_timeframe_to_engulfing(timeframe: str) -> TimeFrame:
    tf = str(timeframe).upper().strip()
    mapping = {
        "M5": TimeFrame.M5,
        "M15": TimeFrame.M15,
        "H1": TimeFrame.H1,
        "H4": TimeFrame.H4,
    }
    if tf not in mapping:
        raise ValueError("Engulfing v2 supports M5, M15, H1, H4 only")
    return mapping[tf]


def cli_flag_present(flag: str) -> bool:
    return flag in sys.argv


def _parse_ymd(value: str) -> datetime:
    value = (value or "").strip()
    return datetime.strptime(value, "%Y-%m-%d")


def load_v2_config_from_json(path: str) -> TradingConfig:
    cfg = TradingConfig()
    if not path or not os.path.exists(path):
        return cfg

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f) or {}

    rm = (raw.get("risk_management") or {})
    sp = (raw.get("strategy_parameters") or {})
    rsi = (sp.get("rsi_settings") or {})
    vol = (sp.get("volume_settings") or {})
    sr = (sp.get("support_resistance") or {})

    if "risk_per_trade" in rm:
        cfg.risk_per_trade = float(rm.get("risk_per_trade"))
    if "min_risk_reward" in rm:
        cfg.min_risk_reward = float(rm.get("min_risk_reward"))
    if "max_drawdown" in rm:
        cfg.max_drawdown = float(rm.get("max_drawdown"))
    if "initial_capital" in rm:
        cfg.initial_capital = float(rm.get("initial_capital"))
    if "atr_trailing_mult" in rm:
        cfg.atr_trailing_mult = float(rm.get("atr_trailing_mult"))
    if "max_risk_per_trade" in rm:
        cfg.max_risk_per_trade = float(rm.get("max_risk_per_trade") or cfg.max_risk_per_trade)
    if "max_total_open_risk" in rm:
        cfg.max_total_open_risk = float(rm.get("max_total_open_risk") or cfg.max_total_open_risk)
    if "max_open_trades" in rm:
        cfg.max_open_trades = int(rm.get("max_open_trades") or cfg.max_open_trades)
    if "max_open_trades_per_symbol" in rm:
        cfg.max_open_trades_per_symbol = int(rm.get("max_open_trades_per_symbol") or cfg.max_open_trades_per_symbol)
    if "max_open_trades_per_direction" in rm:
        cfg.max_open_trades_per_direction = int(rm.get("max_open_trades_per_direction") or cfg.max_open_trades_per_direction)
    if "allow_opposite_direction_positions" in rm:
        cfg.allow_opposite_direction_positions = bool(rm.get("allow_opposite_direction_positions"))
    if "max_daily_loss" in rm:
        cfg.max_daily_loss = float(rm.get("max_daily_loss") or cfg.max_daily_loss)
    if "allow_partial_close" in rm:
        cfg.allow_partial_close = bool(rm.get("allow_partial_close"))

    if "capital_protection_enabled" in rm:
        cfg.capital_protection_enabled = bool(rm.get("capital_protection_enabled"))
    if "capital_protection_daily_loss_trigger" in rm:
        cfg.cp_daily_loss_trigger = float(rm.get("capital_protection_daily_loss_trigger") or cfg.cp_daily_loss_trigger)
    if "capital_protection_weekly_loss_trigger" in rm:
        cfg.cp_weekly_loss_trigger = float(rm.get("capital_protection_weekly_loss_trigger") or cfg.cp_weekly_loss_trigger)
    if "capital_protection_consecutive_losses_trigger" in rm:
        cfg.cp_consecutive_losses_trigger = int(rm.get("capital_protection_consecutive_losses_trigger") or cfg.cp_consecutive_losses_trigger)
    if "capital_protection_risk_fraction" in rm:
        cfg.cp_risk_fraction = float(rm.get("capital_protection_risk_fraction") or cfg.cp_risk_fraction)
    if "capital_protection_min_signal_score" in rm:
        cfg.cp_min_signal_score = int(rm.get("capital_protection_min_signal_score") or cfg.cp_min_signal_score)
    if "capital_protection_max_trades_per_day" in rm:
        cfg.cp_max_trades_per_day = int(rm.get("capital_protection_max_trades_per_day") or cfg.cp_max_trades_per_day)
    if "losing_streak_pause_after" in rm:
        cfg.losing_streak_pause_after = int(rm.get("losing_streak_pause_after") or cfg.losing_streak_pause_after)
    if "losing_streak_cooldown_seconds" in rm:
        cfg.losing_streak_cooldown_seconds = float(rm.get("losing_streak_cooldown_seconds") or cfg.losing_streak_cooldown_seconds)
    if "dynamic_risk_daily_dd_soft" in rm:
        cfg.dynamic_risk_daily_dd_soft = float(rm.get("dynamic_risk_daily_dd_soft") or cfg.dynamic_risk_daily_dd_soft)
    if "dynamic_risk_daily_dd_hard" in rm:
        cfg.dynamic_risk_daily_dd_hard = float(rm.get("dynamic_risk_daily_dd_hard") or cfg.dynamic_risk_daily_dd_hard)
    if "dynamic_risk_soft_multiplier" in rm:
        cfg.dynamic_risk_soft_multiplier = float(rm.get("dynamic_risk_soft_multiplier") or cfg.dynamic_risk_soft_multiplier)
    if "dynamic_risk_hard_multiplier" in rm:
        cfg.dynamic_risk_hard_multiplier = float(rm.get("dynamic_risk_hard_multiplier") or cfg.dynamic_risk_hard_multiplier)
    if "volatile_regime_risk_multiplier" in rm:
        cfg.volatile_regime_risk_multiplier = float(rm.get("volatile_regime_risk_multiplier") or cfg.volatile_regime_risk_multiplier)
    if "dynamic_risk_boost_enabled" in rm:
        cfg.dynamic_risk_boost_enabled = bool(rm.get("dynamic_risk_boost_enabled"))
    if "dynamic_risk_boost_after_wins" in rm:
        cfg.dynamic_risk_boost_after_wins = int(rm.get("dynamic_risk_boost_after_wins") or cfg.dynamic_risk_boost_after_wins)
    if "dynamic_risk_boost_min_daily_gain" in rm:
        cfg.dynamic_risk_boost_min_daily_gain = float(rm.get("dynamic_risk_boost_min_daily_gain") or cfg.dynamic_risk_boost_min_daily_gain)
    if "dynamic_risk_boost_multiplier" in rm:
        cfg.dynamic_risk_boost_multiplier = float(rm.get("dynamic_risk_boost_multiplier") or cfg.dynamic_risk_boost_multiplier)

    if "ema_fast" in sp:
        cfg.ema_fast = int(sp.get("ema_fast"))
    if "ema_slow" in sp:
        cfg.ema_slow = int(sp.get("ema_slow"))
    if "strategy_mode" in sp:
        cfg.strategy_mode = str(sp.get("strategy_mode"))
    if "skip_weak_range_entries" in sp:
        cfg.skip_weak_range_entries = bool(sp.get("skip_weak_range_entries"))
    if "min_trade_score_full_entry" in sp:
        cfg.min_trade_score_full_entry = int(sp.get("min_trade_score_full_entry"))
    if "min_trade_score_half_entry" in sp:
        cfg.min_trade_score_half_entry = int(sp.get("min_trade_score_half_entry"))
    if "min_planned_rr_execution" in sp:
        cfg.min_planned_rr_execution = float(sp.get("min_planned_rr_execution"))
    tf_raw = str(sp.get("timeframe", "") or "").upper().strip()
    if tf_raw:
        try:
            cfg.timeframe = map_timeframe_to_engulfing(tf_raw)
        except ValueError:
            pass

    cfg.use_rsi_filter = bool(rsi.get("use_rsi_filter", cfg.use_rsi_filter))
    cfg.rsi_period = int(rsi.get("rsi_period", cfg.rsi_period))
    cfg.rsi_overbought = float(rsi.get("rsi_overbought", cfg.rsi_overbought))
    cfg.rsi_oversold = float(rsi.get("rsi_oversold", cfg.rsi_oversold))
    cfg.rsi_pullback_enabled = bool(rsi.get("rsi_pullback_enabled", cfg.rsi_pullback_enabled))
    cfg.rsi_pullback_buy_threshold = float(rsi.get("rsi_pullback_buy_threshold", cfg.rsi_pullback_buy_threshold))
    cfg.rsi_pullback_sell_threshold = float(rsi.get("rsi_pullback_sell_threshold", cfg.rsi_pullback_sell_threshold))
    cfg.rsi_lookback_candles = int(rsi.get("rsi_lookback_candles", cfg.rsi_lookback_candles))

    cfg.volume_multiplier = float(vol.get("volume_multiplier", cfg.volume_multiplier))
    cfg.volume_period = int(vol.get("volume_period", cfg.volume_period))

    cfg.sr_lookback = int(sr.get("sr_lookback", cfg.sr_lookback))
    cfg.sr_tolerance = float(sr.get("sr_tolerance", cfg.sr_tolerance))

    conf = (sp.get("confluence_settings") or {})
    if "min_confluence_score" in conf:
        cfg.min_confluence_score = int(conf.get("min_confluence_score"))

    return cfg


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    args = parse_args()

    # Set up root logger with rotation
    log_file = "logs/bot_run.log"
    os.makedirs("logs", exist_ok=True)
    
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
    )
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            handler,
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger("mt5-bot")

    if load_dotenv is not None:
        load_dotenv(override=False)

    # ── ENV overrides for VPS: allow .env to fully drive the bot ──────────────
    # Only apply env values if the corresponding CLI flag was NOT explicitly set
    if not cli_flag_present("--symbols"):
        env_syms = str(os.getenv("SYMBOLS", "") or "").strip()
        if env_syms:
            args.symbols = [env_syms]          # parse_symbols handles comma-sep
    if not cli_flag_present("--timeframe"):
        env_tf = str(os.getenv("TIMEFRAME", "") or "").strip().upper()
        if env_tf and env_tf in list(__import__("mt5_connection", fromlist=["TIMEFRAME_MAP"]).TIMEFRAME_MAP.keys()):
            args.timeframe = env_tf
    if not cli_flag_present("--poll-seconds"):
        env_poll = str(os.getenv("POLL_SECONDS", "") or "").strip()
        if env_poll.isdigit():
            args.poll_seconds = int(env_poll)
    # ──────────────────────────────────────────────────────────────────────────

    if args.set_bot_switch is not None:
        switch_path = str(os.getenv("BOT_SWITCH_PATH", args.bot_switch_path) or args.bot_switch_path)
        with open(switch_path, "w", encoding="utf-8") as f:
            f.write(str(args.set_bot_switch).strip().lower())
        print(f"Bot switch set to {args.set_bot_switch.upper()} ({switch_path})")
        return

    if args.status_check:
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'scripts')))
        try:
            import check_bot_status
            check_bot_status.main()
        except Exception as e:
            print(f"❌ Status check failed: {e}")
        return

    login = args.login if args.login is not None else os.getenv("MT5_LOGIN")
    password = args.password if args.password is not None else os.getenv("MT5_PASSWORD")
    server = args.server if args.server is not None else os.getenv("MT5_SERVER")
    terminal_path = args.terminal_path if args.terminal_path is not None else os.getenv("MT5_TERMINAL_PATH")

    if login is None:
        print("❌ Missing login. Provide --login or set MT5_LOGIN.")
        return

    if not server:
        print("❌ Missing server. Provide --server or set MT5_SERVER.")
        return

    if not terminal_path:
        print("❌ Missing terminal path. Provide --terminal-path or set MT5_TERMINAL_PATH.")
        return

    credentials = MT5Credentials(
        login=int(login),
        password=str(password) if password is not None else "",
        server=str(server) if server is not None else "",
        terminal_path=str(terminal_path),
        is_demo=not args.live,
    )

    symbols = parse_symbols(args.symbols)

    conn = MT5Connection(credentials)
    if not conn.initialize():
        code, msg = MT5Connection.last_error()
        print(f"❌ MT5 connection failed: [{code}] {msg}")
        print("Fix: open the MT5 terminal, log into the specified account/server, and ensure it shows green bars.")
        return

    try:
        summary = conn.get_account_summary()
        if summary is None:
            print("❌ Connected to MT5 but failed to read account info.")
            return

        if int(summary.get("login", 0)) != int(login):
            print(f"❌ Wrong account: expected {login}, connected to {summary.get('login')}")
            return

        if server and str(summary.get("server", "")).strip().lower() != str(server).strip().lower():
            print(f"❌ Wrong server: expected {server}, connected to {summary.get('server')}")
            return

        status = conn.get_connection_status()
        if status.get("connected") is not True:
            print("❌ MT5 terminal is not connected (no green bars).")
            return

        print("\nACCOUNT VERIFICATION")
        print(
            json.dumps(
                {
                    "login": summary.get("login"),
                    "balance": summary.get("balance"),
                    "equity": summary.get("equity"),
                    "server": summary.get("server"),
                    "account_trade_allowed": summary.get("trade_allowed"),
                    "account_trade_mode": summary.get("trade_mode"),
                    "terminal_connected": status.get("connected"),
                    "terminal_trade_allowed": status.get("trade_allowed"),
                    "terminal_trade_expert": status.get("trade_expert"),
                    "terminal_path": status.get("terminal_path"),
                },
                indent=2,
            )
        )

        account_trade_mode = summary.get("trade_mode")
        is_connected_live = account_trade_mode not in (None, 0, 1)

        if args.safety_check:
            print("\n" + "=" * 60)
            print("🛡️ MT5 BOT PRE-FLIGHT SAFETY DIAGNOSTIC CHECK")
            print("=" * 60)
            
            # Account balance check
            balance = float(summary.get("balance", 0.0) or 0.0)
            print(f"💰 Account Balance: ${balance:,.2f}")
            if balance > 0:
                print("   ✅ [PASS] Account has positive balance")
            else:
                print("   ❌ [FAIL] Account has zero or negative balance")
                
            # Margin Level check
            margin_free = float(summary.get("margin_free", 0.0) or 0.0)
            margin_level = float(summary.get("margin_level", 0.0) or 0.0)
            print(f"📈 Free Margin: ${margin_free:,.2f} | Margin Level: {margin_level:.1f}%")
            if margin_level == 0.0 or margin_level >= 200.0:
                print("   ✅ [PASS] Margin level is safe (no trades or >= 200%)")
            else:
                print("   ❌ [FAIL] Low margin level! Below 200% threshold")
                
            # Daily loss threshold loading
            cfg_path = str(args.system_config_json or "config/config_rsi_pullback.json")
            print(f"⚙️ Config File: {cfg_path}")
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_raw = json.load(f) or {}
                    rm = cfg_raw.get("risk_management") or {}
                    max_daily_loss = rm.get("max_daily_loss", 0.06)
                    print(f"   ℹ️ Max Daily Loss Limit: {max_daily_loss:.1%}")
                    print("   ✅ [PASS] Config file loaded successfully")
                except Exception as e:
                    print(f"   ❌ [FAIL] Error parsing config file: {e}")
            else:
                print(f"   ❌ [FAIL] Config file not found at {cfg_path}")
                
            # Telegram configuration check
            telegram_enabled = False
            try:
                from telegram_notifier import build_telegram_config_from_env
                tele_cfg = build_telegram_config_from_env(os.getenv)
                telegram_enabled = tele_cfg.enabled
                print(f"🤖 Telegram Notifications: {'ENABLED' if telegram_enabled else 'DISABLED'}")
                if telegram_enabled:
                    print(f"   ℹ️ Bot Token: ...{tele_cfg.bot_token[-6:] if tele_cfg.bot_token else 'None'}")
                    print(f"   ℹ️ Chat ID: {tele_cfg.chat_id}")
                    print("   ✅ [PASS] Telegram settings populated")
                else:
                    print("   ℹ️ [INFO] Telegram is not configured in env variables")
            except Exception as e:
                print(f"   ❌ [FAIL] Error reading Telegram config: {e}")
                
            # News filter check
            news_enabled = False
            try:
                from news_filter import build_news_config_from_env
                news_cfg = build_news_config_from_env(os.getenv)
                news_enabled = news_cfg.enabled
                print(f"📰 News Filter: {'ENABLED' if news_enabled else 'DISABLED'}")
                if news_enabled:
                    print(f"   ℹ️ Primary News URL: {news_cfg.url}")
                    print(f"   ℹ️ Avoid News Minutes: {news_cfg.avoid_minutes}m")
                    print("   ✅ [PASS] News Filter environment settings valid")
                else:
                    print("   ℹ️ [INFO] News Filter is not configured (or disabled) in env")
            except Exception as e:
                print(f"   ❌ [FAIL] Error loading News Filter config: {e}")
                
            # Trading permissions
            acc_trade_allowed = summary.get("trade_allowed")
            term_trade_allowed = status.get("trade_allowed")
            term_expert_allowed = status.get("trade_expert")
            print(f"🗝️ Account Trade Allowed: {acc_trade_allowed}")
            print(f"🗝️ Terminal Trade Allowed: {term_trade_allowed}")
            
            expert_str = "None"
            if term_expert_allowed is True:
                expert_str = "True"
            elif term_expert_allowed is False:
                expert_str = "False"
            else:
                expert_str = "Not Supported by Python API (Assumed Enabled)"
            print(f"🤖 AutoTrading / Expert Allowed: {expert_str}")
            
            if acc_trade_allowed and term_trade_allowed and (term_expert_allowed is not False):
                print("   ✅ [PASS] All trading and expert advisor settings permitted")
            else:
                fail_reasons = []
                if not acc_trade_allowed: fail_reasons.append("Account trade restriction")
                if not term_trade_allowed: fail_reasons.append("Terminal trading disabled")
                if term_expert_allowed is False: fail_reasons.append("AutoTrading/Expert permissions disabled (turn on AutoTrading button)")
                print(f"   ❌ [FAIL] Trading restrictions: {', '.join(fail_reasons)}")
                
            print("=" * 60)
            return

        if args.emergency_status:
            print("\n" + "=" * 60)
            print("🚨 MT5 BOT EMERGENCY SNAPSHOT STATUS")
            print("=" * 60)
            
            # Read trades state file
            state_path = str(os.getenv("TRADES_STATE_PATH", args.system_state_path) or args.system_state_path)
            peak_eff = float(summary.get("balance", 10000.0) or 10000.0)
            daily_start_eff = peak_eff
            halted = False
            halt_reason = ""
            heartbeat_str = "Unknown"
            
            if os.path.exists(state_path):
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        state_data = json.load(f) or {}
                    meta = state_data.get("meta") or {}
                    peak_eff = float(meta.get("peak_eff", peak_eff))
                    daily_start_eff = float(meta.get("daily_start_eff", daily_start_eff))
                    halted = bool(meta.get("halted", False))
                    halt_reason = str(meta.get("halt_reason", ""))
                    updated_at = float(state_data.get("updated_at", 0))
                    if updated_at > 0:
                        hb_diff = time.time() - updated_at
                        heartbeat_str = f"{hb_diff:.1f}s ago (at {datetime.fromtimestamp(updated_at).strftime('%Y-%m-%d %H:%M:%S')})"
                    print(f"   ✅ Loaded local state file from: {state_path}")
                except Exception as e:
                    print(f"   ⚠️ Could not parse state file: {e}")
            else:
                print(f"   ⚠️ Local state file not found at: {state_path}")
                
            equity = float(summary.get("equity", 0.0) or 0.0)
            balance = float(summary.get("balance", 0.0) or 0.0)
            
            # Active drawdowns
            active_dd_val = (peak_eff - equity) / peak_eff if peak_eff > 0 else 0.0
            active_dd = max(0.0, active_dd_val)
            
            daily_dd_val = (daily_start_eff - equity) / daily_start_eff if daily_start_eff > 0 else 0.0
            daily_dd = max(0.0, daily_dd_val)
            
            print(f"\n📈 Live Equity: ${equity:,.2f} | Peak Equity: ${peak_eff:,.2f}")
            print(f"📊 Active Drawdown (from Peak): {active_dd:.2%}")
            print(f"📉 Daily Drawdown (from Day Start): {daily_dd:.2%}")
            
            print(f"\n⏸️  Halted State: {'🔴 HALTED' if halted else '🟢 RUNNING'}")
            if halted:
                print(f"   Reason: {halt_reason}")
                
            print(f"💓 Heartbeat Status: {heartbeat_str}")
            
            # Connection and Terminal
            print("\n🖥️ Terminal State:")
            print(f"   Account: {summary.get('login')} ({'LIVE' if is_connected_live else 'DEMO'})")
            print(f"   Server: {summary.get('server')}")
            print(f"   Ping/Latency: {status.get('ping', 'N/A')} ms")
            print(f"   Terminal Path: {status.get('terminal_path')}")
            
            # Positions count
            positions = conn.get_positions()
            p_count = len(positions) if positions is not None else 0
            print(f"💼 Open Position Count: {p_count}")
            
            print("=" * 60)
            return

        if args.close_all:
            # 1. Verification of confirm-trading
            if not args.confirm_trading:
                print("\n❌ WARNING: This will close ALL active positions matching our criteria!")
                print("   To proceed, you MUST explicitly provide the --confirm-trading flag.")
                return
            
            # 2. Check live trading restriction
            if is_connected_live:
                if not args.live:
                    print("\n❌ Connected account appears to be LIVE. Refusing trading actions unless you pass --live and --confirm-live.")
                    return
                if not args.confirm_live:
                    print("\n❌ Refusing to trade on a LIVE account without --confirm-live")
                    return
            
            # 3. Check account trading permissions
            if summary.get("trade_allowed") is False:
                print("\n❌ Account reports trading is not allowed (broker/account restriction).")
                return
            if status.get("trade_allowed") is False:
                print("\n❌ Terminal reports trading is not allowed (check AutoTrading / account permissions)")
                return
            
            magic = int(float(os.getenv("BOT_MAGIC", args.bot_magic) or 20260202))
            symbols_to_close = parse_symbols(args.symbols)
            
            print(f"\n🚨 EMERGENCY CLOSE ALL: Fetching open positions (magic={magic}, symbols={symbols_to_close})...")
            positions = conn.get_positions()
            if positions is None:
                print("❌ Failed to fetch open positions from MT5.")
                return
                
            matching_positions = []
            for p in positions:
                p_magic = int(getattr(p, "magic", 0) or 0)
                p_symbol = str(getattr(p, "symbol", "")).upper()
                if p_magic == magic:
                    if not cli_flag_present("--symbols") or p_symbol in symbols_to_close:
                        matching_positions.append(p)
            
            if not matching_positions:
                print("ℹ️ No open positions found matching the specified magic number and symbols.")
                return
                
            print(f"Found {len(matching_positions)} matching position(s) to close.")
            
            closed_count = 0
            for p in matching_positions:
                ticket = int(getattr(p, "ticket", 0) or getattr(p, "identifier", 0) or 0)
                symbol = getattr(p, "symbol", "")
                volume = getattr(p, "volume", 0.0)
                direction = "BUY" if getattr(p, "type", 0) == 0 else "SELL"
                print(f"Closing position #{ticket}: {symbol} {direction} {volume} lots...")
                
                result = conn.close_position(
                    ticket=ticket,
                    deviation=args.order_deviation,
                    comment="Emergency Close",
                )
                if result is None:
                    print(f"   ❌ Close failed for ticket {ticket}")
                else:
                    print(f"   ✅ Position closed successfully")
                    closed_count += 1
            
            print(f"\n🎉 Emergency Close complete. Closed {closed_count}/{len(matching_positions)} positions.")
            return

        if args.backtest_v2:
            from mt5_system import load_trading_config_from_json

            if not symbols:
                print("\nMissing symbol for backtest")
                return

            sym = symbols[0]

            if str(args.backtest_from or "").strip() and str(args.backtest_to or "").strip():
                date_from = _parse_ymd(str(args.backtest_from))
                date_to = _parse_ymd(str(args.backtest_to))
            else:
                years = float(args.backtest_years or 0.0)
                if years <= 0:
                    years = 2.0
                date_to = datetime.now()
                date_from = date_to - timedelta(days=int(365 * years))

            rates = conn.get_rates_range(sym, timeframe=args.timeframe, date_from=date_from, date_to=date_to)
            if rates is None:
                print("\nBacktest failed: unable to fetch rates")
                return

            df = pd.DataFrame(rates)
            if df.empty:
                print("\nBacktest failed: empty rates")
                return

            df["time"] = pd.to_datetime(df["time"], unit="s")
            if "tick_volume" in df.columns and "volume" not in df.columns:
                df["volume"] = df["tick_volume"]
            df = df.set_index("time")

            cfg_path = str(args.backtest_config_json or "")
            cfg = load_trading_config_from_json(cfg_path)
            cfg.timeframe = map_timeframe_to_engulfing(args.timeframe)
            if cli_flag_present("--v2-strategy-mode"):
                cfg.strategy_mode = str(args.v2_strategy_mode)
            if cli_flag_present("--v2-volume-multiplier"):
                cfg.volume_multiplier = float(args.v2_volume_multiplier)
            if cli_flag_present("--v2-sr-tolerance"):
                cfg.sr_tolerance = float(args.v2_sr_tolerance)
            if cli_flag_present("--v2-rsi-overbought") and float(args.v2_rsi_overbought or 0.0) > 0:
                cfg.rsi_overbought = float(args.v2_rsi_overbought)
            if cli_flag_present("--v2-rsi-oversold") and float(args.v2_rsi_oversold or 0.0) > 0:
                cfg.rsi_oversold = float(args.v2_rsi_oversold)
            if cli_flag_present("--v2-risk-per-trade") and float(args.v2_risk_per_trade or 0.0) > 0:
                cfg.risk_per_trade = float(args.v2_risk_per_trade)
            if cli_flag_present("--v2-min-risk-reward") and float(args.v2_min_risk_reward or 0.0) > 0:
                cfg.min_risk_reward = float(args.v2_min_risk_reward)

            bot = EngulfingBotV2(cfg)

            info = conn.ensure_symbol(sym)
            rules = conn.get_broker_rules(sym)
            point = float(getattr(rules, "point", 0.0) or 0.0) if rules else 0.0
            tick_size = float(getattr(rules, "tick_size", 0.0) or 0.0) if rules else 0.0
            tick_value = float(getattr(rules, "tick_value", 0.0) or 0.0) if rules else 0.0
            value_per_price_unit = (tick_value / tick_size) if tick_size > 0 and tick_value > 0 else 0.0

            spread_points = float(args.backtest_spread_points or 0.0)
            if spread_points <= 0:
                sym_spread = float(getattr(info, "spread", 0.0) or 0.0) if info is not None else 0.0
                if sym_spread > 0:
                    spread_points = sym_spread
                cfg_default = float(getattr(cfg, "max_spread_points", 0.0) or 0.0)
                if spread_points <= 0:
                    spread_points = cfg_default if cfg_default > 0 else 15.0

            commission_per_lot = float(args.backtest_commission_per_lot or 0.0)
            if commission_per_lot <= 0.0 and cfg_path and os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as rf:
                        raw_bt = json.load(rf) or {}
                    rm_bt = raw_bt.get("risk_management") or {}
                    commission_per_lot = float(rm_bt.get("backtest_commission_per_lot_round_turn", 0.0) or 0.0)
                except Exception:
                    commission_per_lot = 0.0

            buf = io.StringIO()
            with redirect_stdout(buf):
                stats = bot.backtest(
                    df,
                    value_per_price_unit=float(value_per_price_unit),
                    point=float(point),
                    spread_points=float(spread_points),
                    commission_per_lot=float(commission_per_lot),
                )
            raw_log = (buf.getvalue() or "")
            stopped_on_drawdown = "maximum drawdown exceeded" in raw_log.lower()

            out = {
                "symbol": sym,
                "timeframe": str(args.timeframe),
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "candles": int(len(df)),
                "strategy_mode": str(cfg.strategy_mode),
                "volume_multiplier": float(cfg.volume_multiplier),
                "sr_tolerance": float(cfg.sr_tolerance),
                "rsi_overbought": float(cfg.rsi_overbought),
                "rsi_oversold": float(cfg.rsi_oversold),
                "risk_per_trade": float(cfg.risk_per_trade),
                "min_risk_reward": float(cfg.min_risk_reward),
                "spread_points": float(spread_points),
                "commission_per_lot_round_turn": float(commission_per_lot),
                "stopped_on_drawdown": bool(stopped_on_drawdown),
                "total_trades": int(stats.get("total_trades") or 0),
                "win_rate": float(stats.get("win_rate") or 0.0),
                "profit_factor": float(stats.get("profit_factor") or 0.0),
                "expectancy_per_trade": float(stats.get("expectancy") or 0.0),
                "net_profit": float(stats.get("net_profit") or 0.0),
                "max_drawdown": float(stats.get("max_drawdown") or 0.0),
                "return_pct": float(stats.get("return_pct") or 0.0),
                "avg_rr_planned": float(stats.get("avg_planned_rr") or 0.0),
                "avg_r_multiple": float(stats.get("avg_r_multiple") or 0.0),
                "monthly_profitable_pct": float(stats.get("monthly_profitable_pct") or 0.0),
                "monthly": stats.get("monthly") or {},
                "regime_breakdown": stats.get("regime_breakdown") or {},
            }
            print("\nBACKTEST SUMMARY")
            print(json.dumps(out, indent=2))
            print("DONE")
            return

        if args.smoke_trade:
            if not symbols:
                print("\nMissing symbol for smoke trade")
                return
            if not args.confirm_trading:
                print("\n❌ Refusing to place smoke trade without --confirm-trading")
                return

            account_trade_mode = summary.get("trade_mode")
            is_connected_live = account_trade_mode not in (None, 0, 1)
            if is_connected_live:
                if not args.live:
                    print(
                        "\n❌ Connected account appears to be LIVE. Refusing trading actions unless you pass --live and --confirm-live."
                    )
                    return
                if not args.confirm_live:
                    print("\n❌ Refusing to trade on a LIVE account without --confirm-live")
                    return

            if summary.get("trade_allowed") is False:
                print("\n❌ Account reports trading is not allowed (broker/account restriction).")
                return
            if status.get("trade_allowed") is False:
                print("\n❌ Terminal reports trading is not allowed (check AutoTrading / account permissions)")
                return
            if status.get("trade_expert") is False:
                print("\n❌ Terminal reports Algo Trading is disabled (enable AutoTrading / Algo Trading)")
                return

            # Initialize Telegram notifier for trade notifications
            telegram_config = build_telegram_config_from_env(os.getenv)
            notifier = TelegramNotifier(telegram_config)

            sym = symbols[0]
            direction = str(args.smoke_direction).lower().strip()
            magic = int(float(os.getenv("BOT_MAGIC", args.bot_magic) or 0))
            comment = "smoke_test"

            positions = conn.get_positions(sym)
            if positions is not None and len(list(positions)) > 0:
                print(f"\n❌ Smoke trade aborted: existing open position detected on {sym}")
                notifier.send(f"❌ Smoke trade aborted: existing open position on {sym}")
                return

            normalized_volume = conn.normalize_volume(sym, float(args.smoke_volume))
            if normalized_volume is None or normalized_volume <= 0:
                print("\n❌ Invalid smoke volume")
                notifier.send("❌ Smoke trade failed: Invalid volume")
                return

            print(f"\nSMOKE TRADE: opening {direction.upper()} {sym} vol={normalized_volume:.2f}")
            result = conn.send_market_order(
                symbol=sym,
                direction=direction,
                volume=float(normalized_volume),
                stop_loss=None,
                take_profit=None,
                deviation=int(args.order_deviation),
                magic=magic,
                comment=comment,
            )
            if result is None:
                print("\n❌ Smoke trade failed to open (see logs)")
                notifier.send(f"❌ Smoke trade failed to open: {sym} {direction.upper()}")
                return

            time.sleep(0.5)
            pos = conn.find_recent_position(
                symbol=sym,
                direction=direction,
                volume=float(normalized_volume),
                magic=int(magic),
                comment=str(comment),
            )
            ticket = int(getattr(pos, "ticket", 0) or getattr(pos, "identifier", 0) or 0) if pos is not None else 0
            if ticket <= 0:
                print("\n⚠️ Smoke trade opened but could not resolve position ticket.")
                notifier.send(f"⚠️ Smoke trade opened but ticket unresolved: {sym} {direction.upper()}")
                return

            print(f"✅ Smoke trade opened: position_ticket={ticket}")
            # Send Telegram notification for trade entry
            notifier.send_trade_execution({
                "direction": direction.upper(),
                "entry_price": str(getattr(pos, 'price_open', 'N/A')),
                "stop_loss": "N/A",
                "take_profit": "N/A",
                "risk_reward": "N/A",
                "volume_multiplier": str(normalized_volume),
                "rsi_value": "N/A",
                "rsi_pullback": "N/A",
                "sr_tolerance": "N/A",
                "daily_loss_pct": "0.0",
                "open_trades": "1",
                "news_pause_status": "Active",
                "slippage_pips": "0.0",
                "spread_points": "0",
                "trades_completed": "0",
            })

            close_after = int(args.smoke_close_seconds or 0)
            if close_after <= 0:
                return

            print(f"Waiting {close_after}s then closing smoke trade...")
            time.sleep(max(1, close_after))
            closed = conn.close_position(ticket=ticket, deviation=int(args.order_deviation), comment="smoke_close")
            if closed is None:
                print("❌ Smoke trade close failed (see logs)")
                notifier.send(f"❌ Smoke trade close failed: ticket={ticket}")
            else:
                print("✅ Smoke trade closed")
                # Send Telegram notification for trade exit
                close_price = getattr(closed, 'price', 'N/A') if hasattr(closed, 'price') else 'N/A'
                notifier.send_trade_exit({
                    "direction": direction.upper(),
                    "entry_price": str(getattr(pos, 'price_open', 'N/A')),
                    "exit_price": str(close_price),
                    "profit_loss": 0.0,
                    "r_multiple": "N/A",
                    "exit_reason": "SMOKE_TEST",
                    "duration_minutes": str(close_after // 60) if close_after >= 60 else f"0:{close_after}",
                    "slippage_pips": "0.0",
                    "trades_completed": "1",
                })
            return

        account_trade_mode = summary.get("trade_mode")
        is_connected_live = account_trade_mode not in (None, 0, 1)

        def live_trading_permitted() -> bool:
            if not is_connected_live:
                return True
            if not args.live:
                print(
                    "\n❌ Connected account appears to be LIVE. Refusing trading actions unless you pass --live and --confirm-live."
                )
                return False
            if not args.confirm_live:
                print("\n❌ Refusing to trade on a LIVE account without --confirm-live")
                return False
            return True

        if args.system:
            if args.confirm_trading:
                if not live_trading_permitted():
                    return
                if summary.get("trade_allowed") is False:
                    print("\nWARNING: Account reports trading is not allowed (broker/account restriction). System will run but cannot trade.")
                if status.get("trade_allowed") is False:
                    print("\nWARNING: MT5 AutoTrading appears OFF. System will run but will pause actions until AutoTrading is enabled.")

            from mt5_system import MT5BotSystem, SystemConfig

            magic = int(float(os.getenv("BOT_MAGIC", args.bot_magic) or 0))
            state_path = str(os.getenv("TRADES_STATE_PATH", args.system_state_path) or args.system_state_path)
            config_json_path = str(os.getenv("CONFIG_JSON", args.system_config_json) or args.system_config_json)
            avoid_news_minutes = int(float(os.getenv("AVOID_NEWS_MINUTES", 60) or 60))
            bot_switch_path = str(os.getenv("BOT_SWITCH_PATH", args.bot_switch_path) or args.bot_switch_path)

            system_cfg = SystemConfig(
                symbols=symbols,
                timeframe=args.timeframe,
                poll_seconds=int(args.poll_seconds),
                magic=magic,
                state_path=state_path,
                avoid_news_minutes=avoid_news_minutes,
                config_json_path=config_json_path,
                bot_switch_path=bot_switch_path,
            )
            system = MT5BotSystem(conn, system_cfg)
            system.run_forever(confirm_trading=bool(args.confirm_trading))
            return

        if args.show_positions:
            print("\nOPEN POSITIONS")
            all_positions = []
            for sym in symbols:
                positions = conn.get_positions(sym)
                if positions is None:
                    continue
                for p in positions:
                    try:
                        all_positions.append(p._asdict())
                    except Exception:
                        all_positions.append({"symbol": sym})
            print(json.dumps(all_positions, indent=2))

        if args.close_ticket:
            if not args.confirm_trading:
                print("\n❌ Refusing to close positions without --confirm-trading")
                return
            if not live_trading_permitted():
                return
            if summary.get("trade_allowed") is False:
                print("\n❌ Account reports trading is not allowed (broker/account restriction).")
                return
            if status.get("trade_allowed") is False:
                print("\n❌ Terminal reports trading is not allowed (check AutoTrading / account permissions)")
                return

            print(f"\nClosing position ticket={args.close_ticket}...")
            result = conn.close_position(
                ticket=int(args.close_ticket),
                deviation=args.order_deviation,
                comment="CLI close",
            )
            if result is None:
                print("❌ Close failed; check logs for details")
            else:
                print("✅ Position close submitted successfully")

        if args.set_sltp_ticket:
            if not args.confirm_trading:
                print("\n❌ Refusing to set SL/TP without --confirm-trading")
                return
            if not live_trading_permitted():
                return
            if summary.get("trade_allowed") is False:
                print("\n❌ Account reports trading is not allowed (broker/account restriction).")
                return
            if status.get("trade_allowed") is False:
                print("\n❌ Terminal reports trading is not allowed (check AutoTrading / account permissions)")
                return
            if args.order_sl is None or args.order_tp is None:
                print("\n❌ --order-sl and --order-tp are required when using --set-sltp-ticket")
                return

            print(f"\nSetting SL/TP for ticket={args.set_sltp_ticket}...")
            result = conn.set_position_sltp(
                ticket=int(args.set_sltp_ticket),
                sl=float(args.order_sl),
                tp=float(args.order_tp),
                comment="CLI SLTP",
            )
            if result is None:
                print("❌ SL/TP update failed; check logs for details")
            else:
                print("✅ SL/TP update submitted successfully")

        print("\nPRICES")
        for sym in symbols:
            px = conn.get_symbol_price(sym)
            if px:
                print(f"{sym}: bid={px.bid} ask={px.ask}")

        print("\nOHLCV (head)")
        strategy_bot = None
        strategy_timeframe = None
        strategy_engulf = None
        if args.strategy == "engulfing_v2":
            strategy_timeframe = map_timeframe_to_engulfing(args.timeframe)
            cfg = TradingConfig(timeframe=strategy_timeframe)
            if cli_flag_present("--v2-strategy-mode"):
                cfg.strategy_mode = str(args.v2_strategy_mode)
            if cli_flag_present("--v2-volume-multiplier"):
                cfg.volume_multiplier = float(args.v2_volume_multiplier)
            if cli_flag_present("--v2-sr-tolerance"):
                cfg.sr_tolerance = float(args.v2_sr_tolerance)
            if cli_flag_present("--v2-rsi-overbought") and float(args.v2_rsi_overbought or 0.0) > 0:
                cfg.rsi_overbought = float(args.v2_rsi_overbought)
            if cli_flag_present("--v2-rsi-oversold") and float(args.v2_rsi_oversold or 0.0) > 0:
                cfg.rsi_oversold = float(args.v2_rsi_oversold)
            if cli_flag_present("--v2-risk-per-trade") and float(args.v2_risk_per_trade or 0.0) > 0:
                cfg.risk_per_trade = float(args.v2_risk_per_trade)
            if cli_flag_present("--v2-min-risk-reward") and float(args.v2_min_risk_reward or 0.0) > 0:
                cfg.min_risk_reward = float(args.v2_min_risk_reward)
            strategy_bot = EngulfingBotV2(cfg)
        if args.strategy == "engulfing":
            cfg = EngulfingConfig(
                risk_per_trade=float(args.engulf_risk),
                min_risk_reward=float(args.engulf_rr),
                ema_fast=int(args.engulf_ema_fast),
                ema_slow=int(args.engulf_ema_slow),
                use_trend_filter=not bool(args.engulf_no_trend),
                use_rsi_filter=not bool(args.engulf_no_rsi),
                rsi_period=int(args.engulf_rsi_period),
                rsi_buy_max=float(args.engulf_rsi_buy_max),
                rsi_sell_min=float(args.engulf_rsi_sell_min),
                candles=int(args.engulf_candles),
                sl_buffer_points=int(args.engulf_sl_buffer),
                allow_live=bool(args.live),
                magic=int(float(os.getenv("BOT_MAGIC", args.bot_magic) or 0)),
                comment="engulfing",
            )
            strategy_engulf = EngulfingCandleStrategy(conn, symbols, timeframe=args.timeframe, config=cfg)

        last_processed_candle_by_symbol = {}

        def evaluate_symbol_once(sym: str, show_head: bool) -> None:
            count = args.count
            if args.strategy == "engulfing_v2":
                count = max(count, 350)
            if args.strategy == "engulfing":
                count = max(count, int(args.engulf_candles))

            rates = conn.get_rates(sym, timeframe=args.timeframe, count=count)
            if rates is None:
                return
            df = pd.DataFrame(rates)
            if df.empty:
                return

            df["time"] = pd.to_datetime(df["time"], unit="s")
            if show_head:
                print(f"\n{sym} {args.timeframe} candles (showing first 5):")
                cols = [c for c in ["time", "open", "high", "low", "close", "tick_volume", "real_volume"] if c in df.columns]
                print(df[cols].head())

            if args.strategy == "engulfing" and strategy_engulf is not None:
                intent = strategy_engulf.evaluate_symbol(sym)
                if intent is None:
                    return
                print(
                    f"\nSTRATEGY engulfing | {sym} {args.timeframe} | {intent.direction.upper()} vol={intent.volume:.2f} SL={intent.stop_loss:.5f} TP={intent.take_profit:.5f}"
                )
                if not args.confirm_trading:
                    return
                if not live_trading_permitted():
                    return
                if summary.get("trade_allowed") is False:
                    return
                if status.get("trade_allowed") is False:
                    return
                result = conn.send_market_order(
                    symbol=sym,
                    direction=intent.direction,
                    volume=float(intent.volume),
                    stop_loss=float(intent.stop_loss or 0.0),
                    take_profit=float(intent.take_profit or 0.0),
                    deviation=args.order_deviation,
                    magic=int(float(os.getenv("BOT_MAGIC", args.bot_magic) or 0)),
                    comment=intent.rationale or "engulfing",
                )
                if result is None:
                    print("❌ Strategy order placement failed; check logs for details")
                else:
                    print(f"✅ Strategy order placed successfully (ticket={result.order})")
                return

            if args.strategy != "engulfing_v2" or strategy_bot is None:
                return
            if "tick_volume" not in df.columns:
                return

            df_sig = df.copy()
            df_sig["volume"] = df_sig["tick_volume"]
            df_sig = df_sig.set_index("time")
            if len(df_sig) < 3:
                return

            df_sig = strategy_bot.calculate_indicators(df_sig)

            if args.scan_signals:
                lookback = max(1, int(args.scan_signals))
                start = max(1, len(df_sig) - 1 - lookback)
                found = []
                counts = {
                    "candles_checked": 0,
                    "engulfing": 0,
                    "trend_ok": 0,
                    "rsi_pullback_ok": 0,
                    "volume_ok": 0,
                    "rsi_ok": 0,
                    "sr_ok": 0,
                    "signals": 0,
                }
                for i in range(start, len(df_sig) - 1):
                    counts["candles_checked"] += 1
                    is_bullish = strategy_bot.is_bullish_engulfing(df_sig, i)
                    is_bearish = strategy_bot.is_bearish_engulfing(df_sig, i)
                    if not (is_bullish or is_bearish):
                        continue
                    counts["engulfing"] += 1
                    sig0 = Signal.BUY if is_bullish else Signal.SELL

                    if not strategy_bot.check_strict_trend(df_sig, i, sig0):
                        continue
                    counts["trend_ok"] += 1

                    if getattr(strategy_bot.config, "strategy_mode", "RSI_PULLBACK") == "RSI_PULLBACK":
                        if not strategy_bot.check_rsi_pullback(df_sig, i, sig0):
                            continue
                    counts["rsi_pullback_ok"] += 1

                    if not strategy_bot.check_volume(df_sig, i):
                        continue
                    counts["volume_ok"] += 1

                    if not strategy_bot.check_rsi(df_sig, i, sig0):
                        continue
                    counts["rsi_ok"] += 1

                    if not strategy_bot.is_near_support_resistance(df_sig, i, sig0):
                        continue
                    counts["sr_ok"] += 1

                    counts["signals"] += 1
                    found.append({"time": str(df_sig.index[i]), "signal": str(sig0)})
                print(f"\nSIGNAL SCAN engulfing_v2 | {sym} {args.timeframe} | lookback={lookback} | found={len(found)}")
                if args.scan_debug:
                    print(json.dumps(counts, indent=2))
                if found:
                    print(json.dumps(found[-10:], indent=2))

            idx = len(df_sig) - 2
            candle_time = df_sig.index[idx]
            if last_processed_candle_by_symbol.get(sym) == candle_time:
                return
            last_processed_candle_by_symbol[sym] = candle_time

            signal = strategy_bot.generate_signal(df_sig, idx)
            print(f"\nSTRATEGY engulfing_v2 | {sym} {args.timeframe} | candle={candle_time} | signal={signal}")

            if signal not in (Signal.BUY, Signal.SELL):
                return

            positions = conn.get_positions(sym)
            if positions is not None and len(positions) > 0:
                print(f"Skipping trade for {sym}: existing open position detected")
                return

            if not args.confirm_trading:
                return
            if not live_trading_permitted():
                return
            if args.order_volume is None:
                print("\n❌ --order-volume is required to trade in strategy mode")
                return

            direction = "buy" if signal == Signal.BUY else "sell"

            tick = conn.get_symbol_price(sym)
            if tick is None:
                return
            entry = float(tick.ask if signal == Signal.BUY else tick.bid)
            sl, tp = strategy_bot.calculate_stop_take_profit(df_sig, idx, signal, entry_price=entry)
            print(f"\nSubmitting {direction.upper()} order on {sym} (strategy engulfing_v2)...")
            result = conn.send_market_order(
                symbol=sym,
                direction=direction,
                volume=args.order_volume,
                stop_loss=float(sl),
                take_profit=float(tp),
                deviation=args.order_deviation,
                comment="engulfing_v2",
            )
            if result is None:
                print("❌ Strategy order placement failed; check logs for details")
            else:
                print(f"✅ Strategy order placed successfully (ticket={result.order})")

        if args.loop and args.strategy != "none":
            print(f"\nStrategy loop running (poll={args.poll_seconds}s). Press Ctrl+C to stop.")
            iterations = 0
            show_head = True
            while True:
                for sym in symbols:
                    evaluate_symbol_once(sym, show_head=show_head)
                show_head = False
                iterations += 1
                if args.max_iterations and iterations >= args.max_iterations:
                    break
                time.sleep(max(1, int(args.poll_seconds)))
        else:
            for sym in symbols:
                evaluate_symbol_once(sym, show_head=True)

        if args.order_direction:
            if not args.confirm_trading:
                print("\n❌ Refusing to place orders without --confirm-trading")
                return
            if not live_trading_permitted():
                return
            if summary.get("trade_allowed") is False:
                print("\n❌ Account reports trading is not allowed (broker/account restriction).")
                return
            if status.get("trade_allowed") is False:
                print("\n❌ Terminal reports trading is not allowed (check AutoTrading / account permissions)")
                return
            if status.get("trade_expert") is False:
                print("\n❌ Terminal reports Algo Trading is disabled (enable AutoTrading / Algo Trading)")
                return
            if args.order_volume is None:
                print("\n❌ --order-volume is required when --order-direction is provided")
            else:
                # Initialize Telegram notifier for trade notifications
                telegram_config = build_telegram_config_from_env(os.getenv)
                notifier = TelegramNotifier(telegram_config)

                target_symbol = symbols[0] if symbols else None
                if target_symbol is None:
                    print("\n❌ Cannot place order because no symbols were provided")
                    notifier.send("❌ Manual order failed: No symbols provided")
                else:
                    print(f"\nSubmitting {args.order_direction.upper()} order on {target_symbol}...")
                    result = conn.send_market_order(
                        symbol=target_symbol,
                        direction=args.order_direction,
                        volume=args.order_volume,
                        stop_loss=args.order_sl,
                        take_profit=args.order_tp,
                        deviation=args.order_deviation,
                        comment="CLI order",
                    )
                    if result is None:
                        print("❌ Order placement failed; check logs for details")
                        notifier.send(f"❌ Manual order failed: {target_symbol} {args.order_direction.upper()}")
                    else:
                        print(f"✅ Order placed successfully (ticket={result.order})")
                        # Send Telegram notification for trade entry
                        notifier.send_trade_execution({
                            "direction": args.order_direction.upper(),
                            "entry_price": str(getattr(result, 'price', 'N/A')),
                            "stop_loss": str(args.order_sl) if args.order_sl else "N/A",
                            "take_profit": str(args.order_tp) if args.order_tp else "N/A",
                            "risk_reward": "N/A",
                            "volume_multiplier": str(args.order_volume),
                            "rsi_value": "N/A",
                            "rsi_pullback": "N/A",
                            "sr_tolerance": "N/A",
                            "daily_loss_pct": "0.0",
                            "open_trades": "1",
                            "news_pause_status": "Active",
                            "slippage_pips": "0.0",
                            "spread_points": "0",
                            "trades_completed": "0",
                        })
    finally:
        conn.shutdown()


if __name__ == "__main__":
    main()
