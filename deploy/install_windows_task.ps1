# ==============================================================================
# Windows Task Scheduler Installer for QUANTA-SYNTH
# Configures the bot and dashboard to auto-start on system boot / VPS restart.
# Run as Administrator in PowerShell.
# ==============================================================================

$RootPath = (Get-Item $PSScriptRoot).Parent.FullName
$BotBatch = Join-Path $PSScriptRoot "start_production_bot.bat"
$DashboardBatch = Join-Path $PSScriptRoot "start_production_dashboard.bat"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " 🛠️ Installing QUANTA-SYNTH Auto-Start Tasks" -ForegroundColor Cyan
Write-Host " Repository Root: $RootPath"
Write-Host "=========================================================="
Write-Host ""

# 1. Register Bot Watchdog Task
$TaskNameBot = "QuantaSynth_Bot_Watchdog"
Unregister-ScheduledTask -TaskName $TaskNameBot -Confirm:$false -ErrorAction SilentlyContinue

$ActionBot = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BotBatch`"" -WorkingDirectory $RootPath
$TriggerBoot = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskNameBot -Action $ActionBot -Trigger $TriggerBoot -Settings $Settings -Description "24/7 QUANTA-SYNTH Autonomous Synthetic Indices Trading Bot" -User "SYSTEM" -RunLevel Highest

Write-Host " [✓] Registered Task: $TaskNameBot (Starts at boot)" -ForegroundColor Green

# 2. Register Dashboard Task
$TaskNameDash = "QuantaSynth_Dashboard"
Unregister-ScheduledTask -TaskName $TaskNameDash -Confirm:$false -ErrorAction SilentlyContinue

$ActionDash = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$DashboardBatch`"" -WorkingDirectory $RootPath
Register-ScheduledTask -TaskName $TaskNameDash -Action $ActionDash -Trigger $TriggerBoot -Settings $Settings -Description "QUANTA-SYNTH Streamlit Web Dashboard" -User "SYSTEM" -RunLevel Highest

Write-Host " [✓] Registered Task: $TaskNameDash (Starts at boot)" -ForegroundColor Green
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " ✅ Auto-Start Configuration Complete!" -ForegroundColor Green
Write-Host " Both the Bot and Dashboard will automatically launch if the VPS reboots."
Write-Host "==========================================================" -ForegroundColor Cyan
