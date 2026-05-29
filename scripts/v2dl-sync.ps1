# v2dl-sync.ps1 - thin wrapper to drive scripts/sync_local.py from
# Windows Task Scheduler. Designed to be safe to run on a daily cron.
#
# Usage examples:
#   pwsh -File scripts/v2dl-sync.ps1 -Destination "D:\v2ph_archive"
#   pwsh -File scripts/v2dl-sync.ps1 -Destination "D:\v2ph_archive" -Mode full
#   pwsh -File scripts/v2dl-sync.ps1 -Destination "D:\v2ph_archive" -Discover
#
# Task Scheduler setup (one-time, in an elevated PowerShell):
#   $action  = New-ScheduledTaskAction -Execute "pwsh.exe" `
#       -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\scripts\v2dl-sync.ps1`" -Destination `"D:\v2ph_archive`""
#   $trigger = New-ScheduledTaskTrigger -Daily -At 3am
#   Register-ScheduledTask -TaskName "v2dl-sync" -Action $action -Trigger $trigger
#
# Designed for personal archival only - do not raise concurrency or
# remove the per-run sleep without thinking; you will get banned.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,

    # incremental: only check first page of each listing (fast, polite, daily-safe).
    # full: paginate through every listing page (run at most weekly).
    [ValidateSet("incremental", "full")]
    [string]$Mode = "incremental",

    # When set, also re-runs the companies discovery before syncing.
    # Companies rarely change; you typically want this maybe once a month.
    [switch]$Discover,

    # Path to the project venv. Defaults to <repo>/.venv.
    [string]$VenvPath = ""
)

$ErrorActionPreference = "Stop"

# Resolve repository root (parent of this scripts/ folder).
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $VenvPath) {
    $VenvPath = Join-Path $RepoRoot ".venv"
}
$PythonExe = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Activate / create the project venv first."
    exit 1
}

# Single-instance lock - bail if a previous run is still in flight.
$LockFile = Join-Path $env:TEMP "v2dl-sync.lock"
if (Test-Path $LockFile) {
    $existingPid = Get-Content $LockFile -ErrorAction SilentlyContinue
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        Write-Host "[v2dl-sync] previous run still active (PID $existingPid), exiting."
        exit 0
    }
    # Stale lock - clean it up.
    Remove-Item $LockFile -ErrorAction SilentlyContinue
}
$PID | Out-File -FilePath $LockFile -Encoding ascii

try {
    Push-Location $RepoRoot
    try {
        if ($Discover) {
            Write-Host "[v2dl-sync] running discover companies..."
            & $PythonExe scripts\sync_local.py discover companies
            if ($LASTEXITCODE -ne 0) {
                Write-Error "discover companies failed with exit code $LASTEXITCODE"
                exit $LASTEXITCODE
            }
        }

        Write-Host "[v2dl-sync] running sync (mode=$Mode, destination=$Destination)..."
        & $PythonExe scripts\sync_local.py sync --destination $Destination --mode $Mode
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item $LockFile -ErrorAction SilentlyContinue
}
