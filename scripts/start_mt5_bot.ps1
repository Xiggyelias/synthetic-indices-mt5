$ErrorActionPreference = "Stop"

$workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $workspace

$pythonExe = "C:\Users\emmanuel\AppData\Local\Programs\Python\Python312\python.exe"
$arguments = @("-X", "utf8", "mt5_bot.py", "--system", "--confirm-trading")
$commandLineMatch = "mt5_bot.py --system --confirm-trading"

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -like "*$commandLineMatch*"
    } |
    Select-Object -First 1

if ($existing) {
    exit 0
}

$stdoutLog = Join-Path $workspace "bot_run.log"
$stderrLog = Join-Path $workspace "bot_run.err"

Start-Process `
    -FilePath $pythonExe `
    -ArgumentList $arguments `
    -WorkingDirectory $workspace `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden
