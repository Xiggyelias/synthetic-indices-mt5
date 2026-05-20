# setup_24_7.ps1
# Run this script as Administrator to register the bot as a background task.

$TaskName = "MT5_Trading_Bot"
$WorkingDir = (Get-Item -Path ".\").FullName
$PythonPath = "python.exe" # Change to absolute path if needed, e.g., C:\Python310\python.exe
$ScriptPath = Join-Path $WorkingDir "mt5_bot.py"
$Args = "--system --symbols EURUSD XAUUSD --timeframe H4 --confirm-trading"

# Create the Action
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "$ScriptPath $Args" -WorkingDirectory $WorkingDir

# Create the Trigger (Start at Startup)
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Create the Settings (Restart if fails, allow run even if on battery)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

# Register the Task
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -User "SYSTEM" -RunLevel Highest

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "🚀 24/7 UPTIME TASK REGISTERED" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "Task Name: $TaskName"
Write-Host "The bot will now start automatically whenever this Windows machine boots up."
Write-Host "It will run under the SYSTEM account in the background."
Write-Host ""
Write-Host "To see if it's running, check your Telegram or use:"
Write-Host "Get-ScheduledTask -TaskName $TaskName"
Write-Host "==========================================" -ForegroundColor Cyan
