@echo off
setlocal
cd /d "%~dp0.."
if not exist .env (
    echo [ERROR] Copy .env.template to .env and fill MT5_LOGIN, MT5_SERVER, MT5_TERMINAL_PATH.
    exit /b 1
)
echo Running MT5 historical backtest ^(requires MetaTrader 5 open / logged in^)...
python mt5_bot.py --backtest-v2 --symbols EURUSD --timeframe H1 --backtest-years 2 --backtest-config-json config/config_rsi_pullback.json
set RC=%ERRORLEVEL%
echo Exit code: %RC%
exit /b %RC%
