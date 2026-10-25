@echo off
TITLE QUANTA-SYNTH Real-Time Mathematical Matrix Dashboard
cd /d %~dp0..

echo =========================================================
echo   [DASHBOARD] Launching Streamlit Synthetic Matrix...
echo =========================================================

streamlit run dashboard_synthetic.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Dashboard exited with error code %errorlevel%
    pause
)
