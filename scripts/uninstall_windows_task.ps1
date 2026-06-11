$TaskName = "NiftySpotSignalEngine"
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed scheduled task: $TaskName"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Lock = Join-Path $Root "data\live\market-session.lock"
if (Test-Path $Lock) {
    $pid = Get-Content $Lock -ErrorAction SilentlyContinue
    if ($pid) { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue }
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
