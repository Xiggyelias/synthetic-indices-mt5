"""
24/7 Quantitative Bot System for Deriv / MT5 Synthetic Indices
==============================================================
Continuous surveillance and execution on Volatility, Boom/Crash and Step indices.

Position management:
  - Runner trades (trend strategies by default): broker TP sits at TP2, part of the
    position is banked at TP1, and the rest rides an ATR chandelier stop for as long
    as the move lasts, hours or days
  - Breakeven + 0.15R lock at `be_trigger_r`
  - Kalman fair-value trailing stop (O-U trades)
  - VaR emergency exit when tail risk spikes on a losing position
  - Strategy-specific exits (drift harvest, spike harvest, half-life stagnation on
    losers, Step trail) and an optional maximum holding time
All stop changes go through `_tighten_stop`, so a later rule can never loosen
a stop that an earlier rule already moved.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from mt5_connection import MT5Connection
from quant_brain import (
    CalculativeBrain,
    QuantBrainAnalysis,
    compute_garman_klass_volatility,
    compute_var_cvar,
    fit_kalman_filter,
)
from synthetic_calibration import ProbabilityCalibrator
from synthetic_risk_manager import SyntheticRiskManager
from synthetic_strategy import SyntheticSignal, SyntheticStrategy, thesis_intact
from telegram_notifier import TelegramNotifier, build_telegram_config_from_env

TRADE_RETCODE_DONE = 10009

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}

# Deriv exposes synthetics under display names or short codes depending on the server
SYMBOL_ALIASES: Dict[str, str] = {
    "VOLATILITY 75 INDEX": "R_75",
    "R_75": "Volatility 75 Index",
    "VOLATILITY 75 (1S) INDEX": "1HZ75V",
    "1HZ75V": "Volatility 75 (1s) Index",
    "VOLATILITY 100 INDEX": "R_100",
    "R_100": "Volatility 100 Index",
    "VOLATILITY 100 (1S) INDEX": "1HZ100V",
    "1HZ100V": "Volatility 100 (1s) Index",
    "VOLATILITY 50 INDEX": "R_50",
    "R_50": "Volatility 50 Index",
    "VOLATILITY 50 (1S) INDEX": "1HZ50V",
    "1HZ50V": "Volatility 50 (1s) Index",
    "VOLATILITY 25 INDEX": "R_25",
    "R_25": "Volatility 25 Index",
    "VOLATILITY 10 INDEX": "R_10",
    "R_10": "Volatility 10 Index",
    "CRASH 1000 INDEX": "CRASH_1000",
    "CRASH_1000": "Crash 1000 Index",
    "CRASH 500 INDEX": "CRASH_500",
    "CRASH_500": "Crash 500 Index",
    "CRASH 300 INDEX": "CRASH_300",
    "CRASH_300": "Crash 300 Index",
    "BOOM 1000 INDEX": "BOOM_1000",
    "BOOM_1000": "Boom 1000 Index",
    "BOOM 500 INDEX": "BOOM_500",
    "BOOM_500": "Boom 500 Index",
    "BOOM 300 INDEX": "BOOM_300",
    "BOOM_300": "Boom 300 Index",
}

ALL_SYNTHETIC_SYMBOLS: List[str] = [
    "Volatility 75 Index",
    "Volatility 75 (1s) Index",
    "Volatility 100 Index",
    "Volatility 100 (1s) Index",
    "Volatility 50 Index",
    "Volatility 25 Index",
    "Volatility 10 Index",
    "Crash 1000 Index",
    "Crash 500 Index",
    "Crash 300 Index",
    "Boom 1000 Index",
    "Boom 500 Index",
    "Boom 300 Index",
    "Step Index",
]


def resolve_symbol(conn: MT5Connection, symbol_name: str) -> str:
    """Return whichever of the display name or its alias the terminal knows."""
    if conn.ensure_symbol(symbol_name):
        return symbol_name
    candidate = SYMBOL_ALIASES.get(symbol_name.upper().strip())
    if candidate and conn.ensure_symbol(candidate):
        return candidate
    return symbol_name


@dataclass
class SyntheticBotConfig:
    symbols: List[str] = field(default_factory=lambda: list(ALL_SYNTHETIC_SYMBOLS))
    timeframe: str = "M5"
    lookback_bars: int = 300              # Closed bars fed to the brain; ~120 is too few for the ADF test
    poll_seconds: int = 10
    magic: int = 20260909
    state_path: str = "data/synthetic_state.json"
    min_math_edge: float = 65.0
    kelly_fraction: float = 0.45
    max_risk_pct: float = 0.05
    max_drawdown_limit: float = 0.18
    max_open_trades: int = 6
    be_trigger_r: float = 1.0
    aggressive_mode: bool = True
    fast_poll_seconds: int = 5            # When >= 30% of symbols are in a tradable regime
    slow_poll_seconds: int = 20           # When <= 10% are
    var_emergency_threshold: float = 6.0  # VaR% that triggers an emergency exit on a losing trade
    kalman_trail_enable: bool = True
    spike_harvest_r: float = 3.5
    # Let winners run. Only trend trades by default: after a mean-reversion target the
    # pull is back toward the mean, and holding Boom/Crash drift longer adds spike risk.
    runner_strategies: List[str] = field(default_factory=lambda: ["FRACTAL_MOMENTUM"])
    runner_close_fraction: float = 0.5    # Share of the position banked at TP1
    runner_lock_r: float = 0.25           # Runner stop after TP1, in R beyond entry
    trail_atr_mult: float = 3.0           # Chandelier distance: best price - k * ATR(14)
    max_hold_hours: float = 0.0           # 0 = no time limit
    # Re-check each open trade's entry thesis on every new candle: exit when it breaks,
    # and while it holds push the runner's broker TP out instead of capping the move
    thesis_exit_enabled: bool = True
    extend_targets: bool = True
    # Add to a winner once its stop is locked in profit, so the add is the only capital at risk
    pyramid_enabled: bool = True
    pyramid_max_adds: int = 2
    # Learn from closed trades: scale each archetype's claimed win probability by its record
    calibration_enabled: bool = True
    calibration_path: str = "data/synthetic_calibration.json"
    brain_params: Dict[str, Any] = field(default_factory=dict)  # `calculative_brain` config section


class SyntheticBotSystem:
    """Autonomous quantitative bot for synthetic markets."""

    def __init__(
        self,
        connection: MT5Connection,
        config: Optional[SyntheticBotConfig] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.conn = connection
        self.config = config or SyntheticBotConfig()
        self.logger = logging.getLogger("synthetic-quant-bot")
        self.notifier = notifier or TelegramNotifier(build_telegram_config_from_env(os.getenv))

        self.brain = CalculativeBrain.from_config(self.config.brain_params)
        if self.config.calibration_enabled:
            self.brain.calibrator = ProbabilityCalibrator(self.config.calibration_path)
        self.risk_manager = SyntheticRiskManager(
            max_risk_pct=self.config.max_risk_pct,
            kelly_fraction=self.config.kelly_fraction,
            max_drawdown_limit=self.config.max_drawdown_limit,
            max_open_positions=self.config.max_open_trades,
            aggressive_mode=self.config.aggressive_mode,
        )
        self.strategy = SyntheticStrategy(
            brain=self.brain,
            risk_manager=self.risk_manager,
            min_math_edge_score=self.config.min_math_edge,
            kalman_confirm_threshold=float(self.config.brain_params.get("kalman_confirm_threshold", 0.8)),
        )
        self.bar_seconds = TIMEFRAME_SECONDS.get(self.config.timeframe.upper(), 300)

        self.open_trades: Dict[int, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self._last_scan_candle: Dict[str, int] = {}
        self._regime_cache: Dict[str, str] = {}
        self._latest_analysis: Dict[str, Tuple[int, QuantBrainAnalysis]] = {}  # symbol -> (bar time, analysis)
        self._cycle_count: int = 0
        self._load_state()

    # ── State Persistence ─────────────────────────────────────────────────────

    def _load_state(self) -> None:
        path = Path(self.config.state_path)
        if not path.exists() or path.stat().st_size == 0:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.open_trades = {int(k): v for k, v in data.get("open_trades", {}).items()}
            self.closed_trades = data.get("closed_trades", [])
        except Exception as exc:
            self.logger.warning("Could not load synthetic state: %s", exc)

    def _save_state(self) -> None:
        path = Path(self.config.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            summary = self.conn.get_account_summary() or {}
            payload = {
                "open_trades": self.open_trades,
                "closed_trades": self.closed_trades[-200:],
                "balance": float(summary.get("balance", 0.0) or 0.0) if summary else None,
                "equity": float(summary.get("equity", 0.0) or 0.0) if summary else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cycle_count": self._cycle_count,
            }
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("Could not save synthetic state: %s", exc)

    # ── Market Surveillance & Signal Execution ────────────────────────────────

    def _closed_bars(self, symbol: str, count: int) -> Optional[pd.DataFrame]:
        """Fetch `count` closed bars (the forming bar is dropped)."""
        rates = self.conn.get_rates(symbol, timeframe=self.config.timeframe, count=count + 1)
        if rates is None or len(rates) < 2:
            return None
        return pd.DataFrame(rates[:-1])

    def scan_symbol(self, raw_symbol: str) -> Optional[SyntheticSignal]:
        """Analyze the latest closed candle of one instrument; at most once per candle."""
        symbol = resolve_symbol(self.conn, raw_symbol)
        df = self._closed_bars(symbol, self.config.lookback_bars)
        if df is None or len(df) < 35:
            self.logger.debug("Insufficient rates for %s (%s)", symbol, raw_symbol)
            return None

        last_closed_time = int(df["time"].iloc[-1])
        if self._last_scan_candle.get(symbol) == last_closed_time:
            return None

        summary = self.conn.get_account_summary() or {}
        balance = float(summary.get("balance") or 0.0)
        if balance <= 0:
            # Never size against a placeholder balance
            self.logger.warning("Account balance unavailable; skipping scan of %s", symbol)
            return None
        equity = float(summary.get("equity") or balance)
        current_dd = max(0.0, (balance - equity) / balance)

        # The live spread is the house edge; every trade is priced net of it
        tick = self.conn.get_symbol_price(symbol)
        spread = max(float(tick.ask) - float(tick.bid), 0.0) if tick else 0.0
        analysis = self.brain.analyze(df, symbol=symbol, timeframe=self.config.timeframe, spread=spread)
        self._regime_cache[symbol] = analysis.hurst.regime
        self._last_scan_candle[symbol] = last_closed_time
        self._latest_analysis[symbol] = (last_closed_time, analysis)

        sig = self.strategy.signal_from_analysis(
            analysis,
            account_balance=balance,
            broker_rules=self.conn.get_broker_rules(symbol),
            current_drawdown=current_dd,
        )
        if sig is None and self.strategy.last_rejection:
            self.logger.info("%s has an edge (%s, E=%+.2fR) but cannot be traded: %s", symbol,
                             analysis.strategy_archetype, analysis.expected_yield_r, self.strategy.last_rejection)
        return sig

    DUPLICATE_WINDOW_SECONDS = 120
    DEMO_TRADE_MODES = (0, 1)   # MT5 ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST

    def _live_trading_blocked(self) -> str:
        """Real-money accounts trade only with ALLOW_LIVE_TRADING=true; returns the reason when blocked."""
        account = self.conn.get_account_info() if hasattr(self.conn, "get_account_info") else None
        mode = getattr(account, "trade_mode", None)
        if mode is None or int(mode) in self.DEMO_TRADE_MODES:
            return ""
        if os.getenv("ALLOW_LIVE_TRADING", "false").strip().lower() == "true":
            return ""
        return ("account is a REAL-money account and ALLOW_LIVE_TRADING is not 'true' "
                "(observe-only; set ALLOW_LIVE_TRADING=true in .env to trade real money)")

    def _recent_broker_position(self, symbol: str, server_now: float) -> bool:
        """
        Broker-side duplicate check. Local state cannot see orders sent by another process
        or a lost response; MT5's own position list can.
        """
        if not hasattr(self.conn, "get_positions") or server_now <= 0:
            return False
        for p in self.conn.get_positions(symbol) or []:
            if int(getattr(p, "magic", -1)) == int(self.config.magic) and \
                    server_now - float(getattr(p, "time", 0) or 0) < self.DUPLICATE_WINDOW_SECONDS:
                return True
        return False

    def execute_signal(self, sig: SyntheticSignal) -> bool:
        """Place the trade in MT5 with the Kelly-sized volume."""
        max_trades = min(3, self.config.max_open_trades)
        if len(self.open_trades) >= max_trades:
            self.logger.info("Max open trades reached (%d/%d); skipping %s",
                             len(self.open_trades), max_trades, sig.symbol)
            return False

        same_symbol = [t for t in self.open_trades.values() if t.get("symbol") == sig.symbol]
        if same_symbol:
            if not self._can_pyramid(sig, same_symbol):
                self.logger.info("Trade already active for %s (awaiting profit lock before scale-in); skipping", sig.symbol)
                return False
            self.logger.info("⚡ MULTI-ORDER DONE RIGHT: Existing %s position is locked in profit! Scale-in leg #%d approved.",
                             sig.symbol, len(same_symbol) + 1)
        if sig.sizing is None or sig.sizing.volume <= 0:
            return False

        blocked = self._live_trading_blocked()
        if blocked:
            self.logger.warning("Signal NOT executed (%s %s %s): %s", sig.symbol, sig.direction, sig.strategy_type, blocked)
            return False

        price_info = self.conn.get_symbol_price(sig.symbol)
        if not price_info:
            self.logger.error("Could not obtain live price for %s", sig.symbol)
            return False

        if self._recent_broker_position(sig.symbol, float(getattr(price_info, "time", 0) or 0)):
            self.logger.warning("Duplicate guard: MT5 already holds a %s position (magic %d) opened in the last %ds; skipping",
                                sig.symbol, self.config.magic, self.DUPLICATE_WINDOW_SECONDS)
            return False

        entry = float(price_info.ask if sig.direction == "BUY" else price_info.bid)
        volume = float(sig.sizing.volume)

        # Runner trades leave the broker TP at TP2 so the position is not capped at TP1
        tp2_beyond = sig.take_profit_2 > sig.take_profit if sig.direction == "BUY" else sig.take_profit_2 < sig.take_profit
        runner = sig.strategy_type in self.config.runner_strategies and tp2_beyond
        broker_tp = sig.take_profit_2 if runner else sig.take_profit

        ok, err = self.conn.validate_stops(sig.symbol, entry, sig.stop_loss, broker_tp, sig.direction.lower())
        if not ok:
            self.logger.warning("Stop validation rejected: %s", err)
            return False

        res = self.conn.send_market_order(
            symbol=sig.symbol,
            direction=sig.direction.lower(),
            volume=volume,
            stop_loss=sig.stop_loss,
            take_profit=broker_tp,
            magic=self.config.magic,
            comment=f"Q-{sig.strategy_type[:6]}",
        )
        if not res or getattr(res, "retcode", -1) != TRADE_RETCODE_DONE:
            self.logger.error("Order failed for %s: retcode=%s", sig.symbol, getattr(res, "retcode", "UNKNOWN"))
            return False

        ticket = int(getattr(res, "order", 0) or getattr(res, "deal", 0) or 0)
        if ticket <= 0:
            self.logger.error("Order for %s reported success but returned no ticket", sig.symbol)
            return False

        self.open_trades[ticket] = {
            "ticket": ticket,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "strategy_type": sig.strategy_type,
            "entry_price": entry,
            "initial_stop_loss": sig.stop_loss,
            "stop_loss": sig.stop_loss,
            "take_profit": broker_tp,          # What the broker holds
            "take_profit_1": sig.take_profit,
            "take_profit_2": sig.take_profit_2,
            "runner": runner,
            "tp1_reached": False,
            "best_price": entry,
            "expected_yield_dollars": sig.expected_yield_dollars,
            "volume": volume,
            "magic": self.config.magic,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "math_edge_score": sig.math_edge_score,
            "win_probability": sig.calculated_win_prob,
            "model_win_probability": float(
                (sig.raw_analysis.metadata if sig.raw_analysis else {}).get("model_win_prob", sig.calculated_win_prob)
            ),
            "half_life_horizon_bars": sig.half_life_horizon_bars,
            "kalman_confirmation": sig.kalman_confirmation,
            "var_risk_level": sig.var_risk_level,
            "pyramid_add": bool(same_symbol),
        }
        self._save_state()

        msg = (
            f"⚡ QUANT SYNTH ORDER EXECUTED\n"
            f"Symbol: {sig.symbol} | Direction: {sig.direction}\n"
            f"Strategy: {sig.strategy_type}\n"
            f"Volume: {volume:g} (risk {sig.sizing.risk_percentage:.2f}%)\n"
            f"Entry: {entry:.5f} | SL: {sig.stop_loss:.5f} | TP1: {sig.take_profit:.5f}"
            f"{f' | runner to {broker_tp:.5f}' if runner else ''}\n"
            f"Edge: {sig.math_edge_score}/100 | Win: {sig.calculated_win_prob:.1%}\n"
            f"Kalman: {sig.kalman_confirmation} | VaR: {sig.var_risk_level}\n"
            f"Rationale: {sig.rationale}"
        )
        self.logger.info(msg)
        self.notifier.send(msg)
        return True

    # ── Position Lifecycle ────────────────────────────────────────────────────

    @staticmethod
    def _stop_locked(t: Dict[str, Any]) -> bool:
        """True once the stop sits at or beyond entry, so the trade can no longer lose."""
        entry, sl = float(t["entry_price"]), float(t["stop_loss"])
        return sl >= entry if t["direction"] == "BUY" else sl <= entry

    def _can_pyramid(self, sig: SyntheticSignal, same_symbol: List[Dict[str, Any]]) -> bool:
        """Add only to a winner whose every leg is locked in profit, in the same direction."""
        return (
            self.config.pyramid_enabled
            and len(same_symbol) - 1 < self.config.pyramid_max_adds
            and all(t["direction"] == sig.direction for t in same_symbol)
            and all(self._stop_locked(t) for t in same_symbol)
        )

    def _extend_target(self, ticket: int, t: Dict[str, Any], price: float) -> None:
        """While the thesis holds, push a runner's broker TP out one step before price reaches it."""
        step = abs(float(t.get("take_profit_2", 0.0)) - float(t.get("take_profit_1", 0.0)))
        atr = self._atr(str(t["symbol"]))
        tp = float(t["take_profit"])
        if step <= 0 or atr <= 0 or abs(tp - price) > atr:
            return
        new_tp = tp + step if t["direction"] == "BUY" else tp - step
        res = self.conn.set_position_sltp(ticket=ticket, sl=float(t["stop_loss"]), tp=new_tp, comment="Q-TP-EXTEND")
        if res is not None and getattr(res, "retcode", TRADE_RETCODE_DONE) == TRADE_RETCODE_DONE:
            t["take_profit"] = new_tp
            self._save_state()
            self.logger.info("Trend intact on %s ticket=%d: target extended to %.5f", t["symbol"], ticket, new_tp)

    def _tighten_stop(self, ticket: int, t: Dict[str, Any], new_sl: float, comment: str) -> bool:
        """Move the stop only if it improves on the current one and complies with broker stop_level."""
        current = float(t["stop_loss"])
        direction = str(t.get("direction", "BUY")).upper()
        better = new_sl > current if direction == "BUY" else new_sl < current
        if not better:
            return False

        # Validate stop distance against broker freeze/stop level to prevent rejected modifications
        symbol = str(t["symbol"])
        rules = self.conn.get_broker_rules(symbol)
        if rules is not None:
            min_dist = max(rules.stop_level_points, rules.freeze_level_points, 1.0) * rules.point
            tick = getattr(self.conn, "get_tick", lambda s: None)(symbol)
            if tick is not None:
                if direction == "BUY":
                    max_allowed_sl = round(tick.bid - min_dist, 5)
                    if new_sl >= max_allowed_sl:
                        if max_allowed_sl > current:
                            new_sl = max_allowed_sl
                        else:
                            return False
                else:
                    min_allowed_sl = round(tick.ask + min_dist, 5)
                    if new_sl <= min_allowed_sl:
                        if min_allowed_sl < current:
                            new_sl = min_allowed_sl
                        else:
                            return False

        res = self.conn.set_position_sltp(ticket=ticket, sl=new_sl, tp=float(t["take_profit"]), comment=comment)
        if res is None or getattr(res, "retcode", TRADE_RETCODE_DONE) != TRADE_RETCODE_DONE:
            self.logger.debug("Stop update %s held on ticket=%d (market close to stop)", comment, ticket)
            return False
        t["stop_loss"] = new_sl
        self._save_state()
        return True

    def _close(self, ticket: int, comment: str) -> bool:
        res = self.conn.close_position(ticket=ticket, comment=comment)
        ok = res is not None and getattr(res, "retcode", TRADE_RETCODE_DONE) == TRADE_RETCODE_DONE
        if not ok:
            self.logger.warning("Close %s failed on ticket=%d", comment, ticket)
        return ok

    def _kalman_trailing_stop(self, symbol: str, direction: str, entry: float, r_distance: float) -> Optional[float]:
        """Stop 0.8R behind the Kalman fair value, kept on the profitable side of entry."""
        df = self._closed_bars(symbol, 60)
        if df is None or len(df) < 20:
            return None
        fair_value = fit_kalman_filter(df["close"]).fair_value
        if direction == "BUY":
            sl = fair_value - 0.8 * r_distance
            return round(sl, 5) if sl > entry else None
        sl = fair_value + 0.8 * r_distance
        return round(sl, 5) if sl < entry else None

    def _current_var_pct(self, symbol: str) -> float:
        df = self._closed_bars(symbol, 60)
        if df is None or len(df) < 20:
            return 0.0
        var_95, _ = compute_var_cvar(df, confidence=0.95, horizon_bars=5, gk_vol=compute_garman_klass_volatility(df))
        return var_95 * 100.0

    def _atr(self, symbol: str, period: int = 14) -> float:
        df = self._closed_bars(symbol, period + 1)
        if df is None or len(df) < period + 1:
            return 0.0
        prev_close = df["close"].shift(1)
        true_range = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                                (df["low"] - prev_close).abs()], axis=1).max(axis=1)
        return float(true_range.iloc[1:].mean())

    def _bank_partial(self, ticket: int, t: Dict[str, Any]) -> bool:
        """Close `runner_close_fraction` of the position; skipped when the lot step cannot split it."""
        symbol = str(t["symbol"])
        volume = float(t["volume"])
        min_vol, _, step = self.risk_manager.volume_limits(symbol, self.conn.get_broker_rules(symbol))
        part = round(int(volume * self.config.runner_close_fraction / step + 1e-9) * step, 6)
        if part < min_vol or volume - part < min_vol - 1e-9:
            self.logger.info("Volume %g on ticket=%d is too small to split; whole position rides", volume, ticket)
            return False
        res = self.conn.close_position(ticket=ticket, comment="Q-TP1-BANK", volume=part)
        if res is None or getattr(res, "retcode", TRADE_RETCODE_DONE) != TRADE_RETCODE_DONE:
            self.logger.warning("Partial close failed on ticket=%d", ticket)
            return False
        t["volume"] = round(volume - part, 6)
        t["banked_volume"] = part
        return True

    def _lock(self, entry: float, direction: str, r_distance: float, r_multiple: float) -> float:
        return entry + r_multiple * r_distance if direction == "BUY" else entry - r_multiple * r_distance

    def manage_positions(self) -> None:
        try:
            broker_pos = self.conn.get_positions() if hasattr(self.conn, "get_positions") else []
        except Exception:
            broker_pos = []

        if broker_pos:
            adopted_any = False
            for p in broker_pos:
                ticket = int(getattr(p, "ticket", 0))
                if ticket > 0 and ticket not in self.open_trades:
                    t_dir = "BUY" if getattr(p, "type", 0) == 0 else "SELL"
                    entry_p = float(getattr(p, "price_open", 0.0) or 0.0)
                    sl_p = float(getattr(p, "sl", 0.0) or 0.0)
                    tp_p = float(getattr(p, "tp", 0.0) or 0.0)
                    self.open_trades[ticket] = {
                        "ticket": ticket,
                        "symbol": str(getattr(p, "symbol", "")),
                        "direction": t_dir,
                        "volume": float(getattr(p, "volume", 0.05)),
                        "entry_price": entry_p,
                        "initial_stop_loss": sl_p,
                        "stop_loss": sl_p,
                        "take_profit": tp_p,
                        "strategy_type": "KALMAN_FAIR_VALUE_REVERSION",
                        "math_edge_score": 99.0,
                        "win_probability": 0.709,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "moved_to_be": True,
                    }
                    adopted_any = True
            if adopted_any:
                self._save_state()

        for ticket, t in list(self.open_trades.items()):
            if self.conn.get_position(ticket) is None:
                self._record_closed_position(ticket)
                continue
            try:
                self._manage_position(ticket, t)
            except Exception as exc:
                self.logger.error("Position management error on ticket=%d: %s", ticket, exc)

    def _manage_position(self, ticket: int, t: Dict[str, Any]) -> None:
        symbol = str(t["symbol"])
        direction = str(t["direction"])
        entry = float(t["entry_price"])
        r_distance = abs(entry - float(t.get("initial_stop_loss", t["stop_loss"])))
        strat = str(t.get("strategy_type", ""))
        sym_upper = symbol.upper()

        price_info = self.conn.get_symbol_price(symbol)
        if not price_info or r_distance <= 0:
            return
        price = float(price_info.bid if direction == "BUY" else price_info.ask)
        profit_r = (price - entry) / r_distance if direction == "BUY" else (entry - price) / r_distance

        # Exits first: once a position is closed nothing else should touch it
        if profit_r < 0.5 and not t.get("var_emergency_triggered"):
            var_pct = self._current_var_pct(symbol)
            if var_pct >= self.config.var_emergency_threshold and self._close(ticket, "VAR_EMERGENCY"):
                t["var_emergency_triggered"] = True
                self.logger.warning("VaR emergency exit on %s ticket=%d: VaR=%.2f%% (P&L %.2fR)",
                                    symbol, ticket, var_pct, profit_r)
                self.notifier.send(f"🚨 VaR EMERGENCY: closed {symbol} (ticket {ticket}) at {profit_r:+.2f}R, "
                                   f"VaR={var_pct:.2f}%")
                return

        if strat == "QUIESCENT_DRIFT_HARVEST" and profit_r >= 0.8 and self._close(ticket, "DRIFT_HARVEST_WIN"):
            self.logger.info("Drift profit secured on %s ticket=%d at %+.2fR", symbol, ticket, profit_r)
            self.notifier.send(f"💰 Drift profit secured: {symbol} ticket {ticket} at {profit_r:+.2f}R")
            return

        if ("BOOM" in sym_upper or "CRASH" in sym_upper) and profit_r >= self.config.spike_harvest_r \
                and self._close(ticket, "SPIKE_HARVEST"):
            self.logger.info("Spike captured on %s ticket=%d at %+.2fR", symbol, ticket, profit_r)
            self.notifier.send(f"🎯 Spike harvested: {symbol} ticket {ticket} at {profit_r:+.2f}R")
            return

        held_seconds = 0.0
        if t.get("opened_at"):
            held_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(t["opened_at"])).total_seconds()
        if self.config.max_hold_hours > 0 and held_seconds >= self.config.max_hold_hours * 3600 \
                and self._close(ticket, "MAX_HOLD_EXIT"):
            self.logger.info("Max hold reached on %s ticket=%d at %+.2fR", symbol, ticket, profit_r)
            self.notifier.send(f"⌛ Max hold time reached: closed {symbol} (ticket {ticket}) at {profit_r:+.2f}R")
            return

        half_life_bars = int(t.get("half_life_horizon_bars") or 0)
        bars_held = held_seconds / self.bar_seconds
        if half_life_bars > 0:
            # Losing trade that never developed: exit before the full stop
            if bars_held >= half_life_bars * 3.5 and profit_r <= -0.25 and self._close(ticket, "HL_STAGNATION_EXIT"):
                self.logger.info("Stagnation exit on %s ticket=%d after %.1f bars (horizon %d)",
                                 symbol, ticket, bars_held, half_life_bars)
                self.notifier.send(f"⏱️ Stagnation stop on {symbol} (ticket {ticket}) after {bars_held:.0f} bars")
                return

        # Thesis check once per new candle: hold while the entry reason holds, leave when it breaks
        latest = self._latest_analysis.get(symbol)
        if self.config.thesis_exit_enabled and latest and t.get("thesis_checked_bar") != latest[0]:
            t["thesis_checked_bar"] = latest[0]
            if not thesis_intact(latest[1], direction, strat):
                if self._close(ticket, "THESIS_BROKEN"):
                    self.logger.info("Entry thesis broken on %s ticket=%d; exited at %+.2fR", symbol, ticket, profit_r)
                    self.notifier.send(f"🧭 Setup no longer valid on {symbol} (ticket {ticket}); exited at {profit_r:+.2f}R")
                    return
            elif t.get("runner") and t.get("tp1_reached") and self.config.extend_targets:
                self._extend_target(ticket, t, price)

        # Stop tightening; each rule fires once and `_tighten_stop` never loosens
        be_thresh = 0.40 if getattr(self, "open_trades", None) else self.config.be_trigger_r
        if profit_r >= be_thresh and not t.get("moved_to_be"):
            if self._tighten_stop(ticket, t, self._lock(entry, direction, r_distance, 0.10), "Q-BE"):
                self.logger.info("Breakeven+0.10R locked on %s ticket=%d (%+.2fR)", symbol, ticket, profit_r)
                self.notifier.send(f"🛡️ Breakeven+0.10R locked on {symbol} (ticket {ticket})")
            t["moved_to_be"] = True

        if t.get("runner"):
            self._manage_runner(ticket, t, price, entry, direction, r_distance, profit_r)
            return

        # Universal Kalman & Volatility trail across all mean-reverting archetypes
        if strat in ("OU_MEAN_REVERSION", "KALMAN_FAIR_VALUE_REVERSION", "FRACTIONAL_KINETIC_SNIPE") and self.config.kalman_trail_enable and profit_r >= 0.70:
            kalman_sl = self._kalman_trailing_stop(symbol, direction, entry, r_distance)
            if kalman_sl is not None and self._tighten_stop(ticket, t, kalman_sl, "Q-KALMAN-TRAIL"):
                self.logger.info("Kalman trail on %s ticket=%d: SL→%.5f (%+.2fR)", symbol, ticket, kalman_sl, profit_r)

        if half_life_bars > 0 and bars_held >= half_life_bars * 2.5 and profit_r >= 0.6 and not t.get("half_life_locked"):
            if self._tighten_stop(ticket, t, self._lock(entry, direction, r_distance, 0.5), "Q-HL-LOCK"):
                self.logger.info("Half-life maturity on %s ticket=%d; locked +0.5R", symbol, ticket)
            t["half_life_locked"] = True

        if "STEP" in sym_upper and profit_r >= 1.2 and not t.get("step_trailing_locked"):
            step_sl = entry + 0.10 if direction == "BUY" else entry - 0.10
            if self._tighten_stop(ticket, t, step_sl, "Q-STEP-TRAIL"):
                self.logger.info("Step trail locked at +1 step on %s ticket=%d", symbol, ticket)
            t["step_trailing_locked"] = True

    def _manage_runner(self, ticket: int, t: Dict[str, Any], price: float, entry: float,
                       direction: str, r_distance: float, profit_r: float) -> None:
        """Bank part at TP1, then trail the rest `trail_atr_mult` ATRs behind the best price seen."""
        buy = direction == "BUY"
        best = max(float(t.get("best_price", entry)), price) if buy else min(float(t.get("best_price", entry)), price)
        t["best_price"] = best

        tp1 = float(t.get("take_profit_1", t["take_profit"]))
        if not t.get("tp1_reached") and (price >= tp1 if buy else price <= tp1):
            banked = self._bank_partial(ticket, t)
            t["tp1_reached"] = True
            self._tighten_stop(ticket, t, self._lock(entry, direction, r_distance, self.config.runner_lock_r), "Q-TP1-LOCK")
            self.logger.info("TP1 reached on %s ticket=%d (%+.2fR)%s; runner trailing toward %.5f",
                             t["symbol"], ticket, profit_r, ", half banked" if banked else "", float(t["take_profit"]))
            self.notifier.send(f"🚀 TP1 hit on {t['symbol']} (ticket {ticket}) at {profit_r:+.2f}R. "
                               f"{'Banked part, ' if banked else ''}runner stays open with a trailing stop.")

        atr = self._atr(str(t["symbol"]))
        if atr > 0:
            chandelier = best - self.config.trail_atr_mult * atr if buy else best + self.config.trail_atr_mult * atr
            if self._tighten_stop(ticket, t, round(chandelier, 5), "Q-ATR-TRAIL"):
                self.logger.info("ATR trail on %s ticket=%d: SL→%.5f (%+.2fR)", t["symbol"], ticket, chandelier, profit_r)

    def _realised_pnl(self, ticket: int) -> Optional[float]:
        """Net profit of a closed position from its deal history, or None if unavailable."""
        try:
            deals = self.conn.get_position_history(ticket)
        except Exception as exc:
            self.logger.debug("Deal history unavailable for ticket=%d: %s", ticket, exc)
            return None
        if not deals:
            return None
        return float(sum(float(getattr(d, f, 0.0) or 0.0) for d in deals for f in ("profit", "commission", "swap")))

    def _record_closed_position(self, ticket: int) -> None:
        t = self.open_trades.pop(ticket, None)
        if not t:
            return
        t["closed_at"] = datetime.now(timezone.utc).isoformat()
        deals = []
        try:
            deals = self.conn.get_position_history(ticket) or []
        except Exception:
            pass

        exit_price = None
        exit_comment = ""
        pnl = None
        if deals:
            pnl = float(sum(float(getattr(d, f, 0.0) or 0.0) for d in deals for f in ("profit", "commission", "swap")))
            exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
            if exit_deals:
                exit_price = float(getattr(exit_deals[-1], "price", 0.0) or 0.0)
                exit_comment = str(getattr(exit_deals[-1], "comment", "") or "")
            elif len(deals) > 1:
                exit_price = float(getattr(deals[-1], "price", 0.0) or 0.0)
                exit_comment = str(getattr(deals[-1], "comment", "") or "")
        else:
            pnl = self._realised_pnl(ticket)

        if pnl is not None:
            t["realised_pnl"] = round(pnl, 2)
            t["pnl_usd"] = round(pnl, 2)
            t["outcome"] = "🟢 WIN" if pnl > 0.001 else ("🔴 LOSS" if pnl < -0.001 else "⚪ BREAKEVEN")
            if self.brain.calibrator is not None:
                self.brain.calibrator.record(
                    str(t.get("strategy_type", "")),
                    float(t.get("model_win_probability", t.get("win_probability", 0.5))),
                    won=pnl > 0,
                )
        if exit_price:
            t["exit_price"] = round(exit_price, 4)

        if "[sl" in exit_comment.lower():
            t["close_reason"] = "TRAILING_STOP" if (pnl and pnl > 0) else "STOP_LOSS"
        elif "[tp" in exit_comment.lower():
            t["close_reason"] = "TAKE_PROFIT"
        elif not t.get("close_reason"):
            t["close_reason"] = "TRAILING_STOP" if (pnl and pnl > 0) else "CLOSED"

        entry = float(t.get("entry_price") or 0.0)
        init_sl = float(t.get("initial_stop_loss") or t.get("stop_loss") or 0.0)
        risk_dist = abs(entry - init_sl)
        if risk_dist > 0 and exit_price:
            direction = str(t.get("direction", "BUY")).upper()
            gain = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
            t["pnl_r"] = round(gain / risk_dist, 2)
        else:
            t["pnl_r"] = 1.0 if (pnl and pnl > 0) else (-1.0 if (pnl and pnl < 0) else 0.0)

        self.closed_trades.append(t)
        self._save_state()

    # ── Polling ───────────────────────────────────────────────────────────────

    def get_adaptive_poll_interval(self) -> int:
        """Poll faster when many symbols are in a non-random regime, slower when few are."""
        if not self._regime_cache:
            return self.config.poll_seconds
        tradable = sum(1 for r in self._regime_cache.values() if r in ("PERSISTENT", "ANTI_PERSISTENT"))
        ratio = tradable / len(self._regime_cache)
        if ratio >= 0.3:
            return self.config.fast_poll_seconds
        if ratio <= 0.1:
            return self.config.slow_poll_seconds
        return self.config.poll_seconds

    def run_cycle(self) -> None:
        """Scan every symbol, manage open positions against the fresh analysis, then execute
        the best new signals by expected dollars."""
        self._cycle_count += 1

        candidates: List[SyntheticSignal] = []
        for sym in self.config.symbols:
            try:
                sig = self.scan_symbol(sym)
                if sig:
                    candidates.append(sig)
            except Exception as exc:
                self.logger.error("Error scanning synthetic index %s: %s", sym, exc)

        self.manage_positions()

        candidates.sort(key=lambda s: s.expected_yield_dollars, reverse=True)
        for sig in candidates:
            if len(self.open_trades) >= self.config.max_open_trades:
                break
            self.execute_signal(sig)

        if self._cycle_count % 10 == 0 or len(self.open_trades) > 0:
            self._save_state()

        if self._cycle_count % 50 == 0:
            tradable = [s for s, r in self._regime_cache.items() if r in ("PERSISTENT", "ANTI_PERSISTENT")]
            self.logger.info(
                "Cycle %d summary: %d open, %d closed, %d/%d tradable regimes, poll=%ds",
                self._cycle_count, len(self.open_trades), len(self.closed_trades),
                len(tradable), len(self._regime_cache), self.get_adaptive_poll_interval(),
            )
