@echo off
:: ============================================================
:: Start Everything — MT5 terminal, trading bot, dashboard
:: Run this once after VPS boots or after a manual restart.
:: ============================================================
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe
set LOG_DIR=%BOT_DIR%\logs
set DATA_DIR=%BOT_DIR%\data

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

echo.
echo  ============================================
echo    Trading Bot — Full Startup Sequence
echo  ============================================
echo.

:: ── 1. Launch MT5 terminal ───────────────────────────────────
echo  [1/3] Launching MetaTrader 5 terminal...
for /f "usebackq tokens=*" %%A in (`"%PYTHON%" -c "import os; from dotenv import dotenv_values; d=dotenv_values('%BOT_DIR%\\.env'); print(d.get('MT5_TERMINAL_PATH',''))"`) do set MT5_PATH=%%A

if defined MT5_PATH (
    if exist "%MT5_PATH%" (
        tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul
        if %ERRORLEVEL% NEQ 0 (
            start "" "%MT5_PATH%"
            echo  MT5 launched. Waiting 30s for it to connect...
            timeout /T 30 /NOBREAK > nul
        ) else (
            echo  MT5 is already running.
        )
    ) else (
        echo  WARNING: MT5 not found at %MT5_PATH% — ensure terminal is running.
    )
) else (
    echo  WARNING: MT5_TERMINAL_PATH not set in .env — ensure terminal is running manually.
)

:: ── 2. Start bot watchdog in new window ──────────────────────
echo.
echo  [2/3] Starting trading bot (system mode)...
start "TradingBot" /MIN cmd /c ""%BOT_DIR%\scripts\run_bot_forever.bat""
timeout /T 3 /NOBREAK > nul

:: ── 3. Start dashboard watchdog in new window ────────────────
echo  [3/3] Starting dashboard on http://0.0.0.0:8501...
start "Dashboard" /MIN cmd /c ""%BOT_DIR%\scripts\run_dashboard_forever.bat""
timeout /T 5 /NOBREAK > nul

echo.
echo  ============================================
echo    All services started!
echo.
echo    Dashboard:    http://localhost:8501
echo    Remote:       http://%COMPUTERNAME%:8501
echo    Bot logs:     %LOG_DIR%\bot_run.log
echo    Watchdog log: %LOG_DIR%\watchdog.log
echo  ============================================
echo.
pause
