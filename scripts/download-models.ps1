[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.local.yaml'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Run scripts\install.ps1 first.'
}

$arguments = @('-m', 'handsfree_pc', '--config', $configPath, 'download-models', '--directory', (Join-Path $projectRoot 'models'))
if ($Force) { $arguments += '--force' }
& $pythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw "Downloading speech models failed ($LASTEXITCODE)." }
