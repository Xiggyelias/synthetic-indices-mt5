#!/usr/bin/env python3
"""
Forward-Test Evaluation Report Script
Generates a Go/No-Go decision report based on baseline metrics.
"""

import sys
import os

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Ensure scripts/ directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from monitor_forward_test import ForwardTestMonitor

def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--final':
        monitor = ForwardTestMonitor()
        print(monitor.generate_report('evaluation'))
    else:
        print("Usage: python evaluate_forward_test.py --final")

if __name__ == '__main__':
    main()
