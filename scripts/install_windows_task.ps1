# Register Windows Task Scheduler jobs:
#   1) Telegram remote control (at user logon)
#   2) Market session bot (Mon-Fri 09:10 India Standard Time)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$MarketScript = Join-Path $Root "scripts\run_market_session.ps1"
$TelegramScript = Join-Path $Root "scripts\run_telegram_remote.ps1"
$MarketTaskName = "NiftySpotSignalEngine"
$TelegramTaskName = "NiftySpotSignalEngineTelegram"

foreach ($path in @($MarketScript, $TelegramScript)) {
    if (-not (Test-Path $path)) {
        Write-Error "Missing $path"
    }
}

function Install-SpotSignalTask {
    param(
        [string]$TaskName,
        [string]$ScriptPath,
        [Microsoft.Management.Infrastructure.CimInstance[]]$Triggers,
        [TimeSpan]$ExecutionTimeLimit = [TimeSpan]::Zero,
        [int]$RestartCount = 0,
        [TimeSpan]$RestartInterval = [TimeSpan]::FromMinutes(1)
    )

    $Action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
        -WorkingDirectory $Root

    $settingsParams = @{
        AllowStartIfOnBatteries    = $true
        DontStopIfGoingOnBatteries = $true
        StartWhenAvailable         = $true
    }
    if ($ExecutionTimeLimit -gt [TimeSpan]::Zero) {
        $settingsParams.ExecutionTimeLimit = $ExecutionTimeLimit
    }
    if ($RestartCount -gt 0) {
        $settingsParams.RestartCount = $RestartCount
        $settingsParams.RestartInterval = $RestartInterval
    }
    $Settings = New-ScheduledTaskSettingsSet @settingsParams

    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Triggers `
        -Settings $Settings `
        -Principal $Principal `
        -Force | Out-Null
}

$MarketTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "09:10"
try {
    $MarketTrigger.StartBoundary = (Get-Date "09:10").ToString("yyyy-MM-ddTHH:mm:ss")
} catch { }

Install-SpotSignalTask `
    -TaskName $MarketTaskName `
    -ScriptPath $MarketScript `
    -Triggers @($MarketTrigger) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)
schtasks /Change /TN $MarketTaskName /TZ "India Standard Time" 2>$null

$TelegramTrigger = New-ScheduledTaskTrigger -AtLogOn
Install-SpotSignalTask `
    -TaskName $TelegramTaskName `
    -ScriptPath $TelegramScript `
    -Triggers @($TelegramTrigger) `
    -RestartCount 999

Write-Host ""
Write-Host "Tasks installed:"
Write-Host "  $MarketTaskName   Mon-Fri 09:10 IST (market session)"
Write-Host "  $TelegramTaskName at logon (Telegram /start, /stop, /status, /tick)"
Write-Host ""
Write-Host "Scripts:"
Write-Host "  Market:   $MarketScript"
Write-Host "  Telegram: $TelegramScript"
Write-Host "Logs:"
Write-Host "  $Root\data\live\market-session.log"
Write-Host "  $Root\data\live\telegram-remote.out.log"
Write-Host ""
Write-Host "Test market:   powershell -File `"$MarketScript`""
Write-Host "Test telegram: powershell -File `"$TelegramScript`""
Write-Host "Check tasks:   Get-ScheduledTask -TaskName $MarketTaskName, $TelegramTaskName"
Write-Host "Uninstall:     powershell -File `"$Root\scripts\uninstall_windows_task.ps1`""
