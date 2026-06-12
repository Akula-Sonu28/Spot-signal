# Start live bot for one market session (09:15-15:30 IST). Windows equivalent of run_market_session.sh.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$LogDir = Join-Path $Root "data\live"
$Log = Join-Path $LogDir "market-session.log"
$Lock = Join-Path $LogDir "market-session.lock"
$Python = if ($env:NIFTY_PYTHON) { $env:NIFTY_PYTHON } else { if (Get-Command "py" -ErrorAction SilentlyContinue) { "py" } else { "python" } }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Message" | Add-Content -Path $Log -Encoding utf8
}

if (Test-Path $Lock) {
    $oldPid = Get-Content $Lock -ErrorAction SilentlyContinue
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Log "SKIP already running pid=$oldPid"
        exit 0
    }
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}

Write-Log "START project=$Root"
$PID | Set-Content $Lock

# Prevent sleep while bot runs (Windows equivalent of caffeinate)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WinPower {
    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
    public const uint ES_CONTINUOUS = 0x80000000;
    public const uint ES_SYSTEM_REQUIRED = 0x00000001;
    public const uint ES_DISPLAY_REQUIRED = 0x00000002;
}
"@ -ErrorAction SilentlyContinue

try {
    [void][WinPower]::SetThreadExecutionState(
        [WinPower]::ES_CONTINUOUS -bor [WinPower]::ES_SYSTEM_REQUIRED -bor [WinPower]::ES_DISPLAY_REQUIRED
    )
    Write-Log "Running: $Python -m bot.main"
    & $Python -m bot.main 2>&1 | Tee-Object -FilePath $Log -Append
}
finally {
    [void][WinPower]::SetThreadExecutionState([WinPower]::ES_CONTINUOUS)
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
    Write-Log "END"

    # Safety-net stop message in case Python exited without sending one
    # (e.g. hard kill, PC restart). Only fires if BOT_STOPPED wasn't logged.
    # Best-effort — ignore failures.
    try {
        $signals = Join-Path $Root "data\live\signals.csv"
        $alreadySent = $false
        if (Test-Path $signals) {
            $lastLines = Get-Content $signals -Tail 5 -ErrorAction SilentlyContinue
            if ($lastLines -match "BOT_STOPPED") { $alreadySent = $true }
        }
        if (-not $alreadySent) {
            & $Python -c "
from bot.config import load_app_config
from bot.alerts import TelegramAlerter
import os
os.chdir(r'$Root')
cfg = load_app_config('.env')
TelegramAlerter(cfg).bot_stopped('session ended unexpectedly')
" 2>$null
        }
    } catch { }
}
