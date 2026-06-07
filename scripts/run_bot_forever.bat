@echo off
:: ============================================================
:: Bot Watchdog — runs mt5_bot.py --system --loop in a restart
:: loop. All config (symbols, timeframe, login, etc.) comes
:: from .env in the project root — no args needed here.
:: ============================================================
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe
set LOG_DIR=%BOT_DIR%\logs
set DATA_DIR=%BOT_DIR%\data
set RESTART_DELAY=15
set CLOSED_DELAY=300

:: ── Ensure directories exist ─────────────────────────────────
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

:: ── Ensure bot_switch.txt defaults to ON ─────────────────────
if not exist "%DATA_DIR%\bot_switch.txt" (
    echo on > "%DATA_DIR%\bot_switch.txt"
)

echo [%date% %time%] Bot watchdog started >> "%LOG_DIR%\watchdog.log"
echo  [%time%] Bot watchdog started. Logs → %LOG_DIR%\bot_run.log

:loop
cd /d "%BOT_DIR%"
"%PYTHON%" scripts\watchdog_gate.py --switch-path "%DATA_DIR%\bot_switch.txt" --mode status > "%DATA_DIR%\watchdog_gate_status.txt"
if errorlevel 1 (
    set "GATE_STATUS=waiting"
    echo [%date% %time%] Bot launch blocked. See %DATA_DIR%\watchdog_gate_status.txt >> "%LOG_DIR%\watchdog.log"
    echo  [%time%] Bot launch blocked by switch or market status. Dashboard stays available.
    timeout /T %CLOSED_DELAY% /NOBREAK > nul
    goto loop
)

echo [%date% %time%] Starting bot... >> "%LOG_DIR%\watchdog.log"
echo  [%time%] Starting MT5 bot (system mode)...
"%PYTHON%" mt5_bot.py --system --loop --confirm-trading >> "%LOG_DIR%\bot_run.log" 2>&1

set EXIT_CODE=%ERRORLEVEL%
echo [%date% %time%] Bot exited (code %EXIT_CODE%). Restarting in %RESTART_DELAY%s... >> "%LOG_DIR%\watchdog.log"
echo  [%time%] Bot stopped (code %EXIT_CODE%). Restarting in %RESTART_DELAY%s...
timeout /T %RESTART_DELAY% /NOBREAK > nul
goto loop
