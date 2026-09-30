# PowerShell 5.1 and 7+ compatible management script for Netdata
param(
    [ValidateSet("start", "stop", "restart", "status", "url", "logs")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $ScriptDir "netdata\docker-compose.yml"
$Port = 19999
$Url = "http://127.0.0.1:$Port"
$ContainerName = "okx-netdata"

function Test-DockerAvailable {
    try {
        $null = docker --version 2>$null
        return $true
    } catch {
        return $false
    }
}

function Test-DockerDaemonRunning {
    try {
        $null = docker info 2>$null
        return $true
    } catch {
        return $false
    }
}

function Get-NetdataContainerStatus {
    try {
        $status = (docker inspect -f "{{.State.Status}}" $ContainerName 2>$null)
        if ($status) { return $status.Trim() }
        return "not_found"
    } catch {
        return "error"
    }
}

function Test-NetdataApi {
    try {
        $resp = Invoke-WebRequest -Uri "$Url/api/v1/info" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            return $true
        }
    } catch {
        return $false
    }
    return $false
}

if ($Action -eq "start") {
    if (-not (Test-DockerAvailable)) {
        Write-Error "Docker is not installed or not in PATH."
        exit 1
    }
    if (-not (Test-DockerDaemonRunning)) {
        Write-Error "Docker daemon is not running. Please start Docker Desktop."
        exit 1
    }

    Write-Host "Starting Netdata via docker compose..." -ForegroundColor Cyan
    docker compose -f $ComposeFile up -d

    Start-Sleep -Seconds 3
    $cStatus = Get-NetdataContainerStatus
    Write-Host "Container $ContainerName status: $cStatus" -ForegroundColor Green
    Write-Host "Netdata Dashboard: $Url" -ForegroundColor Cyan
    exit 0
}

if ($Action -eq "stop") {
    if (-not (Test-DockerDaemonRunning)) {
        Write-Host "Docker daemon is not running." -ForegroundColor Yellow
        exit 0
    }
    Write-Host "Stopping Netdata..." -ForegroundColor Yellow
    docker compose -f $ComposeFile down
    Write-Host "Netdata stopped." -ForegroundColor Green
    exit 0
}

if ($Action -eq "restart") {
    if (-not (Test-DockerDaemonRunning)) {
        Write-Error "Docker daemon is not running."
        exit 1
    }
    Write-Host "Restarting Netdata..." -ForegroundColor Cyan
    docker compose -f $ComposeFile restart
    Write-Host "Netdata restarted." -ForegroundColor Green
    exit 0
}

if ($Action -eq "status") {
    $dockerAvail = Test-DockerAvailable
    $daemonRunning = if ($dockerAvail) { Test-DockerDaemonRunning } else { $false }
    $cStatus = if ($daemonRunning) { Get-NetdataContainerStatus } else { "docker_offline" }
    $apiOk = if ($daemonRunning -and $cStatus -eq "running") { Test-NetdataApi } else { $false }

    [PSCustomObject]@{
        DockerAvailable = $dockerAvail
        DockerDaemon    = $daemonRunning
        Container       = $ContainerName
        Status          = $cStatus
        ApiAvailable    = $apiOk
        DashboardUrl    = $Url
    } | Format-List
    exit 0
}

if ($Action -eq "url") {
    Write-Output $Url
    exit 0
}

if ($Action -eq "logs") {
    if (-not (Test-DockerDaemonRunning)) {
        Write-Error "Docker daemon is not running."
        exit 1
    }
    docker logs --tail 50 $ContainerName
    exit 0
}
