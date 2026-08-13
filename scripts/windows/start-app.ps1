param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$waitressExe = Join-Path $projectRoot ".venv\Scripts\waitress-serve.exe"
$runtimeDir = Join-Path $projectRoot "data\runtime"
$pidFile = Join-Path $runtimeDir "web.pid"
$stdoutLog = Join-Path $runtimeDir "web.stdout.log"
$stderrLog = Join-Path $runtimeDir "web.stderr.log"
$appUrl = "http://127.0.0.1:8765"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Project virtual environment was not found: $pythonExe"
}
if (-not (Test-Path -LiteralPath $waitressExe)) {
    throw "Waitress was not found. Run 'uv sync' in the project directory."
}

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        Write-Host "Framewise is already running at $appUrl"
        if (-not $NoBrowser) {
            Start-Process $appUrl
        }
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force
}

Push-Location $projectRoot
try {
    & $pythonExe manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed."
    }

    $process = Start-Process `
        -FilePath $waitressExe `
        -ArgumentList @("--listen=127.0.0.1:8765", "content_studio.wsgi:application") `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline

    $healthy = $false
    foreach ($attempt in 1..30) {
        Start-Sleep -Milliseconds 250
        try {
            $response = Invoke-WebRequest -Uri "$appUrl/health/" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {
            if ($process.HasExited) {
                break
            }
        }
    }

    if (-not $healthy) {
        throw "Framewise did not become healthy. See $stderrLog"
    }

    Write-Host "Framewise is running at $appUrl"
    if (-not $NoBrowser) {
        Start-Process $appUrl
    }
} finally {
    Pop-Location
}

