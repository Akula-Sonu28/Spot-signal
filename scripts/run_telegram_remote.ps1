# Always-on Telegram command listener (single getUpdates poller per bot token).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$LogDir = Join-Path $Root "data\live"
$OutLog = Join-Path $LogDir "telegram-remote.out.log"
$ErrLog = Join-Path $LogDir "telegram-remote.err.log"
$Python = if ($env:NIFTY_PYTHON) { $env:NIFTY_PYTHON } else { "python" }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "Starting Telegram remote control: $Python -m bot.telegram_remote"
& $Python -m bot.telegram_remote 1>> $OutLog 2>> $ErrLog
