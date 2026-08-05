$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot '.venv'
$PythonPath = Join-Path $VenvPath 'Scripts\python.exe'
$RequirementsPath = Join-Path $ProjectRoot 'requirements-local.txt'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw 'Sanal ortam olusturulamadi.'
    }
}

& $PythonPath -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw 'pip guncellenemedi.'
}

& $PythonPath -m pip install -r $RequirementsPath
if ($LASTEXITCODE -ne 0) {
    throw 'Python bagimliliklari kurulamadi.'
}

& $PythonPath -m ipykernel install --user --name youtube-automation-v0 --display-name 'Python (Youtube Automation v0)'
if ($LASTEXITCODE -ne 0) {
    throw 'Jupyter kernel kaydedilemedi.'
}

Write-Host ''
Write-Host 'Kurulum tamamlandi.'
Write-Host 'Notebook kernel secimi: Python (Youtube Automation v0)'
