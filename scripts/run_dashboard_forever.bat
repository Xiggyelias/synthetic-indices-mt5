@echo off
:: ============================================================
:: Dashboard Watchdog — keeps Streamlit alive 24/7
:: Accessible remotely on http://<VPS-IP>:8501
:: ============================================================
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe
set LOG_DIR=%BOT_DIR%\logs
set RESTART_DELAY=5

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [%date% %time%] Dashboard watchdog started >> "%LOG_DIR%\watchdog.log"

:loop
echo  [%time%] Starting dashboard on http://0.0.0.0:8501...
cd /d "%BOT_DIR%"
"%PYTHON%" -m streamlit run dashboard.py ^
    --server.port 8501 ^
    --server.address 0.0.0.0 ^
    --server.headless true ^
    --browser.gatherUsageStats false >> "%LOG_DIR%\dashboard.log" 2>&1

echo  [%time%] Dashboard stopped. Restarting in %RESTART_DELAY%s...
timeout /T %RESTART_DELAY% /NOBREAK > nul
goto loop
