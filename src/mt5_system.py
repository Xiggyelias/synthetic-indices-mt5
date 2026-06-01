from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from mt5_connection import MT5Connection
from news_filter import NewsFilter, build_news_config_from_env
from telegram_notifier import TelegramNotifier, build_telegram_config_from_env
from trade_manager import ManagedTrade, TradeManager
from trade_supervisor import TradeSupervisor
from trading_bot_engulfing_v2 import EngulfingBotV2, Signal, TradingConfig
from market_regime import MarketRegime, RegimeClassifier
from performance_tracker import PerformanceTracker
from trade_journal import TradeJournal

# Session times in UTC (forex market sessions)
LONDON_OPEN = 7   # 07:00 UTC
LONDON_CLOSE = 16  # 16:00 UTC
NY_OPEN = 12      # 12:00 UTC
NY_CLOSE = 21     # 21:00 UTC


@dataclass
class SystemConfig:
    symbols: list[str]
    timeframe: str
    poll_seconds: int
    magic: int
    state_path: str
    avoid_news_minutes: int
    
    # NEW: Portfolio Management
    max_correlated_trades: int = 1
    correlation_map: dict = None # e.g. {"EURUSD": ["GBPUSD", "AUDUSD"]}
    
    config_json_path: str = "config/config_rsi_pullback.json"
    bot_switch_path: str = "data/bot_switch.txt"


def load_trading_config_from_json(path: str) -> TradingConfig:
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
    if "capital_allocation" in rm:
        cfg.capital_allocation = float(rm.get("capital_allocation") or 0.0)
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
    if "atr_trailing_mult" in rm:
        cfg.atr_trailing_mult = float(rm.get("atr_trailing_mult") or cfg.atr_trailing_mult)
    if "initial_capital" in rm:
        cfg.initial_capital = float(rm.get("initial_capital"))

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

    execution = (sp.get("execution") or {})
    if "max_spread_points" in execution:
        cfg.max_spread_points = int(float(execution.get("max_spread_points") or 0))
    if "max_slippage_points" in execution:
        cfg.max_slippage_points = int(float(execution.get("max_slippage_points") or cfg.max_slippage_points))
    if "friday_close_enabled" in execution:
        cfg.friday_close_enabled = bool(execution.get("friday_close_enabled"))
    if "friday_close_hour_utc" in execution:
        cfg.friday_close_hour_utc = int(float(execution.get("friday_close_hour_utc") or cfg.friday_close_hour_utc))

    cfg.sr_lookback = int(sr.get("sr_lookback", cfg.sr_lookback))
    cfg.sr_tolerance = float(sr.get("sr_tolerance", cfg.sr_tolerance))

    conf = (sp.get("confluence_settings") or {})
    if "min_confluence_score" in conf:
        cfg.min_confluence_score = int(conf.get("min_confluence_score"))
    if "ema_trend" in conf:
        cfg.ema_trend = int(conf.get("ema_trend"))
    if "macd_fast" in conf:
        cfg.macd_fast = int(conf.get("macd_fast"))
    if "macd_slow" in conf:
        cfg.macd_slow = int(conf.get("macd_slow"))
    if "macd_signal" in conf:
        cfg.macd_signal = int(conf.get("macd_signal"))
    if "stoch_k" in conf:
        cfg.stoch_k = int(conf.get("stoch_k"))
    if "stoch_d" in conf:
        cfg.stoch_d = int(conf.get("stoch_d"))
    if "stoch_smooth" in conf:
        cfg.stoch_smooth = int(conf.get("stoch_smooth"))

    return cfg


def compute_volume_from_risk(conn: MT5Connection, symbol: str, entry: float, sl: float, risk_fraction: float) -> Optional[float]:
    rules = conn.get_broker_rules(symbol)
    if rules is None:
        return None

    summary = conn.get_account_summary() or {}
    balance = float(summary.get("balance") or 0.0)
    if balance <= 0:
        return None

    risk_amount = balance * float(risk_fraction)
    price_risk = abs(float(entry) - float(sl))
    if price_risk <= 0:
        return None

    tick_size = float(getattr(rules, "tick_size", 0.0) or 0.0)
    tick_value = float(getattr(rules, "tick_value", 0.0) or 0.0)
    if tick_size <= 0 or tick_value <= 0:
        return None

    value_per_price_unit = tick_value / tick_size
    risk_per_lot = price_risk * value_per_price_unit
    if risk_per_lot <= 0:
        return None

    raw_vol = risk_amount / risk_per_lot
    return conn.normalize_volume(symbol, raw_vol)


class MT5BotSystem:
    def __init__(self, conn: MT5Connection, system_cfg: SystemConfig) -> None:
        self.conn = conn
        self.system_cfg = system_cfg
        self.logger = logging.getLogger("mt5-system")

        self.trade_manager = TradeManager(system_cfg.state_path)
        self.trade_manager.load()

        self.notifier = TelegramNotifier(build_telegram_config_from_env(os.getenv))
        self.notifier.set_status_provider(self._telegram_status_snapshot)
        self.news = NewsFilter(build_news_config_from_env(os.getenv))
        
        self.strategy_cfg = load_trading_config_from_json(system_cfg.config_json_path)
        self.supervisor = TradeSupervisor(
            conn,
            self.trade_manager,
            magic=system_cfg.magic,
            timeframe=system_cfg.timeframe,
            notifier=self.notifier,
            on_trade_closed=self._record_trade_result,
            config=self.strategy_cfg,
        )

        self._losing_streak_limit = max(1, int(getattr(self.strategy_cfg, "losing_streak_pause_after", 5) or 5))
        self.strategy = EngulfingBotV2(self.strategy_cfg)
        self.regime_classifier = RegimeClassifier(atr_multiplier=2.0)
        
        # Performance tracking
        self.performance = PerformanceTracker(state_path="data/performance_state.json")
        self.journal = TradeJournal(journal_path="data/trade_journal.json")
        self._last_weekly_report: str = ""  # ISO week string

        self._notified_conn_lost = False
        self._autotrading_last: Optional[bool] = None
        self._bot_switch_last: Optional[bool] = None
        self._news_block_last: str = ""
        self._risk_halt_last: Optional[bool] = None
        self._consecutive_losses: int = int((self.trade_manager.meta or {}).get("consecutive_losses", 0) or 0)
        self._losing_streak_paused: bool = bool((self.trade_manager.meta or {}).get("losing_streak_paused_at"))
        self._losing_streak_limit: int = max(1, int(getattr(self.strategy_cfg, "losing_streak_pause_after", 5) or 5))
        self._last_heartbeat: float = 0
        self._last_conn_check: float = time.time()
        self._conn_fail_count: int = 0
        self._last_performance_review_count: int = 0
        self._market_status_last: dict[str, str] = {}
        self._setup_commands()

    def _effective_balance(self, summary: dict) -> float:
        bal = float(summary.get("balance") or 0.0)
        cap = float(getattr(self.strategy_cfg, "capital_allocation", 0.0) or 0.0)
        if cap > 0:
            return float(min(bal, cap))
        return bal

    def _daily_loss_fraction(self, summary: Optional[dict] = None) -> float:
        summary = summary or (self.conn.get_account_summary() or {})
        eff = self._effective_balance(summary)
        if eff <= 0:
            return 0.0

        meta = self.trade_manager.meta or {}
        today = datetime.now(timezone.utc).date().isoformat()
        if meta.get("daily_date") != today:
            return 0.0

        daily_start = float(meta.get("daily_start_eff") or 0.0)
        if daily_start <= 0:
            return 0.0
        return max(0.0, (daily_start - eff) / daily_start)

    def _bot_status_text(self) -> str:
        status = self.conn.get_connection_status() or {}
        if status.get("connected") is not True:
            return "Disconnected"
        if not self._is_autotrading_enabled():
            return "Paused (AutoTrading OFF)"
        if not self._is_bot_switch_enabled():
            return "Paused (Bot switch OFF)"
        if self.trade_manager.meta.get("halted"):
            reason = str(self.trade_manager.meta.get("halt_reason") or "").strip()
            return f"Risk Halted: {reason}" if reason else "Risk Halted"
        if not self._is_weekly_market_open():
            return "Scanning (Market Closed)"
        return "Running"

    def _telegram_status_snapshot(self) -> dict:
        summary = self.conn.get_account_summary() or {}
        return {
            "daily_loss_pct": self._daily_loss_fraction(summary) * 100.0,
            "open_trades": len(
                [t for t in self.trade_manager.open_trades.values() if int(getattr(t, "magic", 0) or 0) == int(self.system_cfg.magic)]
            ),
            "bot_status": self._bot_status_text(),
        }

    def _risk_limits_gate(self) -> bool:
        summary = self.conn.get_account_summary() or {}
        eff = self._effective_balance(summary)
        if eff <= 0:
            return False

        today = datetime.now(timezone.utc).date().isoformat()
        meta = self.trade_manager.meta or {}

        if meta.get("daily_date") != today:
            meta["daily_date"] = today
            meta["daily_start_eff"] = float(eff)

        peak = float(meta.get("peak_eff") or 0.0)
        if peak <= 0 or eff > peak:
            peak = float(eff)
            meta["peak_eff"] = float(peak)

        daily_start = float(meta.get("daily_start_eff") or eff)
        daily_loss = (daily_start - eff) / daily_start if daily_start > 0 else 0.0
        drawdown = (peak - eff) / peak if peak > 0 else 0.0

        max_daily_loss = float(getattr(self.strategy_cfg, "max_daily_loss", 0.02) or 0.02)
        max_drawdown = float(getattr(self.strategy_cfg, "max_drawdown", 0.10) or 0.10)

        halted = False
        reason = ""
        if daily_loss >= max_daily_loss:
            halted = True
            reason = f"Max daily loss breached: {daily_loss * 100:.2f}% >= {max_daily_loss * 100:.2f}%"
        if drawdown >= max_drawdown:
            halted = True
            reason = f"Max drawdown breached: {drawdown * 100:.2f}% >= {max_drawdown * 100:.2f}%"

        meta["halted"] = bool(halted)
        meta["halt_reason"] = str(reason)
        
        # NEW: Update Weekly Start if not set or new week
        now = datetime.now(timezone.utc)
        if meta.get("weekly_date") != now.strftime("%Y-W%U"):
            meta["weekly_date"] = now.strftime("%Y-W%U")
            meta["weekly_start_eff"] = float(eff)
            if meta.get("capital_protection_active"):
                self.logger.info("New week: Deactivating Capital Protection Mode.")
                meta["capital_protection_active"] = False
                meta["cp_winning_streak"] = 0

        self.trade_manager.meta = meta
        self.trade_manager.save()

        # Update CP State
        self._update_capital_protection_state(summary)

        if self._risk_halt_last is None or bool(halted) != bool(self._risk_halt_last):
            if halted:
                msg = f"RISK HALT: {reason}"
                self.logger.warning(msg)
            else:
                msg = "RISK HALT CLEARED: limits OK"
                self.logger.info(msg)
            self.notifier.send(msg)
            self._risk_halt_last = bool(halted)

        return bool(halted)

    def _update_capital_protection_state(self, summary: dict) -> bool:
        meta = self.trade_manager.meta or {}
        cfg = self.strategy_cfg

        if not bool(getattr(cfg, "capital_protection_enabled", True)):
            if meta.get("capital_protection_active"):
                meta["capital_protection_active"] = False
                self.trade_manager.meta = meta
                self.trade_manager.save()
            return False

        daily_loss = self._daily_loss_fraction(summary)
        weekly_start = float(meta.get("weekly_start_eff") or summary.get("balance", 0))
        eff = self._effective_balance(summary)
        weekly_loss = (weekly_start - eff) / weekly_start if weekly_start > 0 else 0
        consecutive_losses = int(self._consecutive_losses)

        d_thresh = float(getattr(cfg, "cp_daily_loss_trigger", 0.05) or 0.05)
        w_thresh = float(getattr(cfg, "cp_weekly_loss_trigger", 0.12) or 0.12)
        c_thresh = int(getattr(cfg, "cp_consecutive_losses_trigger", 5) or 5)

        active = daily_loss >= d_thresh or weekly_loss >= w_thresh or consecutive_losses >= c_thresh

        if active and not meta.get("capital_protection_active"):
            cp_risk_pct = float(getattr(cfg, "cp_risk_fraction", 0.005) or 0.005) * 100.0
            max_td = int(getattr(cfg, "cp_max_trades_per_day", 4) or 4)
            min_sc = int(getattr(cfg, "cp_min_signal_score", 7) or 7)
            msg = (
                f"🛡️ CAPITAL PROTECTION MODE ACTIVATED!\n"
                f"- Risk capped near {cp_risk_pct:.2f}% per trade\n"
                f"- Setups score {min_sc}+ only\n"
                f"- Max {max_td} trades/day"
            )
            self.logger.warning(msg)
            self.notifier.send(msg)
            meta["capital_protection_active"] = True
            meta["cp_winning_streak"] = 0
            meta["cp_daily_trades"] = 0

        self.trade_manager.meta = meta
        self.trade_manager.save()
        return bool(meta.get("capital_protection_active", False))

    def _open_risk_amount(self) -> float:
        total = 0.0
        for ticket, t in list(self.trade_manager.open_trades.items()):
            if int(t.magic) != int(self.system_cfg.magic):
                continue
            try:
                entry = float(t.entry_price or 0.0)
                sl = float(t.stop_loss or 0.0)
                vol = float(t.volume or 0.0)
                if entry <= 0 or sl <= 0 or vol <= 0:
                    continue
                rules = self.conn.get_broker_rules(str(t.symbol))
                if rules is None:
                    continue
                tick_size = float(getattr(rules, "tick_size", 0.0) or 0.0)
                tick_value = float(getattr(rules, "tick_value", 0.0) or 0.0)
                if tick_size <= 0 or tick_value <= 0:
                    continue
                value_per_price_unit = tick_value / tick_size
                total += abs(entry - sl) * value_per_price_unit * vol
            except Exception:
                continue
        return float(total)

    def _is_autotrading_enabled(self) -> bool:
        status = self.conn.get_connection_status() or {}
        terminal_trade_allowed = status.get("trade_allowed")
        if terminal_trade_allowed is not True:
            return False

        summary = self.conn.get_account_summary() or {}
        account_trade_allowed = summary.get("trade_allowed")
        if account_trade_allowed is False:
            return False
        return True

    def _autotrading_gate(self) -> bool:
        enabled = self._is_autotrading_enabled()
        if self._autotrading_last is None or enabled != self._autotrading_last:
            if enabled:
                msg = "MT5 AutoTrading ON: bot actions resumed"
                self.logger.info(msg)
            else:
                msg = "MT5 AutoTrading OFF: bot actions paused"
                self.logger.warning(msg)
            self.notifier.send(msg)
            self._autotrading_last = enabled
        return enabled

    def _is_bot_switch_enabled(self) -> bool:
        path = str(getattr(self.system_cfg, "bot_switch_path", "") or "")
        if not path:
            return True
        try:
            if not os.path.exists(path):
                return True
            with open(path, "r", encoding="utf-8") as f:
                raw = (f.read() or "").strip().lower()
            if raw in ("0", "off", "false", "no", "disabled", "pause", "paused"):
                return False
            if raw in ("1", "on", "true", "yes", "enabled", "run", "running", "resume", "resumed"):
                return True
            return True
        except Exception:
            return True

    def _bot_switch_gate(self) -> bool:
        enabled = self._is_bot_switch_enabled()
        if self._bot_switch_last is None or enabled != self._bot_switch_last:
            if enabled:
                msg = "Bot switch ON: bot actions resumed"
                self.logger.info(msg)
            else:
                msg = "Bot switch OFF: bot actions paused"
                self.logger.warning(msg)
            self.notifier.send(msg)
            self._bot_switch_last = enabled
        return enabled

    def _rates_df(self, symbol: str, count: int = 400) -> Optional[pd.DataFrame]:
        rates = self.conn.get_rates(symbol, timeframe=self.system_cfg.timeframe, count=count)
        if rates is None:
            return None
        df = pd.DataFrame(rates)
        if df.empty:
            return None
        df["time"] = pd.to_datetime(df["time"], unit="s")
        if "tick_volume" in df.columns and "volume" not in df.columns:
            df["volume"] = df["tick_volume"]
        df = df.set_index("time")
        return df

    def _connection_guard(self) -> bool:
        ok = self.conn.ensure_connected()
        status = self.conn.get_connection_status()
        if ok and status.get("connected") is True:
            if self._notified_conn_lost:
                self.notifier.send("MT5 connection restored")
            self._notified_conn_lost = False
            return True

        if not self._notified_conn_lost:
            self.notifier.send("MT5 connection lost")
            self._notified_conn_lost = True
        return False

    def _has_open_position(self, symbol: str) -> bool:
        positions = self.conn.get_positions(symbol)
        if positions is None:
            return False
        return len(list(positions)) > 0

    def _open_position_count(
        self,
        symbol: str = "",
        direction: str = "",
        magic_only: bool = True,
    ) -> int:
        positions = self.conn.get_positions(symbol or None)
        if positions is None:
            # Local state fallback keeps risk gates usable during a transient MT5 read failure.
            count = 0
            want_symbol = str(symbol or "").upper()
            want_direction = str(direction or "").lower()
            for t in self.trade_manager.open_trades.values():
                if magic_only and int(getattr(t, "magic", 0) or 0) != int(self.system_cfg.magic):
                    continue
                if want_symbol and str(getattr(t, "symbol", "") or "").upper() != want_symbol:
                    continue
                if want_direction and str(getattr(t, "direction", "") or "").lower() != want_direction:
                    continue
                count += 1
            return count

        want_direction = str(direction or "").lower()
        want_type = None
        if want_direction == "buy":
            want_type = 0
        elif want_direction == "sell":
            want_type = 1

        count = 0
        for p in positions:
            try:
                if magic_only and int(getattr(p, "magic", 0) or 0) != int(self.system_cfg.magic):
                    continue
                if want_type is not None and int(getattr(p, "type", -1) or -1) != int(want_type):
                    continue
                count += 1
            except Exception:
                continue
        return count

    def _signal_already_traded(self, symbol: str, direction: str, candle_time: str) -> bool:
        meta = self.trade_manager.meta or {}
        traded = meta.get("last_traded_signal_by_symbol")
        if not isinstance(traded, dict):
            return False
        key = f"{str(symbol).upper()}:{str(direction).lower()}"
        return str(traded.get(key) or "") == str(candle_time)

    def _mark_signal_traded(self, symbol: str, direction: str, candle_time: str) -> None:
        meta = self.trade_manager.meta or {}
        traded = meta.get("last_traded_signal_by_symbol")
        if not isinstance(traded, dict):
            traded = {}
        key = f"{str(symbol).upper()}:{str(direction).lower()}"
        traded[key] = str(candle_time)
        meta["last_traded_signal_by_symbol"] = traded
        self.trade_manager.meta = meta
        self.trade_manager.save()

    def _resolve_position_ticket(
        self,
        symbol: str,
        direction: str,
        volume: float,
        magic: int,
    ) -> int:
        positions = self.conn.get_positions(symbol)
        if positions is None:
            return 0

        desired_type = 0 if str(direction).lower() == "buy" else 1
        candidates = []
        for p in positions:
            try:
                if int(getattr(p, "magic", 0) or 0) != int(magic):
                    continue
                if int(getattr(p, "type", -1) or -1) != int(desired_type):
                    continue
                pvol = float(getattr(p, "volume", 0.0) or 0.0)
                if abs(pvol - float(volume)) > 1e-9:
                    continue
                candidates.append(p)
            except Exception:
                continue

        if not candidates:
            return 0

        def sort_key(p) -> int:
            return int(getattr(p, "time_update_msc", 0) or getattr(p, "time_msc", 0) or getattr(p, "time", 0) or 0)

        pos = sorted(candidates, key=sort_key, reverse=True)[0]
        return int(getattr(pos, "ticket", 0) or getattr(pos, "identifier", 0) or 0)

    def _place_trade(self, symbol: str, direction: str, entry: float, sl: float, tp: float, volume: float, confluence_score: int = 0, confluence_details: dict = None, score: int = 0, regime: str = "") -> bool:
        self.logger.critical(
            "═══════════════════════════════════════════════════════════════════════════════════════\n"
            "🟢 PLACE_TRADE_START 🟢\n"
            "   Symbol: %s | Direction: %s | Entry: %.5f | SL: %.5f | TP: %.5f | Vol: %.2f\n"
            "   Score: %d | Regime: %s | Confluence: %d\n"
            "═══════════════════════════════════════════════════════════════════════════════════════",
            symbol, direction.upper(), entry, sl, tp, volume, score, regime, confluence_score
        )

        result = self.conn.send_market_order(
            symbol=symbol,
            direction=direction,
            volume=float(volume),
            stop_loss=float(sl),
            take_profit=float(tp),
            deviation=int(getattr(self.strategy_cfg, "max_slippage_points", 10) or 10),
            magic=int(self.system_cfg.magic),
            comment=f"score{score}@{datetime.now(timezone.utc).isoformat()}",
        )

        if result is None:
            err = self.conn.get_last_order_error()
            detail = str(err.get("message") or "").strip()
            stage = str(err.get("stage") or "order_send")
            self.logger.error(
                "❌ ORDER_SEND_RETURNED_NONE: %s %s | entry=%.5f sl=%.5f tp=%.5f vol=%.2f | stage=%s | detail=%s",
                direction.upper(), symbol, entry, sl, tp, volume, stage, detail
            )
            if detail:
                self.notifier.send(f"Order blocked: {symbol} {direction.upper()} vol={volume}\n{detail}")
            else:
                self.notifier.send(f"Order failed: {symbol} {direction.upper()} vol={volume}")
            return False

        retcode = int(getattr(result, "retcode", -1) or -1)
        retcode_comment = str(getattr(result, "comment", "") or "")
        order_id = int(getattr(result, "order", 0) or 0)
        deal_id = int(getattr(result, "deal", 0) or 0)
        filled_entry = float(getattr(result, "price", 0.0) or entry)

        if retcode != 10009:  # TRADE_RETCODE_DONE = 10009
            self.logger.error(
                "❌ ORDER_REJECTED: retcode=%d comment='%s' | %s %s %.2f %s",
                retcode, retcode_comment, direction.upper(), symbol, volume, "lots"
            )
            self.notifier.send(f"❌ Order rejected: {symbol} | retcode={retcode} | {retcode_comment}")
            return False

        self.logger.critical(
            "✅ ORDER_ACCEPTED: retcode=%d | order=%d | deal=%d | %s %s %.2f lots @ %.5f",
            retcode, order_id, deal_id, direction.upper(), symbol, volume, filled_entry
        )

        ticket = self._resolve_position_ticket(
            symbol=symbol,
            direction=direction,
            volume=float(volume),
            magic=int(self.system_cfg.magic),
        )
        if ticket <= 0:
            ticket = int(getattr(result, "order", 0) or 0)

        # Build confluence signal summary for Telegram
        session = self._session_name()
        conf_lines = ""
        if confluence_details:
            conf_lines = " | ".join(f"{'✅' if v else '❌'}{k}" for k, v in confluence_details.items())

        self.notifier.send_trade_execution({
            "direction": direction.upper(),
            "entry_price": str(filled_entry),
            "stop_loss": str(sl),
            "take_profit": str(tp),
            "risk_reward": str(round(abs(tp - filled_entry) / abs(filled_entry - sl), 1)) if abs(filled_entry - sl) > 0 else "N/A",
            "volume_multiplier": str(volume),
            "rsi_value": "N/A",
            "rsi_pullback": "N/A",
            "sr_tolerance": str(self.strategy_cfg.sr_tolerance),
            "daily_loss_pct": f"{self._daily_loss_fraction() * 100:.1f}",
            "open_trades": str(len([t for t in self.trade_manager.open_trades.values() if int(getattr(t, 'magic', 0) or 0) == int(self.system_cfg.magic)]) + 1),
            "news_pause_status": "Active",
            "slippage_pips": "0.0",
            "spread_points": "0",
            "trades_completed": str(len(self.trade_manager.closed_trades)),
            "confluence_score": str(confluence_score),
            "trade_score": str(score),
            "confluence_signals": conf_lines,
            "session": session,
            "regime": regime,
        })

        mt = ManagedTrade(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            volume=float(volume),
            entry_price=float(filled_entry),
            stop_loss=float(sl),
            take_profit=float(tp),
            magic=int(self.system_cfg.magic),
            comment=str(getattr(result, "comment", "") or ""),
            opened_at=datetime.now(timezone.utc).isoformat(),
            moved_to_break_even=False,
            last_known_sl=float(sl),
            last_known_tp=float(tp),
            score=score,
            regime=regime,
            planned_rr=abs(tp - filled_entry) / abs(filled_entry - sl) if abs(filled_entry - sl) > 0 else 0,
            confluence_signals=conf_lines,
            confluence_score=confluence_score,
            session=session,
        )
        self.trade_manager.upsert(mt)
        
        # Track CP daily trades
        meta = self.trade_manager.meta or {}
        if meta.get("capital_protection_active"):
            meta["cp_daily_trades"] = meta.get("cp_daily_trades", 0) + 1
            self.trade_manager.meta = meta
            self.trade_manager.save()

        self.logger.critical(
            "═══════════════════════════════════════════════════════════════════════════════════════\n"
            "✅ PLACE_TRADE_COMPLETE - POSITION RECORDED\n"
            "   Ticket: %d | Order: %d | Deal: %d | Regime: %s | Session: %s\n"
            "═══════════════════════════════════════════════════════════════════════════════════════",
            ticket, order_id, deal_id, regime, session
        )
        return True

    def _is_high_liquidity_session(self) -> bool:
        """Check if current time is within London or NY session (UTC)."""
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        in_london = LONDON_OPEN <= hour < LONDON_CLOSE
        in_ny = NY_OPEN <= hour < NY_CLOSE
        return in_london or in_ny

    def _session_name(self) -> str:
        """Return current session name for logging."""
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        in_london = LONDON_OPEN <= hour < LONDON_CLOSE
        in_ny = NY_OPEN <= hour < NY_CLOSE
        if in_london and in_ny:
            return "London+NY Overlap"
        if in_london:
            return "London"
        if in_ny:
            return "New York"
        return "Off-Session"

    def _is_weekly_market_open(self, now_utc: Optional[datetime] = None) -> bool:
        now = now_utc or datetime.now(timezone.utc)
        weekday = now.weekday()  # Monday=0, Sunday=6
        if weekday == 5:
            return False
        if weekday == 6 and now.hour < 21:
            return False
        if weekday == 4 and now.hour >= 21:
            return False
        return True

    def _market_status_key(self, is_open: bool, reason: str) -> str:
        text = str(reason or "").lower()
        if "last quote is" in text:
            text = "stale quote"
        return f"{int(is_open)}:{text}"

    def _is_closed_market_reason(self, reason: str) -> bool:
        text = str(reason or "").lower()
        closed_markers = (
            "weekly market close",
            "market appears closed",
            "no live tick",
            "quote is invalid",
            "quote time is unavailable",
        )
        return any(marker in text for marker in closed_markers)

    def _market_entry_status(self, symbol: str, direction: str = "") -> tuple[bool, str]:
        max_age = int(float(os.getenv("MARKET_OPEN_MAX_TICK_AGE_SECONDS", "1800") or 1800))
        try:
            is_open, reason = self.conn.is_symbol_tradable_now(
                symbol,
                direction=direction,
                max_tick_age_seconds=max_age,
            )
        except Exception as exc:
            self.logger.exception("Market status check failed for %s: %s", symbol, exc)
            is_open, reason = False, f"market status check failed: {exc}"

        status_key = self._market_status_key(is_open, reason)
        if self._market_status_last.get(symbol) != status_key:
            if is_open:
                msg = f"Market open for {symbol}: live entries enabled."
                self.logger.info(msg)
            else:
                if self._is_closed_market_reason(reason):
                    msg = f"Market closed for {symbol}: {reason}. Scanning only; valid setups will be queued for re-check."
                else:
                    msg = f"Market not tradable for {symbol}: {reason}. Entries blocked until this clears."
                self.logger.warning(msg)
            self.notifier.send(msg)
            self._market_status_last[symbol] = status_key

        return bool(is_open), str(reason or "")

    def _pending_market_signals(self) -> dict:
        meta = self.trade_manager.meta or {}
        pending = meta.get("pending_market_closed_signals")
        if not isinstance(pending, dict):
            pending = {}
            meta["pending_market_closed_signals"] = pending
            self.trade_manager.meta = meta
        return pending

    def _queue_closed_market_signal(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        score: int,
        confluence_score: int,
        regime: str,
        candle_time: str,
        reason: str,
    ) -> None:
        planned_rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0.0
        pending = self._pending_market_signals()
        payload = {
            "symbol": symbol,
            "direction": direction.upper(),
            "entry": float(entry),
            "stop_loss": float(sl),
            "take_profit": float(tp),
            "score": int(score),
            "confluence_score": int(confluence_score),
            "regime": str(regime),
            "planned_rr": float(planned_rr),
            "candle_time": str(candle_time),
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "reason": str(reason or "market closed"),
        }

        existing = pending.get(symbol)
        dedupe_key = (
            payload["direction"],
            payload["candle_time"],
            payload["score"],
            payload["confluence_score"],
        )
        existing_key = None
        if isinstance(existing, dict):
            existing_key = (
                existing.get("direction"),
                existing.get("candle_time"),
                existing.get("score"),
                existing.get("confluence_score"),
            )

        pending[symbol] = payload
        self.trade_manager.meta["pending_market_closed_signals"] = pending
        self.trade_manager.save()

        if existing_key == dedupe_key:
            return

        self.logger.info(
            "Closed-market setup queued: %s %s score=%s confluence=%s rr=%.2f candle=%s",
            symbol,
            payload["direction"],
            score,
            confluence_score,
            planned_rr,
            candle_time,
        )
        self.notifier.send(
            "Market closed - setup queued for re-check\n"
            f"{symbol} {payload['direction']} | score={score} | confluence={confluence_score} | RR={planned_rr:.2f}\n"
            f"Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f}\n"
            f"Reason: {reason}\n"
            "The bot will rescan and place it only if it is still valid when the broker is tradable."
        )

    def _clear_pending_closed_market_signal(self, symbol: str) -> None:
        pending = self._pending_market_signals()
        if symbol not in pending:
            return
        pending.pop(symbol, None)
        self.trade_manager.meta["pending_market_closed_signals"] = pending
        self.trade_manager.save()

    def _check_losing_streak(self) -> bool:
        """Check if we should pause due to consecutive losses. Returns True if paused."""
        if self._losing_streak_paused:
            # Auto-resume after a new candle cycle (check every poll)
            # Only resume if at least 1 hour has passed since pause
            meta = self.trade_manager.meta or {}
            paused_at = meta.get("losing_streak_paused_at")
            if paused_at:
                try:
                    paused_dt = datetime.fromisoformat(paused_at)
                    elapsed = (datetime.now(timezone.utc) - paused_dt).total_seconds()
                    cooldown = float(getattr(self.strategy_cfg, "losing_streak_cooldown_seconds", 3600.0) or 3600.0)
                    if elapsed >= cooldown:
                        self._losing_streak_paused = False
                        self._consecutive_losses = 0
                        meta["losing_streak_paused_at"] = None
                        meta["consecutive_losses"] = 0
                        self.trade_manager.meta = meta
                        self.trade_manager.save()
                        msg = "Losing streak pause lifted after cooldown. Resuming trading."
                        self.logger.info(msg)
                        self.notifier.send(msg)
                except Exception:
                    pass
            return self._losing_streak_paused
        return False

    def _is_london_ny_session(self) -> bool:
        """Check if current time is within London or NY session (UTC)."""
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        # London: 7:00 - 16:00 UTC, NY: 12:00 - 21:00 UTC
        # Covering the combined range 7:00 - 21:00 UTC
        return 7 <= hour < 21

    def _record_trade_result(self, win: bool, trade_data: dict = None) -> None:
        """Record trade result for losing streak tracking and performance metrics."""
        meta = self.trade_manager.meta or {}
        if win:
            self._consecutive_losses = 0
            self._losing_streak_paused = False
            meta["consecutive_wins"] = int(meta.get("consecutive_wins", 0) or 0) + 1
            meta["consecutive_losses"] = 0
            meta["losing_streak_paused_at"] = None
            if meta.get("capital_protection_active"):
                meta["cp_winning_streak"] = meta.get("cp_winning_streak", 0) + 1
        else:
            self._consecutive_losses += 1
            meta["consecutive_wins"] = 0
            meta["consecutive_losses"] = int(self._consecutive_losses)
            if meta.get("capital_protection_active"):
                meta["cp_winning_streak"] = 0
                # Mandatory 1-hour break after each loss in CP mode
                meta["cp_pause_until"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

            if self._consecutive_losses >= self._losing_streak_limit:
                self._losing_streak_paused = True
                meta["losing_streak_paused_at"] = datetime.now(timezone.utc).isoformat()
                msg = f"LOSING STREAK PAUSE: {self._consecutive_losses} consecutive losses. Pausing for review (1h cooldown)."
                self.logger.warning(msg)
                self.notifier.send(msg)
        
        self.trade_manager.meta = meta
        self.trade_manager.save()
        
        # Record in performance tracker
        if trade_data:
            try:
                self.performance.record_trade(
                    ticket=trade_data.get("ticket", 0),
                    symbol=trade_data.get("symbol", ""),
                    direction=trade_data.get("direction", ""),
                    entry_price=trade_data.get("entry_price", 0.0),
                    exit_price=trade_data.get("exit_price", 0.0),
                    volume=trade_data.get("volume", 0.0),
                    pnl=trade_data.get("pnl", 0.0),
                    r_achieved=trade_data.get("r_achieved", 0.0),
                    confluence_score=trade_data.get("confluence_score", 0),
                    session=trade_data.get("session", "Unknown"),
                    opened_at=trade_data.get("opened_at", ""),
                    closed_at=trade_data.get("closed_at", ""),
                    commission=trade_data.get("commission", 0.0),
                    swap=trade_data.get("swap", 0.0),
                    confluence_signals=trade_data.get("confluence_signals", {}),
                )
            except Exception as e:
                self.logger.warning("Failed to record trade in performance tracker: %s", e)
            
            # Add journal entry
            try:
                entry = self.journal.add_entry(
                    ticket=trade_data.get("ticket", 0),
                    symbol=trade_data.get("symbol", ""),
                    direction=trade_data.get("direction", ""),
                    entry_price=trade_data.get("entry_price", 0.0),
                    exit_price=trade_data.get("exit_price", 0.0),
                    volume=trade_data.get("volume", 0.0),
                    opened_at=trade_data.get("opened_at", ""),
                    closed_at=trade_data.get("closed_at", ""),
                    pnl=trade_data.get("pnl", 0.0),
                    r_achieved=trade_data.get("r_achieved", 0.0),
                    confluence_score=trade_data.get("confluence_score", 0),
                    confluence_signals=trade_data.get("confluence_signals", {}),
                    session=trade_data.get("session", "Unknown"),
                    regime=trade_data.get("regime", "Unknown"),
                    setup_type=trade_data.get("setup_type", ""),
                    initial_sl=trade_data.get("initial_sl", 0.0),
                    initial_tp=trade_data.get("initial_tp", 0.0),
                    planned_rr=trade_data.get("planned_rr", 0.0),
                    moved_to_be=trade_data.get("moved_to_be", False),
                    partial_closed=trade_data.get("partial_closed", False),
                    commission=trade_data.get("commission", 0.0),
                    swap=trade_data.get("swap", 0.0),
                )
                # Send journal summary to Telegram
                summary = self.journal.format_entry_summary(entry)
                self.notifier.send(summary)
            except Exception as e:
                self.logger.warning("Failed to add journal entry: %s", e)
        
        # Check for weekly report
        self._maybe_send_weekly_report()
        
        # Run Performance Review every 20 trades
        total_closed = len(self.trade_manager.closed_trades)
        if total_closed > 0 and total_closed % 20 == 0 and total_closed != self._last_performance_review_count:
            self._last_performance_review_count = total_closed
            self._run_performance_review()

    def _maybe_send_weekly_report(self) -> None:
        """Send weekly performance report on Sunday at 20:00 UTC."""
        now = datetime.now(timezone.utc)
        current_week = now.strftime("%Y-W%U")
        
        # Check if we already sent this week's report
        if self._last_weekly_report == current_week:
            return
        
        # Send on Sunday at 20:00 UTC (end of trading week)
        if now.weekday() == 6 and now.hour == 20:
            self._last_weekly_report = current_week
            try:
                metrics = self.performance.calculate_metrics(period_days=7)
                report = self.performance.format_weekly_report(metrics)
                self.notifier.send(report)
                self.logger.info("Sent weekly performance report")
            except Exception as e:
                self.logger.warning("Failed to send weekly report: %s", e)

    def _run_performance_review(self):
        closed = self.trade_manager.closed_trades
        if len(closed) < 20:
            return
        last_20 = closed[-20:]
        wins = [t for t in last_20 if float(t.get('profit_loss', 0)) > 0]
        win_rate = len(wins) / 20
        
        msg = f"📊 PERFORMANCE REVIEW (Last 20 Trades)\nWin Rate: {win_rate*100:.1f}%\n"
        if win_rate < 0.45:
            msg += "⚠️ Win rate below 45%! Consider tightening entry confluence."
        self.notifier.send(msg)

    def evaluate_and_trade(self, symbol: str) -> None:
        # Friday Close Logic
        if self.strategy_cfg.friday_close_enabled:
            now_utc = datetime.now(timezone.utc)
            if now_utc.weekday() == 4 and now_utc.hour >= self.strategy_cfg.friday_close_hour_utc:
                if self._has_open_position(symbol):
                    self.logger.warning("Friday Close active: closing all positions for %s", symbol)
                    self.notifier.send(f"⚠️ Friday Close active: closing all positions for {symbol} to avoid weekend gap risk.")
                    self.conn.close_all_positions(symbol)
                return

        market_open_for_scan, _ = self._market_entry_status(symbol)

        df = self._rates_df(symbol)
        if df is None or len(df) < 250:
            self.logger.debug("[EVAL_%s] Data check blocked: df_len=%s", symbol, len(df) if df is not None else None)
            return
        df = self.strategy.calculate_indicators(df)

        idx = len(df) - 2
        candle_time = str(df.index[idx].isoformat())
        
        # HTF Trend Check (D1) — gate early, before any heavy computation
        htf_trend_aligned = True
        mtf_df = self.conn.get_rates(symbol, "D1", count=100)
        if mtf_df is not None:
            mtf_df = pd.DataFrame(mtf_df)
            is_bullish_signal = self.strategy.is_bullish_engulfing(df, idx)
            htf_trend_aligned = self.strategy.check_mtf_trend(mtf_df, Signal.BUY if is_bullish_signal else Signal.SELL)

        if not htf_trend_aligned:
            if market_open_for_scan:
                self._clear_pending_closed_market_signal(symbol)
            self.logger.info("[EVAL_%s] HTF_TREND_MISMATCH: D1 trend not aligned with H1 signal", symbol)
            return  # fast-exit: no point running regime + signal if D1 disagrees

        # Macro Context & Session Checks
        is_london_ny = self._is_london_ny_session()

        # News filter — cache result (single network call; used for both regime + news gate)
        self.news.config.avoid_minutes = 120
        news_blocked, news_reason = self.news.is_blocked(symbol)

        self.logger.info(
            "[EVAL_%s] NEWS_FILTER_CHECK: blocked=%s | reason=%s",
            symbol, news_blocked, news_reason if news_blocked else "(none)"
        )

        # MARKET REGIME CLASSIFICATION
        regime = self.regime_classifier.classify(df, news_blocked)
        self.logger.info("[EVAL_%s] REGIME_CLASSIFIED: regime=%s | news_blocked=%s", symbol, regime.value, news_blocked)
        
        if bool(getattr(self.strategy_cfg, "skip_weak_range_entries", False)) and regime == MarketRegime.RANGING:
            if market_open_for_scan:
                self._clear_pending_closed_market_signal(symbol)
            self.logger.info("[EVAL_%s] SKIP_WEAK_RANGE: ranging chop filter enabled", symbol)
            return
        
        # Capital Protection Mode Checks
        meta = self.trade_manager.meta or {}
        is_cp_active = bool(meta.get("capital_protection_active", False))
        if is_cp_active:
            cp_max_day = int(getattr(self.strategy_cfg, "cp_max_trades_per_day", 4) or 4)
            if meta.get("cp_daily_trades", 0) >= cp_max_day:
                self.logger.info("[EVAL_%s] CP_MAX_DAILY_TRADES: %d >= %d", symbol, meta.get("cp_daily_trades", 0), cp_max_day)
                return
            # Break after loss
            pause_until = meta.get("cp_pause_until")
            if pause_until and datetime.now(timezone.utc) < datetime.fromisoformat(pause_until):
                self.logger.info("[EVAL_%s] CP_PAUSE_ACTIVE: paused until %s", symbol, pause_until)
                return

        sig, score, risk_mult = self.strategy.generate_signal(
            df, idx, regime=regime, is_london_ny=is_london_ny, htf_trend_aligned=htf_trend_aligned
        )

        if sig not in (Signal.BUY, Signal.SELL):
            if market_open_for_scan:
                self._clear_pending_closed_market_signal(symbol)
            self.logger.debug("[EVAL_%s] NO_SIGNAL: sig=%s", symbol, sig)
            return
        direction = "buy" if sig == Signal.BUY else "sell"
            
        # 2. Meaningful level? (Handled by scoring/strategy)
        
        # 3. Confluence — must meet configured minimum (aligned with strategy.generate_signal)
        conf_score, conf_details = self.strategy.confluence_score(df, idx, sig, regime=regime)
        min_conf = int(getattr(self.strategy_cfg, "min_confluence_score", 3) or 3)
        if conf_score < min_conf:
            if market_open_for_scan:
                self._clear_pending_closed_market_signal(symbol)
            self.logger.info("[EVAL_%s] CONFLUENCE_BLOCKED: score=%d < min=%d | details=%s", symbol, conf_score, min_conf, conf_details)
            return
            
        # 4. Planned R:R execution floor
        sl, tp = self.strategy.calculate_stop_take_profit(df, idx, sig, regime=regime)
        entry = df.iloc[idx]['close']
        planned_rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        min_rr_ex = float(getattr(self.strategy_cfg, "min_planned_rr_execution", 0.3) or 0.3)
        if min_rr_ex > 0 and planned_rr < min_rr_ex:
            if market_open_for_scan:
                self._clear_pending_closed_market_signal(symbol)
            self.logger.info("[EVAL_%s] RR_BLOCKED_EARLY: planned_rr=%.2f < min=%.2f", symbol, planned_rr, min_rr_ex)
            return

        cp_min_score = int(getattr(self.strategy_cfg, "cp_min_signal_score", 7) or 7)
        if is_cp_active and score < cp_min_score:
            if market_open_for_scan:
                self._clear_pending_closed_market_signal(symbol)
            self.logger.info("[EVAL_%s] CP_SIGNAL_SCORE_BLOCKED: score=%d < min=%d", symbol, score, cp_min_score)
            return

        if self._signal_already_traded(symbol, direction, candle_time):
            self.logger.info(
                "[EVAL_%s] DUPLICATE_SIGNAL_BLOCKED: %s setup on candle %s was already traded",
                symbol,
                direction.upper(),
                candle_time,
            )
            return

        max_open_trades = int(getattr(self.strategy_cfg, "max_open_trades", 0) or 0)
        if max_open_trades > 0:
            open_count = self._open_position_count(magic_only=True)
            if open_count >= max_open_trades:
                self.logger.info("[EVAL_%s] MAX_OPEN_TRADES: %d >= %d", symbol, open_count, max_open_trades)
                return

        symbol_count = self._open_position_count(symbol=symbol, magic_only=True)
        direction_count = self._open_position_count(symbol=symbol, direction=direction, magic_only=True)

        allow_opposite = bool(getattr(self.strategy_cfg, "allow_opposite_direction_positions", False))
        if symbol_count > direction_count and not allow_opposite:
            self.logger.info(
                "[EVAL_%s] OPPOSITE_DIRECTION_BLOCKED: existing opposite-side position is open",
                symbol,
            )
            return

        max_symbol_trades = int(getattr(self.strategy_cfg, "max_open_trades_per_symbol", 1) or 1)
        if max_symbol_trades > 0:
            if symbol_count >= max_symbol_trades:
                self.logger.info(
                    "[EVAL_%s] MAX_SYMBOL_TRADES: %d >= %d",
                    symbol,
                    symbol_count,
                    max_symbol_trades,
                )
                return

        max_direction_trades = int(getattr(self.strategy_cfg, "max_open_trades_per_direction", 1) or 1)
        if max_direction_trades > 0:
            if direction_count >= max_direction_trades:
                self.logger.info(
                    "[EVAL_%s] MAX_DIRECTION_TRADES: %s count %d >= %d",
                    symbol,
                    direction.upper(),
                    direction_count,
                    max_direction_trades,
                )
                return

        # Session filter: only trade during high-liquidity sessions
        if not self._is_high_liquidity_session():
            if market_open_for_scan:
                self.logger.debug("[EVAL_%s] OFF_SESSION: not in London or NY session", symbol)
                return
            self.logger.debug("[EVAL_%s] OFF_SESSION while market is closed; continuing scan-only.", symbol)

        # Losing streak pause
        if self._check_losing_streak():
            self.logger.info("[EVAL_%s] LOSING_STREAK: paused due to consecutive losses", symbol)
            return

        # ═════════════════════════════════════════════════════════════════════════════════════════════════════
        # CRITICAL: NEWS_MODE BLOCKING SECTION
        # ═════════════════════════════════════════════════════════════════════════════════════════════════════
        if regime == MarketRegime.NEWS_MODE:
            key = f"{symbol}:NEWS_MODE"
            if key != self._news_block_last:
                self.logger.critical(
                    "[EVAL_%s] ⛔ NEWS_MODE_BLOCKING_TRADE ⛔ | REASON: %s | AVOID_WINDOW_MINS: %d",
                    symbol, news_reason, self.news.config.avoid_minutes
                )
                self.notifier.send(f"⚠️ NEWS MODE Detected! Standing aside on {symbol}.\nReason: {news_reason}")
                self._news_block_last = key
            return
        # ═════════════════════════════════════════════════════════════════════════════════════════════════════

        if self._news_block_last.startswith(f"{symbol}:"):
            self._news_block_last = ""

        # Risk adjustment for VOLATILE
        risk_multiplier = 1.0
        if regime == MarketRegime.VOLATILE:
            vol_mult = float(getattr(self.strategy_cfg, "volatile_regime_risk_multiplier", 0.78) or 0.78)
            self.logger.warning(
                "[EVAL_%s] VOLATILE_MODE: reducing risk to %.0f%% of base",
                symbol,
                vol_mult * 100.0,
            )
            risk_multiplier = vol_mult

        market_open, market_reason = self._market_entry_status(symbol, direction)
        if not market_open:
            if not self._is_closed_market_reason(market_reason):
                self.logger.warning("[EVAL_%s] ENTRY_BLOCKED_NOT_TRADABLE: %s", symbol, market_reason)
                return
            self._queue_closed_market_signal(
                symbol=symbol,
                direction=direction,
                entry=float(entry),
                sl=float(sl),
                tp=float(tp),
                score=int(score),
                confluence_score=int(conf_score),
                regime=str(regime.value),
                candle_time=candle_time,
                reason=market_reason,
            )
            return

        tick = self.conn.get_symbol_price(symbol)
        if tick is None:
            self.logger.error("[EVAL_%s] TICK_FAILED: cannot get current price", symbol)
            return
        rules = self.conn.get_broker_rules(symbol)
        if rules is None:
            self.logger.error("[EVAL_%s] RULES_FAILED: cannot get broker rules", symbol)
            return

        spread_points = 0.0
        spread_price  = 0.0
        try:
            if float(rules.point) > 0:
                spread_points = (float(tick.ask) - float(tick.bid)) / float(rules.point)
                spread_price  = float(tick.ask) - float(tick.bid)
        except Exception:
            spread_points = 0.0

        # Fixed-point spread gate (optional, config-driven; 0 = disabled)
        max_spread = float(getattr(self.strategy_cfg, "max_spread_points", 0) or 0)
        if max_spread > 0 and spread_points > max_spread:
            self.logger.info("[EVAL_%s] SPREAD_BLOCKED: spread=%.1f pts > max=%.1f pts", symbol, spread_points, max_spread)
            return

        # ATR-relative spread gate — works across EURUSD (pips) and XAUUSD ($)
        # Skip if spread > 15 % of current ATR (catches unusually wide spreads near news)
        try:
            _atr_now = float(df.iloc[idx].get("ATR", 0) or 0)
            if _atr_now > 0 and spread_price > _atr_now * 0.15:
                self.logger.info(
                    "[EVAL_%s] SPREAD_ATR_BLOCKED: spread=%.5f > atr*15%=%.5f",
                    symbol, spread_price, _atr_now * 0.15,
                )
                return
        except Exception:
            pass

        entry = float(tick.ask if sig == Signal.BUY else tick.bid)
        sl, tp = self.strategy.calculate_stop_take_profit(df, idx, sig, entry_price=entry, regime=regime)

        min_rr_ex = float(getattr(self.strategy_cfg, "min_planned_rr_execution", 0.3) or 0.3)
        if min_rr_ex > 0 and abs(entry - sl) > 0:
            planned_rr_live = abs(tp - entry) / abs(entry - sl)
            if planned_rr_live < min_rr_ex:
                self.logger.info(
                    "[EVAL_%s] RR_BLOCKED_FINAL: planned_rr=%.2f < min=%.2f",
                    symbol,
                    planned_rr_live,
                    min_rr_ex,
                )
                return

        ok, stop_err = self.conn.validate_stops(symbol, entry, float(sl), float(tp), direction=direction)
        if not ok:
            self.logger.error("[EVAL_%s] STOP_VALIDATION_FAILED: %s", symbol, stop_err)
            return

        summary = self.conn.get_account_summary() or {}
        eff = self._effective_balance(summary)
        if eff <= 0:
            self.logger.error("[EVAL_%s] EFFECTIVE_BALANCE_INVALID: eff=%.2f", symbol, eff)
            return

        # Margin safety: don't trade if margin level is below 200 %
        margin_level = float(summary.get("margin_level") or 0.0)
        if 0.0 < margin_level < 200.0:
            self.logger.warning(
                "[EVAL_%s] LOW_MARGIN: level=%.1f%% (need >= 200%%)",
                symbol,
                margin_level
            )
            return

        # Correlation Filter
        if self._is_correlated_trade_open(symbol):
            self.logger.info("[EVAL_%s] CORRELATED_POSITION_OPEN: skipped to avoid correlation", symbol)
            return

        # Dynamic Risk Sizing (Drawdown Protection + Regime adjustment + Scoring)
        risk_fraction = self._calculate_dynamic_risk() * risk_multiplier * risk_mult
        
        # Capital Protection Mode Risk Override
        if is_cp_active:
            risk_fraction = float(getattr(self.strategy_cfg, "cp_risk_fraction", 0.005) or 0.005)
        else:
            max_risk_fraction = float(getattr(self.strategy_cfg, "max_risk_per_trade", 0.02) or 0.02)
            if max_risk_fraction > 0:
                risk_fraction = min(float(risk_fraction), max_risk_fraction)
        risk_fraction = max(0.0, float(risk_fraction))
        
        tick_size = float(getattr(rules, "tick_size", 0.0) or 0.0)
        tick_value = float(getattr(rules, "tick_value", 0.0) or 0.0)
        if tick_size <= 0 or tick_value <= 0:
            self.logger.error("[EVAL_%s] TICK_SIZE_INVALID: tick_size=%.6f tick_value=%.6f", symbol, tick_size, tick_value)
            return

        value_per_price_unit = tick_value / tick_size
        price_risk = abs(float(entry) - float(sl))
        if price_risk <= 0:
            self.logger.error("[EVAL_%s] PRICE_RISK_INVALID: entry=%.5f sl=%.5f", symbol, entry, sl)
            return

        risk_amount_money = float(eff) * float(risk_fraction)
        risk_per_lot = float(price_risk) * float(value_per_price_unit)
        if risk_per_lot <= 0:
            self.logger.error("[EVAL_%s] RISK_PER_LOT_INVALID", symbol)
            return

        raw_vol = risk_amount_money / risk_per_lot
        vol = self.conn.normalize_volume(symbol, raw_vol)
        if vol is None or float(vol) <= 0:
            self.logger.error("[EVAL_%s] VOLUME_INVALID: normalized_vol=%s", symbol, vol)
            return

        new_risk_amount = abs(float(entry) - float(sl)) * float(value_per_price_unit) * float(vol)
        max_open_risk = float(getattr(self.strategy_cfg, "max_total_open_risk", 0.03) or 0.03) * float(eff)
        open_risk = self._open_risk_amount()
        if (open_risk + new_risk_amount) > max_open_risk:
            self.logger.warning(
                "[EVAL_%s] EXPOSURE_LIMIT_BLOCKED: open_risk=%.2f + new=%.2f > max=%.2f",
                symbol,
                open_risk,
                new_risk_amount,
                max_open_risk,
            )
            self.notifier.send(
                f"Exposure limit reached: open_risk={open_risk:.2f} + new_risk={new_risk_amount:.2f} > max_open_risk={max_open_risk:.2f}"
            )
            return

        # ═════════════════════════════════════════════════════════════════════════════════════════════════════
        # REACHED ORDER SUBMISSION ✅
        # ═════════════════════════════════════════════════════════════════════════════════════════════════════
        session = self._session_name()
        self.logger.critical(
            "✅ [EVAL_%s] PASSING ALL GATES - SUBMITTING ORDER 🟢\n"
            "   Signal=%s | Entry=%.5f | SL=%.5f | TP=%.5f | Vol=%.2f\n"
            "   Session=%s | Score=%d | Regime=%s | Confluence=%d",
            symbol, direction.upper(), entry, sl, tp, vol,
            session, score, regime.value, conf_score
        )

        placed = self._place_trade(
            symbol, direction, entry, float(sl), float(tp), float(vol),
            confluence_score=conf_score, confluence_details=conf_details,
            score=score, regime=regime.value
        )
        if placed:
            self._mark_signal_traded(symbol, direction, candle_time)
            self._clear_pending_closed_market_signal(symbol)

    def _is_correlated_trade_open(self, symbol: str) -> bool:
        """Check if a trade is already open in a correlated symbol."""
        correlation_map = {
            "EURUSD": ["GBPUSD", "AUDUSD", "NZDUSD"],
            "GBPUSD": ["EURUSD", "AUDUSD"],
            "XAUUSD": ["XAGUSD"],
            "USDCAD": ["USDJPY"], # USD strength pairs
        }
        correlated_pairs = correlation_map.get(symbol, [])
        for cp in correlated_pairs:
            if self._has_open_position(cp):
                self.logger.info("Correlation Filter: Skipping %s because %s is open", symbol, cp)
                return True
        return False

    def _calculate_dynamic_risk(self) -> float:
        """Scale risk down during drawdown and up only after positive conditions."""
        base_risk = float(getattr(self.strategy_cfg, "risk_per_trade", 0.005) or 0.005)
        max_risk = float(getattr(self.strategy_cfg, "max_risk_per_trade", base_risk) or base_risk)
        summary = self.conn.get_account_summary() or {}
        meta = self.trade_manager.meta or {}
        dd = self._daily_loss_fraction(summary)
        soft = float(getattr(self.strategy_cfg, "dynamic_risk_daily_dd_soft", 0.025) or 0.025)
        hard = float(getattr(self.strategy_cfg, "dynamic_risk_daily_dd_hard", 0.05) or 0.05)
        m_soft = float(getattr(self.strategy_cfg, "dynamic_risk_soft_multiplier", 0.88) or 0.88)
        m_hard = float(getattr(self.strategy_cfg, "dynamic_risk_hard_multiplier", 0.72) or 0.72)
        risk = base_risk
        reason = "base"

        if hard > 0 and dd >= hard:
            risk = base_risk * m_hard
            reason = "hard_drawdown"
        elif soft > 0 and dd >= soft:
            risk = base_risk * m_soft
            reason = "soft_drawdown"
        else:
            boost_enabled = bool(getattr(self.strategy_cfg, "dynamic_risk_boost_enabled", True))
            cp_active = bool(meta.get("capital_protection_active", False))
            halted = bool(meta.get("halted", False))
            if boost_enabled and not cp_active and not halted:
                wins = int(meta.get("consecutive_wins", 0) or 0)
                wins_needed = int(getattr(self.strategy_cfg, "dynamic_risk_boost_after_wins", 3) or 3)
                min_gain = float(getattr(self.strategy_cfg, "dynamic_risk_boost_min_daily_gain", 0.01) or 0.01)
                boost_mult = float(getattr(self.strategy_cfg, "dynamic_risk_boost_multiplier", 1.25) or 1.25)

                eff = self._effective_balance(summary)
                daily_start = float(meta.get("daily_start_eff") or eff or 0.0)
                daily_gain = ((eff - daily_start) / daily_start) if daily_start > 0 else 0.0
                streak_ok = wins_needed > 0 and wins >= wins_needed
                gain_ok = min_gain > 0 and daily_gain >= min_gain

                if streak_ok or gain_ok:
                    risk = base_risk * max(1.0, boost_mult)
                    reason = "boost_winning_streak" if streak_ok else "boost_daily_gain"

        if max_risk > 0:
            risk = min(risk, max_risk)
        risk = max(0.0, float(risk))

        current_pct = round(risk * 100.0, 4)
        if (
            meta.get("dynamic_risk_reason") != reason
            or float(meta.get("dynamic_risk_current_pct", -1.0) or -1.0) != current_pct
        ):
            meta["dynamic_risk_reason"] = reason
            meta["dynamic_risk_current_pct"] = current_pct
            meta["dynamic_risk_base_pct"] = round(base_risk * 100.0, 4)
            meta["dynamic_risk_max_pct"] = round(max_risk * 100.0, 4)
            self.trade_manager.meta = meta
            self.trade_manager.save()

        return risk

    def _send_heartbeat(self) -> None:
        """Send a periodic 12-hour status heartbeat to Telegram."""
        now = time.time()
        if now - getattr(self, "_last_heartbeat", 0) < 43200:
            return
        self._last_heartbeat = now
        try:
            acc_info  = self.conn.get_account_info()
            balance   = float(getattr(acc_info, "balance", 0.0) or 0.0) if acc_info else 0.0
            is_active = self._is_bot_switch_enabled() and self._is_autotrading_enabled()
            icon      = "🟢" if is_active else "🔴"
            label     = "Trading Active" if is_active else "Paused"
            open_cnt  = len([t for t in self.trade_manager.open_trades.values()
                             if int(getattr(t, "magic", 0) or 0) == int(self.system_cfg.magic)])
            daily_dd  = self._daily_loss_fraction() * 100.0
            tg_msg = (
                f"❤️ HEARTBEAT — {icon} {label}\n"
                f"Balance: ${balance:,.2f}  |  Open trades: {open_cnt}\n"
                f"Daily P&L: {'-' if daily_dd > 0 else '+'}{daily_dd:.2f}%"
            )
            self.logger.info("Heartbeat: %s active=%s balance=%.2f daily_dd=%.2f%%",
                             label, is_active, balance, daily_dd)
            self.notifier.send(tg_msg)
        except Exception as exc:
            self.logger.warning("Heartbeat error: %s", exc)
    
    def _setup_commands(self) -> None:
        # Core commands
        self.notifier.register_command("/status",    self._cmd_status)
        self.notifier.register_command("/stop",      self._cmd_stop)
        self.notifier.register_command("/start",     self._cmd_start)
        self.notifier.register_command("/report",    self._cmd_report)
        # New conversational commands
        self.notifier.register_command("/help",      self._cmd_help)
        self.notifier.register_command("/menu",      self._cmd_help)
        self.notifier.register_command("/balance",   self._cmd_balance)
        self.notifier.register_command("/positions", self._cmd_positions)
        self.notifier.register_command("/pause",     self._cmd_stop)    # alias
        self.notifier.register_command("/resume",    self._cmd_start)   # alias
        self.notifier.register_command("/risk",      self._cmd_risk)
    
    def _cmd_report(self) -> str:
        # Import report generator
        sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "scripts")))
        try:
            import generate_report
            path, metrics = generate_report.generate_report(
                state_path=self.system_cfg.state_path,
                output_path=os.path.join("logs", f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
            )
            if path:
                self.notifier.send_photo(path, caption=f"📊 Performance Report\n\n{metrics}")
                return "✅ Report generated and sent!"
            else:
                return f"❌ Failed to generate report: {metrics}"
        except Exception as exc:
            self.logger.exception("Error in /report command: %s", exc)
            return f"❌ Error: {exc}"
    
    def _cmd_status(self) -> str:
        try:
            acc       = self.conn.get_account_info()
            positions = self.conn.get_positions()
            pos_count = len(list(positions)) if positions else 0
            is_active = self._is_bot_switch_enabled() and self._is_autotrading_enabled()
            icon      = "🟢 ACTIVE" if is_active else "🔴 PAUSED"
            balance   = float(getattr(acc, "balance", 0.0) or 0.0) if acc else 0.0
            equity    = float(getattr(acc, "equity",  0.0) or 0.0) if acc else 0.0
            login     = int(getattr(acc, "login", 0) or 0) if acc else 0
            daily_dd  = self._daily_loss_fraction() * 100.0
            meta      = self.trade_manager.meta or {}
            cp_line   = "\n🛡️ Capital Protection ACTIVE" if meta.get("capital_protection_active") else ""
            weekly_open = self._is_weekly_market_open()
            if is_active and not weekly_open:
                icon = "🟡 SCAN-ONLY"
            pending = meta.get("pending_market_closed_signals") if isinstance(meta.get("pending_market_closed_signals"), dict) else {}
            pending_line = f"\nQueued Setups:{len(pending):>5}" if pending else ""
            return (
                f"📊 BOT STATUS\n"
                f"{'─'*26}\n"
                f"Status:      {icon}\n"
                f"Market:      {'Open' if weekly_open else 'Closed (scan only)'}\n"
                f"Account:     {login}\n"
                f"Balance:     ${balance:,.2f}\n"
                f"Equity:      ${equity:,.2f}\n"
                f"Open Trades: {pos_count}\n"
                f"Daily Loss:  {daily_dd:.2f}%\n"
                f"Symbols:     {', '.join(self.system_cfg.symbols)}\n"
                f"Timeframe:   {self.system_cfg.timeframe}"
                f"{pending_line}"
                f"{cp_line}"
            )
        except Exception as exc:
            self.logger.exception("_cmd_status error: %s", exc)
            return f"❌ Could not get status: {exc}"

    def _cmd_stop(self) -> str:
        with open(self.system_cfg.bot_switch_path, "w") as f:
            f.write("off")
        return "🛑 Bot has been PAUSED. No new trades will be taken."

    def _cmd_start(self) -> str:
        with open(self.system_cfg.bot_switch_path, "w") as f:
            f.write("on")
        return "🚀 Bot has been RESUMED. Scanning for signals..."

    def _cmd_help(self) -> None:
        """Send the interactive main menu with inline keyboard buttons."""
        text = (
            "🤖 RSI-Pullback Trading Bot\n\n"
            "What would you like to do? Tap a button or type a command:"
        )
        self.notifier.send_menu(text)
        return None  # response already sent via send_menu

    def _cmd_balance(self) -> str:
        """Detailed account balance snapshot."""
        try:
            acc = self.conn.get_account_info()
            summary = self.conn.get_account_summary() or {}
            daily_loss_pct = self._daily_loss_fraction(summary) * 100.0
            balance  = float(getattr(acc, "balance",     0.0) or 0.0)
            equity   = float(getattr(acc, "equity",      0.0) or 0.0)
            margin_f = float(getattr(acc, "margin_free", 0.0) or 0.0)
            pnl      = equity - balance
            pnl_sign = "+" if pnl >= 0 else "-"
            cap_lim  = float(getattr(self.strategy_cfg, "capital_allocation", 0.0) or 0.0)
            cap_str  = f"${cap_lim:,.0f}" if cap_lim > 0 else "Full balance"
            return (
                f"💰 ACCOUNT BALANCE\n"
                f"{'─'*24}\n"
                f"Balance:      ${balance:,.2f}\n"
                f"Equity:       ${equity:,.2f}\n"
                f"Free Margin:  ${margin_f:,.2f}\n"
                f"Floating P&L: {pnl_sign}${abs(pnl):,.2f}\n"
                f"Daily Loss:   {daily_loss_pct:.2f}% / {float(getattr(self.strategy_cfg,'max_daily_loss',0.06))*100:.0f}% max\n"
                f"Capital Limit:{cap_str}"
            )
        except Exception as exc:
            self.logger.exception("_cmd_balance error: %s", exc)
            return f"❌ Could not fetch balance: {exc}"

    def _cmd_positions(self) -> str:
        """List every open position with entry, current P&L, and magic."""
        try:
            positions = self.conn.get_positions()
            pos_list  = list(positions) if positions else []
            if not pos_list:
                return "📈 No open positions right now."

            lines = [f"📈 OPEN POSITIONS ({len(pos_list)})", "─" * 28]
            for p in pos_list:
                ptype   = int(getattr(p, "type",       0) or 0)
                side    = "BUY" if ptype == 0 else "SELL"
                symbol  = str(getattr(p, "symbol",     "?"))
                volume  = float(getattr(p, "volume",   0.0) or 0.0)
                entry   = float(getattr(p, "price_open", 0.0) or 0.0)
                sl      = float(getattr(p, "sl",        0.0) or 0.0)
                tp      = float(getattr(p, "tp",        0.0) or 0.0)
                profit  = float(getattr(p, "profit",    0.0) or 0.0)
                magic   = int(getattr(p, "magic",      0)   or 0)
                emoji   = "💚" if profit >= 0 else "🔴"
                mine    = " ✦" if magic == int(self.system_cfg.magic) else ""
                lines.append(
                    f"{emoji} {symbol} {side} {volume}L{mine}\n"
                    f"   Entry: {entry:.5f}  SL: {sl:.5f}  TP: {tp:.5f}\n"
                    f"   P&L: ${profit:+.2f}"
                )
            return "\n".join(lines)
        except Exception as exc:
            self.logger.exception("_cmd_positions error: %s", exc)
            return f"❌ Could not fetch positions: {exc}"

    def _cmd_risk(self) -> str:
        """Current risk configuration and dynamic adjustments."""
        try:
            cfg        = self.strategy_cfg
            meta       = self.trade_manager.meta or {}
            cp_active  = bool(meta.get("capital_protection_active", False))
            base_r     = float(getattr(cfg, "risk_per_trade",        0.01) or 0.01) * 100
            max_r      = float(getattr(cfg, "max_risk_per_trade",    0.02) or 0.02) * 100
            max_dd     = float(getattr(cfg, "max_drawdown",          0.15) or 0.15) * 100
            max_daily  = float(getattr(cfg, "max_daily_loss",        0.06) or 0.06) * 100
            max_open   = float(getattr(cfg, "max_total_open_risk",  0.075) or 0.075) * 100
            max_trades = int(getattr(cfg, "max_open_trades", 3) or 3)
            max_symbol = int(getattr(cfg, "max_open_trades_per_symbol", 1) or 1)
            max_dir    = int(getattr(cfg, "max_open_trades_per_direction", 1) or 1)
            opposite   = bool(getattr(cfg, "allow_opposite_direction_positions", False))
            streak_lim = int(getattr(cfg, "losing_streak_pause_after", 6) or 6)
            current_r  = self._calculate_dynamic_risk() * 100
            cp_r       = float(getattr(cfg, "cp_risk_fraction", 0.006) or 0.006) * 100
            wins       = int(meta.get("consecutive_wins", 0) or 0)
            boost_on   = bool(getattr(cfg, "dynamic_risk_boost_enabled", True))
            boost_need = int(getattr(cfg, "dynamic_risk_boost_after_wins", 3) or 3)
            boost_mult = float(getattr(cfg, "dynamic_risk_boost_multiplier", 1.25) or 1.25)
            risk_state = str(meta.get("dynamic_risk_reason", "base") or "base")
            cp_status = f"🛡️ ACTIVE — {cp_r:.2f}% risk" if cp_active else "✅ Normal mode"
            return (
                f"⚙️ RISK SETTINGS\n"
                f"{'─'*24}\n"
                f"Base risk/trade:    {base_r:.2f}%\n"
                f"Current risk:       {current_r:.3f}% (dyn. adjusted)\n"
                f"Risk state:         {risk_state}\n"
                f"Max risk/trade:     {max_r:.2f}%\n"
                f"Max daily loss:     {max_daily:.0f}%\n"
                f"Max drawdown:       {max_dd:.0f}%\n"
                f"Open risk limit:    {max_open:.1f}%\n"
                f"Open trade slots:   {max_trades} total / {max_symbol} per symbol / {max_dir} per side\n"
                f"Opposite-side hedge:{'allowed' if opposite else 'blocked'}\n"
                f"Losing streak pause:{streak_lim} losses\n"
                f"Risk boost:         {'ON' if boost_on else 'OFF'} ({wins}/{boost_need} wins, x{boost_mult:.2f})\n"
                f"Capital Protection: {cp_status}"
            )
        except Exception as exc:
            self.logger.exception("_cmd_risk error: %s", exc)
            return f"❌ Could not fetch risk settings: {exc}"

    def _restart_terminal(self) -> None:
        """Emergency restart of the MT5 terminal (VPS watchdog)."""
        terminal_path = os.getenv("MT5_TERMINAL_PATH", "")
        if not terminal_path or not os.path.exists(terminal_path):
            self.logger.error("Watchdog: MT5_TERMINAL_PATH not found, cannot restart.")
            return

        self.logger.warning("Watchdog: Disconnected for too long. Restarting MT5 terminal...")
        self.notifier.send("🚨 MT5 disconnected 30 min. Restarting terminal...")

        # Kill existing instance cleanly
        try:
            subprocess.run(["taskkill", "/f", "/im", "terminal64.exe"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        time.sleep(5)

        # Launch MT5 terminal (non-blocking)
        try:
            subprocess.Popen(
                [terminal_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
        except Exception as exc:
            self.logger.error("Watchdog: Failed to launch terminal: %s", exc)
            return

        self.logger.info("Watchdog: MT5 launched, waiting 45s for it to connect...")
        time.sleep(45)

        if self.conn.initialize():
            self.logger.info("Watchdog: MT5 reconnected successfully.")
            self.notifier.send("✅ MT5 terminal restarted and reconnected.")
        else:
            self.logger.error("Watchdog: MT5 still not connected after restart.")

    def run_forever(self, confirm_trading: bool) -> None:
        self.logger.info("System starting | magic=%s | symbols=%s | tf=%s", self.system_cfg.magic, self.system_cfg.symbols, self.system_cfg.timeframe)
        self._last_heartbeat = 0  # force immediate heartbeat on start
        self._shutdown_requested = False

        # ── Graceful shutdown on SIGTERM / SIGINT (VPS reboot / Ctrl+C) ──────
        def _handle_signal(signum, frame):
            self.logger.warning("Shutdown signal received (%s). Exiting cleanly...", signum)
            self.notifier.send("🛑 Bot shutting down (system signal). Positions left open.")
            self._shutdown_requested = True

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT,  _handle_signal)
        # ──────────────────────────────────────────────────────────────────────

        _reconnect_wait = int(self.system_cfg.poll_seconds)  # start at poll interval
        while not self._shutdown_requested:
            self.notifier.poll_updates()
            self._send_heartbeat()
            
            # Watchdog: Restart terminal if disconnected for too long
            if not self.conn.connected:
                self._conn_fail_count += 1
                if self._conn_fail_count >= 30: # 30 polls * 60s = 30 mins
                    self._restart_terminal()
                    self._conn_fail_count = 0
            else:
                self._conn_fail_count = 0

            if not self._connection_guard():
                # Exponential backoff: double wait each failure up to 5 min max
                _reconnect_wait = min(_reconnect_wait * 2, 300)
                self.logger.warning("Connection guard failed. Retrying in %ds...", _reconnect_wait)
                time.sleep(_reconnect_wait)
                continue

            _reconnect_wait = int(self.system_cfg.poll_seconds)  # reset on success

            autotrading_enabled = self._autotrading_gate()
            bot_enabled = self._bot_switch_gate()
            allow_actions = autotrading_enabled and bot_enabled

            risk_halted = self._risk_limits_gate()
            weekly_market_open = self._is_weekly_market_open()

            # 1) Always sync/adopt existing positions; modify SL/TP only if AutoTrading is enabled
            self.supervisor.manage_all(allow_modify=allow_actions and weekly_market_open)

            # 2) Optional entry logic (also gated by AutoTrading)
            if confirm_trading and allow_actions and (not risk_halted):
                for sym in self.system_cfg.symbols:
                    self.evaluate_and_trade(sym)

            time.sleep(max(1, int(self.system_cfg.poll_seconds)))

        # Clean shutdown
        self.logger.info("Bot loop exited cleanly.")
        try:
            self.conn.shutdown()
        except Exception:
            pass


def build_system_config_from_env(symbols: list[str], timeframe: str, poll_seconds: int) -> SystemConfig:
    magic = int(float(os.getenv("BOT_MAGIC", 0) or 0))
    state_path = str(os.getenv("TRADES_STATE_PATH", "data/trades_state.json") or "data/trades_state.json")
    avoid_news_minutes = int(float(os.getenv("AVOID_NEWS_MINUTES", 60) or 60))
    cfg_json = str(os.getenv("CONFIG_JSON", "config/config_rsi_pullback.json") or "config/config_rsi_pullback.json")
    bot_switch_path = str(os.getenv("BOT_SWITCH_PATH", "data/bot_switch.txt") or "data/bot_switch.txt")
    return SystemConfig(
        symbols=symbols,
        timeframe=timeframe,
        poll_seconds=int(poll_seconds),
        magic=magic,
        state_path=state_path,
        avoid_news_minutes=avoid_news_minutes,
        config_json_path=cfg_json,
        bot_switch_path=bot_switch_path,
    )


def load_env() -> None:
    if load_dotenv is None:
        return
    load_dotenv(override=False)
