# Always-on Telegram command listener (single getUpdates poller per bot token).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$LogDir = Join-Path $Root "data\live"
$OutLog = Join-Path $LogDir "telegram-remote.out.log"
$ErrLog = Join-Path $LogDir "telegram-remote.err.log"
$LockFile = Join-Path $LogDir "telegram-remote.lock"
$Python = if ($env:NIFTY_PYTHON) { $env:NIFTY_PYTHON } else { if (Get-Command "py" -ErrorAction SilentlyContinue) { "py" } else { "python" } }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Message" | Add-Content -Path $OutLog -Encoding utf8
}

# Prevent duplicate instances — only one Telegram poller per bot token
if (Test-Path $LockFile) {
    $oldPid = Get-Content $LockFile -ErrorAction SilentlyContinue
    if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
        Write-Log "SKIP already running pid=$oldPid"
        exit 0
    }
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
}

$PID | Set-Content $LockFile
Write-Log "START pid=$PID python=$Python"

try {
    & $Python -m bot.telegram_remote 1>> $OutLog 2>> $ErrLog
}
finally {
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    Write-Log "END"
}
