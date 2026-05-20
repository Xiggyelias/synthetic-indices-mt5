@echo off
setlocal
cd /d "%~dp0.."
if not exist .env (
    echo [ERROR] Missing .env — see .env.template
    exit /b 1
)
echo LIVE OBSERVE MODE: syncs positions / Telegram / risk gates — NO new entries ^(omit --confirm-trading^).
echo Use a DEMO account. Enable AutoTrading in MT5.
python mt5_bot.py --system --symbols EURUSD XAUUSD --timeframe H4 --poll-seconds 60
exit /b %ERRORLEVEL%
