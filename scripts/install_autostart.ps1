<#
.SYNOPSIS
Register HandsFreePC to start listening automatically at Windows logon.

.DESCRIPTION
Creates a per-user scheduled task ("HandsFreePC") that runs at logon inside the
interactive session (the overlay, the speaker, and the microphone all need the
desktop). A small VBScript launcher starts the voice runtime without a console
window and appends its stdout/stderr to %LOCALAPPDATA%\HandsFreePC\logs\run.log.
The task restarts the runtime if it exits, and the runtime's own single-instance
lock prevents two listeners from fighting over the microphone.

.EXAMPLE
pwsh scripts/install_autostart.ps1
pwsh scripts/install_autostart.ps1 -Config C:\path\to\config.local.yaml -StartNow
#>
param(
    [string]$Config = "",
    [string]$TaskName = "HandsFreePC",
    [switch]$StartNow
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Config) { $Config = Join-Path $repo "config.local.yaml" }
$Config = (Resolve-Path $Config).Path
# pythonw has no console window; the runtime redirects its own stdout/stderr to
# logs\run.log when it notices it has no console. Running the interpreter as the
# task action itself lets Task Scheduler own the process: Stop-ScheduledTask
# really stops it and the restart-on-failure setting really restarts it.
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "venv pythonw not found at $python" }

$stateDir = Join-Path $env:LOCALAPPDATA "HandsFreePC"
$logDir = Join-Path $stateDir "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "run.log"

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m handsfree_pc.cli --config `"$Config`" run" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT20S"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "registered scheduled task '$TaskName' (at logon, 20 s delay)"
Write-Host "action: $python -m handsfree_pc.cli --config $Config run"
Write-Host "runtime log: $log"
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "started"
}
