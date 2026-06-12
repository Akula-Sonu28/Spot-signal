# Register Windows Task Scheduler jobs for NIFTY Spot Signal Engine
#   1) NiftySpotSignalEngine        — Mon-Fri at 09:10 (market session)
#   2) NiftySpotSignalEngineTelegram — at logon (Telegram remote control)
#
# Run this script from an ELEVATED (Administrator) PowerShell.

$ErrorActionPreference = "Stop"

$Root            = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$MarketScript    = Join-Path $Root "scripts\run_market_session.ps1"
$TelegramScript  = Join-Path $Root "scripts\run_telegram_remote.ps1"
$MarketTask      = "NiftySpotSignalEngine"
$TelegramTask    = "NiftySpotSignalEngineTelegram"
$HiddenArgs      = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File"

# ── Validate scripts exist ────────────────────────────────────────────────────
foreach ($path in @($MarketScript, $TelegramScript)) {
    if (-not (Test-Path $path)) {
        Write-Error "Script not found: $path"
        exit 1
    }
}

# ── Helper ────────────────────────────────────────────────────────────────────
function Register-BotTask {
    param(
        [string]   $TaskName,
        [string]   $ScriptPath,
        [object[]] $Triggers,
        [hashtable]$ExtraSettings = @{}
    )

    $Action = New-ScheduledTaskAction `
        -Execute        "powershell.exe" `
        -Argument       "$HiddenArgs `"$ScriptPath`"" `
        -WorkingDirectory $Root

    $BaseSettings = @{
        AllowStartIfOnBatteries    = $true
        DontStopIfGoingOnBatteries = $true
        StartWhenAvailable         = $true
        MultipleInstances          = "IgnoreNew"
    }
    # Merge extra settings into base
    foreach ($key in $ExtraSettings.Keys) { $BaseSettings[$key] = $ExtraSettings[$key] }
    $Settings = New-ScheduledTaskSettingsSet @BaseSettings

    $Principal = New-ScheduledTaskPrincipal `
        -UserId    $env:USERNAME `
        -LogonType Interactive `
        -RunLevel  Limited

    # Remove old task silently before re-registering
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName  $TaskName `
        -Action    $Action `
        -Trigger   $Triggers `
        -Settings  $Settings `
        -Principal $Principal `
        -Force | Out-Null

    Write-Host "  Registered: $TaskName"
}

# ── 1. Market session — Mon-Fri at 09:10 ──────────────────────────────────────
Write-Host ""
Write-Host "Registering market session task..."

$MarketTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "09:10AM"

Register-BotTask `
    -TaskName    $MarketTask `
    -ScriptPath  $MarketScript `
    -Triggers    @($MarketTrigger) `
    -ExtraSettings @{ ExecutionTimeLimit = (New-TimeSpan -Hours 8) }

# ── 2. Telegram remote — at every logon ──────────────────────────────────────
Write-Host "Registering Telegram remote task..."

$TelegramTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

Register-BotTask `
    -TaskName    $TelegramTask `
    -ScriptPath  $TelegramScript `
    -Triggers    @($TelegramTrigger) `
    -ExtraSettings @{ RestartCount = 999; RestartInterval = (New-TimeSpan -Minutes 1) }

# ── Verify ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Verifying tasks..."
$tasks = Get-ScheduledTask -TaskName $MarketTask, $TelegramTask
$info  = $tasks | Get-ScheduledTaskInfo

foreach ($t in $tasks) {
    $i = $info | Where-Object TaskName -eq $t.TaskName
    Write-Host ("  {0,-38} State={1}  NextRun={2}" -f $t.TaskName, $t.State, $i.NextRunTime)
}

# ── Start Telegram remote immediately ────────────────────────────────────────
Write-Host ""
Write-Host "Starting Telegram remote now (background, no window)..."
Start-ScheduledTask -TaskName $TelegramTask
Start-Sleep -Seconds 4
$tState = (Get-ScheduledTask -TaskName $TelegramTask).State
Write-Host "  Telegram task state: $tState"

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=============================================="
Write-Host " All done. Both tasks are registered."
Write-Host "=============================================="
Write-Host ""
Write-Host "  Market bot:      Mon-Fri 09:10  ->  auto-starts, runs till 15:30"
Write-Host "  Telegram remote: at logon        ->  started now, always in background"
Write-Host ""
Write-Host "  Check in Telegram: send /status to @ItzmyMoneyCall_bot"
Write-Host ""
Write-Host "  Logs:"
Write-Host "    $Root\data\live\market-session.log"
Write-Host "    $Root\data\live\telegram-remote.out.log"
Write-Host ""
Write-Host "  To uninstall:"
Write-Host "    powershell -File `"$Root\scripts\uninstall_windows_task.ps1`""
