@echo off
TITLE Quantum Brain v2 Dashboard (read-only)
cd /d %~dp0..

echo =========================================================
echo   [DASHBOARD] Quantum Brain v2 (read-only, no trading)
echo =========================================================

streamlit run dashboard_quantum.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Dashboard exited with error code %errorlevel%
    pause
)
