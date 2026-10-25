#!/usr/bin/env python3
"""
Bot Status Checker
Checks if the MT5 bot is running and displays current trading status.
"""

import os
import sys
import json
import psutil
try:
    from tabulate import tabulate
except ImportError:
    tabulate = None

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

def check_process():
    """Check if the mt5_bot process is running."""
    current_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            if 'python' in proc.info['name'].lower():
                cmdline = proc.info.get('cmdline', [])
                if any('mt5_bot.py' in arg for arg in cmdline):
                    # Ignore one-off utilities and backtesters
                    exclude_flags = ['--status-check', '--backtest', '--backtest-v2', '--safety-check', '--close-all', '--emergency-status']
                    if any(any(flag in arg for arg in cmdline) for flag in exclude_flags):
                        continue
                    return proc.info['pid'], cmdline
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None, None

def get_latest_logs(n=10):
    """Get the last N lines of the log file."""
    for log_path, enc in [("logs/bot_run.log", "utf-8"), ("logs/bot_run.err", "utf-8"), ("logs/bot_run.err", "utf-16le")]:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
            try:
                with open(log_path, 'r', encoding=enc, errors='replace') as f:
                    lines = f.readlines()
                    if lines:
                        return "".join(lines[-n:])
            except Exception:
                continue
    return "No recent log activity."

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
        
    print("=" * 60)
    print("🤖 MT5 BOT STATUS CHECKER")
    print("=" * 60)
    
    # 1. Process Status
    pid, cmd = check_process()
    if pid:
        print(f"✅ STATUS: RUNNING (PID: {pid})")
        # print(f"   Command: {' '.join(cmd)}")
    else:
        print("❌ STATUS: NOT RUNNING")
    
    # 2. Trades Summary
    print("\n📊 TRADING SUMMARY")
    state_path = "data/trades_state.json"
    if os.path.exists(state_path):
        with open(state_path, 'r') as f:
            state = json.load(f)
        
        trades = state.get('trades', [])
        open_trades = [t for t in trades if t.get('status') == 'OPEN']
        closed_trades = [t for t in trades if t.get('status') == 'CLOSED']
        
        total_pnl = sum(t.get('profit_loss', 0) for t in closed_trades)
        win_rate = len([t for t in closed_trades if t.get('profit_loss', 0) > 0]) / len(closed_trades) if closed_trades else 0
        
        print(f"   Open Positions:   {len(open_trades)}")
        print(f"   Closed Trades:    {len(closed_trades)}")
        print(f"   Total Realized PnL: ${total_pnl:.2f}")
        print(f"   Win Rate:         {win_rate:.1%}")
        
        if open_trades:
            print("\n   ACTIVE POSITIONS:")
            for t in open_trades:
                print(f"   - {t.get('direction')} {t.get('symbol')} @ {t.get('entry_price')} (SL: {t.get('stop_loss')})")
    else:
        print("   No trade state found.")

    # 3. Recent Activity
    print("\n📜 RECENT LOG ACTIVITY")
    print("-" * 60)
    print(get_latest_logs(5))
    print("=" * 60)

if __name__ == "__main__":
    main()
