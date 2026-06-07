@echo off
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe

cd /d "%BOT_DIR%"
"%PYTHON%" mt5_bot.py --set-bot-switch off
echo.
echo  Bot switch is now OFF.
echo  The dashboard can keep running, but the watchdog will not launch trading.
echo.
pause
