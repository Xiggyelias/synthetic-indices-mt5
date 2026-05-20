@echo off
:: ============================================================
:: NSSM Windows Service Installer
:: NSSM runs the bot as a true Windows Service — survives
:: reboots even WITHOUT a user logged in, with auto-restart.
::
:: 1. Download NSSM: https://nssm.cc/download
:: 2. Extract nssm.exe to C:\nssm\nssm.exe
:: 3. RIGHT-CLICK this file → "Run as Administrator"
:: ============================================================

set NSSM=C:\nssm\nssm.exe
set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe
set LOG_DIR=%BOT_DIR%\logs

if not exist "%NSSM%" (
    echo  ERROR: NSSM not found at %NSSM%
    echo  Download from https://nssm.cc/download
    echo  Extract nssm.exe to C:\nssm\nssm.exe
    pause
    exit /B 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo.
echo  Installing Trading Bot as Windows Services...
echo.

:: ── Remove old services if they exist ────────────────────────
"%NSSM%" stop  TradingBot 2>nul
"%NSSM%" remove TradingBot confirm 2>nul
"%NSSM%" stop  TradingBotDashboard 2>nul
"%NSSM%" remove TradingBotDashboard confirm 2>nul

:: ── Install Trading Bot service ───────────────────────────────
echo  Installing TradingBot service...
"%NSSM%" install TradingBot "%PYTHON%"
"%NSSM%" set TradingBot AppParameters "mt5_bot.py --system --loop --confirm-trading"
"%NSSM%" set TradingBot AppDirectory "%BOT_DIR%"
"%NSSM%" set TradingBot AppStdout "%LOG_DIR%\bot_run.log"
"%NSSM%" set TradingBot AppStderr "%LOG_DIR%\bot_run.log"
"%NSSM%" set TradingBot AppRotateFiles 1
"%NSSM%" set TradingBot AppRotateOnline 1
"%NSSM%" set TradingBot AppRotateBytes 10485760
"%NSSM%" set TradingBot Start SERVICE_AUTO_START
"%NSSM%" set TradingBot ObjectName LocalSystem
"%NSSM%" set TradingBot AppRestartDelay 15000
echo  [OK] TradingBot service installed

:: ── Install Dashboard service ─────────────────────────────────
echo  Installing TradingBotDashboard service...
"%NSSM%" install TradingBotDashboard "%PYTHON%"
"%NSSM%" set TradingBotDashboard AppParameters "-m streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false"
"%NSSM%" set TradingBotDashboard AppDirectory "%BOT_DIR%"
"%NSSM%" set TradingBotDashboard AppStdout "%LOG_DIR%\dashboard.log"
"%NSSM%" set TradingBotDashboard AppStderr "%LOG_DIR%\dashboard.log"
"%NSSM%" set TradingBotDashboard Start SERVICE_AUTO_START
"%NSSM%" set TradingBotDashboard ObjectName LocalSystem
"%NSSM%" set TradingBotDashboard AppRestartDelay 5000
echo  [OK] TradingBotDashboard service installed

:: ── Start them ────────────────────────────────────────────────
echo.
echo  Starting services...
"%NSSM%" start TradingBot
timeout /T 5 /NOBREAK > nul
"%NSSM%" start TradingBotDashboard

echo.
echo  ============================================
echo    NSSM Services installed and running!
echo.
echo    Manage via:  services.msc
echo    Or:  nssm stop/start TradingBot
echo         nssm stop/start TradingBotDashboard
echo.
echo    Dashboard: http://YOUR-VPS-IP:8501
echo  ============================================
echo.
pause
