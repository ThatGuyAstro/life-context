$ErrorActionPreference = "Stop"

function Get-LifeContextProcessTreeIds([int]$RootPid) {
    $all = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    [void]$ids.Add($RootPid)
    $queue.Enqueue($RootPid)

    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($child in $all | Where-Object { [int]$_.ParentProcessId -eq $parent }) {
            $childPid = [int]$child.ProcessId
            if ($ids.Add($childPid)) {
                $queue.Enqueue($childPid)
            }
        }
    }
    return $ids
}

function Get-LifeContextLauncherProcess(
    [int]$ServerPort = 8000,
    [int]$DashboardPort = 5173
) {
    $candidates = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -eq "node.exe" -and
                $_.CommandLine -match "scripts[\\/]start-local\.mjs" -and
                $_.CommandLine -match "--server-port\s+$ServerPort" -and
                $_.CommandLine -match "--dashboard-port\s+$DashboardPort"
            }
    )
    if ($candidates.Count -gt 1) {
        throw "Multiple Life Context launchers match ports $ServerPort/$DashboardPort; refusing to choose one automatically."
    }
    if ($candidates.Count -eq 0) {
        return $null
    }

    $candidate = $candidates[0]
    $treeIds = Get-LifeContextProcessTreeIds -RootPid ([int]$candidate.ProcessId)
    $listeners = @(
        foreach ($port in @($ServerPort, $DashboardPort)) {
            Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        }
    )
    $foreign = @($listeners | Where-Object { -not $treeIds.Contains([int]$_.OwningProcess) })
    if ($foreign.Count -gt 0) {
        $owners = ($foreign | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
        throw "Refusing to stop launcher $($candidate.ProcessId): port ownership is not confined to its process tree (PID $owners)."
    }
    return $candidate
}

function Stop-LifeContextProcess([int]$ProcessId) {
    taskkill.exe /PID $ProcessId /T /F | Out-Null
    Start-Sleep -Seconds 2
}

function Wait-LifeContextEndpoint([string]$Uri, [string]$Label, [int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = "no response"
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return
            }
            $lastError = "HTTP $($response.StatusCode)"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Label did not become ready at $Uri within $TimeoutSeconds seconds: $lastError"
}
