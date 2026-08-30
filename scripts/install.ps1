[CmdletBinding()]
param(
    [switch]$WithDevTools,
    [switch]$WithWhisper,
    [switch]$DownloadModels
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'
$versionCheck = 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -c $versionCheck
    if ($LASTEXITCODE -ne 0) { throw 'HandsFreePC requires Python 3.11 or 3.12.' }
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment failed ($LASTEXITCODE)." }
}

& $pythonPath -c $versionCheck
if ($LASTEXITCODE -ne 0) { throw 'The existing .venv must use Python 3.11 or 3.12.' }

& $pythonPath -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Upgrading pip failed ($LASTEXITCODE)." }
$extraParts = @('audio', 'windows')
if ($WithWhisper) { $extraParts += 'whisper' }
if ($WithDevTools) { $extraParts += 'dev' }
$extras = $extraParts -join ','
& $pythonPath -m pip install -e ("$projectRoot[$extras]")
if ($LASTEXITCODE -ne 0) { throw "Installing HandsFreePC failed ($LASTEXITCODE)." }

$configPath = Join-Path $projectRoot 'config.local.yaml'
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config.example.yaml') -Destination $configPath
    Write-Host "Created $configPath"
}

if ($DownloadModels) {
    & $pythonPath -m handsfree_pc --config $configPath download-models --directory (Join-Path $projectRoot 'models')
    if ($LASTEXITCODE -ne 0) { throw "Downloading speech models failed ($LASTEXITCODE)." }
}

$doctorArguments = @('-m', 'handsfree_pc', '--config', $configPath, 'doctor')
if ($DownloadModels) { $doctorArguments += '--strict' }
& $pythonPath @doctorArguments
if ($LASTEXITCODE -ne 0) { throw "HandsFreePC doctor failed ($LASTEXITCODE)." }
if ($DownloadModels) {
    Write-Host 'HandsFreePC installation finished and the default runtime is ready.'
} else {
    Write-Host 'Package installation finished. Download models, then run doctor --strict before live use.'
}
