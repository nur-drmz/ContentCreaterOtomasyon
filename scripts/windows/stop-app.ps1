$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pidFile = Join-Path $projectRoot "data\runtime\web.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "Framewise is not running."
    exit 0
}

$webPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $webPid -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $webPid
    $process.WaitForExit(5000)
}

Remove-Item -LiteralPath $pidFile -Force
Write-Host "Framewise stopped."

