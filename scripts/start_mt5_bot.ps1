$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspace = Split-Path -Parent $scriptDir
Set-Location $workspace

$cmdExe = Join-Path $env:SystemRoot "System32\cmd.exe"
$watchdogPath = Join-Path $workspace "scripts\run_bot_forever.bat"

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -eq "cmd.exe" -and $_.CommandLine -like "*run_bot_forever.bat*") -or
        ($_.Name -eq "python.exe" -and $_.CommandLine -like "*mt5_bot.py*" -and $_.CommandLine -like "*--system*")
    } |
    Select-Object -First 1

if ($existing) {
    exit 0
}

Start-Process `
    -FilePath $cmdExe `
    -ArgumentList @("/c", "`"$watchdogPath`"") `
    -WorkingDirectory $workspace `
    -WindowStyle Hidden
