<#
.SYNOPSIS
Remove the HandsFreePC logon task created by install_autostart.ps1 and stop the runtime.
#>
param([string]$TaskName = "HandsFreePC")
$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
# Also stop any listener that is still running (for example one started by hand).
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*handsfree_pc.cli*" -and $_.Name -like "python*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$launcher = Join-Path $env:LOCALAPPDATA "HandsFreePC\autostart\run_hidden.vbs"
if (Test-Path $launcher) { Remove-Item $launcher }
Write-Host "removed scheduled task '$TaskName' and stopped the listener"
