#!/usr/bin/env python3
"""
Forward-Testing Metrics Tracker
Tracks live performance vs backtest results for the MT5 Engulfing Bot.

Fixes applied:
- Unique trade IDs (ticket-based, no duplicates)
- Daily PnL computed from actual trades (not hardcoded)
- MySQL storage with JSON fallback
- backtest_reference updated to 70% win-rate target
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

# Allow imports from src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from database import DatabaseManager
    _DB_MODULE_OK = True
except ImportError:
    _DB_MODULE_OK = False

import pandas as pd


@dataclass
class LiveTrade:
    """Live trade execution data"""
    trade_id: int
    direction: str          # BUY / SELL
    entry_time: datetime
    entry_price: float
    expected_entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    profit_loss: Optional[float] = None
    slippage_pips: float = 0.0
    spread_at_entry: float = 0.0
    news_affected: bool = False
    exit_reason: Optional[str] = None   # SL / TP / BE / TRAIL / MANUAL


@dataclass
class DailyMetrics:
    """Daily performance metrics"""
    date: datetime
    trades_taken: int
    total_pnl: float
    total_slippage: float
    max_spread: float
    uptime_percentage: float
    news_blocks: int
    connection_issues: int


class ForwardTestTracker:
    """Tracks forward-testing performance against backtest expectations."""

    def __init__(self, config_file: str = "config/forward_test_config.json", telegram_notifier=None):
        self.config_file = config_file
        self.telegram_notifier = telegram_notifier
        self.trades: List[LiveTrade] = []
        self.daily_metrics: List[DailyMetrics] = []
        self.start_date = datetime.now()
        self.logger = self._setup_logger()
        self.last_milestone_check = 0

        # FIX: target win-rate aligned to strategy goal (75%)
        self.backtest_reference = {
            "expected_win_rate": 0.75,
            "expected_profit_factor": 1.06,
            "expected_expectancy": 0.20,
            "expected_avg_slippage": 0.5,
            "max_acceptable_slippage": 1.5,
            "target_trades": 20,
            "test_duration_days": 30,
        }

        # MySQL
        self._db: Optional[DatabaseManager] = None
        self._use_db = False
        if _DB_MODULE_OK:
            try:
                self._db = DatabaseManager.from_env()
                self._use_db = self._db.available
            except Exception as e:
                self.logger.warning("DB unavailable: %s — using JSON fallback", e)

        if self._use_db:
            self.logger.info("ForwardTestTracker using MySQL")
            self._load_from_db()
        else:
            self.load_state()

    # ── setup ─────────────────────────────────────────────────────────────────
    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("ForwardTestTracker")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            os.makedirs("logs", exist_ok=True)
            fh = logging.FileHandler("logs/forward_test.log")
            fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            logger.addHandler(ch)
        return logger

    # ── persistence ───────────────────────────────────────────────────────────
    def save_state(self) -> None:
        """Persist state to MySQL or JSON config file."""
        if self._use_db:
            return  # writes happen inline

        def _serial(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return str(obj)

        state = {
            "trades": [asdict(t) for t in self.trades],
            "daily_metrics": [asdict(d) for d in self.daily_metrics],
            "start_date": self.start_date.isoformat(),
        }
        os.makedirs(os.path.dirname(self.config_file) or ".", exist_ok=True)
        tmp = self.config_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=_serial)
        os.replace(tmp, self.config_file)

    def load_state(self) -> None:
        """Load state from JSON config file."""
        try:
            with open(self.config_file, "r") as f:
                state = json.load(f)

            self.trades = []
            for t in state.get("trades", []):
                try:
                    self.trades.append(LiveTrade(
                        trade_id=int(t["trade_id"]),
                        direction=t["direction"],
                        entry_time=datetime.fromisoformat(t["entry_time"]) if t.get("entry_time") else datetime.now(),
                        entry_price=float(t["entry_price"]),
                        expected_entry_price=float(t.get("expected_entry_price", t["entry_price"])),
                        exit_time=datetime.fromisoformat(t["exit_time"]) if t.get("exit_time") else None,
                        exit_price=float(t["exit_price"]) if t.get("exit_price") is not None else None,
                        profit_loss=float(t["profit_loss"]) if t.get("profit_loss") is not None else None,
                        slippage_pips=float(t.get("slippage_pips", 0.0)),
                        spread_at_entry=float(t.get("spread_at_entry", 0.0)),
                        news_affected=bool(t.get("news_affected", False)),
                        exit_reason=t.get("exit_reason"),
                    ))
                except Exception as ex:
                    self.logger.warning("Skipping malformed trade entry: %s", ex)

            self.daily_metrics = []
            for d in state.get("daily_metrics", []):
                try:
                    self.daily_metrics.append(DailyMetrics(
                        date=datetime.fromisoformat(d["date"]) if isinstance(d["date"], str) else d["date"],
                        trades_taken=int(d["trades_taken"]),
                        total_pnl=float(d["total_pnl"]),
                        total_slippage=float(d["total_slippage"]),
                        max_spread=float(d["max_spread"]),
                        uptime_percentage=float(d["uptime_percentage"]),
                        news_blocks=int(d["news_blocks"]),
                        connection_issues=int(d["connection_issues"]),
                    ))
                except Exception as ex:
                    self.logger.warning("Skipping malformed daily metric: %s", ex)

            self.start_date = datetime.fromisoformat(state.get("start_date", datetime.now().isoformat()))
        except FileNotFoundError:
            self.logger.info("No existing state found, starting fresh")
        except Exception as e:
            self.logger.error("Error loading state: %s", e)

    def _load_from_db(self) -> None:
        """Load trades and daily metrics from MySQL."""
        try:
            rows = self._db.fetch_ft_trades(include_open=True)
            self.trades = []
            for r in rows:
                self.trades.append(LiveTrade(
                    trade_id=int(r["trade_id"]),
                    direction=str(r["direction"]),
                    entry_time=r["entry_time"] if isinstance(r["entry_time"], datetime) else datetime.fromisoformat(str(r["entry_time"])),
                    entry_price=float(r["entry_price"]),
                    expected_entry_price=float(r["expected_entry_price"]),
                    exit_time=r.get("exit_time"),
                    exit_price=float(r["exit_price"]) if r.get("exit_price") is not None else None,
                    profit_loss=float(r["profit_loss"]) if r.get("profit_loss") is not None else None,
                    slippage_pips=float(r.get("slippage_pips", 0.0)),
                    spread_at_entry=float(r.get("spread_at_entry", 0.0)),
                    news_affected=bool(r.get("news_affected", False)),
                    exit_reason=r.get("exit_reason"),
                ))
        except Exception as e:
            self.logger.error("DB load failed: %s", e)

        try:
            rows = self._db.fetch_daily_metrics(days=90)
            self.daily_metrics = []
            for r in rows:
                d = r.get("metric_date")
                if d and not isinstance(d, datetime):
                    d = datetime.combine(d, datetime.min.time())
                self.daily_metrics.append(DailyMetrics(
                    date=d or datetime.now(),
                    trades_taken=int(r.get("trades_taken", 0)),
                    total_pnl=float(r.get("total_pnl", 0.0)),
                    total_slippage=float(r.get("total_slippage", 0.0)),
                    max_spread=float(r.get("max_spread", 0.0)),
                    uptime_percentage=float(r.get("uptime_percentage", 100.0)),
                    news_blocks=int(r.get("news_blocks", 0)),
                    connection_issues=int(r.get("connection_issues", 0)),
                ))
        except Exception as e:
            self.logger.error("DB daily metrics load failed: %s", e)

    # ── recording ─────────────────────────────────────────────────────────────
    def record_trade_entry(self, trade_data: Dict) -> None:
        """Record a new trade entry. trade_data must contain a unique 'ticket'."""
        try:
            ticket = int(trade_data["ticket"])

            # FIX: prevent duplicate trade IDs — skip if already recorded
            if any(t.trade_id == ticket for t in self.trades):
                self.logger.warning("Trade %d already recorded — skipping duplicate entry", ticket)
                return

            trade = LiveTrade(
                trade_id=ticket,
                direction=trade_data["direction"],
                entry_time=datetime.fromisoformat(trade_data["entry_time"]),
                entry_price=float(trade_data["entry_price"]),
                expected_entry_price=float(trade_data.get("expected_price", trade_data["entry_price"])),
                slippage_pips=float(trade_data.get("slippage_pips", 0.0)),
                spread_at_entry=float(trade_data.get("spread", 0.0)),
                news_affected=trade_data.get("news_affected", False),
            )
            self.trades.append(trade)
            self.logger.info("Trade Entry Recorded: #%d %s @ %.5f (Slippage: %.1f pips)",
                             ticket, trade.direction, trade.entry_price, trade.slippage_pips)

            if self._use_db:
                self._db.upsert_ft_trade({
                    "trade_id": ticket, "direction": trade.direction,
                    "entry_time": trade.entry_time, "entry_price": trade.entry_price,
                    "expected_entry_price": trade.expected_entry_price,
                    "exit_time": None, "exit_price": None, "profit_loss": None,
                    "slippage_pips": trade.slippage_pips,
                    "spread_at_entry": trade.spread_at_entry,
                    "news_affected": trade.news_affected, "exit_reason": None,
                })

            if self.telegram_notifier:
                self.telegram_notifier.send_trade_execution(trade_data)

            if trade.slippage_pips > self.backtest_reference["max_acceptable_slippage"]:
                if self.telegram_notifier:
                    self.telegram_notifier.send_alert("HIGH_SLIPPAGE",
                        f"Slippage {trade.slippage_pips:.1f} pips exceeds limit", "WARNING")

            self.save_state()
        except Exception as e:
            self.logger.error("Error recording trade entry: %s", e)

    def record_trade_exit(self, trade_data: Dict) -> None:
        """Record trade exit."""
        try:
            ticket = int(trade_data["ticket"])
            trade = next((t for t in self.trades if t.trade_id == ticket), None)
            if not trade:
                self.logger.error("Trade %d not found for exit", ticket)
                return

            trade.exit_time = datetime.fromisoformat(trade_data["exit_time"])
            trade.exit_price = float(trade_data["exit_price"])
            trade.profit_loss = float(trade_data["profit_loss"])
            trade.exit_reason = trade_data.get("exit_reason", "UNKNOWN")

            self.logger.info("Trade Exit Recorded: #%d %s PnL: $%.2f (%s)",
                             ticket, trade.direction, trade.profit_loss, trade.exit_reason)

            if self._use_db:
                self._db.upsert_ft_trade({
                    "trade_id": ticket, "direction": trade.direction,
                    "entry_time": trade.entry_time, "entry_price": trade.entry_price,
                    "expected_entry_price": trade.expected_entry_price,
                    "exit_time": trade.exit_time, "exit_price": trade.exit_price,
                    "profit_loss": trade.profit_loss,
                    "slippage_pips": trade.slippage_pips,
                    "spread_at_entry": trade.spread_at_entry,
                    "news_affected": trade.news_affected,
                    "exit_reason": trade.exit_reason,
                })

            if self.telegram_notifier:
                self.telegram_notifier.send_trade_exit(trade_data)

            self._check_milestones()
            self.save_state()
        except Exception as e:
            self.logger.error("Error recording trade exit: %s", e)

    def record_daily_metrics(self, metrics: Dict) -> None:
        """
        Record daily performance.
        FIX: total_pnl is now computed from actual trades for the given date
        if not explicitly provided, preventing stale hardcoded values.
        """
        try:
            date_val = datetime.fromisoformat(metrics["date"])
            date_str = date_val.date()

            # FIX: compute PnL from actual closed trades on this day if not supplied
            if metrics.get("total_pnl") is None or metrics.get("_auto_pnl", False):
                day_trades = [
                    t for t in self.trades
                    if t.exit_time and t.exit_time.date() == date_str and t.profit_loss is not None
                ]
                computed_pnl = sum(t.profit_loss for t in day_trades)
                computed_count = len(day_trades)
            else:
                computed_pnl = float(metrics["total_pnl"])
                computed_count = int(metrics["trades_taken"])

            daily = DailyMetrics(
                date=date_val,
                trades_taken=computed_count,
                total_pnl=computed_pnl,
                total_slippage=float(metrics.get("total_slippage", 0.0)),
                max_spread=float(metrics.get("max_spread", 0.0)),
                uptime_percentage=float(metrics.get("uptime_percentage", 100.0)),
                news_blocks=int(metrics.get("news_blocks", 0)),
                connection_issues=int(metrics.get("connection_issues", 0)),
            )
            self.daily_metrics.append(daily)
            self.logger.info("Daily Metrics Recorded: %d trades, PnL: $%.2f",
                             daily.trades_taken, daily.total_pnl)

            if self._use_db:
                self._db.upsert_daily_metrics({
                    "metric_date": date_str,
                    "trades_taken": daily.trades_taken,
                    "total_pnl": daily.total_pnl,
                    "total_slippage": daily.total_slippage,
                    "max_spread": daily.max_spread,
                    "uptime_percentage": daily.uptime_percentage,
                    "news_blocks": daily.news_blocks,
                    "connection_issues": daily.connection_issues,
                })

            if daily.total_pnl < 0 and abs(daily.total_pnl) > 150:
                if self.telegram_notifier:
                    self.telegram_notifier.send_alert("DAILY_LOSS_LIMIT",
                        f"Daily loss ${daily.total_pnl:.2f} approaching limit", "WARNING")

            if daily.uptime_percentage < 99.0:
                if self.telegram_notifier:
                    self.telegram_notifier.send_alert("CONNECTIVITY_ISSUE",
                        f"Uptime {daily.uptime_percentage:.1f}% below 99%", "ERROR")

            self.save_state()
        except Exception as e:
            self.logger.error("Error recording daily metrics: %s", e)

    # ── analysis ──────────────────────────────────────────────────────────────
    def calculate_current_metrics(self) -> Dict:
        """Calculate current forward-testing metrics."""
        if not self.trades:
            return {"status": "No trades recorded"}

        completed = [t for t in self.trades if t.exit_time is not None]
        if not completed:
            return {"status": "No completed trades"}

        wins = [t for t in completed if (t.profit_loss or 0) > 0]
        losses = [t for t in completed if (t.profit_loss or 0) < 0]

        win_rate = len(wins) / len(completed)
        total_profit = sum(t.profit_loss for t in wins)
        total_loss = abs(sum(t.profit_loss for t in losses))
        profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
        expectancy = sum(t.profit_loss for t in completed) / len(completed)
        avg_slippage = sum(t.slippage_pips for t in self.trades) / len(self.trades)
        avg_spread = sum(t.spread_at_entry for t in self.trades) / len(self.trades)

        return {
            "total_trades": len(completed),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "avg_slippage_pips": avg_slippage,
            "max_slippage": max(t.slippage_pips for t in self.trades),
            "avg_spread_pips": avg_spread,
            "max_spread_pips": max(t.spread_at_entry for t in self.trades),
            "total_pnl": sum(t.profit_loss for t in completed),
            "test_duration_days": (datetime.now() - self.start_date).days,
            "target_completion": len(completed) >= self.backtest_reference["target_trades"],
        }

    def _check_milestones(self) -> None:
        completed = len([t for t in self.trades if t.exit_time is not None])
        metrics = self.calculate_current_metrics()
        milestones = {5: "Trade #5 Reached", 10: "Trade #10 Reached",
                      15: "Trade #15 Reached", 20: "20 Trades — Forward Test Complete!"}
        for count, label in milestones.items():
            if completed == count and self.last_milestone_check < count:
                self._send_milestone(label, metrics)
                self.last_milestone_check = count
                break
        test_days = (datetime.now() - self.start_date).days
        time_milestones = {7: (100, "1 Week Completed"), 15: (101, "2 Weeks Completed"),
                           30: (102, "30 Days Completed")}
        for days, (flag, label) in time_milestones.items():
            if test_days == days and self.last_milestone_check < flag:
                self._send_milestone(label, metrics)
                self.last_milestone_check = flag
                break

    def _send_milestone(self, label: str, metrics: Dict) -> None:
        if self.telegram_notifier:
            self.telegram_notifier.send_milestone(label, {
                "total_trades": metrics.get("total_trades", 0),
                "win_rate": metrics.get("win_rate", 0),
                "profit_factor": metrics.get("profit_factor", 0),
                "expectancy": metrics.get("expectancy", 0),
                "total_pnl": metrics.get("total_pnl", 0),
                "avg_slippage": metrics.get("avg_slippage_pips", 0),
                "test_days": (datetime.now() - self.start_date).days,
            })

    def generate_report(self) -> str:
        """Generate comprehensive forward-testing report."""
        m = self.calculate_current_metrics()
        if "status" in m:
            return m["status"]

        ref = self.backtest_reference
        report = f"""
=== FORWARD-TESTING REPORT ===
Test Duration: {m['test_duration_days']} days
Target Progress: {m['total_trades']}/{ref['target_trades']} trades

PERFORMANCE METRICS:
Win Rate:       {m['win_rate']:.1%}  (Target: {ref['expected_win_rate']:.1%})
Profit Factor:  {m['profit_factor']:.2f}  (Target: {ref['expected_profit_factor']:.2f})
Expectancy:     ${m['expectancy']:.2f}  (Target: ${ref['expected_expectancy']:.2f})
Total P&L:      ${m['total_pnl']:.2f}

EXECUTION QUALITY:
Avg Slippage:  {m['avg_slippage_pips']:.1f} pips  (Limit: {ref['max_acceptable_slippage']} pips)
Max Slippage:  {m['max_slippage']:.1f} pips
Avg Spread:    {m['avg_spread_pips']:.1f} pips
Max Spread:    {m['max_spread_pips']:.1f} pips
"""
        if m["avg_slippage_pips"] > ref["max_acceptable_slippage"]:
            adj = m["profit_factor"] / (m["avg_slippage_pips"] / ref["max_acceptable_slippage"])
            report += f"\nWARNING: High slippage! Adjusted PF: {adj:.2f}\n"
        else:
            report += "\nSlippage within acceptable limits.\n"

        report += "\nRECOMMENDATIONS:\n"
        if m["win_rate"] < ref["expected_win_rate"] * 0.9:
            report += "- Win rate below 90% of target — review entry quality\n"
        if m["profit_factor"] < ref["expected_profit_factor"] * 0.9:
            report += "- Profit factor below target — review exit management\n"
        if m["avg_slippage_pips"] > ref["max_acceptable_slippage"]:
            report += "- High slippage — consider VPS location or broker review\n"

        if m["total_trades"] >= ref["target_trades"]:
            report += "\nTARGET TRADES REACHED — Forward test complete!\n"
            passed = (m["win_rate"] >= ref["expected_win_rate"] * 0.9 and
                      m["profit_factor"] >= ref["expected_profit_factor"] * 0.9)
            report += "PASSED — Ready for live consideration\n" if passed else "FAILED — Requires optimisation\n"

        return report.strip()


if __name__ == "__main__":
    tracker = ForwardTestTracker()
    print(tracker.generate_report())
