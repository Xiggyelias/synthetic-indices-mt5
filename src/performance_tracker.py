"""
Performance Metrics Tracker
===========================
Tracks win rate, profit factor, average R, Sharpe ratio, and other metrics.
Sends weekly performance reports via Telegram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import json
import os
from collections import defaultdict

try:
    from database import DatabaseManager
    _DB_MODULE_OK = True
except ImportError:
    _DB_MODULE_OK = False


@dataclass
class TradeRecord:
    """Single trade record for performance tracking"""
    ticket: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    volume: float
    pnl: float  # Net PnL including commission/swap
    r_achieved: float
    confluence_score: int
    session: str
    opened_at: str
    closed_at: str
    win: bool
    commission: float = 0.0
    swap: float = 0.0
    confluence_signals: Dict[str, bool] = field(default_factory=dict)


@dataclass
class PerformanceMetrics:
    """Aggregated performance metrics"""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    avg_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    sharpe_ratio: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    current_streak: int = 0  # Positive = wins, negative = losses
    
    # Session breakdown
    session_stats: Dict[str, Dict] = field(default_factory=dict)
    
    # Symbol breakdown
    symbol_stats: Dict[str, Dict] = field(default_factory=dict)
    
    # Time period
    period_start: str = ""
    period_end: str = ""


class PerformanceTracker:
    """Tracks and calculates performance metrics. Stores in MySQL, falls back to JSON."""

    def __init__(self, state_path: str = "data/performance_state.json", db: "DatabaseManager" = None):
        self.state_path = state_path
        self.trades: List[TradeRecord] = []
        self.logger = logging.getLogger("performance-tracker")
        self._db: Optional["DatabaseManager"] = db
        self._use_db = False

        if db is None and _DB_MODULE_OK:
            try:
                self._db = DatabaseManager.from_env()
                self._use_db = self._db.available
            except Exception as e:
                self.logger.warning("DB init failed: %s — using JSON fallback", e)

        if self._use_db:
            self.logger.info("PerformanceTracker using MySQL")
            self._load_from_db()
        else:
            self._load()
    
    def _load(self) -> None:
        """Load performance state from JSON (fallback)."""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            self.trades = []
            for t in raw.get("trades", []):
                try:
                    self.trades.append(TradeRecord(
                        ticket=int(t.get("ticket", 0)),
                        symbol=str(t.get("symbol", "")),
                        direction=str(t.get("direction", "")),
                        entry_price=float(t.get("entry_price", 0.0)),
                        exit_price=float(t.get("exit_price", 0.0)),
                        volume=float(t.get("volume", 0.0)),
                        pnl=float(t.get("pnl", 0.0)),
                        r_achieved=float(t.get("r_achieved", 0.0)),
                        confluence_score=int(t.get("confluence_score", 0)),
                        session=str(t.get("session", "Unknown")),
                        opened_at=str(t.get("opened_at", "")),
                        closed_at=str(t.get("closed_at", "")),
                        win=bool(t.get("win", False)),
                        commission=float(t.get("commission", 0.0)),
                        swap=float(t.get("swap", 0.0)),
                        confluence_signals=t.get("confluence_signals", {}),
                    ))
                except Exception as e:
                    self.logger.warning("Failed to load trade record: %s", e)
        except Exception as e:
            self.logger.error("Failed to load performance state: %s", e)

    def _load_from_db(self) -> None:
        """Load recent trades from MySQL."""
        try:
            rows = self._db.fetch_trades(period_days=365)
            self.trades = []
            for t in rows:
                try:
                    self.trades.append(TradeRecord(
                        ticket=int(t.get("ticket", 0)),
                        symbol=str(t.get("symbol", "")),
                        direction=str(t.get("direction", "")),
                        entry_price=float(t.get("entry_price", 0.0)),
                        exit_price=float(t.get("exit_price", 0.0)),
                        volume=float(t.get("volume", 0.0)),
                        pnl=float(t.get("pnl", 0.0)),
                        r_achieved=float(t.get("r_achieved", 0.0)),
                        confluence_score=int(t.get("confluence_score", 0)),
                        session=str(t.get("session", "Unknown")),
                        opened_at=str(t.get("opened_at", "")),
                        closed_at=str(t.get("closed_at", "")),
                        win=bool(t.get("win", False)),
                        commission=float(t.get("commission", 0.0)),
                        swap=float(t.get("swap", 0.0)),
                        confluence_signals=t.get("confluence_signals") or {},
                    ))
                except Exception as e:
                    self.logger.warning("Failed to load DB trade: %s", e)
        except Exception as e:
            self.logger.error("Failed to load from DB: %s", e)
    
    def save(self) -> None:
        """Persist the most-recently-appended trade to MySQL or JSON."""
        if self._use_db:
            return  # record_trade() writes directly; nothing to flush
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "trades": [
                {
                    "ticket": t.ticket, "symbol": t.symbol, "direction": t.direction,
                    "entry_price": t.entry_price, "exit_price": t.exit_price,
                    "volume": t.volume, "pnl": t.pnl, "r_achieved": t.r_achieved,
                    "confluence_score": t.confluence_score, "session": t.session,
                    "opened_at": t.opened_at, "closed_at": t.closed_at, "win": t.win,
                    "commission": t.commission, "swap": t.swap,
                    "confluence_signals": t.confluence_signals,
                }
                for t in self.trades
            ],
        }
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.state_path)
    
    def record_trade(
        self,
        ticket: int,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        volume: float,
        pnl: float,
        r_achieved: float,
        confluence_score: int,
        session: str,
        opened_at: str,
        closed_at: str,
        commission: float = 0.0,
        swap: float = 0.0,
        confluence_signals: Dict[str, bool] = None,
    ) -> None:
        """Record a completed trade"""
        trade = TradeRecord(
            ticket=ticket, symbol=symbol, direction=direction,
            entry_price=entry_price, exit_price=exit_price, volume=volume,
            pnl=pnl, r_achieved=r_achieved, confluence_score=confluence_score,
            session=session, opened_at=opened_at, closed_at=closed_at,
            win=pnl > 0, commission=commission, swap=swap,
            confluence_signals=confluence_signals or {},
        )
        self.trades.append(trade)

        if self._use_db:
            try:
                self._db.insert_trade({
                    "ticket": ticket, "symbol": symbol, "direction": direction,
                    "entry_price": entry_price, "exit_price": exit_price,
                    "volume": volume, "pnl": pnl, "r_achieved": r_achieved,
                    "confluence_score": confluence_score, "session": session,
                    "opened_at": opened_at, "closed_at": closed_at,
                    "win": pnl > 0, "commission": commission, "swap": swap,
                    "confluence_signals": confluence_signals or {},
                })
            except Exception as e:
                self.logger.warning("DB insert failed, falling back to JSON: %s", e)
                self.save()
        else:
            self.save()

        self.logger.info(
            "Recorded trade: %s %s ticket=%d pnl=%.2f R=%.2f win=%s",
            symbol, direction.upper(), ticket, pnl, r_achieved, trade.win
        )
    
    def calculate_metrics(self, period_days: int = 7) -> PerformanceMetrics:
        """Calculate performance metrics for the given period"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        
        # Filter trades in period
        period_trades = []
        for t in self.trades:
            try:
                closed_dt = datetime.fromisoformat(t.closed_at.replace("Z", "+00:00"))
                if closed_dt >= cutoff:
                    period_trades.append(t)
            except Exception:
                continue
        
        if not period_trades:
            return PerformanceMetrics(
                period_start=cutoff.isoformat(),
                period_end=datetime.now(timezone.utc).isoformat(),
            )
        
        wins = [t for t in period_trades if t.win]
        losses = [t for t in period_trades if not t.win]
        
        total_pnl = sum(t.pnl for t in period_trades)
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        
        # Win rate
        win_rate = len(wins) / len(period_trades) * 100 if period_trades else 0
        
        # Profit factor
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        # Average R
        r_values = [t.r_achieved for t in period_trades]
        avg_r = sum(r_values) / len(r_values) if r_values else 0
        avg_win_r = sum(t.r_achieved for t in wins) / len(wins) if wins else 0
        avg_loss_r = sum(t.r_achieved for t in losses) / len(losses) if losses else 0
        
        # Best/worst R
        best_r = max(r_values) if r_values else 0
        worst_r = min(r_values) if r_values else 0
        
        # Sharpe ratio (simplified: avg_r / std_dev of R)
        if len(r_values) > 1:
            mean_r = avg_r
            variance = sum((r - mean_r) ** 2 for r in r_values) / len(r_values)
            std_dev = variance ** 0.5
            sharpe = mean_r / std_dev if std_dev > 0 else 0
        else:
            sharpe = 0
        
        # Consecutive wins/losses
        max_consec_wins = 0
        max_consec_losses = 0
        current_streak = 0
        for t in period_trades:
            if t.win:
                current_streak = max(1, current_streak + 1) if current_streak >= 0 else 1
                max_consec_wins = max(max_consec_wins, current_streak)
            else:
                current_streak = min(-1, current_streak - 1) if current_streak <= 0 else -1
                max_consec_losses = max(max_consec_losses, abs(current_streak))
        
        # Session breakdown
        session_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "avg_r": 0.0})
        for t in period_trades:
            session_stats[t.session]["trades"] += 1
            if t.win:
                session_stats[t.session]["wins"] += 1
            session_stats[t.session]["pnl"] += t.pnl
            session_stats[t.session]["avg_r"] += t.r_achieved
        
        for session, stats in session_stats.items():
            stats["win_rate"] = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
            stats["avg_r"] = stats["avg_r"] / stats["trades"] if stats["trades"] > 0 else 0
        
        # Symbol breakdown
        symbol_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "avg_r": 0.0})
        for t in period_trades:
            symbol_stats[t.symbol]["trades"] += 1
            if t.win:
                symbol_stats[t.symbol]["wins"] += 1
            symbol_stats[t.symbol]["pnl"] += t.pnl
            symbol_stats[t.symbol]["avg_r"] += t.r_achieved
        
        for symbol, stats in symbol_stats.items():
            stats["win_rate"] = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
            stats["avg_r"] = stats["avg_r"] / stats["trades"] if stats["trades"] > 0 else 0
        
        return PerformanceMetrics(
            total_trades=len(period_trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            total_pnl=total_pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=profit_factor,
            avg_r=avg_r,
            avg_win_r=avg_win_r,
            avg_loss_r=avg_loss_r,
            best_trade_r=best_r,
            worst_trade_r=worst_r,
            sharpe_ratio=sharpe,
            max_consecutive_wins=max_consec_wins,
            max_consecutive_losses=max_consec_losses,
            current_streak=current_streak,
            session_stats=dict(session_stats),
            symbol_stats=dict(symbol_stats),
            period_start=cutoff.isoformat(),
            period_end=datetime.now(timezone.utc).isoformat(),
        )
    
    def format_weekly_report(self, metrics: PerformanceMetrics) -> str:
        """Format weekly performance report for Telegram"""
        # Find best/worst session and symbol
        best_session = max(
            metrics.session_stats.items(),
            key=lambda x: x[1]["win_rate"],
            default=(None, {})
        )
        worst_symbol = min(
            metrics.symbol_stats.items(),
            key=lambda x: x[1]["pnl"],
            default=(None, {})
        )
        
        streak_text = ""
        if metrics.current_streak > 0:
            streak_text = f"🔥 Winning Streak: {metrics.current_streak}"
        elif metrics.current_streak < 0:
            streak_text = f"❄️ Losing Streak: {abs(metrics.current_streak)}"
        
        report = f"""
📊 WEEKLY PERFORMANCE REPORT
{'=' * 30}

📈 Overall Performance
• Total Trades: {metrics.total_trades}
• Win Rate: {metrics.win_rate:.1f}% ({metrics.wins}W / {metrics.losses}L)
• Profit Factor: {metrics.profit_factor:.2f}
• Net PnL: ${metrics.total_pnl:.2f}
• Sharpe Ratio: {metrics.sharpe_ratio:.2f}

🎯 R-Multiple Analysis
• Average R: {metrics.avg_r:.2f}R
• Avg Win: {metrics.avg_win_r:.2f}R | Avg Loss: {metrics.avg_loss_r:.2f}R
• Best Trade: {metrics.best_trade_r:.2f}R | Worst: {metrics.worst_trade_r:.2f}R

🕐 Session Performance
"""
        for session, stats in sorted(metrics.session_stats.items(), key=lambda x: -x[1]["win_rate"]):
            report += f"• {session}: {stats['win_rate']:.0f}% WR, ${stats['pnl']:.2f}\n"
        
        report += f"""
💱 Symbol Performance
"""
        for symbol, stats in sorted(metrics.symbol_stats.items(), key=lambda x: -x[1]["pnl"]):
            report += f"• {symbol}: {stats['win_rate']:.0f}% WR, ${stats['pnl']:.2f}\n"
        
        if best_session[0]:
            report += f"\n🏆 Best Session: {best_session[0]} ({best_session[1]['win_rate']:.0f}% WR)"
        if worst_symbol[0]:
            report += f"\n⚠️ Worst Pair: {worst_symbol[0]} (${worst_symbol[1]['pnl']:.2f})"
        
        if streak_text:
            report += f"\n{streak_text}"
        
        report += f"\n\n📅 Period: {metrics.period_start[:10]} to {metrics.period_end[:10]}"
        
        return report.strip()
    
    def get_current_streak(self) -> int:
        """Get current winning/losing streak (positive = wins, negative = losses)"""
        if not self.trades:
            return 0
        streak = 0
        for t in reversed(self.trades):
            if t.win and streak >= 0:
                streak += 1
            elif not t.win and streak <= 0:
                streak -= 1
            else:
                break
        return streak
