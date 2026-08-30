[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.local.yaml'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Run scripts\install.ps1 first.'
}

& $pythonPath -m handsfree_pc --config $configPath doctor --strict
if ($LASTEXITCODE -ne 0) { throw "HandsFreePC is not ready to run ($LASTEXITCODE)." }
& $pythonPath -m handsfree_pc --config $configPath run
if ($LASTEXITCODE -ne 0) { throw "HandsFreePC exited with code $LASTEXITCODE." }
