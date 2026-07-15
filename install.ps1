<#
.SYNOPSIS
    One-line installer for claude-meter on Windows (PowerShell).

.DESCRIPTION
    Installs the claude-meter CLI directly from GitHub using uv (preferred) or
    pip as a fallback, runs `claude-meter init`, and creates a double-clickable
    desktop launcher (claude-meter.bat) that runs `claude-meter start`. No local
    repository clone is required.

    Downloading to a temp file first (rather than piping `irm` directly into
    `iex`) means the download must complete fully before anything runs, so a
    connection drop mid-transfer can never execute a truncated script. The
    downloaded content is then piped into `Invoke-Expression` rather than
    executed as a script file directly: `Invoke-WebRequest` marks downloaded
    files with a Zone.Identifier (Mark of the Web), and running such an
    unsigned `.ps1` file directly would be blocked under the `RemoteSigned`
    execution policy (a common default, e.g. on Windows Server);
    `Invoke-Expression` evaluates the content as a string rather than running
    a script file, so it isn't subject to that restriction. The temp file is
    removed afterwards regardless of the outcome. For safety, review the
    downloaded script's contents (the temp file below) before it runs.

    Note: this script itself is fetched from the `master` branch below, so the
    bootstrap fetch is not integrity-pinned; only the *installed* claude-meter
    package version is pinned to a release tag by Resolve-Ref(), which throws
    rather than silently falling back to an unreviewed `master` HEAD if that
    resolution fails.

.EXAMPLE
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "claude-meter-install-$([Guid]::NewGuid()).ps1"
    try {
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/yohi/claude-meter/master/install.ps1" -OutFile $tmp -ErrorAction Stop
        Get-Content -Raw -Path $tmp | Invoke-Expression
    } finally {
        Remove-Item -Path $tmp -Force -ErrorAction SilentlyContinue
    }

    Downloads the installer to a temp file, runs it in the current PowerShell
    session, then removes the temp file.
#>

$ErrorActionPreference = "Stop"

# Base git+https URL (without a ref). A specific ref (a release tag, by
# default) is appended by Resolve-Ref() below so that a plain one-line
# install never installs an unreviewed `master` HEAD.
$RepoBaseUrl = "git+https://github.com/yohi/claude-meter.git"
$GitHubReleasesLatestUrl = "https://github.com/yohi/claude-meter/releases/latest"
$UvInstallDocs = "https://docs.astral.sh/uv/getting-started/installation/"

function Test-CommandExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# Resolve the git ref to install. Defaults to the latest published release
# tag (resolved via GitHub's /releases/latest redirect) so this installer is
# pinned to a reviewed, stable release instead of the master branch's
# unreviewed HEAD. Set $env:CLAUDE_METER_REF to override (e.g. to a specific
# tag, a commit SHA, or "master" to opt back into HEAD).
function Resolve-Ref {
    if ($env:CLAUDE_METER_REF) {
        return $env:CLAUDE_METER_REF
    }
    try {
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $client = [System.Net.Http.HttpClient]::new($handler)
        try {
            $response = $client.GetAsync($GitHubReleasesLatestUrl).GetAwaiter().GetResult()
            $location = $response.Headers.Location
            if ($location -and ($location.ToString() -match '/tag/([^/]+)$')) {
                return $Matches[1]
            }
        }
        finally {
            $client.Dispose()
        }
    }
    catch {
        # Fall through to the throw below.
    }
    throw "Could not determine the latest release tag from $GitHubReleasesLatestUrl. Refusing to install an unreviewed master HEAD. Set `$env:CLAUDE_METER_REF to a specific tag, a commit SHA, or `"master`" to opt in explicitly, then re-run this script."
}

Write-Host "==> Starting claude-meter installation..."

$Ref = Resolve-Ref
$RepoUrl = "$RepoBaseUrl@$Ref"

# 1. Install the package with the first available installer.
if (Test-CommandExists -Name "uv") {
    Write-Host "==> Installing claude-meter $Ref with uv (uv tool install)..."
    uv tool install $RepoUrl
}
elseif (Test-CommandExists -Name "python") {
    Write-Host "==> uv not found; installing claude-meter $Ref with python -m pip (--user)..."
    python -m pip install --user $RepoUrl

    # `python -m pip install --user` places console scripts in the per-user
    # Scripts directory (e.g. %APPDATA%\Python\PythonXY\Scripts), which is
    # not part of the current PowerShell session's PATH even when a Python
    # installer has added it to the persistent user PATH. Prepend it now so
    # this run can find `claude-meter` without requiring a new shell.
    $UserScriptsDir = (python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))" 2>$null)
    if ($UserScriptsDir -and (Test-Path $UserScriptsDir)) {
        $env:PATH = "$UserScriptsDir;$env:PATH"
    }
}
else {
    throw "No suitable installer found: neither uv nor python is available. Install uv first, then re-run this script: $UvInstallDocs"
}

# 2. Verify the command is reachable on PATH and resolve its absolute path.
# Desktop/GUI launches (double-clicking claude-meter.bat below) do not always
# inherit this session's PATH, so the launcher embeds this resolved path
# directly rather than relying on a bare `claude-meter` command lookup.
$ClaudeMeterCommand = Get-Command -Name "claude-meter" -ErrorAction SilentlyContinue
if (-not $ClaudeMeterCommand) {
    throw "claude-meter was installed but is not on your PATH. Open a new terminal and re-run this script, or add the install location to your PATH."
}
$ClaudeMeterPath = $ClaudeMeterCommand.Source

# 3. Run first-time initialization.
Write-Host "==> Initializing claude-meter (claude-meter init)..."
claude-meter init

# 4. Create a double-clickable desktop launcher that runs `claude-meter start`.
Write-Host "==> Creating Windows desktop launcher..."
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$LauncherPath = Join-Path $DesktopPath "claude-meter.bat"
$LauncherContent = @"
@echo off
"$ClaudeMeterPath" start
cmd /k
"@
Set-Content -Path $LauncherPath -Value $LauncherContent -Encoding ascii
Write-Host "==> Created launcher: $LauncherPath"

Write-Host "==> Done. Launch the dashboard by double-clicking the launcher or running: claude-meter start"
