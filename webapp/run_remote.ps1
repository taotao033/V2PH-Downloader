<#
.SYNOPSIS
    Start the V2PH local viewer bound to all interfaces so it can be reached
    over a Tailscale private network (方案一：仅自己/少数设备远程访问).

.DESCRIPTION
    - Binds the server to 0.0.0.0 so Tailscale peers can connect.
    - Prints the Tailscale IP + ready-to-open URL if Tailscale is installed.
    - Uses the repo-root .venv python.

.EXAMPLE
    .\webapp\run_remote.ps1
    .\webapp\run_remote.ps1 -Port 9000
#>
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

# Resolve repo root (parent of this script's folder) and the venv python.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$Python    = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Warning "venv python not found at $Python — falling back to 'python' on PATH."
    $Python = "python"
}

# Bind to every interface so Tailscale (and LAN) peers can reach it.
$env:V2PH_HOST = "0.0.0.0"
$env:V2PH_PORT = "$Port"

Write-Host ""
Write-Host "==== V2PH remote viewer ====" -ForegroundColor Cyan

# Show how to reach it. Prefer the Tailscale IP when available.
$tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
if ($tailscale) {
    try {
        $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1).Trim()
    } catch { $tsIp = $null }

    if ($tsIp) {
        Write-Host "Tailscale access (from your other logged-in devices):" -ForegroundColor Green
        Write-Host "    http://$tsIp`:$Port" -ForegroundColor Yellow
    } else {
        Write-Warning "Tailscale is installed but not connected. Run 'tailscale up' and sign in, then reopen this URL."
    }
} else {
    Write-Warning "Tailscale not detected. Install it from https://tailscale.com/download and sign in on this machine + your remote device with the SAME account."
}

# Always show the LAN address too.
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object -First 1).IPAddress
if ($lanIp) {
    Write-Host "LAN access (same Wi-Fi/router):           http://$lanIp`:$Port" -ForegroundColor DarkGray
}
Write-Host "Local access (this machine only):         http://127.0.0.1:$Port" -ForegroundColor DarkGray
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

# Launch the app from the repo root so 'python -m webapp' resolves the package.
Push-Location $RepoRoot
try {
    & $Python -X utf8 -m webapp
}
finally {
    Pop-Location
}
