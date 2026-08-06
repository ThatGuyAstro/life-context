$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$existing = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match "services\.collector" }
if ($existing) {
    Write-Output "COLLECTOR_PID=$($existing[0].ProcessId)"
    Write-Output "COLLECTOR_ALREADY_RUNNING=true"
    exit 0
}

$logDir = Join-Path $repoRoot ".context-artifacts\local-site"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdout = Join-Path $logDir "collector.stdout.log"
$stderr = Join-Path $logDir "collector.stderr.log"
$uv = (Get-Command uv.exe).Source

$process = Start-Process `
    -FilePath $uv `
    -WorkingDirectory $repoRoot `
    -ArgumentList @("run", "--locked", "python", "-m", "services.collector") `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 2
if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
    throw "Collector exited during startup. Inspect $stderr"
}

Write-Output "COLLECTOR_PID=$($process.Id)"
Write-Output "COLLECTOR_ALREADY_RUNNING=false"
Write-Output "STDOUT=$stdout"
Write-Output "STDERR=$stderr"
