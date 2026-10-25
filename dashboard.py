"""
QUANTA-SYNTH: Synthetic Indices Institutional Command Dashboard
==============================================================
Main entry point for Streamlit web interface.
Redirects execution directly to the upgraded QUANTA-SYNTH matrix (dashboard_synthetic.py).
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

target_dashboard = ROOT / "dashboard_synthetic.py"

if __name__ == "__main__" or "streamlit" in sys.modules:
    runpy.run_path(str(target_dashboard), run_name="__main__")
