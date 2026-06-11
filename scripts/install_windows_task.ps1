# Register Windows Task Scheduler job: Mon-Fri 09:10 India Standard Time.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Script = Join-Path $Root "scripts\run_market_session.ps1"
$TaskName = "NiftySpotSignalEngine"

if (-not (Test-Path $Script)) {
    Write-Error "Missing $Script"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $Root

# 09:10 IST — set time zone explicitly (Windows 10 1803+)
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:10"
try {
    $Trigger.StartBoundary = (Get-Date "09:10").ToString("yyyy-MM-ddTHH:mm:ss")
} catch { }

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

# Prefer India Standard Time for the trigger (if supported on this Windows build)
schtasks /Change /TN $TaskName /TZ "India Standard Time" 2>$null

Write-Host ""
Write-Host "Task installed: $TaskName"
Write-Host "Schedule:     Mon-Fri 09:10 (set TZ to India Standard Time in Task Scheduler if needed)"
Write-Host "Script:       $Script"
Write-Host "Logs:         $Root\data\live\market-session.log"
Write-Host ""
Write-Host "Test now:     powershell -File `"$Script`""
Write-Host "Check task:   Get-ScheduledTask -TaskName $TaskName"
Write-Host "Run task:     Start-ScheduledTask -TaskName $TaskName"
Write-Host "Uninstall:    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
