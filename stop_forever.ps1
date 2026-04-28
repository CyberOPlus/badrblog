$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot "logs\\forever-launcher.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "No launcher PID file found."
    exit 0
}

$launcherPid = (Get-Content -LiteralPath $pidFile | Select-Object -First 1).Trim()

if (-not $launcherPid) {
    Write-Host "Launcher PID file is empty."
    exit 0
}

$process = Get-Process -Id $launcherPid -ErrorAction SilentlyContinue

if (-not $process) {
    Write-Host "Launcher process is not running."
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

Stop-Process -Id $launcherPid -Force
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Host "Stopped launcher PID $launcherPid."
