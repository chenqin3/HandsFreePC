[CmdletBinding()]
param(
    [switch]$DoctorOnly
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.local.yaml'
$visualOcrStarter = Join-Path $projectRoot 'scripts\start-visual-ocr.ps1'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Run scripts\install.ps1 first.'
}

$visualOcrInfo = & $pythonPath -c 'import sys; from handsfree_pc.config import load_settings; settings = load_settings(sys.argv[1], allow_missing=False); print(1 if settings.visual_ocr.enabled else 0); print(1 if settings.visual_ocr.ocr_regions_enabled else 0); print(settings.visual_ocr.endpoint)' $configPath
if ($LASTEXITCODE -ne 0) { throw 'HandsFreePC could not read visual OCR settings.' }
if (
    $visualOcrInfo.Count -eq 3 -and
    $visualOcrInfo[0] -eq '1' -and
    $visualOcrInfo[1] -eq '1' -and
    $visualOcrInfo[2] -eq 'http://127.0.0.1:8766/layout-parsing'
) {
    & $visualOcrStarter
    if ($LASTEXITCODE -ne 0) { throw 'HandsFreePC local visual OCR did not start.' }
}

& $pythonPath -m handsfree_pc --config $configPath doctor --strict
if ($LASTEXITCODE -ne 0) { throw "HandsFreePC is not ready to run ($LASTEXITCODE)." }
if ($DoctorOnly) { exit 0 }
& $pythonPath -m handsfree_pc --config $configPath run
if ($LASTEXITCODE -ne 0) { throw "HandsFreePC exited with code $LASTEXITCODE." }
