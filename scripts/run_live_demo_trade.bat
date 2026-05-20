@echo off
setlocal
cd /d "%~dp0.."
if not exist .env (
    echo [ERROR] Missing .env — see .env.template
    exit /b 1
)
echo DEMO TRADING: opens new trades when signals fire. Requires DEMO account + AutoTrading ON.
echo Refuses live accounts unless you add --live --confirm-live to this script intentionally.
python mt5_bot.py --system --symbols EURUSD XAUUSD --timeframe H4 --confirm-trading --poll-seconds 60
exit /b %ERRORLEVEL%
