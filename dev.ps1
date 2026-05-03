param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
$StateFile = Join-Path $Root ".dev-state.json"
$LogDir = Join-Path $Root ".dev-logs"

$BackendPort = 8000
$FrontendPort = 5173
$CleanupPorts = @($BackendPort, 5173, 5174, 5175)

function Ensure-LogDir {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir | Out-Null
    }
}

function Get-ListeningPids {
    param([int[]]$Ports)
    $pids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($port in $Ports) {
        try {
            $rows = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
            foreach ($row in $rows) {
                [void]$pids.Add([int]$row.OwningProcess)
            }
        } catch {
            $lines = netstat -ano | Select-String ":$port\s+.*LISTENING\s+(\d+)$"
            foreach ($line in $lines) {
                if ($line.Matches.Count -gt 0) {
                    [void]$pids.Add([int]$line.Matches[0].Groups[1].Value)
                }
            }
        }
    }
    return @($pids)
}

function Stop-Pids {
    param([int[]]$Pids, [string]$Reason = "")
    foreach ($procId in ($Pids | Select-Object -Unique)) {
        if ($procId -le 0) { continue }
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            if ($Reason) {
                Write-Host "[OK] PID $procId durduruldu ($Reason)."
            } else {
                Write-Host "[OK] PID $procId durduruldu."
            }
        } catch {
            # süreç zaten kapanmış olabilir
        }
    }
}

function Read-State {
    if (-not (Test-Path $StateFile)) { return $null }
    try {
        return Get-Content $StateFile -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Write-State {
    param([int]$BackendPid, [int]$FrontendPid)
    $payload = @{
        backend_pid = $BackendPid
        frontend_pid = $FrontendPid
        updated_at = (Get-Date).ToString("s")
    } | ConvertTo-Json
    Set-Content -Path $StateFile -Value $payload -Encoding UTF8
}

function Remove-State {
    if (Test-Path $StateFile) {
        Remove-Item $StateFile -Force -ErrorAction SilentlyContinue
    }
}

function Stop-All {
    $state = Read-State
    if ($null -ne $state) {
        Stop-Pids -Pids @([int]$state.backend_pid, [int]$state.frontend_pid) -Reason "state"
    }
    $portPids = Get-ListeningPids -Ports $CleanupPorts
    Stop-Pids -Pids $portPids -Reason "port-cleanup"
    Remove-State
}

function Start-All {
    Ensure-LogDir
    Stop-All

    $backendLog = Join-Path $LogDir "backend.log"
    $backendErr = Join-Path $LogDir "backend.err.log"
    $frontendLog = Join-Path $LogDir "frontend.log"
    $frontendErr = Join-Path $LogDir "frontend.err.log"

    $backend = Start-Process -FilePath "python" `
        -ArgumentList "-m uvicorn main:app --host 127.0.0.1 --port $BackendPort" `
        -WorkingDirectory $BackendDir `
        -RedirectStandardOutput $backendLog `
        -RedirectStandardError $backendErr `
        -PassThru

    $frontend = Start-Process -FilePath "npm.cmd" `
        -ArgumentList "run dev -- --host 127.0.0.1 --port $FrontendPort --strictPort" `
        -WorkingDirectory $FrontendDir `
        -RedirectStandardOutput $frontendLog `
        -RedirectStandardError $frontendErr `
        -PassThru

    Write-State -BackendPid $backend.Id -FrontendPid $frontend.Id

    $backendReady = $false
    $frontendReady = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 500
        $backendLive = Get-ListeningPids -Ports @($BackendPort)
        $frontendLive = Get-ListeningPids -Ports @($FrontendPort)
        if ($backendLive) { $backendReady = $true }
        if ($frontendLive) { $frontendReady = $true }
        if ($backendReady -and $frontendReady) { break }
    }

    Write-Host ""
    Write-Host "DerinNLP calisiyor."
    Write-Host "Backend : http://127.0.0.1:$BackendPort"
    Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
    Write-Host "Loglar   : $LogDir"

    if (-not $backendReady) {
        Write-Host "[UYARI] Backend portu acilamadi. $backendErr dosyasina bak."
    }
    if (-not $frontendReady) {
        Write-Host "[UYARI] Frontend portu acilamadi. $frontendErr dosyasina bak."
    }
}

function Show-Status {
    $state = Read-State
    $backendLive = Get-ListeningPids -Ports @($BackendPort)
    $frontendLive = Get-ListeningPids -Ports @($FrontendPort)
    Write-Host "Backend($BackendPort) PID: $($backendLive -join ', ')"
    Write-Host "Frontend($FrontendPort) PID: $($frontendLive -join ', ')"
    if ($null -ne $state) {
        Write-Host "State dosyasi -> backend: $($state.backend_pid), frontend: $($state.frontend_pid)"
    } else {
        Write-Host "State dosyasi yok."
    }
}

switch ($Action) {
    "start" { Start-All; break }
    "stop" { Stop-All; Write-Host "DerinNLP servisleri durduruldu."; break }
    "restart" { Stop-All; Start-All; break }
    "status" { Show-Status; break }
}
