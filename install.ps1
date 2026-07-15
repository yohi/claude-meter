<#
.SYNOPSIS
    One-line installer for claude-meter on Windows (PowerShell).

.DESCRIPTION
    Installs the claude-meter CLI directly from GitHub using uv (preferred) or
    pip as a fallback, runs `claude-meter init`, and creates a double-clickable
    desktop launcher (claude-meter.bat) that runs `claude-meter start`. No local
    repository clone is required.

    This command downloads and runs a remote script directly in your shell. For
    safety, review the contents of this script before piping it into your shell.

.EXAMPLE
    irm https://raw.githubusercontent.com/yohi/claude-meter/master/install.ps1 | iex

    Downloads and runs the installer in the current PowerShell session.
#>

$ErrorActionPreference = "Stop"

$RepoUrl = "git+https://github.com/yohi/claude-meter.git"
$UvInstallDocs = "https://docs.astral.sh/uv/getting-started/installation/"

function Test-CommandExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

Write-Host "==> Starting claude-meter installation..."

# 1. Install the package with the first available installer.
if (Test-CommandExists -Name "uv") {
    Write-Host "==> Installing claude-meter with uv (uv tool install)..."
    uv tool install $RepoUrl
}
elseif (Test-CommandExists -Name "python") {
    Write-Host "==> uv not found; installing claude-meter with python -m pip (--user)..."
    python -m pip install --user $RepoUrl
}
else {
    throw "No suitable installer found: neither uv nor python is available. Install uv first, then re-run this script: $UvInstallDocs"
}

# 2. Verify the command is reachable on PATH.
if (-not (Test-CommandExists -Name "claude-meter")) {
    throw "claude-meter was installed but is not on your PATH. Open a new terminal and re-run this script, or add the install location to your PATH."
}

# 3. Run first-time initialization.
Write-Host "==> Initializing claude-meter (claude-meter init)..."
claude-meter init

# 4. Create a double-clickable desktop launcher that runs `claude-meter start`.
Write-Host "==> Creating Windows desktop launcher..."
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$LauncherPath = Join-Path $DesktopPath "claude-meter.bat"
$LauncherContent = @"
@echo off
cmd /k claude-meter start
"@
Set-Content -Path $LauncherPath -Value $LauncherContent -Encoding ascii
Write-Host "==> Created launcher: $LauncherPath"

Write-Host "==> Done. Launch the dashboard by double-clicking the launcher or running: claude-meter start"
