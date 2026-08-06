$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "local-launcher-helpers.ps1")

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDir = Join-Path $repoRoot ".context-artifacts\local-site"
$pidFile = Join-Path $logDir "launcher.pid"

# Restart only a uniquely identified Life Context launcher tree. The model
# server on port 8080 is intentionally not touched.
$launcher = Get-LifeContextLauncherProcess -ServerPort 8000 -DashboardPort 5173

if ($launcher) {
    Write-Output "STOP_LAUNCHER=$($launcher.ProcessId)"
    Stop-LifeContextProcess -ProcessId $launcher.ProcessId
}

$stdout = Join-Path $logDir "launcher-plain.stdout.log"
$stderr = Join-Path $logDir "launcher-plain.stderr.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

# Use capability-aware auto mode. Gemma b9898 falls back to json_object because
# its model listing does not advertise JSON Schema support.
$env:CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE = "auto"
$env:CONTEXT_MODEL_REQUIRES_MULTIMODAL = "true"

$node = (Get-Command node.exe).Source
$process = Start-Process `
    -FilePath $node `
    -WorkingDirectory $repoRoot `
    -ArgumentList @("scripts/start-local.mjs", "--server-port", "8000", "--dashboard-port", "5173") `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline -Encoding ascii
try {
    Wait-LifeContextEndpoint -Uri "http://127.0.0.1:8000/v1/health" -Label "Life Context API"
    Wait-LifeContextEndpoint -Uri "http://127.0.0.1:5173/" -Label "Life Context dashboard"
} catch {
    Stop-LifeContextProcess -ProcessId $process.Id
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    throw
}

Write-Output "NEW_LAUNCHER=$($process.Id)"
Write-Output "READY_API=http://127.0.0.1:8000/v1/health"
Write-Output "READY_DASHBOARD=http://127.0.0.1:5173/"
Write-Output "STDOUT=$stdout"
Write-Output "STDERR=$stderr"
