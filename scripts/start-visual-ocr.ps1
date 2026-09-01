[CmdletBinding()]
param(
    [int]$Port = 8766,
    [int]$WaitSeconds = 90
)

$ErrorActionPreference = 'Stop'
$healthUri = "http://127.0.0.1:$Port/health"

function Test-VisualOcrHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUri -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

if (Test-VisualOcrHealth) {
    Write-Host "HandsFreePC local visual OCR is ready at $healthUri"
    exit 0
}

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$serverPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'visual_ocr_server.py'))
if (-not $serverPath.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw 'The visual OCR server path escaped the HandsFreePC project.'
}
if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw 'scripts\visual_ocr_server.py is missing.'
}

$wslServerPath = (& wsl.exe --exec wslpath -a $serverPath).Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslServerPath.StartsWith('/')) {
    throw 'Could not resolve the HandsFreePC visual OCR server inside WSL.'
}

$configuredPython = $env:HANDSFREEPC_WSL_OCR_PYTHON
if ([string]::IsNullOrWhiteSpace($configuredPython)) {
    $configuredPython = '~/paddleenv/bin/python'
}
if ($configuredPython -notmatch '^[~\/][A-Za-z0-9._~\/-]+$') {
    throw 'HANDSFREEPC_WSL_OCR_PYTHON must be one Linux path, not a command.'
}
$wslPython = (& wsl.exe --exec bash -lc "readlink -f '$configuredPython'").Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslPython.StartsWith('/')) {
    throw 'The configured WSL visual OCR Python could not be resolved.'
}
& wsl.exe --exec test -x $wslPython
if ($LASTEXITCODE -ne 0) {
    throw "The WSL visual OCR Python is not executable: $wslPython"
}

$process = Start-Process -FilePath 'wsl.exe' -WindowStyle Hidden -PassThru -ArgumentList @(
    '--exec',
    $wslPython,
    $wslServerPath,
    '--host',
    '127.0.0.1',
    '--port',
    [string]$Port
)

$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-VisualOcrHealth) {
        Write-Host "HandsFreePC local visual OCR started at $healthUri"
        exit 0
    }
    if ($process.HasExited) {
        throw "The WSL visual OCR server exited early with code $($process.ExitCode)."
    }
    Start-Sleep -Milliseconds 500
}

if (-not $process.HasExited) {
    Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
}
throw "The WSL visual OCR server did not become ready within $WaitSeconds seconds."
