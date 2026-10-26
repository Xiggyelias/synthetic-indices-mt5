"""
Cloud Application Entry Point
=============================
Default entry point for cloud hosting platforms (Nixpacks, Heroku, Railway, Render).
Launches the QUANTA-SYNTH Institutional Dashboard on the assigned host port.
"""

from __future__ import annotations

import os
import subprocess
import sys

if __name__ == "__main__":
    port = os.getenv("PORT", "8501")
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "dashboard.py",
        f"--server.port={port}",
        "--server.address=0.0.0.0",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    sys.exit(subprocess.call(cmd))
