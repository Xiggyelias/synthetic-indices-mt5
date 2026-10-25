@echo off
TITLE QUANTA-SYNTH Autonomous Bot - Deriv Synthetic Indices
cd /d %~dp0..

echo =========================================================
echo   [QUANT] QUANTA-SYNTH: Synthetic Indices Mathematical Bot
echo =========================================================
echo Target Markets: Volatility 75/100/25, Crash 1000/500, Boom 1000/500, Step
echo Core Engines: Hurst Exponent, Ornstein-Uhlenbeck SDE, Poisson Spike Hazard, Kelly Risk
echo.

python synthetic_bot.py --run

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Bot exited with error code %errorlevel%
    pause
)
