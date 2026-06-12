$TaskNames = @("NiftySpotSignalEngine", "NiftySpotSignalEngineTelegram")
foreach ($TaskName in $TaskNames) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task: $TaskName"
}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Lock = Join-Path $Root "data\live\market-session.lock"
if (Test-Path $Lock) {
    $sessionPid = Get-Content $Lock -ErrorAction SilentlyContinue
    if ($sessionPid) {
        taskkill /PID $sessionPid /T /F 2>$null | Out-Null
    }
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
