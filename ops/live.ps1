<#
Live-runner кармана как фоновый процесс (задача LIVE-RUNNER, insights/business-plan.md).

    ops\live.ps1 start    # preflight, затем запуск src.live_runner: лог logs\live_runner.log, PID в data\live\runner.pid
    ops\live.ps1 status   # жив ли процесс + риск live (src.ops status --mode live) + последний preflight + хвост лога
    ops\live.ps1 stop     # штатная остановка через флаг data\live\STOP_RUNNER (ждёт до 60 с)

Аварийная остановка live-ТОРГОВЛИ — `python -m src.ops kill --mode live "причина"`
(работает и без запущенного runner'а) или файл data\live\KILL (runner сделает kill-switch и выйдет).
#>
param([ValidateSet("start", "stop", "status")][string]$Action = "status")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$LiveDir = Join-Path $Root "data\live"
$PidFile = Join-Path $LiveDir "runner.pid"
$StopFlag = Join-Path $LiveDir "STOP_RUNNER"
$Preflight = Join-Path $LiveDir "preflight_last.json"
$Log = Join-Path $Root "logs\live_runner.log"

function Get-RunnerProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $runnerPid = [int](Get-Content $PidFile -Raw)
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$runnerPid" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match "src\.live_runner") { return $proc }
    return $null
}

New-Item -ItemType Directory -Force $LiveDir, (Join-Path $Root "logs") | Out-Null

switch ($Action) {
    "start" {
        $running = Get-RunnerProcess
        if ($running) { Write-Output "Live-runner уже работает: PID $($running.ProcessId)"; exit 0 }
        Push-Location $Root
        try { & $Py -m src.live_preflight | Out-Null; $ok = ($LASTEXITCODE -eq 0) } finally { Pop-Location }
        if (-not $ok) {
            Write-Output "Preflight не пройден — live-runner не запущен. Отчёт: $Preflight"
            exit 2
        }
        if (Test-Path $StopFlag) {
            Remove-Item $StopFlag -Force
            Write-Output "Снят устаревший флаг data\live\STOP_RUNNER"
        }
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 0) {
            $archived = Join-Path $Root ("logs\live_runner_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
            Move-Item $Log $archived
            Write-Output "Предыдущий лог сохранён: $archived"
        }
        $proc = Start-Process -FilePath $Py -ArgumentList "-m", "src.live_runner" -WorkingDirectory $Root `
            -RedirectStandardError $Log -RedirectStandardOutput "$Log.out" -WindowStyle Hidden -PassThru
        Set-Content -Path $PidFile -Value $proc.Id
        Start-Sleep -Seconds 8
        if (Get-RunnerProcess) { Write-Output "Live-runner запущен: PID $($proc.Id), лог $Log" }
        else { Write-Output "Live-runner завершился сразу после старта — см. $Log"; Get-Content $Log -Tail 20; exit 1 }
    }
    "stop" {
        $running = Get-RunnerProcess
        if (-not $running) { Write-Output "Live-runner не запущен"; exit 0 }
        Set-Content -Path $StopFlag -Value ("ops/live.ps1 stop {0}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"))
        # runner проверяет флаги раз в 30 с
        for ($i = 0; $i -lt 30 -and (Get-RunnerProcess); $i++) { Start-Sleep -Seconds 2 }
        if (Get-RunnerProcess) {
            Write-Output "Не остановился за 60 с — завершаю PID $($running.ProcessId)"
            Stop-Process -Id $running.ProcessId -Force
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Write-Output "Live-runner остановлен"
    }
    "status" {
        $running = Get-RunnerProcess
        if ($running) { Write-Output "Live-runner работает: PID $($running.ProcessId), с $($running.CreationDate)" }
        else { Write-Output "Live-runner НЕ запущен" }
        Push-Location $Root
        try { & $Py -m src.ops status --mode live } finally { Pop-Location }
        if (Test-Path $Preflight) {
            $report = Get-Content $Preflight -Raw | ConvertFrom-Json
            Write-Output ("--- последний preflight {0}: ok={1}" -f $report.ts, $report.ok)
            $report.checks | Where-Object { $_.status -ne "ok" } | ForEach-Object { Write-Output ("  {0} {1}: {2}" -f $_.status, $_.name, $_.detail) }
        }
        if (Test-Path $Log) { Write-Output "--- хвост $Log"; Get-Content $Log -Tail 8 }
    }
}
