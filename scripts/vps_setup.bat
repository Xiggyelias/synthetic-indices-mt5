@echo off
:: ============================================================
:: VPS Setup Wizard — run once on a fresh VPS
:: RIGHT-CLICK → "Run as Administrator"
:: ============================================================
echo.
echo  ============================================
echo    Trading Bot — VPS Setup Wizard
echo  ============================================
echo.

set BOT_DIR=C:\Users\emmanuel\Desktop\files (1)
set PYTHON=C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe

:: ── 1. Create required directories ───────────────────────────
echo  [1] Creating directories...
for %%D in (logs data config src scripts) do (
    if not exist "%BOT_DIR%\%%D" mkdir "%BOT_DIR%\%%D"
)
echo     OK

:: ── 2. Install Python dependencies ───────────────────────────
echo.
echo  [2] Installing Python packages...
"%PYTHON%" -m pip install --upgrade pip --quiet
"%PYTHON%" -m pip install -r "%BOT_DIR%\requirements.txt" --quiet
if %ERRORLEVEL% EQU 0 (echo     OK) else (echo     FAILED — check internet/pip)

:: ── 3. Initialise bot switch ──────────────────────────────────
echo.
echo  [3] Setting bot switch to ON...
echo on > "%BOT_DIR%\data\bot_switch.txt"
echo     OK

:: ── 4. Configure power settings (no sleep / no screen lock) ──
echo.
echo  [4] Disabling sleep and screen-off...
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo     OK (screen and sleep set to Never)

:: ── 5. Firewall rule for dashboard ───────────────────────────
echo.
echo  [5] Opening firewall port 8501 for dashboard...
netsh advfirewall firewall delete rule name="TradingBotDashboard" > nul 2>&1
netsh advfirewall firewall add rule ^
    name="TradingBotDashboard" ^
    dir=in ^
    action=allow ^
    protocol=TCP ^
    localport=8501 ^
    profile=any > nul
if %ERRORLEVEL% EQU 0 (echo     OK) else (echo     FAILED — run as Administrator)

:: ── 6. Register Task Scheduler auto-start ─────────────────────
echo.
echo  [6] Registering auto-start tasks...

schtasks /delete /tn "TradingBot" /f > nul 2>&1
schtasks /create /tn "TradingBot" ^
    /tr "\"%BOT_DIR%\scripts\run_bot_forever.bat\"" ^
    /sc ONLOGON /rl HIGHEST /delay 0001:00 /f > nul
if %ERRORLEVEL% EQU 0 (echo     TradingBot task OK) else (echo     TradingBot task FAILED)

schtasks /delete /tn "TradingBotDashboard" /f > nul 2>&1
schtasks /create /tn "TradingBotDashboard" ^
    /tr "\"%BOT_DIR%\scripts\run_dashboard_forever.bat\"" ^
    /sc ONLOGON /rl HIGHEST /delay 0001:30 /f > nul
if %ERRORLEVEL% EQU 0 (echo     Dashboard task OK) else (echo     Dashboard task FAILED)

:: ── 7. MySQL setup prompt ─────────────────────────────────────
echo.
echo  [7] MySQL check...
sc query MySQL84 > nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo     MySQL84 service found — OK
) else (
    echo     MySQL not detected. Run scripts\install_mysql.bat as Admin to install.
    echo     (Bot runs fine on JSON fallback until then)
)

:: ── Done ─────────────────────────────────────────────────────
echo.
echo  ============================================
echo    Setup complete!
echo.
echo    NEXT STEPS:
echo    1. Edit .env with your VPS MT5 details
echo       (MT5_TERMINAL_PATH, MT5_LOGIN, etc.)
echo    2. Open MetaTrader 5 and log in
echo    3. Enable AutoTrading in MT5
echo    4. Run scripts\start_all.bat to launch everything
echo    5. Dashboard: http://YOUR-VPS-IP:8501
echo  ============================================
echo.
pause
