[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.local.yaml'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts\install.ps1 first.' }

New-Item -ItemType Directory -Force (Join-Path $projectRoot '.pytest-tmp') | Out-Null
& $pythonPath -m pytest -q -m "not live" --basetemp (Join-Path $projectRoot '.pytest-tmp')
if ($LASTEXITCODE -ne 0) { throw "Unit tests failed ($LASTEXITCODE)." }
& $pythonPath -m ruff check handsfree_pc tests
if ($LASTEXITCODE -ne 0) { throw "Lint failed ($LASTEXITCODE)." }
& $pythonPath -m handsfree_pc --config $configPath doctor --strict
if ($LASTEXITCODE -ne 0) { throw "HandsFreePC doctor failed ($LASTEXITCODE)." }
