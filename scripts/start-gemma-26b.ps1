[CmdletBinding()]
param(
    [switch]$Json
)

$ErrorActionPreference = "Stop"

# Compatibility entry point. The measured Gemma 4 26B b9898 profile lives in
# model-runtime.ps1 so preflight and start cannot silently drift apart.
$runtimeScript = Join-Path $PSScriptRoot "model-runtime.ps1"
if (-not (Test-Path -LiteralPath $runtimeScript -PathType Leaf)) {
    throw "The canonical model runtime launcher is missing: $runtimeScript"
}

& $runtimeScript -Start -Json:$Json
