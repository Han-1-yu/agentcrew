# Create a desktop shortcut that launches AgentCrew (backend + Studio).
# Run once from PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
#
# Optional: -Name "AgentCrew"  -Arguments "--port 8766"

param(
    [string]$Name = "AgentCrew",
    [string]$Arguments = "",
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "start-agentcrew.cmd"
if (-not (Test-Path $target)) {
    throw "Launcher not found: $target"
}

if (-not $Destination) {
    $Destination = [Environment]::GetFolderPath("Desktop")
}
$linkPath = Join-Path $Destination "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.Description = "Start AgentCrew (local backend + Studio)"
if ($Arguments) { $shortcut.Arguments = $Arguments }

$icon = Join-Path $root "studio\node_modules\electron\dist\electron.exe"
if (Test-Path $icon) {
    $shortcut.IconLocation = "$icon,0"
}

$shortcut.Save()
Write-Host "Shortcut created: $linkPath"
