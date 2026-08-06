[CmdletBinding()]
param(
    [switch]$SkipCargoCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$manifestPath = Join-Path $desktopRoot "src-tauri\Cargo.toml"
$cargoLockPath = Join-Path $desktopRoot "src-tauri\Cargo.lock"

function Find-CommandPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string[]]$Fallbacks = @()
    )

    $resolved = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -ne $resolved) {
        return $resolved.Source
    }

    foreach ($fallback in $Fallbacks) {
        if (Test-Path -LiteralPath $fallback -PathType Leaf) {
            return $fallback
        }
    }

    return $null
}

function Write-Check {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [bool]$Present,
        [Parameter(Mandatory = $true)]
        [string]$Detail
    )

    $state = if ($Present) { "PASS" } else { "MISS" }
    $colour = if ($Present) { "Green" } else { "Yellow" }
    Write-Host ("[{0}] {1}: {2}" -f $state, $Name, $Detail) -ForegroundColor $colour
}

$cargoFallback = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
$rustcFallback = Join-Path $env:USERPROFILE ".cargo\bin\rustc.exe"
$rustupFallback = Join-Path $env:USERPROFILE ".cargo\bin\rustup.exe"
$cargoPath = Find-CommandPath -Name "cargo" -Fallbacks @($cargoFallback)
$rustcPath = Find-CommandPath -Name "rustc" -Fallbacks @($rustcFallback)
$rustupPath = Find-CommandPath -Name "rustup" -Fallbacks @($rustupFallback)

Write-Host "Life Context Tauri Windows smoke check" -ForegroundColor Cyan
Write-Host ("Desktop root: {0}" -f $desktopRoot)
Write-Check -Name "Cargo manifest" -Present (Test-Path -LiteralPath $manifestPath -PathType Leaf) -Detail $manifestPath
Write-Check -Name "Cargo.lock" -Present (Test-Path -LiteralPath $cargoLockPath -PathType Leaf) -Detail $cargoLockPath

if ($null -ne $cargoPath) {
    $cargoVersion = (& $cargoPath --version 2>$null | Select-Object -First 1)
    Write-Check -Name "Cargo" -Present $true -Detail ("{0} ({1})" -f $cargoPath, $cargoVersion)
} else {
    Write-Check -Name "Cargo" -Present $false -Detail "Install Rustup or add %USERPROFILE%\.cargo\bin to PATH."
}

if ($null -ne $rustcPath) {
    $rustcVersion = (& $rustcPath --version 2>$null | Select-Object -First 1)
    Write-Check -Name "rustc" -Present $true -Detail ("{0} ({1})" -f $rustcPath, $rustcVersion)
} else {
    Write-Check -Name "rustc" -Present $false -Detail "The stable MSVC Rust toolchain is required."
}

if ($null -ne $rustupPath) {
    Write-Check -Name "Rustup" -Present $true -Detail $rustupPath
} else {
    Write-Check -Name "Rustup" -Present $false -Detail "Install from https://rustup.rs/ when the toolchain is absent."
}

$vswherePath = Find-CommandPath -Name "vswhere" -Fallbacks @(
    "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
)
$vsInstall = $null
if ($null -ne $vswherePath) {
    $vsInstall = & $vswherePath -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
}
$vsPresent = -not [string]::IsNullOrWhiteSpace($vsInstall)
Write-Check -Name "MSVC Build Tools" -Present $vsPresent -Detail $(if ($vsPresent) { $vsInstall.Trim() } else { "Install the Desktop development with C++ workload." })

$sdkRoot = "C:\Program Files (x86)\Windows Kits\10\bin"
$sdkVersion = Get-ChildItem -LiteralPath $sdkRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "^10\." } |
    Sort-Object Name -Descending |
    Select-Object -First 1
Write-Check -Name "Windows SDK" -Present ($null -ne $sdkVersion) -Detail $(if ($null -ne $sdkVersion) { $sdkVersion.FullName } else { "Install a Windows 10/11 SDK through Visual Studio Installer." })

$webViewRoot = "C:\Program Files (x86)\Microsoft\EdgeWebView\Application"
$webViewVersion = Get-ChildItem -LiteralPath $webViewRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "^\d+\.\d+\.\d+\.\d+$" } |
    Sort-Object Name -Descending |
    Select-Object -First 1
Write-Check -Name "WebView2 Runtime" -Present ($null -ne $webViewVersion) -Detail $(if ($null -ne $webViewVersion) { $webViewVersion.FullName } else { "Install the Evergreen WebView2 Runtime." })

if ($SkipCargoCheck) {
    Write-Host "Cargo check skipped by request." -ForegroundColor DarkYellow
    exit 0
}

if ($null -eq $cargoPath) {
    Write-Host "Cargo check blocked: cargo.exe is not available in this shell." -ForegroundColor Yellow
    exit 2
}

Write-Host "Running cargo check --locked..." -ForegroundColor Cyan
& $cargoPath check --locked --manifest-path $manifestPath
if ($LASTEXITCODE -ne 0) {
    Write-Host ("Cargo check failed with exit code {0}." -f $LASTEXITCODE) -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Cargo check passed." -ForegroundColor Green
exit 0
