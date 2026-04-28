$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logsDir = Join-Path $projectRoot "logs"
$launcherLog = Join-Path $logsDir "forever-launcher.log"
$botLog = Join-Path $logsDir "bot-loop.log"
$pidFile = Join-Path $logsDir "forever-launcher.pid"

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
Set-Location $projectRoot

function Write-LauncherLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $launcherLog -Value "[$timestamp] $Message"
}

$python = (Get-Command python -ErrorAction Stop).Source
Set-Content -LiteralPath $pidFile -Value $PID
Write-LauncherLog "Launcher started. PID=$PID Python=$python"

while ($true) {
    Write-LauncherLog "Starting bot loop."

    try {
        & $python main.py --loop *>> $botLog
        $exitCode = $LASTEXITCODE
        Write-LauncherLog "Bot loop exited with code $exitCode."
    }
    catch {
        $errorText = $_ | Out-String
        Add-Content -LiteralPath $botLog -Value $errorText
        Write-LauncherLog "Bot loop crashed: $($_.Exception.Message)"
    }

    Write-LauncherLog "Restarting bot loop in 15 seconds."
    Start-Sleep -Seconds 15
}
