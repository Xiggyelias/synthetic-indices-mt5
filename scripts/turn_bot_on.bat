@echo off
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe

cd /d "%BOT_DIR%"
"%PYTHON%" mt5_bot.py --set-bot-switch on
echo.
echo  Bot switch is now ON.
echo  If the forex market is open, the watchdog will launch trading automatically.
echo.
pause
