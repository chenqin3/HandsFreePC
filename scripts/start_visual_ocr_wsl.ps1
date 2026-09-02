<#
.SYNOPSIS
Start the HandsFreePC visual OCR service inside WSL (GPU PaddleOCR) and wait for /health.

.DESCRIPTION
Runs scripts/visual_ocr_server.py with the PP-OCRv5 line engine inside a WSL
distribution that already has a PaddlePaddle GPU environment. The server binds
loopback inside WSL; WSL2 localhost forwarding makes it reachable from Windows
at http://127.0.0.1:<Port>/layout-parsing, which is what config.local.yaml's
visual_ocr.endpoint should point to.

.EXAMPLE
pwsh scripts/start_visual_ocr_wsl.ps1
pwsh scripts/start_visual_ocr_wsl.ps1 -Port 8767 -Distro Ubuntu -Python '~/paddleenv/bin/python'
#>
param(
    [int]$Port = 8767,
    [string]$Distro = "Ubuntu",
    [string]$Python = "~/paddleenv/bin/python",
    [ValidateSet("ppocr", "vl")][string]$Engine = "ppocr",
    [ValidateSet("mobile", "server")][string]$OcrModels = "server",
    [int]$HealthTimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$drive = $repo.Substring(0, 1).ToLower()
$wslRepo = "/mnt/$drive" + ($repo.Substring(2) -replace "\\", "/")
$health = "http://127.0.0.1:$Port/health"

try {
    $existing = Invoke-WebRequest -Uri $health -TimeoutSec 2 -UseBasicParsing
    if ($existing.StatusCode -eq 200) {
        Write-Host "visual OCR already healthy at $health"
        exit 0
    }
} catch {
    # not running yet
}

$command = "cd '$wslRepo' && nohup $Python scripts/visual_ocr_server.py --port $Port --engine $Engine --ocr-models $OcrModels > /tmp/hfp_ocr_$Port.log 2>&1 &"
Write-Host "starting in WSL ($Distro): $command"
wsl -d $Distro -- bash -lc $command

$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $response = Invoke-WebRequest -Uri $health -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            Write-Host "visual OCR healthy at $health (first OCR call also warms the models)"
            exit 0
        }
    } catch {
        continue
    }
}

Write-Error "visual OCR did not answer at $health within $HealthTimeoutSeconds s; see /tmp/hfp_ocr_$Port.log inside WSL"
exit 1
