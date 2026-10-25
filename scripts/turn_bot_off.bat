@echo off
setlocal

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe

cd /d "%BOT_DIR%"
"%PYTHON%" scripts\bot_runtime_control.py off
echo.
echo  Bot switch is now OFF.
echo  Active bot processes were stopped and MT5 was closed.
echo  The watchdog can stay running, but it will remain idle until you turn the bot back ON.
echo.
pause
