#!/usr/bin/env python3
"""
Bot Performance Reporter
Generates an equity curve plot and performance summary.
"""

import os
import sys
import json
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime

def generate_report(state_path="data/trades_state.json", output_path="logs/performance_report.png"):
    if not os.path.exists(state_path):
        return None, "State file not found."
    
    with open(state_path, 'r') as f:
        state = json.load(f)
    
    trades = state.get('trades', [])
    if not trades:
        return None, "No trades found in state."
    
    df = pd.DataFrame(trades)
    df = df[df['status'] == 'CLOSED'].copy()
    if df.empty:
        return None, "No closed trades to report."
    
    # Calculate Equity Curve (Net PnL)
    initial_capital = 10000.0 # Default
    
    # Fill missing comm/swap with 0
    df['commission'] = df['commission'].fillna(0)
    df['swap'] = df['swap'].fillna(0)
    
    df['net_profit_loss'] = df['profit_loss'] + df['commission'] + df['swap']
    df['cumulative_pnl'] = df['net_profit_loss'].cumsum()
    df['equity'] = initial_capital + df['cumulative_pnl']
    
    # Plotting
    plt.figure(figsize=(12, 6))
    plt.plot(df['equity'], marker='o', linestyle='-', color='#2ecc71', linewidth=2)
    plt.title('Bot Equity Curve - Multi-Symbol H4', fontsize=16, fontweight='bold')
    plt.xlabel('Trade Number', fontsize=12)
    plt.ylabel('Equity ($)', fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.fill_between(range(len(df)), df['equity'], initial_capital, color='#2ecc71', alpha=0.1)
    
    # Add metrics text
    total_pnl = df['profit_loss'].sum()
    win_rate = (df['profit_loss'] > 0).mean()
    max_dd = (df['equity'].cummax() - df['equity']).max()
    
    metrics_text = f"Total PnL: ${total_pnl:.2f}\nWin Rate: {win_rate:.1%}\nMax Drawdown: ${max_dd:.2f}"
    plt.annotate(metrics_text, xy=(0.02, 0.95), xycoords='axes fraction', 
                 bbox=dict(boxstyle="round,pad=0.5", fc="white", ec="gray", alpha=0.8),
                 verticalalignment='top')
    
    plt.savefig(output_path)
    plt.close()
    
    return output_path, metrics_text

if __name__ == "__main__":
    path, metrics = generate_report()
    if path:
        print(f"✅ Report generated: {path}")
        print(metrics)
    else:
        print(f"❌ Failed: {metrics}")
