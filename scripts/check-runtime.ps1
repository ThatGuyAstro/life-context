[CmdletBinding()]
param(
    [string]$PythonPath,
    [switch]$Json,
    [switch]$RequireDesktop,
    [switch]$SkipPythonImports
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [ValidateSet("pass", "warn", "fail", "skip")] [string]$Status,
        [Parameter(Mandatory = $true)] [bool]$Required,
        [string]$Version = "",
        [string]$Details = ""
    )

    [void]$checks.Add([pscustomobject]@{
            name     = $Name
            status   = $Status
            required = $Required
            version  = $Version
            details  = $Details
        })
}

function Resolve-Executable {
    param([Parameter(Mandatory = $true)] [string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) {
            continue
        }

        if ($command.PSObject.Properties.Name -contains "Path" -and $command.Path) {
            return [string]$command.Path
        }
        if ($command.PSObject.Properties.Name -contains "Source" -and $command.Source) {
            return [string]$command.Source
        }
        return [string]$command.Definition
    }

    return $null
}

function Get-VersionToken {
    param([Parameter(Mandatory = $true)] [string]$Text)

    $match = [regex]::Match($Text, "(?<!\d)\d+(?:\.\d+){1,3}")
    if ($match.Success) {
        return $match.Value
    }
    return ""
}

function Test-MinimumVersion {
    param(
        [Parameter(Mandatory = $true)] [string]$Actual,
        [Parameter(Mandatory = $true)] [string]$Minimum
    )

    try {
        return ([Version]$Actual -ge [Version]$Minimum)
    }
    catch {
        return $false
    }
}

function Invoke-Probe {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [string[]]$Arguments = @()
    )

    # Some Windows CLI tools (notably uv) write informational progress to
    # stderr even on success.  Capture it as probe text instead of letting
    # global Stop-on-error turn a successful version check into a failure.
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = @(& $Path @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    $output = ($lines -join "`n").Trim()
    return [pscustomobject]@{
        exitCode = $exitCode
        output   = $output
    }
}

function Check-Tool {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [string[]]$Candidates,
        [string[]]$Arguments = @("--version"),
        [string]$MinimumVersion = "",
        [bool]$Required = $true
    )

    $path = Resolve-Executable -Names $Candidates
    if (-not $path) {
        Add-Check -Name $Name -Status $(if ($Required) { "fail" } else { "warn" }) `
            -Required $Required -Details "Not found on PATH."
        return $null
    }

    try {
        $probe = Invoke-Probe -Path $path -Arguments $Arguments
    }
    catch {
        Add-Check -Name $Name -Status $(if ($Required) { "fail" } else { "warn" }) `
            -Required $Required -Details "Could not execute ${path}: $($_.Exception.Message)"
        return $path
    }

    $version = Get-VersionToken -Text $probe.output
    if ($probe.exitCode -ne 0 -and [string]::IsNullOrWhiteSpace($probe.output)) {
        Add-Check -Name $Name -Status $(if ($Required) { "fail" } else { "warn" }) `
            -Required $Required -Version $version -Details "Command exited with $($probe.exitCode)."
        return $path
    }

    if ($MinimumVersion -and (-not $version -or -not (Test-MinimumVersion -Actual $version -Minimum $MinimumVersion))) {
        Add-Check -Name $Name -Status $(if ($Required) { "fail" } else { "warn" }) `
            -Required $Required -Version $version `
            -Details "Requires version $MinimumVersion or newer; found $($probe.output.Split("`n")[0])."
        return $path
    }

    Add-Check -Name $Name -Status "pass" -Required $Required -Version $version `
        -Details "$path; $($probe.output.Split("`n")[0])"
    return $path
}

function Check-DesktopRequirement {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [bool]$Found,
        [string]$Version = "",
        [string]$Details = ""
    )

    $required = [bool]$RequireDesktop
    if ($Found) {
        Add-Check -Name $Name -Status "pass" -Required $required -Version $Version -Details $Details
        return
    }

    Add-Check -Name $Name -Status $(if ($required) { "fail" } else { "warn" }) `
        -Required $required -Version $Version -Details $Details
}

if ($env:OS -ne "Windows_NT") {
    Add-Check -Name "Windows host" -Status "warn" -Required $false `
        -Details "This repository's capture and Tauri paths are tested on Windows only."
}

$cargoBin = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".cargo\bin"
if (Test-Path -LiteralPath $cargoBin) {
    $env:PATH = "$cargoBin;$env:PATH"
}

$rootFiles = @(
    "README.md",
    "package.json",
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "uv.lock",
    "scripts\run-task.mjs",
    "apps\dashboard\package.json",
    "apps\desktop\package.json",
    "packages\contracts\pyproject.toml"
)
foreach ($relativePath in $rootFiles) {
    $absolutePath = Join-Path $repositoryRoot $relativePath
    if (Test-Path -LiteralPath $absolutePath -PathType Leaf) {
        Add-Check -Name "file:$relativePath" -Status "pass" -Required $true -Details "Present."
    }
    else {
        Add-Check -Name "file:$relativePath" -Status "fail" -Required $true -Details "Missing."
    }
}

$gitPath = Check-Tool -Name "Git" -Candidates @("git")
$uvPath = Check-Tool -Name "uv" -Candidates @("uv") -MinimumVersion "0.10.7"
$nodePath = Check-Tool -Name "Node.js" -Candidates @("node") -MinimumVersion "18.0"
$pnpmPath = Check-Tool -Name "pnpm" -Candidates @("pnpm", "pnpm.cmd") -MinimumVersion "9.0"

$pythonArguments = @()
if ($PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        Add-Check -Name "Python" -Status "fail" -Required $true -Details "Explicit path does not exist: $PythonPath"
        $PythonPath = $null
    }
}
if (-not $PythonPath) {
    $PythonPath = Resolve-Executable -Names @("python")
}
if (-not $PythonPath) {
    $PythonPath = Resolve-Executable -Names @("py")
    $pythonArguments = @("-3")
}

if (-not $PythonPath) {
    Add-Check -Name "Python" -Status "fail" -Required $true -Details "Python 3.11 or newer was not found."
}
else {
    try {
        $pythonProbe = Invoke-Probe -Path $PythonPath -Arguments ($pythonArguments + @("--version"))
        $pythonVersion = Get-VersionToken -Text $pythonProbe.output
        if ($pythonProbe.exitCode -ne 0 -or -not $pythonVersion -or -not (Test-MinimumVersion -Actual $pythonVersion -Minimum "3.11")) {
            Add-Check -Name "Python" -Status "fail" -Required $true -Version $pythonVersion `
                -Details "Requires Python 3.11 or newer; found $($pythonProbe.output)."
        }
        else {
            Add-Check -Name "Python" -Status "pass" -Required $true -Version $pythonVersion `
                -Details "$PythonPath; $($pythonProbe.output.Split("`n")[0])"
        }
    }
    catch {
        Add-Check -Name "Python" -Status "fail" -Required $true -Details "Could not execute ${PythonPath}: $($_.Exception.Message)"
    }
}

if ($PythonPath -and -not $SkipPythonImports) {
    try {
        $imports = "import fastapi, httpx, mss, PIL, psutil, pydantic, uvicorn"
        $importProbe = Invoke-Probe -Path $PythonPath -Arguments ($pythonArguments + @("-c", $imports))
        if ($importProbe.exitCode -eq 0) {
            Add-Check -Name "Python runtime imports" -Status "pass" -Required $true `
                -Details "FastAPI, httpx, mss, Pillow, psutil, Pydantic, and uvicorn import successfully."
        }
        else {
            Add-Check -Name "Python runtime imports" -Status "fail" -Required $true `
                -Details "Install the server/collector dependencies before starting the application. $($importProbe.output)"
        }
    }
    catch {
        Add-Check -Name "Python runtime imports" -Status "fail" -Required $true `
            -Details "Dependency probe failed: $($_.Exception.Message)"
    }
}
else {
    Add-Check -Name "Python runtime imports" -Status "skip" -Required $false `
        -Details "Skipped by -SkipPythonImports."
}

$pyprojectPath = Join-Path $repositoryRoot "pyproject.toml"
$pyprojectText = Get-Content -LiteralPath $pyprojectPath -Raw -ErrorAction SilentlyContinue
if ($uvPath -and $pyprojectText -notmatch "(?m)^\[project\]") {
    Add-Check -Name "uv lock metadata" -Status "warn" -Required $false `
        -Details "Root pyproject.toml has no [project] table; uv lock --check is not applicable yet."
}
elseif ($uvPath) {
    $lockProbe = Invoke-Probe -Path $uvPath -Arguments @("lock", "--check")
    if ($lockProbe.exitCode -eq 0) {
        Add-Check -Name "uv lock metadata" -Status "pass" -Required $true -Details "uv.lock matches pyproject.toml."
    }
    else {
        Add-Check -Name "uv lock metadata" -Status "fail" -Required $true -Details $lockProbe.output
    }
}

$workspaceText = Get-Content -LiteralPath (Join-Path $repositoryRoot "pnpm-workspace.yaml") -Raw -ErrorAction SilentlyContinue
if ($workspaceText -match "set this to true or false") {
    Add-Check -Name "pnpm build policy" -Status "warn" -Required $false `
        -Details "pnpm-workspace.yaml contains an unresolved esbuild build-policy placeholder; use --ignore-scripts or resolve it before a normal install."
}
else {
    Add-Check -Name "pnpm build policy" -Status "pass" -Required $false -Details "No unresolved build-policy placeholder found."
}

if ($env:OS -eq "Windows_NT") {
    $rustupPath = Resolve-Executable -Names @("rustup")
    $cargoPath = Resolve-Executable -Names @("cargo")
    $rustcPath = Resolve-Executable -Names @("rustc")
    Check-DesktopRequirement -Name "Rustup" -Found ([bool]$rustupPath) -Details $(if ($rustupPath) { $rustupPath } else { "Install Rustup with the stable MSVC toolchain." })
    Check-DesktopRequirement -Name "Cargo" -Found ([bool]$cargoPath) -Details $(if ($cargoPath) { $cargoPath } else { "Cargo is required to build the Tauri shell." })
    Check-DesktopRequirement -Name "Rust compiler" -Found ([bool]$rustcPath) -Details $(if ($rustcPath) { $rustcPath } else { "rustc is required to build the Tauri shell." })

    $vswhereCandidates = @(
        (Join-Path ([Environment]::GetEnvironmentVariable("ProgramFiles(x86)")) "Microsoft Visual Studio\Installer\vswhere.exe"),
        (Join-Path ([Environment]::GetEnvironmentVariable("ProgramFiles")) "Microsoft Visual Studio\Installer\vswhere.exe")
    )
    $vswherePath = $vswhereCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    $visualStudioPath = $null
    if ($vswherePath) {
        $visualStudioPath = ((Invoke-Probe -Path $vswherePath -Arguments @(
                    "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"
                )).output -split "`n" | Select-Object -First 1).Trim()
    }
    $hasCl = [bool](Resolve-Executable -Names @("cl"))
    Check-DesktopRequirement -Name "MSVC C++ Build Tools" -Found ([bool]($visualStudioPath -or $hasCl)) `
        -Details $(if ($visualStudioPath) { "Visual Studio installation: $visualStudioPath" } elseif ($hasCl) { "cl.exe is available on PATH." } else { "Install the MSVC x64/x86 workload and a Windows SDK." })

    $webViewRoots = @(
        (Join-Path ([Environment]::GetEnvironmentVariable("ProgramFiles(x86)")) "Microsoft\EdgeWebView\Application"),
        (Join-Path ([Environment]::GetEnvironmentVariable("ProgramFiles")) "Microsoft\EdgeWebView\Application"),
        (Join-Path ([Environment]::GetEnvironmentVariable("LOCALAPPDATA")) "Microsoft\EdgeWebView\Application")
    )
    $webViewVersion = $null
    foreach ($webViewRoot in $webViewRoots) {
        if (-not (Test-Path -LiteralPath $webViewRoot -PathType Container)) {
            continue
        }
        $webViewVersion = Get-ChildItem -LiteralPath $webViewRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "^\d+\.\d+\.\d+\.\d+$" } |
            Sort-Object Name -Descending |
            Select-Object -First 1 -ExpandProperty Name
        if ($webViewVersion) {
            break
        }
    }
    Check-DesktopRequirement -Name "WebView2 Runtime" -Found ([bool]$webViewVersion) -Version $webViewVersion `
        -Details $(if ($webViewVersion) { "Evergreen runtime detected." } else { "Install the Evergreen WebView2 Runtime." })
}
else {
    foreach ($name in @("Rustup", "Cargo", "Rust compiler", "MSVC C++ Build Tools", "WebView2 Runtime")) {
        Add-Check -Name $name -Status "skip" -Required $false -Details "Windows-only prerequisite check."
    }
}

$failedRequired = @($checks | Where-Object { $_.status -eq "fail" -and $_.required }).Count
$warnings = @($checks | Where-Object { $_.status -eq "warn" }).Count
$summary = [pscustomobject]@{
    repositoryRoot = $repositoryRoot
    generatedAtUtc = [DateTime]::UtcNow.ToString("o")
    failedRequired = $failedRequired
    warnings       = $warnings
    desktopRequired = [bool]$RequireDesktop
    checks         = @($checks)
}

if ($Json) {
    $summary | ConvertTo-Json -Depth 5
}
else {
    $checks | Format-Table -Property status, required, name, version, details -Wrap -AutoSize
    Write-Output "Required failures: $failedRequired; warnings: $warnings"
}

if ($failedRequired -gt 0) {
    exit 1
}
exit 0
