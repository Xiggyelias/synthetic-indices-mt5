@echo off
TITLE MT5 Trading Bot - AutoLauncher
cd /d %~dp0..

echo ==========================================
echo   🚀 STARTING MT5 TRADING BOT SYSTEM
echo ==========================================
echo Current Dir: %CD%

:: Check for .env
if not exist .env (
    echo [ERROR] .env file not found!
    pause
    exit /b
)

:: Activate environment if needed
:: call venv\Scripts\activate

:: Start the bot
:: Redirecting to NUL because the bot now handles its own log rotation internally
python mt5_bot.py --system --symbols EURUSD XAUUSD --timeframe H4 --confirm-trading

if %errorlevel% neq 0 (
    echo [CRITICAL] Bot crashed or stopped with error.
    pause
)
