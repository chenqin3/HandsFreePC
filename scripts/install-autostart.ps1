[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$pythonwPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$configPath = Join-Path $projectRoot 'config.local.yaml'
if (-not (Test-Path -LiteralPath $pythonwPath)) { throw 'Run scripts\install.ps1 first.' }
if (-not (Test-Path -LiteralPath $configPath)) { throw 'config.local.yaml is missing.' }

$shell = New-Object -ComObject WScript.Shell
$startup = $shell.SpecialFolders.Item('Startup')
$shortcutPath = Join-Path $startup 'HandsFreePC.lnk'
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwPath
$shortcut.Arguments = "-m handsfree_pc --config `"$configPath`" run"
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'HandsFreePC local voice controller'
$shortcut.Save()
Write-Host "Installed current-user autostart shortcut: $shortcutPath"
