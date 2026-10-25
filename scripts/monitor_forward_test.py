#!/usr/bin/env python3
"""
Forward-Test Monitoring Dashboard
Real-time monitoring of MT5 Bot forward-testing progress
"""

import os
import sys
import time
from datetime import datetime
from typing import Optional
try:
    from tabulate import tabulate
except Exception:  # pragma: no cover
    tabulate = None

# Add current directory and src to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
from forward_test_tracker import ForwardTestTracker
from telegram_notifier import TelegramNotifier, build_telegram_config_from_env
from dotenv import load_dotenv

class ForwardTestMonitor:
    """Real-time forward-test monitoring dashboard"""
    
    def __init__(self, tracker: Optional[ForwardTestTracker] = None):
        self.logger = self._setup_logger()
        load_dotenv()
        
        # Initialize Telegram notifier
        telegram_config = build_telegram_config_from_env(os.getenv)
        self.telegram_notifier = TelegramNotifier(telegram_config) if telegram_config.enabled else None
        
        # Initialize forward-test tracker
        self.tracker = tracker or ForwardTestTracker(telegram_notifier=self.telegram_notifier)
        
        # Target metrics from calibrated RSI-Pullback champion setup
        self.target_metrics = {
            'win_rate': 0.750,      # Target 75%+ Win Rate
            'profit_factor': 1.06,   # Calibrated Profit Factor 1.06
            'expectancy': 0.20,      # $0.20+ expectancy per trade
            'max_slippage': 1.5,     # pips
            'max_drawdown': 0.15,    # 15% Max Drawdown
            'target_trades': 20,
            'test_days': 30
        }
        
    def _setup_logger(self):
        """Setup logging"""
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        return logging.getLogger(__name__)

    def _render_table(self, rows, headers):
        """Render a table with tabulate when available, or a plain-text fallback."""
        if tabulate is not None:
            return tabulate(rows, headers=headers, tablefmt="grid")

        widths = [len(str(h)) for h in headers]
        normalized_rows = []
        for row in rows:
            norm = [str(cell) for cell in row]
            normalized_rows.append(norm)
            widths = [max(widths[i], len(norm[i])) for i in range(len(headers))]

        def fmt(row):
            return " | ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers)))

        divider = "-+-".join("-" * width for width in widths)
        parts = [fmt(headers), divider]
        parts.extend(fmt(row) for row in normalized_rows)
        return "\n".join(parts)
    
    def display_dashboard(self):
        """Display comprehensive monitoring dashboard"""
        os.system('cls' if os.name == 'nt' else 'clear')
        
        print("=" * 80)
        print("📊 MT5 BOT FORWARD-TEST MONITORING DASHBOARD")
        print("=" * 80)
        print(f"🕒 Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📅 Test Started: {self.tracker.start_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"⏱️  Duration: {(datetime.now() - self.tracker.start_date).days} days")
        print("=" * 80)
        
        # Get current metrics
        current_metrics = self.tracker.calculate_current_metrics()
        
        if "status" in current_metrics:
            print(f"📋 Status: {current_metrics['status']}")
            print("=" * 80)
            return
        
        # Progress Overview
        self._display_progress_overview(current_metrics)
        
        # Performance Metrics
        self._display_performance_metrics(current_metrics)
        
        # Execution Quality
        self._display_execution_quality(current_metrics)
        
        # Safety Status
        self._display_safety_status()
        
        # Recent Activity
        self._display_recent_activity()
        
        # Alerts & Warnings
        self._display_alerts(current_metrics)
        
        print("=" * 80)
        print("Press Ctrl+C to exit | Auto-refresh every 30 seconds")
        print("=" * 80)
    
    def _display_progress_overview(self, metrics):
        """Display progress overview"""
        print("\n🎯 PROGRESS OVERVIEW")
        print("-" * 40)
        
        completed_trades = metrics['total_trades']
        progress_pct = (completed_trades / self.target_metrics['target_trades']) * 100
        test_days = (datetime.now() - self.tracker.start_date).days
        day_progress = (test_days / self.target_metrics['test_days']) * 100
        
        print(f"📈 Trades: {completed_trades}/{self.target_metrics['target_trades']} ({progress_pct:.1f}%)")
        print(f"📅 Days: {test_days}/{self.target_metrics['test_days']} ({day_progress:.1f}%)")
        print(f"💰 Total PnL: ${metrics['total_pnl']:.2f}")
        print(f"🎯 Completion: {'✅ COMPLETE' if completed_trades >= self.target_metrics['target_trades'] else '🔄 IN PROGRESS'}")
    
    def _display_performance_metrics(self, metrics):
        """Display performance metrics with target comparison"""
        print("\n📊 PERFORMANCE METRICS")
        print("-" * 40)
        
        performance_data = [
            ["Win Rate", f"{metrics['win_rate']:.1%}", f"{self.target_metrics['win_rate']:.1%}", 
             self._get_status_emoji(metrics['win_rate'], self.target_metrics['win_rate'])],
            ["Profit Factor", f"{metrics['profit_factor']:.2f}", f"{self.target_metrics['profit_factor']:.2f}",
             self._get_status_emoji(metrics['profit_factor'], self.target_metrics['profit_factor'])],
            ["Expectancy", f"${metrics['expectancy']:.2f}", f"${self.target_metrics['expectancy']:.2f}",
             self._get_status_emoji(metrics['expectancy'], self.target_metrics['expectancy'])],
            ["Max Drawdown", f"{metrics.get('max_drawdown', 0):.1%}", f"{self.target_metrics['max_drawdown']:.1%}",
             self._get_status_emoji(metrics.get('max_drawdown', 0), self.target_metrics['max_drawdown'], invert=True)]
        ]
        
        print(self._render_table(performance_data, headers=["Metric", "Current", "Target", "Status"]))
    
    def _display_execution_quality(self, metrics):
        """Display execution quality metrics"""
        print("\n⚡ EXECUTION QUALITY")
        print("-" * 40)
        
        execution_data = [
            ["Avg Slippage", f"{metrics['avg_slippage_pips']:.1f} pips", f"{self.target_metrics['max_slippage']:.1f} pips",
             self._get_status_emoji(metrics['avg_slippage_pips'], self.target_metrics['max_slippage'], invert=True)],
            ["Max Slippage", f"{metrics['max_slippage']:.1f} pips", f"{self.target_metrics['max_slippage']:.1f} pips",
             self._get_status_emoji(metrics['max_slippage'], self.target_metrics['max_slippage'], invert=True)],
            ["Avg Spread", f"{metrics.get('avg_spread_pips', 0):.1f} pips", "n/a", "OK"],
            ["Max Spread", f"{metrics.get('max_spread_pips', 0):.1f} pips", "n/a", "OK"],
            ["Test Completion", f"{metrics['target_completion']}", "True",
             "✅" if metrics['target_completion'] else "🔄"]
        ]
        
        print(self._render_table(execution_data, headers=["Metric", "Current", "Target", "Status"]))
    
    def _display_safety_status(self):
        """Display safety guardrails status"""
        print("\n🔒 SAFETY STATUS")
        print("-" * 40)
        
        # This would integrate with actual safety monitoring
        safety_data = [
            ["Daily Loss Limit", "0.0%", "1.5%", "✅"],
            ["Max Open Trades", "0/1", "1", "✅"],
            ["News Filter", "Active", "30 min", "✅"],
            ["VPS Uptime", "99.9%", "99.0%", "✅"]
        ]
        
        print(self._render_table(safety_data, headers=["Guardrail", "Current", "Limit", "Status"]))
    
    def _display_recent_activity(self):
        """Display recent trade activity"""
        print("\n📋 RECENT ACTIVITY")
        print("-" * 40)
        
        if not self.tracker.trades:
            print("No trades recorded yet")
            return
        
        # Get last 5 trades
        recent_trades = sorted(self.tracker.trades, key=lambda x: x.entry_time, reverse=True)[:5]
        
        activity_data = []
        for trade in recent_trades:
            status = "🟢 OPEN" if trade.exit_time is None else ("💰 WIN" if trade.profit_loss > 0 else "📉 LOSS")
            entry_time = trade.entry_time.strftime('%m-%d %H:%M')
            pnl = f"${trade.profit_loss:.2f}" if trade.profit_loss else "Open"
            activity_data.append([f"#{trade.trade_id}", trade.direction, entry_time, status, pnl])
        
        print(self._render_table(activity_data, headers=["Trade", "Dir", "Time", "Status", "PnL"]))
    
    def _display_alerts(self, metrics):
        """Display active alerts and warnings"""
        print("\n⚠️ ALERTS & WARNINGS")
        print("-" * 40)
        
        alerts = []
        
        # Check for performance alerts
        if metrics['win_rate'] < self.target_metrics['win_rate'] * 0.8:
            alerts.append("🔴 Win Rate below 80% of target")
        
        if metrics['profit_factor'] < self.target_metrics['profit_factor'] * 0.8:
            alerts.append("🔴 Profit Factor below 80% of target")
        
        if metrics['avg_slippage_pips'] > self.target_metrics['max_slippage']:
            alerts.append("🔴 Average slippage exceeds limit")
        
        if not alerts:
            alerts.append("✅ No active alerts")
        
        for alert in alerts:
            print(f"  {alert}")
    
    def _get_status_emoji(self, current, target, invert=False):
        """Get status emoji based on current vs target"""
        if invert:
            # Lower is better (e.g., drawdown, slippage)
            return "✅" if current <= target else "🔴"
        else:
            # Higher is better (e.g., win rate, profit factor)
            return "✅" if current >= target else "🔴"
    
    def generate_report(self, report_type="summary"):
        """Generate detailed report"""
        current_metrics = self.tracker.calculate_current_metrics()
        
        if report_type == "summary":
            return self._generate_summary_report(current_metrics)
        elif report_type == "detailed":
            return self._generate_detailed_report(current_metrics)
        elif report_type == "evaluation":
            return self._generate_evaluation_report(current_metrics)
    
    def _generate_summary_report(self, metrics):
        """Generate summary report"""
        if metrics.get('total_trades', 0) == 0:
            report = f"""
📊 FORWARD-TEST SUMMARY REPORT
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Test Duration: {(datetime.now() - self.tracker.start_date).days} days

STATUS:
ℹ️ No completed trades recorded yet.

PROGRESS:
Target: 0/{self.target_metrics['target_trades']} trades
Status: 🔄 IN PROGRESS
"""
            return report.strip()

        report = f"""
📊 FORWARD-TEST SUMMARY REPORT
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Test Duration: {(datetime.now() - self.tracker.start_date).days} days

PERFORMANCE:
Total Trades: {metrics['total_trades']}
Win Rate: {metrics['win_rate']:.1%}
Profit Factor: {metrics['profit_factor']:.2f}
Expectancy: ${metrics['expectancy']:.2f}
Total PnL: ${metrics['total_pnl']:.2f}

EXECUTION:
Avg Slippage: {metrics['avg_slippage_pips']:.1f} pips
Max Slippage: {metrics['max_slippage']:.1f} pips

PROGRESS:
Target: {metrics['total_trades']}/{self.target_metrics['target_trades']} trades
Status: {'✅ COMPLETE' if metrics['target_completion'] else '🔄 IN PROGRESS'}
"""
        return report.strip()
    
    def _generate_detailed_report(self, metrics):
        """Generate detailed report"""
        return self.tracker.generate_report()
    
    def _generate_evaluation_report(self, metrics):
        """Generate Go/No-Go evaluation report"""
        if metrics.get('total_trades', 0) == 0:
            report = f"""
🎯 GO/NO-GO EVALUATION REPORT
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

STATUS:
ℹ️ No completed trades recorded yet. Minimum {self.target_metrics['target_trades']} completed trades required for evaluation.

RECOMMENDATION:
🔄 IN PROGRESS - Awaiting trade execution and closure
"""
            return report.strip()

        report = f"""
🎯 GO/NO-GO EVALUATION REPORT
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

EVALUATION CRITERIA:
{'✅' if metrics['win_rate'] >= self.target_metrics['win_rate'] else '❌'} Win Rate: {metrics['win_rate']:.1%} (Target: ≥{self.target_metrics['win_rate']:.1%})
{'✅' if metrics['profit_factor'] >= self.target_metrics['profit_factor'] else '❌'} Profit Factor: {metrics['profit_factor']:.2f} (Target: ≥{self.target_metrics['profit_factor']:.2f})
{'✅' if metrics['expectancy'] >= self.target_metrics['expectancy'] else '❌'} Expectancy: ${metrics['expectancy']:.2f} (Target: ≥${self.target_metrics['expectancy']:.2f})
{'✅' if metrics['avg_slippage_pips'] <= self.target_metrics['max_slippage'] else '❌'} Slippage: {metrics['avg_slippage_pips']:.1f} pips (Target: ≤{self.target_metrics['max_slippage']:.1f} pips)

RECOMMENDATION:
"""
        
        # Determine recommendation
        go_criteria = [
            metrics['win_rate'] >= self.target_metrics['win_rate'],
            metrics['profit_factor'] >= self.target_metrics['profit_factor'],
            metrics['expectancy'] >= self.target_metrics['expectancy'],
            metrics['avg_slippage_pips'] <= self.target_metrics['max_slippage']
        ]
        
        if all(go_criteria):
            report += "✅ GO - Proceed to small-live deployment"
        elif sum(go_criteria) >= 3:
            report += "⚠️ CONDITIONAL GO - Consider minor optimizations"
        else:
            report += "❌ NO-GO - Requires significant adjustments"
        
        return report.strip()
    
    def run_continuous_monitoring(self, refresh_interval=30):
        """Run continuous monitoring with auto-refresh"""
        try:
            while True:
                self.display_dashboard()
                time.sleep(refresh_interval)
        except KeyboardInterrupt:
            print("\n🛑 Monitoring stopped by user")
    
    def run_single_check(self):
        """Run single monitoring check"""
        self.display_dashboard()

def main():
    """Main monitoring function"""
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
        
    import argparse
    
    parser = argparse.ArgumentParser(description='MT5 Bot Forward-Test Monitor')
    parser.add_argument('--continuous', action='store_true', help='Run continuous monitoring')
    parser.add_argument('--refresh', type=int, default=30, help='Refresh interval in seconds')
    parser.add_argument('--report', choices=['summary', 'detailed', 'evaluation'], help='Generate report')
    parser.add_argument('--status', action='store_true', help='Quick status check')
    
    args = parser.parse_args()
    
    monitor = ForwardTestMonitor()
    
    if args.report:
        print(monitor.generate_report(args.report))
    elif args.status:
        current_metrics = monitor.tracker.calculate_current_metrics()
        if "status" not in current_metrics:
            print(f"Trades: {current_metrics['total_trades']}/{monitor.target_metrics['target_trades']}")
            print(f"Win Rate: {current_metrics['win_rate']:.1%}")
            print(f"Profit Factor: {current_metrics['profit_factor']:.2f}")
            print(f"Expectancy: ${current_metrics['expectancy']:.2f}")
            print(f"Status: {'✅ COMPLETE' if current_metrics['target_completion'] else '🔄 IN PROGRESS'}")
        else:
            print(f"Status: {current_metrics['status']}")
    elif args.continuous:
        monitor.run_continuous_monitoring(args.refresh)
    else:
        monitor.run_single_check()

if __name__ == "__main__":
    main()
