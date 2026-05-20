@echo off
:: ============================================================
:: Dashboard Quick-Start (single run, not watchdog)
:: Access locally:  http://localhost:8501
:: Access remotely: http://<VPS-IP>:8501
:: ============================================================
cd /d "%~dp0\.."

python -c "import streamlit" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  Installing dependencies...
    pip install -r requirements.txt
)

echo.
echo  Dashboard starting...
echo  Local:   http://localhost:8501
echo  Remote:  http://%COMPUTERNAME%:8501  (or use your VPS IP)
echo  Press Ctrl+C to stop.
echo.

python -m streamlit run dashboard.py ^
    --server.port 8501 ^
    --server.address 0.0.0.0 ^
    --server.headless true ^
    --browser.gatherUsageStats false

pause
