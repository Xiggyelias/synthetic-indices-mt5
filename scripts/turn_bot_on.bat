@echo off
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe

cd /d "%BOT_DIR%"
"%PYTHON%" scripts\bot_runtime_control.py on
echo.
echo  Bot switch is now ON.
echo  If the watchdog was not already running, it has been started for you.
echo  If the forex market is open, trading will launch automatically.
echo.
pause
