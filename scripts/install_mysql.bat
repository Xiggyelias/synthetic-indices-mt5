@echo off
:: ============================================================
:: MySQL 8.4 Installer — RIGHT-CLICK and "Run as Administrator"
:: ============================================================
echo.
echo  Installing MySQL 8.4 for Trading Bot...
echo.

set MSI=%LOCALAPPDATA%\Temp\WinGet\Oracle.MySQL.8.4.9\mysql-8.4.9-winx64.msi

if not exist "%MSI%" (
    echo  MySQL installer not found at: %MSI%
    echo  Re-downloading via winget...
    winget download Oracle.MySQL --download-directory "%TEMP%\mysql_dl"
    set MSI=%TEMP%\mysql_dl\mysql-8.4.9-winx64.msi
)

echo  Running MSI installer...
msiexec /i "%MSI%" /qn /norestart ^
    INSTALLDIR="C:\Program Files\MySQL\MySQL Server 8.4" ^
    DATADIR="C:\ProgramData\MySQL\MySQL Server 8.4\Data" ^
    SERVICENAME=MySQL84

if %ERRORLEVEL% EQU 0 (
    echo.
    echo  MySQL installed successfully!
    echo  Starting MySQL service...
    net start MySQL84
    echo.
    echo  NEXT STEPS:
    echo   1. Open .env and set MYSQL_PASSWORD= (root password set during install)
    echo   2. Run: python src\db_setup.py
    echo.
) else (
    echo.
    echo  Installation failed (error %ERRORLEVEL%)
    echo  Try running MySQL Installer manually from mysql.com
    echo.
)
pause
