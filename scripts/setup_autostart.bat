@echo off
:: ============================================================
:: Auto-Start Setup — RIGHT-CLICK "Run as Administrator"
:: Registers bot + dashboard to start automatically at login
:: ============================================================
echo.
echo  Setting up auto-start for Trading Bot + Dashboard...
echo.

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set SCRIPTS=%BOT_DIR%\scripts

:: ── Task 1: Trading Bot ───────────────────────────────────────
schtasks /delete /tn "TradingBot" /f > nul 2>&1
schtasks /create ^
  /tn "TradingBot" ^
  /tr "\"%SCRIPTS%\run_bot_forever.bat\"" ^
  /sc ONLOGON ^
  /rl HIGHEST ^
  /delay 0001:00 ^
  /f

if %ERRORLEVEL% EQU 0 (
    echo  [OK] Trading Bot task created
) else (
    echo  [FAIL] Could not create Trading Bot task
)

:: ── Task 2: Dashboard ──────────────────────────────────────────
schtasks /delete /tn "TradingBotDashboard" /f > nul 2>&1
schtasks /create ^
  /tn "TradingBotDashboard" ^
  /tr "\"%SCRIPTS%\run_dashboard_forever.bat\"" ^
  /sc ONLOGON ^
  /rl HIGHEST ^
  /delay 0001:30 ^
  /f

if %ERRORLEVEL% EQU 0 (
    echo  [OK] Dashboard task created
) else (
    echo  [FAIL] Could not create Dashboard task
)

echo.
echo  Done! Both will auto-start next time you log in.
echo  To start them NOW without rebooting, run:
echo    schtasks /run /tn "TradingBot"
echo    schtasks /run /tn "TradingBotDashboard"
echo.
pause
