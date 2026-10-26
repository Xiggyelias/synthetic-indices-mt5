@echo off
TITLE QUANTA-SYNTH Production Watchdog [24/7 Execution]
cd /d %~dp0\..

echo ======================================================================
echo  🚀 QUANTA-SYNTH 24/7 PRODUCTION SUPERVISOR
echo ======================================================================
echo  Active Directory: %CD%
echo  Starting pre-flight system diagnostics...
echo.

python deploy\verify_deployment.py
echo.
echo ======================================================================
echo  Starting Autonomous Bot with Auto-Restart Protection...
echo  If the bot terminates or MT5 restarts, watchdog re-spawns it in 5s.
echo  Press Ctrl+C to terminate the watchdog.
echo ======================================================================
echo.

:WATCHDOG_LOOP
echo [%DATE% %TIME%] Starting QUANTA-SYNTH core loop...
python synthetic_bot.py --run

echo.
echo [%DATE% %TIME%] ⚠ Bot process exited with code %ERRORLEVEL%.
echo Restarting in 5 seconds... (Press Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto WATCHDOG_LOOP
