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
from pathlib import Path

if __name__ == "__main__":
    port = os.getenv("PORT", "8501")
    root = Path(__file__).resolve().parent

    # Auto-start 24/7 autonomous bot if enabled
    if os.getenv("AUTONOMOUS_AUTOSTART", "true").lower() == "true":
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "synthetic_bot.log"
        pid_file = root / "data" / "synthetic_bot.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_out = open(log_file, "a", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, str(root / "synthetic_bot.py"), "--run"],
                cwd=str(root),
                stdout=log_out,
                stderr=log_out,
            )
            pid_file.write_text(str(proc.pid), encoding="utf-8")
        except Exception as exc:
            print(f"[app.py] Could not auto-start synthetic_bot: {exc}")

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

