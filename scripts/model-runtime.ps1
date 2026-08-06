[CmdletBinding()]
param(
    [switch]$Start,
    [switch]$Json,
    [string]$ServerExe = 'E:\LocalLLM\bin\llama.cpp\b9898\llama-server.exe',
    [string]$Model = 'E:\LocalLLM\cache\llama.cpp\models--ggml-org--gemma-4-26B-A4B-it-GGUF\snapshots\ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3\gemma-4-26B-A4B-it-Q4_K_M.gguf',
    [string]$Mmproj = 'E:\LocalLLM\cache\llama.cpp\models--ggml-org--gemma-4-26B-A4B-it-GGUF\snapshots\ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3\mmproj-gemma-4-26B-A4B-it-Q8_0.gguf',
    [int]$Port = 8080,
    [int]$ContextSize = 8192,
    [int]$GpuLayers = 999,
    [string]$Device = 'CUDA0',
    [int]$MainGpu = 0,
    [int]$Parallel = 1,
    [int]$BatchSize = 1024,
    [int]$UbatchSize = 512
)

$ErrorActionPreference = 'Stop'

function Get-FileRecord([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    [ordered]@{
        path = $item.FullName
        bytes = $item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
    }
}

function Fail([string]$Message) {
    if ($Json) {
        [pscustomobject]@{ ready = $false; error = $Message } | ConvertTo-Json -Depth 6
    } else {
        Write-Error $Message
    }
    exit 2
}

function Wait-ModelReady([int]$ProcessId, [int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = "no response"
    while ((Get-Date) -lt $deadline) {
        $running = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $running) {
            throw "llama-server PID $ProcessId exited before becoming healthy"
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($health.status -eq "ok" -or $health.ready -eq $true) {
                $models = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/models" -TimeoutSec 2
                return [pscustomobject]@{
                    health = $health
                    models = $models
                }
            }
            $lastError = "health response did not report ready"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 250
    }
    throw "llama-server did not become healthy within $TimeoutSeconds seconds: $lastError"
}

if (-not (Test-Path -LiteralPath $ServerExe -PathType Leaf)) {
    Fail "llama-server.exe was not found: $ServerExe"
}
if (-not (Test-Path -LiteralPath $Model -PathType Leaf)) {
    Fail "The configured model was not found: $Model"
}
if (-not (Test-Path -LiteralPath $Mmproj -PathType Leaf)) {
    Fail "The configured multimodal projector was not found: $Mmproj"
}

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $owners = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    Fail "Port $Port is already listening (PID $($owners -join ', ')); inspect ownership before starting llama-server."
}

$record = [ordered]@{
    ready = $true
    start_requested = [bool]$Start
    server = Get-FileRecord $ServerExe
    model = Get-FileRecord $Model
    mmproj = Get-FileRecord $Mmproj
    source = 'https://huggingface.co/ggml-org/gemma-4-26B-A4B-it-GGUF'
    revision = 'ae4d537a6345467d1c86bb5cc0d4505ff3ebe0f3'
    license = 'Apache-2.0 (repository metadata; verify upstream terms before redistribution)'
    host = '127.0.0.1'
    port = $Port
    device = $Device
    split_mode = 'none'
    main_gpu = $MainGpu
    context_size = $ContextSize
    gpu_layers = $GpuLayers
    parallel = $Parallel
    batch_size = $BatchSize
    ubatch_size = $UbatchSize
    flash_attention = 'on'
    webui = $false
    api_base = "http://127.0.0.1:$Port/v1"
}

$arguments = @(
    '--model', $Model,
    '--mmproj', $Mmproj,
    '--host', '127.0.0.1',
    '--port', [string]$Port,
    '--alias', 'gemma-4-26b-a4b-it',
    '--device', $Device,
    '--n-gpu-layers', [string]$GpuLayers,
    '--split-mode', 'none',
    '--main-gpu', [string]$MainGpu,
    '--ctx-size', [string]$ContextSize,
    '--parallel', [string]$Parallel,
    '--batch-size', [string]$BatchSize,
    '--ubatch-size', [string]$UbatchSize,
    '--flash-attn', 'on',
    '--metrics',
    '--jinja',
    '--no-webui'
)
$record.command = ('"{0}" {1}' -f $ServerExe, (($arguments | ForEach-Object { '"{0}"' -f ($_ -replace '"', '\"') }) -join ' '))

if ($Start) {
    $logDir = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')) '.context-artifacts\model-runtime'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stdout = Join-Path $logDir 'llama-server.stdout.log'
    $stderr = Join-Path $logDir 'llama-server.stderr.log'
    $process = Start-Process -FilePath $ServerExe -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $ServerExe) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
    $record.pid = $process.Id
    $record.stdout = $stdout
    $record.stderr = $stderr
    try {
        $probe = Wait-ModelReady -ProcessId $process.Id
        $record.health = $probe.health
        $record.models = $probe.models
    } catch {
        taskkill.exe /PID $process.Id /T /F | Out-Null
        Fail $_.Exception.Message
    }
}

if ($Json) {
    [pscustomobject]$record | ConvertTo-Json -Depth 8
} else {
    Write-Host 'llama.cpp runtime preflight: ready'
    Write-Host ("Binary:  {0}" -f $record.server.path)
    Write-Host ("Model:   {0} ({1:N1} GB)" -f $record.model.path, ($record.model.bytes / 1GB))
    Write-Host ("Projector:{0} ({1:N1} GB)" -f $record.mmproj.path, ($record.mmproj.bytes / 1GB))
    Write-Host ("API:     {0}" -f $record.api_base)
    Write-Host ("Command: {0}" -f $record.command)
    if ($Start) {
        Write-Host ("Started llama-server PID {0}; logs: {1}" -f $record.pid, $record.stdout)
    } else {
        Write-Host 'No process started. Pass -Start only after reviewing the command and model license.'
    }
}
