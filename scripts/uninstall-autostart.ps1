[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$startup = [System.IO.Path]::GetFullPath($shell.SpecialFolders.Item('Startup'))
$shortcutPath = [System.IO.Path]::GetFullPath((Join-Path $startup 'HandsFreePC.lnk'))
if (-not $shortcutPath.StartsWith($startup, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resolved shortcut path escaped the Startup folder.'
}
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath
    Write-Host "Removed $shortcutPath"
} else {
    Write-Host 'HandsFreePC autostart shortcut was not present.'
}
